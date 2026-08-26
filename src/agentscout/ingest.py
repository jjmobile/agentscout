"""Ingestion cycle: rooms, events, DID notes, room owners, artifact checks. Read-only."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .census import extract_kv_refs, fingerprint, is_signed, parse_note, text_hash, parse_ts
from .config import Settings
from .storage import Storage
from .technocore import TechnocoreClient, TechnocoreError

log = logging.getLogger("agentscout.ingest")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


DID_SHARDS = 256    # /kv/did-<first 2 hex>/<remaining 14>: 00..ff


class Ingestor:
    def __init__(self, settings: Settings, client: TechnocoreClient, storage: Storage):
        self.s = settings
        self.c = client
        self.db = storage
        self._note_keys: List[str] = []
        self._note_keys_fetched_at: Optional[datetime] = None
        # sharded DID notes (/kv/did-<2>/<14>): one shard listed per cycle, so a full sweep
        # costs 256 reads spread over ~an hour instead of a burst that stalls room polling
        self._shard_keys: Dict[str, List[str]] = {}    # shard -> fingerprints seen in the last sweep
        self._shard_cursor: int = DID_SHARDS           # next shard to list; == DID_SHARDS when idle
        self._owner_rooms: List[str] = []

    # ---- limits -----------------------------------------------------------------------
    def discover_limits(self) -> dict:
        """Read the deployment's real limits; cap our read budget to 20 % of the published one."""
        try:
            card = self.c.agent_card()
        except TechnocoreError as exc:
            log.warning("agent.json unreachable (%s); keeping configured caps", exc)
            return {}
        limits = card.get("limits", {}) if isinstance(card, dict) else {}
        version = str(card.get("version", "?"))
        prev = self.db.get_setting("technocore_version")
        if prev and prev != version:
            log.warning("TECHNOCORE VERSION CHANGED %s -> %s (protocol drift: re-check docs)", prev, version)
        self.db.set_setting("technocore_version", version)
        reads = limits.get("reads_per_minute_per_ip")
        if isinstance(reads, int) and reads > 0:
            cap = max(1, reads // 5)
            if cap < self.c.budget.per_minute:
                log.info("read budget lowered from %d to %d/min (20%% of published %d)", self.c.budget.per_minute, cap, reads)
                self.c.budget.per_minute = cap
        log.info("technocore version=%s limits=%s effective_read_budget=%d/min", version,
                 {k: limits.get(k) for k in ("reads_per_minute_per_ip", "writes_per_minute_per_ip", "message_chars", "note_chars", "retention_seconds")},
                 self.c.budget.per_minute)
        return limits

    # ---- protocol docs ----------------------------------------------------------------
    DOC_KEYWORDS = ("faucet", "testnet", "airdrop", "flop", "wallet", "reward", "claim")

    def watch_docs(self, now: datetime) -> bool:
        """Every docs_watch_hours re-read llms.txt + agent.json; a change is a WARNING (it reaches Telegram) that
        names which of DOC_KEYWORDS appear — the $FLOP faucet is announced to arrive through this service, DID-gated,
        and we want to act the day it lands, not when someone notices. Returns True when a change was seen."""
        if self.s.docs_watch_hours <= 0:
            return False
        last = self.db.get_setting("docs_checked_at")
        if last and last >= iso(now - timedelta(hours=self.s.docs_watch_hours)):
            return False
        try:
            status, llms = self.c.get("/llms.txt")
            if status != 200:
                raise TechnocoreError(f"GET /llms.txt: HTTP {status}")
            card = json.dumps(self.c.agent_card(), sort_keys=True)
        except TechnocoreError as exc:
            log.warning("docs watch: %s", exc)
            return False
        self.db.set_setting("docs_checked_at", iso(now))
        text = llms + "\n" + card
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        low = text.casefold()
        words = sorted(w for w in self.DOC_KEYWORDS if w in low)
        prev_digest = self.db.get_setting("docs_hash")
        prev_words = set((self.db.get_setting("docs_keywords") or "").split(",")) - {""}
        self.db.set_setting("docs_hash", digest)
        self.db.set_setting("docs_keywords", ",".join(words))
        if prev_digest is None:
            log.info("docs watch: baseline llms.txt+agent.json (%d chars), keywords present: %s", len(text), ",".join(words) or "-")
            return False
        if prev_digest == digest:
            return False
        new = sorted(set(words) - prev_words)
        log.warning("TECHNOCORE DOCS CHANGED (llms.txt/agent.json): new keywords %s; present %s — re-read https://technocore.chat/llms.txt",
                    ",".join(new) or "-", ",".join(words) or "-")
        return True

    # ---- rooms ------------------------------------------------------------------------
    def ensure_config_rooms(self, now: datetime) -> None:
        for room in self.s.watch_rooms:
            self.db.ensure_room(room, "config", None, iso(now))
        self.db.ensure_room("events", "config", None, iso(now))

    def poll_events(self, now: datetime) -> int:
        """Record every announced public room; watch only the newest few for a short while."""
        state = self.db.room_state("events")
        since = state["last_seen_seq"] if state else 0
        data = self._read("events", since)
        if data is None:
            return 0
        new_rooms = 0
        candidates = []
        for m in data["messages"]:
            text = m.get("text", "")
            if m.get("from") == "server" and text.startswith("created ") and isinstance(m.get("ts"), str):
                room = text[len("created "):].strip()
                if self.db.add_room_seen(room, m["ts"], iso(now)):
                    new_rooms += 1
                    candidates.append((m["ts"], room))
        if self.s.new_room_watch_hours > 0 and self.s.max_event_rooms > 0:
            candidates.sort(reverse=True)
            for ts, room in candidates[: self.s.max_event_rooms]:
                try:
                    until = parse_ts(ts) + timedelta(hours=self.s.new_room_watch_hours)
                except ValueError:
                    continue
                if until > now and room not in self.s.watch_rooms:
                    self.db.ensure_room(room, "events", iso(until), iso(now))
        pruned = self.db.prune_event_rooms(self.s.max_event_rooms, iso(now))
        if since == 0 and new_rooms:
            log.info("events backlog: %d rooms recorded, watching the newest %d for %dh", new_rooms,
                     min(new_rooms, self.s.max_event_rooms), self.s.new_room_watch_hours)
        if pruned:
            log.debug("pruned %d event rooms from the watch set", pruned)
        self._store(data, "events", since, now, store_messages=False)
        return new_rooms

    def poll_rooms(self, now: datetime, deadline: Optional[float] = None) -> int:
        """Poll rooms least-recently-polled first; stop at the cycle deadline (monotonic) and resume next cycle."""
        inserted = 0
        event_cutoff = iso(now - timedelta(seconds=self.s.event_room_poll_seconds))
        rooms = self.db.rooms_to_poll(iso(now), event_rooms_updated_before=event_cutoff)
        for i, room in enumerate(rooms):
            if room == "events":
                continue
            if deadline is not None and time.monotonic() > deadline:
                log.info("cycle budget spent after %d/%d rooms; the rest are polled next cycle", i, len(rooms))
                break
            state = self.db.room_state(room)
            since = state["last_seen_seq"] if state else 0
            data = self._read(room, since)
            if data is None:
                continue
            inserted += self._store(data, room, since, now, store_messages=True)
        return inserted

    def _read(self, room: str, since: int) -> Optional[dict]:
        try:
            if since > 0:
                return self.c.read_room(room, since=since, limit=200)
            if not self.s.process_backlog:
                data = self.c.read_room(room, limit=1)
                return data
            return self.c.read_room(room, limit=200)
        except TechnocoreError as exc:
            log.warning("room %s: %s", room, exc)
            return None
        except ValueError as exc:
            log.warning("room %s skipped: %s", room, exc)
            return None

    def _store(self, data: dict, room: str, since: int, now: datetime, store_messages: bool) -> int:
        msgs = data.get("messages", [])
        first_seq = data.get("first_seq")
        last_seq = data.get("last_seq")
        if since > 0 and isinstance(first_seq, int) and first_seq > since + 1:
            self.db.record_gap(room, since + 1, first_seq, iso(now))
            log.warning("sequence gap in %s: expected %d, first available %d", room, since + 1, first_seq)
        inserted = 0
        if store_messages and msgs:
            rows = []
            for m in msgs:
                if not isinstance(m.get("seq"), int) or not isinstance(m.get("text"), str):
                    continue
                signed = is_signed(m)
                sender = str(m.get("from", ""))[:200]
                rows.append((m["seq"], str(m.get("ts", "")), sender, sender if signed else None, signed, m["text"][:4096], text_hash(m["text"])))
            inserted = self.db.insert_messages(room, rows, iso(now))
            for m in msgs:
                if is_signed(m):
                    for ns, key in extract_kv_refs(m.get("text", "")):
                        self.db.add_artifact_ref(f"/kv/{ns}/{key}", ns, key, m["from"], str(m.get("ts", "")))
        if isinstance(last_seq, int) and last_seq > since:
            self.db.set_cursor(room, last_seq, iso(now))
        else:
            self.db.touch_room(room, iso(now))
        if inserted:
            log.info("room %s: +%d messages (cursor %s)", room, inserted, last_seq)
        return inserted

    # ---- DID notes ------------------------------------------------------------------------
    def scan_notes(self, now: datetime) -> int:
        if self.s.notes_per_cycle <= 0:
            return 0
        stale_keys = self._note_keys_fetched_at is None or (now - self._note_keys_fetched_at) > timedelta(hours=self.s.did_scan_hours)
        if stale_keys:
            try:
                keys = self.c.list_note_keys("did")
            except TechnocoreError as exc:
                log.warning("list /kv/did failed: %s", exc)
                keys = []
            fps = [k for k in keys if len(k) == 16 and all(ch in "0123456789abcdef" for ch in k)]
            self._note_keys = fps
            self._note_keys_fetched_at = now
            self.db.set_setting("did_namespace_keys", str(len(keys)))
            log.info("/kv/did lists %d keys (%d fingerprint-shaped)", len(keys), len(fps))
            self._list_owner_rooms()
            self._shard_cursor = 0                     # the flat namespace is full: new notes live in the shards
        self._list_next_shard()
        # agents observed in messages may lack a note key in a (capped) listing: try them first anyway
        sharded = [fp for fps in self._shard_keys.values() for fp in fps]
        candidates = list(dict.fromkeys([r["fp"] for r in self.db.agents()] + self._note_keys + sharded))
        stale_before = iso(now - timedelta(days=self.s.note_refresh_days))
        queue = self.db.note_fetch_queue(candidates, stale_before, self.s.notes_per_cycle)
        fetched = 0
        for fp in queue:
            try:
                text = self.c.read_did_note(fp)
            except TechnocoreError as exc:
                log.warning("note %s: %s", fp, exc)
                continue
            if text is None:
                self.db.upsert_note(fp, "", "", None, iso(now))  # remember the miss; refreshed after note_refresh_days
                continue
            did, _ = parse_note(text)
            self.db.upsert_note(fp, text[:8192], text_hash(text), did, iso(now))
            fetched += 1
        if fetched:
            log.info("DID notes fetched: %d (queue had %d)", fetched, len(queue))
        fetched += self._scan_owners(now)
        return fetched

    def _list_next_shard(self) -> None:
        """List one /kv/did-<shard> namespace per call; a failed shard keeps last sweep's keys."""
        if self._shard_cursor >= DID_SHARDS:
            return
        shard = f"{self._shard_cursor:02x}"
        self._shard_cursor += 1
        try:
            keys = self.c.list_note_keys(f"did-{shard}")
            self._shard_keys[shard] = [shard + k for k in keys if len(k) == 14 and all(ch in "0123456789abcdef" for ch in k)]
        except TechnocoreError as exc:
            log.warning("list /kv/did-%s failed: %s", shard, exc)
        if self._shard_cursor == DID_SHARDS:
            total = sum(len(v) for v in self._shard_keys.values())
            self.db.set_setting("did_sharded_keys", str(total))
            log.info("/kv/did-00..ff list %d sharded fingerprints", total)

    def _list_owner_rooms(self) -> None:
        try:
            self._owner_rooms = self.c.list_note_keys("room-owners")
            log.info("/kv/room-owners lists %d rooms", len(self._owner_rooms))
        except TechnocoreError as exc:
            log.warning("list /kv/room-owners failed: %s", exc)

    def _scan_owners(self, now: datetime) -> int:
        """Incremental: a few owner notes per cycle, missing first, then stale."""
        stale_before = iso(now - timedelta(days=self.s.note_refresh_days))
        queue = self.db.owner_fetch_queue(self._owner_rooms, stale_before, self.s.owners_per_cycle)
        found = 0
        for room in queue:
            try:
                val = self.c.read_note("room-owners", room)
            except (TechnocoreError, ValueError):
                continue
            did = parse_note(val)[0] if val else None
            self.db.set_room_owner(room, did or "", iso(now))
            if did:
                found += 1
        if queue:
            log.info("room owners: %d fetched, %d with a did:key owner", len(queue), found)
        return found

    # ---- artifacts ------------------------------------------------------------------------
    def check_artifacts(self, now: datetime) -> int:
        checked = 0
        for row in self.db.pending_artifact_refs(self.s.artifact_checks_per_cycle):
            try:
                val = self.c.read_note(row["ns"], row["key"])
            except (TechnocoreError, ValueError) as exc:
                log.debug("artifact %s: %s", row["ref"], exc)
                self.db.set_artifact_result(row["ref"], False, iso(now))
                continue
            self.db.set_artifact_result(row["ref"], bool(val), iso(now))
            checked += 1
        return checked


def fp_for(did: str) -> str:
    return fingerprint(did)
