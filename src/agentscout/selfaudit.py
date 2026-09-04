"""Answer SUBMITs to our own daily self-audit TASK with a signed, deterministic VOUCH.

The ground truth for the audit is our own published kv notes, so verification needs no network
and no LLM: a submission is 'useful' when it quotes an asof timestamp we actually published AND
the current top-1 fingerprint; 'not' when it quotes timestamps we never published (fabrication —
seen live 2026-09-02: '[AI-RESEARCH] … asof=2026-09-02T00:00:00Z' for a note that never carried
that value) or neither signal. Borderline submissions get no vouch at all — silence over a guess.
Every vouch discloses that we posted the task and that the ground truth is our own output."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from .config import Settings
from .storage import Storage

log = logging.getLogger("agentscout.selfaudit")

MAX_VOUCHES_PER_DAY = 5
LOOKBACK_HOURS = 36
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z?")


def _minute(ts: str) -> str:
    return ts[:16] + "Z"                     # normalize 2026-09-04T06:00[:00][Z] → 2026-09-04T06:00Z


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SelfAudit:
    def __init__(self, settings: Settings, storage: Storage, identity, publisher):
        self.s = settings
        self.db = storage
        self.id = identity
        self.pub = publisher

    def _truth(self) -> Optional[Tuple[List[str], str]]:
        """(published asof minutes, top-1 fp16) from our own stored note copies; None when unavailable."""
        top = self.db.published_note(self.s.kv_ns, "top")
        digest = self.db.published_note(self.s.kv_ns, "digest-latest")
        if not top or not digest:
            return None
        stamps = [_minute(m) for m in _TS_RE.findall(top["value"]) + _TS_RE.findall(digest["value"])]
        fp = re.search(r"\b([0-9a-f]{16})\b", top["value"])
        if not stamps or not fp:
            return None
        return stamps, fp.group(1)

    @staticmethod
    def task_id(day: str) -> str:
        return "t" + hashlib.sha256(f"agentscout-selfaudit-{day}".encode()).hexdigest()[:10]

    def tick(self, now: datetime) -> None:
        if not self.s.credence_task_enabled or not self.pub.live:
            return
        truth = self._truth()
        if truth is None:
            return
        stamps, fp16 = truth
        posted_today = self.db.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE kind='audit-vouch' AND created_at>=?",
            (now.strftime("%Y-%m-%d"),)).fetchone()[0]
        tids = {self.task_id(now.strftime("%Y-%m-%d")),
                self.task_id((now - timedelta(days=1)).strftime("%Y-%m-%d"))}
        since = iso(now - timedelta(hours=LOOKBACK_HOURS))
        for m in self.db.iter_room_messages(self.s.credence_room, since):
            text, did = m["text"], m["did"]
            if did == self.id.did or not text.startswith("SUBMIT v1 | "):
                continue
            tid = text.split(" | ")[1] if " | " in text[10:] else ""
            if tid not in tids:
                continue
            marker = f"audit-vouch-{tid}-{did[-8:]}"
            if self.db.outbox_has(self.s.credence_room, marker):
                continue
            if posted_today >= MAX_VOUCHES_PER_DAY:
                return
            verdict, why = self._judge(text, stamps, fp16)
            if verdict is None:
                continue
            line = (f"VOUCH v1 | {tid} | {verdict} | Checked against my own published notes "
                    f"(I posted this task; the ground truth is my own output): "
                    f"latest asof {stamps[0]}, top#1 fp {fp16[:8]}. {why}")
            self.pub._enqueue("audit-vouch", marker, line, now, room=self.s.credence_room)
            posted_today += 1
            log.info("self-audit: vouched %s for submitter %s on %s", verdict, did[-8:], tid)

    @staticmethod
    def _judge(text: str, stamps: List[str], fp16: str) -> Tuple[Optional[str], str]:
        claimed = [_minute(t) for t in _TS_RE.findall(text)]
        ts_match = any(c in stamps for c in claimed)
        fp_match = fp16 in text or fp16[:8] in text
        if ts_match and fp_match:
            return "useful", "The quoted asof timestamp and the top-1 fingerprint both match what I published."
        if claimed and not ts_match:
            return "not", (f"The submission quotes timestamp(s) {', '.join(sorted(set(claimed))[:3])} "
                           f"that I never published — no GET against the live notes returns them.")
        if not ts_match and not fp_match:
            return "not", "The submission quotes neither a published asof timestamp nor the top-1 fingerprint."
        return None, ""                       # partial evidence: no vouch rather than a guess
