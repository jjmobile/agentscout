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

from . import partners, render, tclk
from .config import Settings
from .storage import Storage
from .technocore import TechnocoreClient, TechnocoreError

log = logging.getLogger("agentscout.commerce")

AMOUNT = "200"                        # the task mill's going rate on the paper rail (W3, 2026-09-09); was 1000000
OFFER_OPEN_HOURS = 6                  # expiresMs: offer dies unanswered after this
CLAIM_BY_HOURS = 20                   # payee's safe claim window ends here
REFUND_AFTER_HOURS = 22               # we may reclaim (and close the day's deal) from here
DEAL_ROOM_POLL_SECONDS = 120          # while a deal is live, read its room at most this often
OFFER_SPACING_MINUTES = 20            # pairings: earliest next offer after the previous deal of the day closed
ACCEPT_WINDOW_SECONDS = 40            # accepts land ~1 s after an offer (p50 1.1 s, p90 3.3 s on 2026-09-11)
ACCEPT_SETTLE_SECONDS = 12            # keep listening this long after the first accept so a partner can show up
ACCEPT_POLL_PAUSE_SECONDS = 2         # between live reads of the board (each read costs one budget unit)
PARTNER_WINDOW_DAYS = 7


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
        self._partners_day: Optional[str] = None
        self._partners: set = set()
        self._sleep = time.sleep

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
        if now.hour < self.s.digest_utc_hour:
            return
        todays = self.db.tclk_deals_for_day(day)
        if len(todays) >= self.s.tclk_offers_per_day:
            return
        if todays and max(r["updated_at"] for r in todays) > iso(now - timedelta(minutes=OFFER_SPACING_MINUTES)):
            return
        partner_set = self._partners_for(now)
        task = render.credence_task_line(self.s.kv_ns, self.id.did, now)
        offer = tclk.make_offer(
            self.id.did, AMOUNT,
            expires_ms=now_ms(now + timedelta(hours=OFFER_OPEN_HOURS)),
            claim_by_ms=now_ms(now + timedelta(hours=CLAIM_BY_HOURS)),
            refund_after_ms=now_ms(now + timedelta(hours=REFUND_AFTER_HOURS)),
            job_id=task.split(" | ")[1],
            job_context=render.offer_job_context(self.s.kv_ns, day))
        self.db.tclk_upsert(day, offer["id"], json.dumps(offer), "proposed", iso(now))
        self.pub._enqueue("tclk-offer", offer["id"], tclk.encode_frame(offer), now,
                          room=self.s.tclk_offers_room)
        log.info("tclk: day %s offer %s opened (%d/%d today, job %s)", day, offer["id"][:18], len(todays) + 1,
                 self.s.tclk_offers_per_day, offer.get("job", {}).get("id"))
        # Post it now and listen right away: accepts arrive within seconds, and a since-read of the board only
        # ever returns the newest page, so an accept not read within ~200 board messages is gone for good.
        self.pub.flush_outbox(now)
        posted = self.db.outbox_has(self.s.tclk_offers_room, offer["id"])
        seq = posted["posted_seq"] if posted is not None else None
        if not seq:
            return                                 # not landed yet: the stored-board scan remains the fallback
        self.db.tclk_upsert(day, offer["id"], json.dumps(offer), "proposed", iso(now), posted_seq=int(seq))
        used_today = {r["payee"] for r in todays if r["payee"]}
        self._collect_accepts_live(day, offer, int(seq), partner_set, used_today, now)

    def _partners_for(self, now: datetime) -> set:
        """The day's partner set (partners.rank over the last 7 days of the board), published once a day."""
        day = now.strftime("%Y-%m-%d")
        if self._partners_day == day:
            return self._partners
        since = iso(now - timedelta(days=PARTNER_WINDOW_DAYS))
        ranked = partners.rank(((r["sender_did"], r["text"]) for r in
                                self.db.iter_settlement_frames(self.s.tclk_offers_room, since)), self.id.did)
        self._partners = {p.did for p in ranked}
        self._partners_day = day
        note = render.partners_note(self.s.kv_ns, now, ranked, self.s.tclk_offers_per_day)
        self.pub.write_note_cas(self.s.kv_ns, "partners", note, now)
        log.info("tclk: %d partners for %s (top: %s)", len(ranked), day,
                 ", ".join(f"{p.did[-8:]}/{p.distinct_payers}" for p in ranked[:5]) or "none")
        return self._partners

    def _collect_accepts_live(self, day: str, offer: Dict, posted_seq: int, partner_set: set,
                              used_today: set, now: datetime) -> None:
        room = self.s.tclk_offers_room
        base = tclk.open_contract(offer)
        found: Dict[str, int] = {}            # payee -> board seq of its (valid) accept
        frames: Dict[str, Dict] = {}
        cursor = posted_seq
        start = time.monotonic()
        first_at: Optional[float] = None
        while True:
            try:
                data = self.c.read_room(room, since=cursor, limit=200, wait=5)
            except (TechnocoreError, ValueError) as exc:
                log.info("tclk: live accept read failed (%s); the stored-board scan will pick it up", exc)
                break
            for m in data.get("messages", []):
                try:
                    seq = int(m.get("seq"))
                except (TypeError, ValueError):
                    continue
                cursor = max(cursor, seq)
                text = m.get("text", "")
                if not tclk.is_tclk_line(text):
                    continue
                try:
                    frame = tclk.decode_frame(text)
                except tclk.TclkError:
                    continue
                if frame["type"] != "accept" or frame.get("ref") != offer["id"] or frame["from"] != m.get("from"):
                    continue
                _nxt, ok, _why = tclk.apply_frame(base, frame, now_ms(now))
                if ok and frame["from"] not in found:
                    found[frame["from"]] = seq
                    frames[frame["from"]] = frame
                    first_at = first_at if first_at is not None else time.monotonic()
            elapsed = time.monotonic() - start
            if elapsed >= ACCEPT_WINDOW_SECONDS:
                break
            if first_at is not None and time.monotonic() - first_at >= ACCEPT_SETTLE_SECONDS:
                break
            self._sleep(ACCEPT_POLL_PAUSE_SECONDS)
        if not found:
            log.info("tclk: offer %s: no accept within %ds (%d acceptors seen none valid)", offer["id"][:18],
                     ACCEPT_WINDOW_SECONDS, 0)
            return
        payee = partners.choose_payee(found, partner_set, used_today)
        frame = frames[payee]
        nxt, ok, why = tclk.apply_frame(base, frame, now_ms(now))
        if not ok:
            log.info("tclk: chosen accept no longer applies (%s)", why)
            return
        accept = dict(frame, _seen_ms=now_ms(now))
        self.db.tclk_upsert(day, offer["id"], json.dumps(offer), "accepted", iso(now), contract=nxt["contract"],
                            accept_json=json.dumps(accept), payee=payee)
        log.info("tclk: offer %s: %d acceptors, locking with %s (%s); contract %s", offer["id"][:18], len(found),
                 payee[-8:], "partner" if payee in partner_set else "first acceptor", nxt["contract"][:18])
        row = self.db.tclk_active_deal()
        state = self._fold(row) if row is not None else None
        if state is not None and state["state"] == "accepted":
            self._lock(row, state, now)
            self.pub.flush_outbox(now)

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
            seen_ms = accept.pop("_seen_ms", 0)      # bookkeeping key; the fail-closed validator must never see it
            state, ok, why = tclk.apply_frame(state, accept, seen_ms)
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
                                iso(now), contract=nxt["contract"], accept_json=json.dumps(accept),
                                payee=frame["from"])
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
