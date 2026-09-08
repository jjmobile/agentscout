"""W1 — the payee side of tclk/1: accept other agents' offers we can fulfil deterministically.

Loop (per cycle, after ingest):
  1. advance deals in flight from the derived deal rooms: lock → deliver + reveal; receipt → claimed.
  2. scan new offers on the board; for each one whose job we can solve (jobs.solve) and whose payer
     passes screening, mint a hash lock, post `accept` on the board and a `heartbeat` in the deal
     room (which creates it), and remember the answer.

Everything is deterministic and fail-closed. No LLM. Paper rail only: nothing of value moves.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from . import jobs, tclk
from .config import Settings
from .identity import Identity
from .publisher import Publisher
from .storage import Storage
from .technocore import TechnocoreClient, TechnocoreError

log = logging.getLogger("agentscout.worker")

SEQ_SETTING = "worker_offers_seq"
MIN_EXPIRY_MARGIN_MS = 3 * 60 * 1000      # skip offers that expire sooner than this
MIN_CLAIM_MARGIN_MS = 8 * 60 * 1000       # …or whose claim window is nearly gone
MAX_ACCEPTS_PER_TICK = 2
SCREEN_FLAGS = ("contract_spam", "injection", "opaque")
RECEIPT_GRACE_MS = 30 * 60 * 1000
_REVIEW_RE = re.compile(r"\breview\b.*\b(PASS|FAIL)\b")


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class Worker:
    def __init__(self, settings: Settings, client: TechnocoreClient, storage: Storage, identity: Identity,
                 publisher: Publisher, scored_provider: Optional[Callable[[], Optional[dict]]] = None):
        self.s = settings
        self.c = client
        self.db = storage
        self.id = identity
        self.pub = publisher
        self.scored = scored_provider or (lambda: None)

    # ---- cycle ----------------------------------------------------------------------------
    def tick(self, now: datetime) -> bool:
        """Returns True when something was queued for posting (the caller flushes the outbox)."""
        if not self.s.worker_enabled or not self.pub.live:
            return False
        queued = self._advance_open(now)
        queued = self._scan_offers(now) or queued
        return queued

    # ---- deals in flight ---------------------------------------------------------------------
    def _advance_open(self, now: datetime) -> bool:
        queued = False
        for row in self.db.worker_open():
            try:
                queued = self._advance_one(row, now) or queued
            except (TechnocoreError, ValueError, tclk.TclkError) as exc:
                log.info("worker: deal %s: %s", row["contract"][:18], exc)
        return queued

    def _advance_one(self, row, now: datetime) -> bool:
        contract, offer = row["contract"], json.loads(row["offer_json"])
        payer, state = row["payer"], row["state"]
        room = tclk.deal_room(contract)
        t = now_ms(now)
        frames, lines = self._read_deal_room(room, payer)
        for f in frames:
            if f["type"] == "cancel":
                self.db.worker_set_state(contract, "cancelled", iso(now))
                log.info("worker: %s cancelled by the payer", contract[:18])
                return False
            if f["type"] == "receipt" and state in ("revealed", "locked"):
                grade = next((m.group(1) for m in (_REVIEW_RE.search(l) for l in lines) if m), None)
                final = "claimed" if f["outcome"] == "claimed" else f["outcome"]
                self.db.worker_set_state(contract, final, iso(now), grade=grade)
                log.log(logging.WARNING if grade == "FAIL" else logging.INFO,
                        "worker: %s %s (%s) grade=%s", contract[:18], final, row["family"], grade)
                return False
        if state == "accepted":
            lock = next((f for f in frames if f["type"] == "lock" and f.get("rail") in offer["rails"]), None)
            if lock is None:
                if t > offer["claimByMs"]:
                    self.db.worker_set_state(contract, "lapsed", iso(now))
                    log.info("worker: %s never locked before claimBy; lapsed", contract[:18])
                return False
            self.db.worker_set_state(contract, "locked", iso(now), lock_ref=lock.get("ref"))
            return self._deliver(row, contract, room, lock.get("ref"), now)
        if state == "locked":                    # attest: waiting for our own line's seq
            return self._deliver(row, contract, room, row["lock_ref"], now)
        if state == "revealed" and t > offer["refundAfterMs"] + RECEIPT_GRACE_MS:
            self.db.worker_set_state(contract, "unreceipted", iso(now))
            log.info("worker: %s revealed but no receipt before refundAfter+grace", contract[:18])
        return False

    def _deliver(self, row, contract: str, room: str, lock_ref: Optional[str], now: datetime) -> bool:
        answer = row["answer"]
        c18 = contract[:18]
        if answer == jobs.ATTEST_ANSWER:
            marker = f"wk-attest-{c18}"
            posted = self.db.outbox_has(room, marker)
            if posted is None:
                self.pub._enqueue("worker-attest", marker, f"tclk-attest {contract}", now, room=room)
                return True
            seq = posted["posted_seq"] if "posted_seq" in posted.keys() else None
            if not seq:
                return False                      # still posting; try again next cycle
            answer = f"attested seq {seq}"
        self.pub._enqueue("worker-deliver", f"wk-deliver-{c18}", answer, now, room=room)
        reveal = tclk.make_frame("reveal", self.id.did, contract, secret=row["secret"],
                                 **({"ref": lock_ref} if lock_ref else {}))
        self.pub._enqueue("worker-reveal", f"wk-reveal-{c18}", tclk.encode_frame(reveal), now, room=room)
        self.db.worker_set_state(contract, "revealed", iso(now))
        log.info("worker: %s delivered (%s) and revealed", c18, row["family"])
        return True

    def _read_deal_room(self, room: str, payer: str):
        """(payer's tclk frames, payer's plain lines) in the deal room; a missing room reads as empty."""
        try:
            data = self.c.read_room(room, limit=100)
        except TechnocoreError:
            return [], []
        frames: List[Dict] = []
        lines: List[str] = []
        for m in data.get("messages", []):
            if m.get("from") != payer:
                continue
            text = m.get("text", "")
            if tclk.is_tclk_line(text):
                try:
                    frames.append(tclk.decode_frame(text))
                except tclk.TclkError:
                    continue
            else:
                lines.append(text)
        return frames, lines

    # ---- new offers --------------------------------------------------------------------------
    def _scan_offers(self, now: datetime) -> bool:
        room = self.s.tclk_offers_room
        last = self.db.get_setting(SEQ_SETTING)
        if last is None:                          # first run: never replay the board's history
            last_seq = self.db.room_last_seq(room)
            self.db.set_setting(SEQ_SETTING, str(last_seq))
            return False
        last_seq = int(last)
        day = now.strftime("%Y-%m-%d")
        open_n = len(self.db.worker_open())
        day_n, per_payer = self.db.worker_counts(day)
        accepted = 0
        queued = False
        for m in self.db.iter_room_after_seq(room, last_seq):
            last_seq = max(last_seq, int(m["seq"]))
            if accepted >= MAX_ACCEPTS_PER_TICK or open_n >= self.s.worker_max_open or day_n >= self.s.worker_max_per_day:
                continue
            offer = self._eligible_offer(m, now, per_payer)
            if offer is None:
                continue
            answer = self._answer_for(offer)
            if answer is None:
                continue
            if self._accept(offer, answer, m["did"], day, now):
                accepted += 1
                open_n += 1
                day_n += 1
                per_payer[m["did"]] = per_payer.get(m["did"], 0) + 1
                queued = True
        self.db.set_setting(SEQ_SETTING, str(last_seq))
        return queued

    def _eligible_offer(self, m, now: datetime, per_payer: Dict[str, int]) -> Optional[Dict]:
        if not tclk.is_tclk_line(m["text"]):
            return None
        try:
            offer = tclk.decode_frame(m["text"])
        except tclk.TclkError:
            return None
        if offer["type"] != "offer" or offer["role"] != "payer" or offer["lock"] != "hash":
            return None
        if offer["from"] != m["did"] or offer["from"] == self.id.did:
            return None
        if "paper" not in offer["rails"]:
            return None
        t = now_ms(now)
        if offer["expiresMs"] - t < MIN_EXPIRY_MARGIN_MS or offer["claimByMs"] - t < MIN_CLAIM_MARGIN_MS:
            return None
        job = offer.get("job")
        if not isinstance(job, dict) or not isinstance(job.get("context"), str):
            return None
        if per_payer.get(offer["from"], 0) >= 1 or self.db.worker_has_offer(offer["id"]):
            return None
        if not self._payer_ok(offer["from"]):
            return None
        return offer

    def _payer_ok(self, did: str) -> bool:
        scored = self.scored() or {}
        entry = scored.get(did)
        if entry is None:
            return True                           # unknown / one-shot payer: nothing observed against it
        facts, result = entry
        flagged = any(k in result.penalties for k in SCREEN_FLAGS)
        return not (flagged and result.score < 40)

    def _answer_for(self, offer: Dict) -> Optional[str]:
        context = offer["job"]["context"]
        family = jobs.context_family(context)
        if family not in jobs.SUPPORTED_FAMILIES:
            return None
        text = context
        ref = jobs.context_spec_ref(context)
        if ref is not None:
            try:
                full = self.c.read_note(ref[0], ref[1])
            except TechnocoreError as exc:
                log.debug("worker: spec note %s/%s unreadable (%s)", ref[0], ref[1], exc)
                return None
            if full:
                text = full
        spec = jobs.parse_spec(text)
        if spec is None or spec.family != family:
            return None
        mref = jobs.material_ref(spec)
        if mref is not None:
            try:
                material = self.c.read_note(mref[0], mref[1])
            except TechnocoreError as exc:
                log.debug("worker: material note %s/%s unreadable (%s)", mref[0], mref[1], exc)
                return None
            if not material:
                return None
            jobs.attach_material(spec, material)
        return jobs.solve(spec)

    def _accept(self, offer: Dict, answer: str, payer: str, day: str, now: datetime) -> bool:
        secret, statement = tclk.generate_hash_lock()
        try:
            accept = tclk.make_accept(offer, self.id.did, statement)
        except tclk.TclkError as exc:
            log.info("worker: cannot accept %s (%s)", offer["id"][:18], exc)
            return False
        contract = accept["contract"]
        c18 = contract[:18]
        family = jobs.context_family(offer["job"]["context"]) or "?"
        self.db.worker_insert(contract, day, offer["id"], payer, json.dumps(offer), json.dumps(accept),
                              secret, family, answer, iso(now))
        self.pub._enqueue("worker-accept", f"wk-accept-{c18}", tclk.encode_frame(accept), now,
                          room=self.s.tclk_offers_room)
        hb = tclk.make_heartbeat(self.id.did, contract, note="agentscout worker")
        self.pub._enqueue("worker-heartbeat", f"wk-hb-{c18}", tclk.encode_frame(hb), now,
                          room=tclk.deal_room(contract))
        log.info("worker: accepted %s from %s (%s, %s %s); contract %s; answer %r",
                 offer["id"][:18], payer[-8:], family, offer["amount"], offer["asset"], c18, answer[:60])
        return True
