"""SQLite persistence — the only source of truth AgentScout has."""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MIGRATIONS: List[str] = [
    # v1
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE room_state (
        room TEXT PRIMARY KEY,
        last_seen_seq INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL,            -- 'config' | 'events'
        watch_until TEXT,                -- ISO UTC; NULL = watch forever
        first_polled_at TEXT,
        updated_at TEXT
    );
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        room TEXT NOT NULL,
        seq INTEGER NOT NULL,
        ts TEXT NOT NULL,
        sender TEXT NOT NULL,
        sender_did TEXT,
        signed INTEGER NOT NULL,
        text TEXT NOT NULL,
        text_hash TEXT NOT NULL,
        received_at TEXT NOT NULL,
        UNIQUE(room, seq)
    );
    CREATE INDEX messages_did ON messages(sender_did, ts);
    CREATE INDEX messages_room_ts ON messages(room, ts);
    CREATE TABLE sequence_gaps (
        id INTEGER PRIMARY KEY, room TEXT NOT NULL, expected_seq INTEGER NOT NULL,
        first_available_seq INTEGER NOT NULL, detected_at TEXT NOT NULL
    );
    CREATE TABLE agents (
        did TEXT PRIMARY KEY, fp TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
    );
    CREATE INDEX agents_fp ON agents(fp);
    CREATE TABLE did_notes (
        fp TEXT PRIMARY KEY, text TEXT NOT NULL, text_hash TEXT NOT NULL,
        did_in_note TEXT, fetched_at TEXT NOT NULL
    );
    CREATE TABLE room_owners (room TEXT PRIMARY KEY, owner_did TEXT NOT NULL, fetched_at TEXT NOT NULL);
    CREATE TABLE artifact_refs (
        ref TEXT PRIMARY KEY, ns TEXT NOT NULL, key TEXT NOT NULL, first_did TEXT NOT NULL,
        first_seen TEXT NOT NULL, checked_at TEXT, ok INTEGER
    );
    CREATE TABLE rooms_seen (room TEXT PRIMARY KEY, created_ts TEXT NOT NULL, first_seen_at TEXT NOT NULL);
    CREATE TABLE score_snapshots (
        day TEXT NOT NULL, did TEXT NOT NULL, score INTEGER NOT NULL, confidence INTEGER NOT NULL,
        components TEXT NOT NULL, PRIMARY KEY(day, did)
    );
    """,
    # v2 — Milestone B: signed publishing
    """
    ALTER TABLE room_state ADD COLUMN last_sent_nonce INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE outbox (
        id INTEGER PRIMARY KEY, room TEXT NOT NULL, kind TEXT NOT NULL, marker TEXT NOT NULL,
        text TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        nonce INTEGER, posted_seq INTEGER, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(room, marker)
    );
    CREATE TABLE published_notes (
        ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, written_at TEXT NOT NULL,
        tamper_events INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(ns, key)
    );
    """,
]


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit transactions below
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] or 0
        for idx, sql in enumerate(MIGRATIONS, start=1):
            if idx > current:
                self.conn.executescript(sql)  # executescript commits by itself
                self.conn.execute("INSERT INTO schema_version(version) VALUES (?)", (idx,))

    # ---- settings --------------------------------------------------------------------
    def get_setting(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- rooms ------------------------------------------------------------------------
    def ensure_room(self, room: str, source: str, watch_until: Optional[str], now: str) -> None:
        self.conn.execute(
            """INSERT INTO room_state(room,last_seen_seq,source,watch_until,updated_at) VALUES(?,0,?,?,NULL)
               ON CONFLICT(room) DO UPDATE SET
                 source=CASE WHEN room_state.source='config' THEN 'config' ELSE excluded.source END,
                 watch_until=CASE WHEN room_state.source='config' THEN NULL ELSE excluded.watch_until END""",
            (room, source, watch_until),
        )

    def room_state(self, room: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM room_state WHERE room=?", (room,)).fetchone()

    def rooms_to_poll(self, now: str, event_rooms_updated_before: Optional[str] = None) -> List[str]:
        """Config rooms always; event-discovered rooms only while watched and (optionally) only if not
        polled since `event_rooms_updated_before` — they are polled on a slower cadence."""
        rows = self.conn.execute(
            "SELECT room, source, updated_at FROM room_state WHERE watch_until IS NULL OR watch_until > ? ORDER BY room", (now,)
        ).fetchall()
        out = []
        for r in rows:
            if r["source"] == "events" and event_rooms_updated_before is not None:
                if r["updated_at"] and r["updated_at"] >= event_rooms_updated_before:
                    continue
            out.append(r["room"])
        return out

    def touch_room(self, room: str, now: str) -> None:
        self.conn.execute("UPDATE room_state SET updated_at=? WHERE room=?", (now, room))

    def prune_event_rooms(self, keep_newest: int, now: str) -> int:
        """Drop expired event rooms and all but the newest `keep_newest` still-watched ones (messages stay)."""
        cur = self.conn.execute("DELETE FROM room_state WHERE source='events' AND watch_until IS NOT NULL AND watch_until <= ?", (now,))
        removed = cur.rowcount
        rows = self.conn.execute("SELECT room FROM room_state WHERE source='events' ORDER BY watch_until DESC, room").fetchall()
        for r in rows[keep_newest:]:
            self.conn.execute("DELETE FROM room_state WHERE room=?", (r["room"],))
            removed += 1
        return removed

    def set_cursor(self, room: str, last_seen_seq: int, now: str) -> None:
        self.conn.execute(
            "UPDATE room_state SET last_seen_seq=?, updated_at=?, first_polled_at=COALESCE(first_polled_at,?) WHERE room=?",
            (last_seen_seq, now, now, room),
        )

    def next_nonce(self, room: str, now_ms: int) -> int:
        """Strictly increasing per room, transaction-safe: max(now_ms, last+1), persisted before use."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT last_sent_nonce FROM room_state WHERE room=?", (room,)).fetchone()
            last = row["last_sent_nonce"] if row else 0
            nonce = max(int(now_ms), last + 1)
            if row:
                self.conn.execute("UPDATE room_state SET last_sent_nonce=? WHERE room=?", (nonce, room))
            else:
                self.conn.execute("INSERT INTO room_state(room,last_seen_seq,source,last_sent_nonce) VALUES(?,0,'feed',?)", (room, nonce))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return nonce

    def bump_nonce_floor(self, room: str, floor: int) -> None:
        self.conn.execute("UPDATE room_state SET last_sent_nonce=MAX(last_sent_nonce, ?) WHERE room=?", (floor, room))

    # ---- outbox ----
    def enqueue(self, room: str, kind: str, marker: str, text: str, now: str) -> Optional[int]:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO outbox(room,kind,marker,text,state,created_at,updated_at) VALUES(?,?,?,?,'PENDING',?,?)",
            (room, kind, marker, text, now, now))
        return cur.lastrowid if cur.rowcount == 1 else None

    def outbox_pending(self, limit: int = 5) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM outbox WHERE state IN ('PENDING','FAILED_RETRYABLE') ORDER BY id LIMIT ?", (limit,)).fetchall()

    def outbox_update(self, row_id: int, state: str, now: str, nonce: Optional[int] = None, posted_seq: Optional[int] = None,
                      error: Optional[str] = None, bump_attempts: bool = False) -> None:
        self.conn.execute(
            "UPDATE outbox SET state=?, updated_at=?, nonce=COALESCE(?,nonce), posted_seq=COALESCE(?,posted_seq), error=?,"
            " attempts=attempts+? WHERE id=?",
            (state, now, nonce, posted_seq, error, 1 if bump_attempts else 0, row_id))

    def outbox_has(self, room: str, marker: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM outbox WHERE room=? AND marker=?", (room, marker)).fetchone()

    # ---- published notes ----
    def published_note(self, ns: str, key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM published_notes WHERE ns=? AND key=?", (ns, key)).fetchone()

    def set_published_note(self, ns: str, key: str, value: str, now: str, tampered: bool = False) -> None:
        self.conn.execute(
            """INSERT INTO published_notes(ns,key,value,written_at,tamper_events) VALUES(?,?,?,?,?)
               ON CONFLICT(ns,key) DO UPDATE SET value=excluded.value, written_at=excluded.written_at,
                 tamper_events=published_notes.tamper_events+excluded.tamper_events""",
            (ns, key, value, now, 1 if tampered else 0))

    def record_gap(self, room: str, expected: int, first_available: int, now: str) -> None:
        self.conn.execute(
            "INSERT INTO sequence_gaps(room,expected_seq,first_available_seq,detected_at) VALUES(?,?,?,?)",
            (room, expected, first_available, now),
        )

    def add_room_seen(self, room: str, created_ts: str, now: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO rooms_seen(room,created_ts,first_seen_at) VALUES(?,?,?)", (room, created_ts, now)
        )
        return cur.rowcount == 1

    # ---- messages / agents ---------------------------------------------------------------
    def insert_messages(self, room: str, rows: Iterable[Tuple], now: str) -> int:
        """rows: (seq, ts, sender, sender_did, signed, text, text_hash). Returns number inserted."""
        inserted = 0
        self.conn.execute("BEGIN")
        try:
            for seq, ts, sender, sender_did, signed, text, text_hash in rows:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO messages(room,seq,ts,sender,sender_did,signed,text,text_hash,received_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (room, seq, ts, sender, sender_did, int(signed), text, text_hash, now),
                )
                if cur.rowcount == 1:
                    inserted += 1
                    if signed and sender_did:
                        self.conn.execute(
                            """INSERT INTO agents(did,fp,first_seen,last_seen) VALUES(?,?,?,?)
                               ON CONFLICT(did) DO UPDATE SET
                                 first_seen=MIN(agents.first_seen, excluded.first_seen),
                                 last_seen=MAX(agents.last_seen, excluded.last_seen)""",
                            (sender_did, _fp_of(sender_did), ts, ts),
                        )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return inserted

    def all_messages(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT room,seq,ts,sender,sender_did,signed,text,text_hash FROM messages ORDER BY room, seq").fetchall()

    def agents(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT did,fp,first_seen,last_seen FROM agents").fetchall()

    def agent_by_fp_or_did(self, needle: str) -> Optional[sqlite3.Row]:
        needle = needle.strip()
        row = self.conn.execute("SELECT * FROM agents WHERE did=? OR fp=?", (needle, needle.lower())).fetchone()
        if row:
            return row
        if len(needle) >= 6:
            return self.conn.execute(
                "SELECT * FROM agents WHERE fp LIKE ? OR did LIKE ? ORDER BY did LIMIT 1",
                (needle.lower() + "%", "did:key:" + needle + "%"),
            ).fetchone()
        return None

    # ---- notes ------------------------------------------------------------------------
    def upsert_note(self, fp: str, text: str, text_hash: str, did_in_note: Optional[str], now: str) -> None:
        self.conn.execute(
            """INSERT INTO did_notes(fp,text,text_hash,did_in_note,fetched_at) VALUES(?,?,?,?,?)
               ON CONFLICT(fp) DO UPDATE SET text=excluded.text, text_hash=excluded.text_hash,
                 did_in_note=excluded.did_in_note, fetched_at=excluded.fetched_at""",
            (fp, text, text_hash, did_in_note, now),
        )

    def note_for_fp(self, fp: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM did_notes WHERE fp=?", (fp,)).fetchone()

    def notes_by_fp(self) -> Dict[str, sqlite3.Row]:
        return {r["fp"]: r for r in self.conn.execute("SELECT * FROM did_notes").fetchall()}

    def note_fetch_queue(self, candidate_fps: Sequence[str], stale_before: str, limit: int) -> List[str]:
        """fps needing a fetch: agents seen in messages first, then the rest; missing before stale."""
        if limit <= 0 or not candidate_fps:
            return []
        known = self.notes_by_fp()
        agent_fps = {r["fp"] for r in self.agents()}
        missing_agents, stale_agents, missing_other, stale_other = [], [], [], []
        for fp in candidate_fps:
            row = known.get(fp)
            if row is None:
                (missing_agents if fp in agent_fps else missing_other).append(fp)
            elif row["fetched_at"] < stale_before:
                (stale_agents if fp in agent_fps else stale_other).append(fp)
        return (missing_agents + stale_agents + missing_other + stale_other)[:limit]

    # ---- owners / artifacts ------------------------------------------------------------------
    def set_room_owner(self, room: str, owner_did: str, now: str) -> None:
        self.conn.execute(
            "INSERT INTO room_owners(room,owner_did,fetched_at) VALUES(?,?,?)"
            " ON CONFLICT(room) DO UPDATE SET owner_did=excluded.owner_did, fetched_at=excluded.fetched_at",
            (room, owner_did, now),
        )

    def owner_fetch_queue(self, rooms: Sequence[str], stale_before: str, limit: int) -> List[str]:
        if limit <= 0:
            return []
        known = {r["room"]: r["fetched_at"] for r in self.conn.execute("SELECT room, fetched_at FROM room_owners")}
        seen = {r["room"] for r in self.conn.execute("SELECT DISTINCT room FROM messages")}
        missing = [r for r in rooms if r not in known]
        stale = [r for r in rooms if r in known and known[r] < stale_before]
        ordered = sorted(missing, key=lambda r: (r not in seen, r)) + sorted(stale, key=lambda r: (r not in seen, r))
        return ordered[:limit]

    def owned_rooms_by_did(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for r in self.conn.execute("SELECT room, owner_did FROM room_owners WHERE owner_did != '' ORDER BY room"):
            out.setdefault(r["owner_did"], []).append(r["room"])
        return out

    def add_artifact_ref(self, ref: str, ns: str, key: str, did: str, seen_at: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO artifact_refs(ref,ns,key,first_did,first_seen) VALUES(?,?,?,?,?)",
            (ref, ns, key, did, seen_at),
        )

    def pending_artifact_refs(self, limit: int) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM artifact_refs WHERE checked_at IS NULL ORDER BY first_seen LIMIT ?", (limit,)
        ).fetchall()

    def set_artifact_result(self, ref: str, ok: bool, now: str) -> None:
        self.conn.execute("UPDATE artifact_refs SET ok=?, checked_at=? WHERE ref=?", (int(ok), now, ref))

    def artifacts_by_did(self) -> Dict[str, Tuple[int, int]]:
        """did -> (checked_ok, total_refs)"""
        out: Dict[str, Tuple[int, int]] = {}
        for r in self.conn.execute("SELECT first_did, COUNT(*) AS n, SUM(COALESCE(ok,0)) AS ok FROM artifact_refs GROUP BY first_did"):
            out[r["first_did"]] = (int(r["ok"] or 0), int(r["n"]))
        return out

    # ---- snapshots ------------------------------------------------------------------------
    def save_snapshot(self, day: str, rows: Iterable[Tuple[str, int, int, dict]]) -> None:
        self.conn.execute("BEGIN")
        try:
            for did, score, confidence, components in rows:
                self.conn.execute(
                    "INSERT OR REPLACE INTO score_snapshots(day,did,score,confidence,components) VALUES(?,?,?,?,?)",
                    (day, did, score, confidence, json.dumps(components, sort_keys=True)),
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def has_snapshot(self, day: str) -> bool:
        return self.conn.execute("SELECT 1 FROM score_snapshots WHERE day=? LIMIT 1", (day,)).fetchone() is not None

    def snapshot_scores_on_or_before(self, day: str) -> Dict[str, int]:
        row = self.conn.execute("SELECT MAX(day) AS d FROM score_snapshots WHERE day<=?", (day,)).fetchone()
        if not row or not row["d"]:
            return {}
        return {r["did"]: r["score"] for r in self.conn.execute("SELECT did,score FROM score_snapshots WHERE day=?", (row["d"],))}

    def counts(self) -> Dict[str, int]:
        out = {}
        for table in ("messages", "agents", "did_notes", "room_owners", "artifact_refs", "rooms_seen", "sequence_gaps", "room_state", "outbox", "published_notes"):
            out[table] = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return out


def _fp_of(did: str) -> str:
    from .census import fingerprint

    return fingerprint(did)
