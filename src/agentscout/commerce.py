"""P10.2 — one tclk/1 paper deal per day: AgentScout as payer, its daily self-audit as the job.

The choreography (offer in /r/tclk-offers → counterparty accept → paper lock → payee reveal →
receipt) is real; the settlement is the paper rail, which settles NOTHING — this is the
rehearsal Hayes' "start today" points at, and it becomes real spend the day a value rail
exists. Deterministic, no LLM anywhere; all posting rides the publisher's outbox (signed lane,
landed-check, idempotent markers)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from . import render, tclk
from .config import Settings
from .storage import Storage
from .technocore import TechnocoreClient, TechnocoreError

log = logging.getLogger("agentscout.commerce")

AMOUNT = "1000000"                    # rail-native minimal units; rehearsal value on the paper rail
OFFER_OPEN_HOURS = 6                  # expiresMs: offer dies unanswered after this
CLAIM_BY_HOURS = 20                   # payee's safe claim window ends here
REFUND_AFTER_HOURS = 22               # we may reclaim (and close the day's deal) from here
DEAL_ROOM_POLL_SECONDS = 120          # while a deal is live, read its room at most this often


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class Commerce:
    def __init__(self, settings: Settings, client: TechnocoreClient, storage: Storage,
                 identity, publisher):
        self.s = settings
        self.c = client
        self.db = storage
        self.id = identity
        self.pub = publisher
        self._last_room_read = 0.0

    # ---- cycle ----------------------------------------------------------------------------
    def tick(self, now: datetime) -> None:
        if not self.s.tclk_enabled or not self.pub.live:
            return
        row = self.db.tclk_active_deal()
        if row is None:
            self._maybe_open_offer(now)
            return
        state = self._fold(row)
        if state is None:
            return
        if state["state"] == "proposed":
            self._scan_accepts(row, state, now)
        elif state["state"] == "accepted":
            self._lock(row, state, now)
        elif state["state"] == "locked":
            self._watch_deal_room(row, state, now)

    # ---- steps ----------------------------------------------------------------------------
    def _maybe_open_offer(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if now.hour < self.s.digest_utc_hour or self.db.tclk_deal(day) is not None:
            return
        task = render.credence_task_line(self.s.kv_ns, self.id.did, now)
        offer = tclk.make_offer(
            self.id.did, AMOUNT,
            expires_ms=now_ms(now + timedelta(hours=OFFER_OPEN_HOURS)),
            claim_by_ms=now_ms(now + timedelta(hours=CLAIM_BY_HOURS)),
            refund_after_ms=now_ms(now + timedelta(hours=REFUND_AFTER_HOURS)),
            job_id=task.split(" | ")[1])
        self.db.tclk_upsert(day, offer["id"], json.dumps(offer), "proposed", iso(now))
        self.pub._enqueue("tclk-offer", offer["id"], tclk.encode_frame(offer), now,
                          room=self.s.tclk_offers_room)
        log.info("tclk: day %s offer %s opened (job %s)", day, offer["id"][:18], offer.get("job", {}).get("id"))

    def _fold(self, row) -> Optional[Dict]:
        try:
            offer = json.loads(row["offer_json"])
            state = tclk.open_contract(offer)
        except (ValueError, tclk.TclkError) as exc:
            log.error("tclk: stored offer for %s unusable (%s); abandoning", row["day"], exc)
            self.db.tclk_upsert(row["day"], row["offer_id"], row["offer_json"], "expired", iso(datetime.now(timezone.utc)))
            return None
        if row["accept_json"]:
            accept = json.loads(row["accept_json"])
            state, ok, why = tclk.apply_frame(state, accept, accept.get("_seen_ms", 0))
            if not ok:
                log.error("tclk: stored accept no longer applies (%s)", why)
                return None
        if row["state"] in ("locked",):        # lock was ours; replay it onto the fold
            state = dict(state, state="locked")
        return state

    def _scan_accepts(self, row, state: Dict, now: datetime) -> None:
        offer = state["offer"]
        if now_ms(now) >= offer["expiresMs"]:
            self.db.tclk_upsert(row["day"], row["offer_id"], row["offer_json"], "expired", iso(now))
            log.info("tclk: offer %s expired unanswered", row["offer_id"][:18])
            return
        since = (now - timedelta(hours=OFFER_OPEN_HOURS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for m in self.db.iter_room_messages(self.s.tclk_offers_room, since):
            if not tclk.is_tclk_line(m["text"]):
                continue
            try:
                frame = tclk.decode_frame(m["text"])
            except tclk.TclkError:
                continue
            if frame["type"] != "accept" or frame.get("ref") != offer["id"]:
                continue
            if frame["from"] != m["did"]:      # transport-verified sender must match the frame
                continue
            accept = dict(frame, _seen_ms=now_ms(now))
            nxt, ok, why = tclk.apply_frame(state, frame, now_ms(now))
            if not ok:
                log.info("tclk: rejecting accept from %s (%s)", frame["from"][-8:], why)
                continue
            self.db.tclk_upsert(row["day"], row["offer_id"], row["offer_json"], "accepted",
                                iso(now), contract=nxt["contract"], accept_json=json.dumps(accept))
            log.info("tclk: offer %s accepted by %s; contract %s", offer["id"][:18],
                     frame["from"][-8:], nxt["contract"][:18])
            return

    def _lock(self, row, state: Dict, now: datetime) -> None:
        contract, offer = row["contract"], state["offer"]
        ns, key = tclk.paper_note(contract)
        record = tclk.encode_paper_record("locked", offer["lock"],
                                          state["accept"]["statement"], offer["refundAfterMs"])
        try:
            status, body = self.c.write_note(ns, key, record, if_absent=True)
        except TechnocoreError as exc:
            log.warning("tclk: paper lock write failed (%s); retrying next cycle", exc)
            return
        if status not in (200, 409):           # 409: our earlier write already landed
            log.warning("tclk: paper lock write HTTP %d %s; retrying next cycle", status, body[:80])
            return
        frame = tclk.make_frame("lock", self.id.did, contract, rail="paper", ref=contract)
        self.pub._enqueue("tclk-lock", f"tclk-lock-{contract[:18]}", tclk.encode_frame(frame),
                          now, room=tclk.deal_room(contract))
        self.db.tclk_upsert(row["day"], row["offer_id"], row["offer_json"], "locked", iso(now))
        log.info("tclk: contract %s locked on the paper rail; deal room %s",
                 contract[:18], tclk.deal_room(contract))

    def _watch_deal_room(self, row, state: Dict, now: datetime) -> None:
        if time.monotonic() - self._last_room_read < DEAL_ROOM_POLL_SECONDS:
            return
        self._last_room_read = time.monotonic()
        contract, offer = row["contract"], state["offer"]
        room = tclk.deal_room(contract)
        try:
            data = self.c.read_room(room, limit=200)
        except (TechnocoreError, ValueError) as exc:
            log.debug("tclk: deal room read failed (%s)", exc)
            data = {"messages": []}
        for m in data.get("messages", []):
            text = m.get("text", "")
            if not tclk.is_tclk_line(text):
                continue
            try:
                frame = tclk.decode_frame(text)
            except tclk.TclkError:
                continue
            if frame["type"] != "reveal" or frame.get("from") != m.get("from"):
                continue
            nxt, ok, why = tclk.apply_frame(state, frame, now_ms(now))
            if not ok:
                continue
            self._settle(row, state, frame["secret"], now)
            return
        if now_ms(now) >= offer["refundAfterMs"]:
            self._refund(row, state, now)

    def _settle(self, row, state: Dict, secret: str, now: datetime) -> None:
        contract, offer = row["contract"], state["offer"]
        ns, key = tclk.paper_note(contract)
        record = tclk.encode_paper_record("claimed", offer["lock"], state["accept"]["statement"],
                                          offer["refundAfterMs"], secret=secret)
        if not self.pub.write_note_cas(ns, key, record, now):
            log.warning("tclk: paper claim write failed; retrying next cycle")
            return
        receipt = tclk.make_frame("receipt", self.id.did, contract, outcome="claimed",
                                  rail="paper", ref=contract)
        self.pub._enqueue("tclk-receipt", f"tclk-receipt-{contract[:18]}",
                          tclk.encode_frame(receipt), now, room=tclk.deal_room(contract))
        self.db.tclk_upsert(row["day"], row["offer_id"], row["offer_json"], "claimed", iso(now))
        log.warning("TCLK DEAL CLAIMED: contract %s — payee revealed; first completed paper deal "
                    "choreography for this DID", contract[:18])

    def _refund(self, row, state: Dict, now: datetime) -> None:
        contract, offer = row["contract"], state["offer"]
        ns, key = tclk.paper_note(contract)
        record = tclk.encode_paper_record("refunded", offer["lock"], state["accept"]["statement"],
                                          offer["refundAfterMs"])
        if not self.pub.write_note_cas(ns, key, record, now):
            return
        for kind, frame in (("tclk-refund", tclk.make_frame("refund", self.id.did, contract)),
                            ("tclk-receipt", tclk.make_frame("receipt", self.id.did, contract,
                                                             outcome="refunded", rail="paper", ref=contract))):
            self.pub._enqueue(kind, f"{kind}-{contract[:18]}", tclk.encode_frame(frame), now,
                              room=tclk.deal_room(contract))
        self.db.tclk_upsert(row["day"], row["offer_id"], row["offer_json"], "refunded", iso(now))
        log.info("tclk: contract %s refunded (no reveal before the deadline)", contract[:18])
