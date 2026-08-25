"""Milestone B: signed publishing to the owned feed room, kv convenience notes, DID-note keepalive.

Every outgoing line is built by formatter/render, persisted in the outbox BEFORE posting, signed over the
swept text, and — on any 5xx/timeout — checked for having landed before it is ever re-posted.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import formatter, render
from .config import Settings
from .identity import Identity
from .storage import Storage
from .technocore import TechnocoreClient, TechnocoreError, strip_banner

log = logging.getLogger("agentscout.publisher")

MAX_POST_ATTEMPTS = 5


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Publisher:
    def __init__(self, settings: Settings, client: TechnocoreClient, storage: Storage, identity: Identity, notify=None):
        self.s = settings
        self.c = client
        self.db = storage
        self.id = identity
        self.notify = notify
        self.live = (not settings.dry_run) and settings.publish_enabled
        self.owner_verified = False

    # ---- startup -------------------------------------------------------------------------
    def verify_ownership(self) -> bool:
        """We post only to a room whose owner note is exactly our DID."""
        try:
            val = self.c.read_note("room-owners", self.s.feed_room)
        except TechnocoreError as exc:
            log.warning("cannot verify ownership of %s: %s", self.s.feed_room, exc)
            return False
        self.owner_verified = (val or "").strip() == self.id.did
        if self.owner_verified:
            log.info("feed room %s ownership verified for our DID — LIVE publishing enabled", self.s.feed_room)
        if not self.owner_verified:
            log.warning("feed room %s is not owned by our DID (owner note: %s); publishing disabled until claimed "
                        "(scripts/claim_room.py)", self.s.feed_room, (val or "absent")[:80])
        return self.owner_verified

    # ---- per-cycle -------------------------------------------------------------------------
    def tick(self, now: datetime, scored: Optional[dict]) -> None:
        day = now.strftime("%Y-%m-%d")
        if scored is not None and now.hour >= self.s.digest_utc_hour:
            marker = f"AGENTSCOUT DIGEST {day}"
            if not self.db.outbox_has(self.s.feed_room, marker):
                text = render.digest_line(scored, self.db, now)
                self._enqueue("digest", marker, text, now)
                self.refresh_notes(scored, now)
            if now.weekday() == 0:
                wmarker = f"AGENTSCOUT WEEKLY {day}"
                if not self.db.outbox_has(self.s.feed_room, wmarker):
                    self._enqueue("weekly", wmarker, render.weekly_line(scored, self.db, now), now)
        self.flush_outbox(now)
        self.keepalive_did_note(now)

    def _enqueue(self, kind: str, marker: str, text: str, now: datetime) -> None:
        if not self.live:
            log.info("DRY_RUN: would post %s to %s: %s", kind, self.s.feed_room, text)
            if self.notify:
                self.notify.send(f"[dry-run] would post {kind}:\n{text}")
            self.db.enqueue(self.s.feed_room, kind, marker, text, iso(now))
            row = self.db.outbox_has(self.s.feed_room, marker)
            if row:
                self.db.outbox_update(row["id"], "DRY_RUN", iso(now))
            return
        self.db.enqueue(self.s.feed_room, kind, marker, text, iso(now))
        log.info("queued %s for %s (%d chars)", kind, self.s.feed_room, len(text))

    # ---- signed posting ------------------------------------------------------------------------
    def flush_outbox(self, now: datetime) -> None:
        if not self.live or not self.owner_verified:
            return
        for row in self.db.outbox_pending():
            if row["attempts"] >= MAX_POST_ATTEMPTS:
                self.db.outbox_update(row["id"], "FAILED_FINAL", iso(now), error="max attempts")
                log.error("outbox %d (%s) gave up after %d attempts", row["id"], row["kind"], row["attempts"])
                continue
            self.post_signed_line(row, now)

    def post_signed_line(self, row, now: datetime) -> str:
        room, text, marker = row["room"], formatter.sweep(row["text"]), row["marker"]
        nonce = self.db.next_nonce(room, int(now.timestamp() * 1000))
        sig = self.id.sign_message(room, nonce, text)
        self.db.outbox_update(row["id"], "POSTING", iso(now), nonce=nonce, bump_attempts=True)
        try:
            status, body = self.c.post_signed(room, self.id.did, sig, nonce, text)
        except TechnocoreError as exc:  # timeout / connection: may have landed
            return self._after_uncertain(row, now, f"{exc}")
        if status == 200:
            seq = self._seq_from_post_body(body)
            if seq is None:  # the POST reply is the room's text view; read the seq back
                seq = self.landed_seq(room, marker)
            self.db.outbox_update(row["id"], "POSTED", iso(now), posted_seq=seq)
            log.info("posted %s to %s (seq %s, nonce %d)", row["kind"], room, seq, nonce)
            if self.notify:
                self.notify.send(f"✅ posted {row['kind']} to /r/{room} (seq {seq}):\n{text}")
            return "POSTED"
        if status == 400:
            floor = self._max_nonce_in_ring(room)
            if floor:
                self.db.bump_nonce_floor(room, floor)
            self.db.outbox_update(row["id"], "FAILED_RETRYABLE", iso(now), error=f"400 {body.strip()[:120]}")
            log.warning("post to %s rejected (400): %s — nonce floor now %s", room, body.strip()[:120], floor)
            return "FAILED_RETRYABLE"
        if status == 403:
            self.db.outbox_update(row["id"], "FAILED_FINAL", iso(now), error=f"403 {body.strip()[:120]}")
            self.owner_verified = False
            log.error("post to %s forbidden (403): %s — ownership lost? publishing disabled", room, body.strip()[:120])
            return "FAILED_FINAL"
        if status >= 500:
            return self._after_uncertain(row, now, f"HTTP {status}")
        self.db.outbox_update(row["id"], "FAILED_RETRYABLE", iso(now), error=f"HTTP {status}")
        log.warning("post to %s: HTTP %d", room, status)
        return "FAILED_RETRYABLE"

    def _after_uncertain(self, row, now: datetime, why: str) -> str:
        """The write may have landed. Read the room; if our DID posted this marker, it is done."""
        seq = self.landed_seq(row["room"], row["marker"])
        if seq is not None:
            self.db.outbox_update(row["id"], "POSTED", iso(now), posted_seq=seq, error=f"landed despite {why}")
            log.info("post to %s landed despite %s (seq %d)", row["room"], why, seq)
            return "POSTED"
        self.db.outbox_update(row["id"], "FAILED_RETRYABLE", iso(now), error=why)
        log.warning("post to %s uncertain (%s) and not found in ring; will re-sign with a fresh nonce", row["room"], why)
        return "FAILED_RETRYABLE"

    def landed_seq(self, room: str, marker: str) -> Optional[int]:
        try:
            data = self.c.read_room(room, limit=50)
        except TechnocoreError:
            return None
        for m in reversed(data.get("messages", [])):
            if m.get("from") == self.id.did and marker in m.get("text", ""):
                return m.get("seq")
        return None

    def _max_nonce_in_ring(self, room: str) -> int:
        try:
            data = self.c.read_room(room, limit=200)
        except TechnocoreError:
            return 0
        return max((int(m.get("nonce") or 0) for m in data.get("messages", []) if m.get("from") == self.id.did), default=0)

    @staticmethod
    def _seq_from_post_body(body: str) -> Optional[int]:
        """JSON `last_seq` if the reply is JSON; else the `range a..b` of the text view; else None."""
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("last_seq") is not None:
                return int(data["last_seq"])
        except (ValueError, TypeError):
            pass
        m = re.search(r"range\s+\d+\.\.(\d+)", body)
        return int(m.group(1)) if m else None

    # ---- kv notes --------------------------------------------------------------------------------
    def refresh_notes(self, scored: dict, now: datetime) -> None:
        ns = self.s.kv_ns
        notes = {
            "new": render.list_note(render.newest(scored, 10), "newest", now),
            "top": render.list_note(render.top(scored, 10), "top", now),
            "digest-latest": formatter.note_line(render.digest_line(scored, self.db, now)),
        }
        for f, r in render.top(scored, self.s.kv_top_n):
            notes[f"agent-{f.fp}"] = render.agent_note(f, r, now)
        for key, value in notes.items():
            self.write_note_cas(ns, key, value, now)

    def write_note_cas(self, ns: str, key: str, value: str, now: datetime) -> bool:
        """Write our value; detect and log tampering (someone changed a note we own); our value wins."""
        prev = self.db.published_note(ns, key)
        if not self.live:
            log.debug("DRY_RUN: would write /kv/%s/%s (%d chars)", ns, key, len(value))
            return False
        if_value, if_absent = (prev["value"], False) if prev else (None, True)
        tampered = False
        for attempt in range(3):
            try:
                status, body = self.c.write_note(ns, key, value, if_value=if_value, if_absent=if_absent)
            except TechnocoreError as exc:
                log.warning("write /kv/%s/%s failed: %s", ns, key, exc)
                return False
            if status == 200:
                self.db.set_published_note(ns, key, value, iso(now), tampered=tampered)
                return True
            if status == 409:
                current = strip_banner(body)
                if prev is None or current != prev["value"]:
                    tampered = True
                    log.warning("NOTE_TAMPERED /kv/%s/%s (attempt %d); rewriting", ns, key, attempt + 1)
                if_value, if_absent = current, False
                continue
            log.warning("write /kv/%s/%s: HTTP %d %s", ns, key, status, body.strip()[:120])
            return False
        return False

    # ---- DID note ---------------------------------------------------------------------------------
    def did_note_value(self) -> str:
        return formatter.note_line(
            f"{self.id.did} name:AgentScout role:network-observer feed:{self.s.feed_room} repo:{self.s.repo_url} "
            f"scoring:{self.s.repo_url}/blob/main/SCORING.md observed-behaviour-not-endorsement")

    def keepalive_did_note(self, now: datetime) -> None:
        if not self.live:
            return
        prev = self.db.published_note("did", self.id.fp)
        due = prev is None or prev["written_at"] < iso(now - timedelta(hours=self.s.keepalive_note_hours))
        if not due:
            return
        if self.write_note_cas("did", self.id.fp, self.did_note_value(), now):
            log.info("DID note refreshed at /kv/did/%s", self.id.fp)
        else:
            log.warning("DID note write failed (namespace may be at capacity); retrying next keepalive window")
            self.db.set_published_note("did", self.id.fp, prev["value"] if prev else "", iso(now))
