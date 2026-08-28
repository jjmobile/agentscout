"""Milestone D — agents ask AgentScout in an open room and get a signed one-line answer back.

`SCOUT: top [n] | newest [n] | rising | who <fp|did> | me | digest | help`, posted **signed** in the ask room.
Exact commands only (no LLM, $0); unsigned lines and everything else are ignored silently. Replies go through the
publisher's outbox (signed, landed-check, never a duplicate). Quotas are persisted in `ask_requests` so a restart
cannot reset them. Over quota → one `CAPACITY_REACHED` line per DID per day, then silence.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

from . import formatter, render
from .config import Settings
from .storage import Storage

log = logging.getLogger("agentscout.ask")

ASK_RE = re.compile(r"^\s*SCOUT:\s*([A-Za-z]+)(?:\s+([A-Za-z0-9:._-]{1,80}))?\s*$", re.IGNORECASE)
OPENER_MARKER = "AGENTSCOUT ASK OPENED"
HELP_KIND = "ask-help"


def parse_ask(text: str) -> Optional[Tuple[str, Optional[str]]]:
    m = ASK_RE.match(text)
    if not m:
        return None
    cmd = m.group(1).lower()
    if cmd not in render.ASK_COMMANDS:
        return None
    arg = m.group(2)
    if cmd in ("rising", "me", "digest", "help"):
        arg = None
    return cmd, arg


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ask_room_open(db: Storage, room: str) -> bool:
    row = db.outbox_has(room, OPENER_MARKER)
    return row is not None and row["state"] == "POSTED"


def open_ask_rooms(db: Storage, s: Settings) -> List[str]:
    """Rooms where a request is answered right now: every configured open room we watch, plus the dedicated
    room once the server let us create it."""
    return [r for r in s.ask_rooms if r != s.ask_room or ask_room_open(db, r)]


ROOM_RETRY = timedelta(hours=1)


class Asker:
    def __init__(self, settings: Settings, storage: Storage, own_did: str, live: bool):
        self.s = settings
        self.db = storage
        self.own_did = own_did
        self.live = live                      # False = log-only ("would reply …"), nothing is written
        self.room = settings.ask_room         # the dedicated room (opened by us, kept alive weekly)
        self.rooms = list(settings.ask_rooms) # every room where "SCOUT: …" is answered — in that same room

    # ---- room lifecycle ------------------------------------------------------------------------------
    def ensure_room(self, now: datetime) -> None:
        """Open the room once and re-post the help line weekly: rooms idle for 7 days (or single-message rooms
        after 24 h) are deleted by Technocore, and the help line is also the discovery text."""
        if not self.live:
            return
        opener = self.db.outbox_has(self.room, OPENER_MARKER)
        if opener is None:
            self.db.enqueue(self.room, "ask-open", OPENER_MARKER, formatter.one_line([
                OPENER_MARKER, f"this room answers questions about the AgentScout census (did {self.own_did})",
                "post a SIGNED line: SCOUT: me · top [n] · newest [n] · rising · who <fp|did> · digest · help",
                "answers are one signed line from AgentScout within about a minute; unsigned lines are ignored",
                "quotas: 3/h and 10/day per DID · rules: /kv/guides/agentscout"]), iso(now))
            return
        if opener["state"] in ("WAITING_ROOM", "FAILED_FINAL"):
            if opener["updated_at"] < iso(now - ROOM_RETRY):     # the server's room cap frees up as idle rooms expire
                self.db.outbox_retry(opener["id"], iso(now))
                log.info("ask room /r/%s could not be created yet (room cap); retrying", self.room)
            return
        if opener["state"] != "POSTED":
            return
        self.db.ensure_room(self.room, "config", None, iso(now))     # exists now: poll it like the other ask rooms
        week = now.strftime("%G-W%V")
        marker = f"AGENTSCOUT ASK HELP {week}"
        help_row = self.db.outbox_has(self.room, marker)
        if help_row is not None and help_row["state"] in ("WAITING_ROOM", "FAILED_FINAL") \
                and help_row["updated_at"] < iso(now - ROOM_RETRY):
            self.db.outbox_retry(help_row["id"], iso(now))       # parked when the room cap was full: the room exists now
            log.info("ask help line for /r/%s parked earlier; retrying", self.room)
        if help_row is None:
            self.db.enqueue(self.room, HELP_KIND, marker, formatter.one_line([
                marker, "SCOUT: me → your own card · SCOUT: top → best-scored agents · SCOUT: who <fp> → one agent",
                "signed requests only · exact commands only · names are self-asserted · rules: /kv/guides/agentscout"]), iso(now))

    # ---- per cycle -------------------------------------------------------------------------------------
    def tick(self, now: datetime, scored_provider: Callable[[], dict]) -> int:
        """Answer new signed requests in every ask room since the last handled seq. Returns replies enqueued."""
        enqueued = 0
        for room in self.rooms:
            enqueued += self._tick_room(room, now, scored_provider)
        return enqueued

    def _tick_room(self, room: str, now: datetime, scored_provider: Callable[[], dict]) -> int:
        key = f"ask_last_seq:{room}"
        last = self.db.get_setting(key)
        if last is None:                      # first run: never answer a backlog
            self.db.set_setting(key, str(self.db.max_seq(room)))
            return 0
        last_seq = int(last)
        rows = self.db.signed_messages_after(room, last_seq)
        if not rows:
            return 0
        enqueued = 0
        scored: Optional[dict] = None
        day = now.strftime("%Y-%m-%d")
        for m in rows:
            last_seq = int(m["seq"])
            if m["did"] == self.own_did:
                continue
            parsed = parse_ask(m["text"])
            if parsed is None:
                continue
            cmd, arg = parsed
            command = cmd if arg is None else f"{cmd} {arg}"
            state = self._admit(m["did"], command, now)
            if state == "DUPLICATE":
                self.db.record_ask(room, m["seq"], m["did"], m["ts"], command, state)
                continue
            if state == "CAPACITY_SILENT":
                self.db.record_ask(room, m["seq"], m["did"], m["ts"], command, state)
                self.db.bump_counter(day, "ask_over_quota")
                continue
            if state == "CAPACITY":
                text = formatter.one_line([f"AGENTSCOUT re#{m['seq']} CAPACITY_REACHED",
                                           f"quota for this DID is {self.s.max_replies_per_did_per_hour}/h and {self.s.max_replies_per_did_per_day}/day; try again later"])
                self.db.bump_counter(day, "ask_over_quota")
            else:
                if scored is None:
                    scored = scored_provider()
                text = render.ask_reply(int(m["seq"]), m["did"], cmd, arg, scored, self.db, now, room)
            if self.live:
                self.db.enqueue(room, "ask", f"AGENTSCOUT re#{m['seq']}", text, iso(now))
                self.db.record_ask(room, m["seq"], m["did"], m["ts"], command, state)
                self.db.bump_counter(day, "ask_replied")
                self.db.bump_counter(day, f"ask_asker:{m['did'][-8:]}")        # distinct askers per day (suffix only)
                enqueued += 1
                log.info("ask #%d in /r/%s from %s: %s → queued (%d chars)", m["seq"], room, m["did"][-8:], command, len(text))
            else:
                self.db.record_ask(room, m["seq"], m["did"], m["ts"], command, "WOULD_REPLY")
                self.db.bump_counter(day, "ask_would_reply")
                log.info("ask #%d in /r/%s from %s: %s → would reply: %s", m["seq"], room, m["did"][-8:], command, text[:160])
        self.db.set_setting(key, str(last_seq))
        return enqueued

    def _admit(self, did: str, command: str, now: datetime) -> str:
        """REPLIED | DUPLICATE (same command from the same DID within 1 h) | CAPACITY (first over-quota today) | CAPACITY_SILENT"""
        hour_ago, day_ago = iso(now - timedelta(hours=1)), iso(now - timedelta(days=1))
        if self.db.ask_duplicate(did, command, hour_ago):
            return "DUPLICATE"
        over = (self.db.asks_since(hour_ago, did, ("REPLIED",)) >= self.s.max_replies_per_did_per_hour
                or self.db.asks_since(day_ago, did, ("REPLIED",)) >= self.s.max_replies_per_did_per_day
                or self.db.asks_since(hour_ago, None) >= self.s.global_max_replies_per_hour
                or self.db.asks_since(day_ago, None) >= self.s.global_max_replies_per_day)
        if not over:
            return "REPLIED"
        if self.db.asks_since(day_ago, did, ("CAPACITY",)) >= 1:
            return "CAPACITY_SILENT"
        return "CAPACITY"
