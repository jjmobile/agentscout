"""Durable request intent, exposure and independently verified settlement evidence.

No network client lives here. A future official HTTP gateway can use the same journal as the
experimental chain client. Unverified receipts/manual imports never release funds or imply credit.
All amounts are integer asset base units; failed or ambiguous work remains a liability.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Dict, Optional


class JournalError(RuntimeError):
    pass


def base_units(value, decimals: int = 18) -> int:
    """Exact decimal configuration conversion. Never round a money amount through binary float."""
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        with localcontext() as context:
            context.prec = 80
            units = amount * (10 ** decimals)
        if units != units.to_integral_value() or units >= 1 << 128:
            raise ValueError
        return int(units)
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise JournalError("amount must be finite, nonnegative and exactly representable in base units") from exc


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class FinalizedSettlement:
    """A trusted verifier's result, obtained independently from a submitted receipt/status claim.

    The verifier must check canonical network finality and successful settlement, not merely parse
    fields from the miner. No production verifier is supplied until the official API is published.
    This establishes expenditure, never airdrop eligibility or credited allocation.
    """
    network: str
    account: str
    channel_id: str
    outcome: str                       # SETTLED or REFUNDED
    spent_base: int
    refunded_base: int
    fees_base: int
    transaction_id: str
    finalized_block: str
    source: str
    final_root: str = ""


class InferenceJournal:
    def __init__(self, storage):
        self.db = storage
        self.conn = storage.conn
        # Enable stronger durability only on connections opting into paid inference. In particular,
        # a persisted SUBMITTED state must survive power loss before an external side effect occurs.
        self.conn.execute("PRAGMA synchronous=FULL")

    @contextmanager
    def _transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def request(self, request_id: str):
        return self.conn.execute("SELECT * FROM inference_requests WHERE request_id=?", (request_id,)).fetchone()

    def events(self, request_id: str):
        return self.conn.execute("SELECT * FROM inference_events WHERE request_id=? ORDER BY id", (request_id,)).fetchall()

    def _event(self, request_id: str, kind: str, payload: Dict[str, Any], now: str, verification: str = "LOCAL") -> int:
        cur = self.conn.execute(
            "INSERT INTO inference_events(request_id,kind,payload_json,verification,created_at) VALUES(?,?,?,?,?)",
            (request_id, kind, _json(payload), verification, now))
        return int(cur.lastrowid)

    def record(self, request_id: str, kind: str, payload: Dict[str, Any], now: str,
               state: Optional[str] = None) -> None:
        """Persist evidence/state atomically before the caller performs the associated side effect."""
        if state is not None and state not in ("OPEN_SUBMITTED", "OPEN", "RECEIPT_READY", "CLOSE_SUBMITTED", "CLOSED"):
            raise JournalError("terminal states require verified reconciliation or local pre-submit cancellation")
        with self._transaction():
            row = self.request(request_id)
            if row is None or row["state"] in ("SETTLED", "REFUNDED", "CANCELLED"):
                raise JournalError("request absent or already finalized")
            self._event(request_id, kind, payload, now)
            self.conn.execute("UPDATE inference_requests SET state=?,updated_at=? WHERE request_id=?",
                              (state or row["state"], now, request_id))

    def totals(self, day: str, asset: str = "FLOP", decimals: int = 18) -> Dict[str, int]:
        # Python integers are deliberate: SQL SUM/CAST would overflow SQLite's signed 64-bit integers.
        rows = self.conn.execute("SELECT * FROM inference_requests WHERE asset=? AND decimals=?", (asset, decimals)).fetchall()
        liability = sum(int(r["liability_base"]) for r in rows)
        today = [r for r in rows if r["day"] == day]
        return {
            "requests_today": len(today),
            "open_liability_base": liability,
            "daily_exposure_base": sum(int(r["liability_base"]) + int(r["finalized_spend_base"])
                                       + int(r["finalized_fees_base"]) for r in today),
            "finalized_spend_base": sum(int(r["finalized_spend_base"]) for r in rows),
            "finalized_fees_base": sum(int(r["finalized_fees_base"]) for r in rows),
        }

    def reserve(self, request_id: str, provider: str, network: str, account: str, escrow_base: int,
                fee_reserve_base: int, intent: dict, now: str, max_requests: int, max_daily_base: int,
                max_open_base: int, asset: str = "FLOP", decimals: int = 18) -> bool:
        """Return False for an identical existing intent. Never silently repeat its external work."""
        if not request_id or not provider or not network or not account or len(request_id) > 200:
            raise JournalError("request identity and attribution are required")
        amounts = (escrow_base, fee_reserve_base, max_daily_base, max_open_base)
        if any(type(v) is not int or v < 0 or v >= 1 << 128 for v in amounts) or escrow_base == 0:
            raise JournalError("invalid base-unit reservation or budget")
        intent_json = _json(intent)
        with self._transaction():
            existing = self.request(request_id)
            if existing is not None:
                binding = ("provider", "network", "account", "asset", "decimals", "escrow_base", "fee_reserve_base", "intent_json")
                wanted = (provider, network, account, asset, decimals, str(escrow_base), str(fee_reserve_base), intent_json)
                if tuple(existing[k] for k in binding) != wanted:
                    raise JournalError("request id reused with different intent or attribution")
                return False
            totals = self.totals(now[:10], asset, decimals)
            amount = escrow_base + fee_reserve_base
            if amount >= 1 << 128:
                raise JournalError("combined reservation overflows u128")
            if totals["requests_today"] >= max_requests:
                raise JournalError("daily session cap reached (all submitted/failed intents count)")
            if totals["daily_exposure_base"] + amount > max_daily_base:
                raise JournalError("daily FLOP exposure limit reached")
            if totals["open_liability_base"] + amount > max_open_base:
                raise JournalError("open FLOP exposure limit reached; reconcile outstanding requests")
            self.conn.execute(
                "INSERT INTO inference_requests(request_id,provider,asset,decimals,network,account,day,state,escrow_base,"
                "fee_reserve_base,liability_base,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'PREPARED',?,?,?,?,?,?)",
                (request_id, provider, asset, decimals, network, account, now[:10], str(escrow_base), str(fee_reserve_base),
                 str(amount), intent_json, now, now))
            self._event(request_id, "RESERVED", {"escrow_base": str(escrow_base), "fee_reserve_base": str(fee_reserve_base)}, now)
        return True

    def bind_channel(self, request_id: str, channel_id: str, now: str) -> None:
        with self._transaction():
            row = self.request(request_id)
            if row is None or row["state"] != "PREPARED" or row["channel_id"] is not None:
                raise JournalError("channel can only be bound once, before submission")
            self.conn.execute("UPDATE inference_requests SET channel_id=?,updated_at=? WHERE request_id=?", (channel_id, now, request_id))

    def fail(self, request_id: str, error: str, now: str, ambiguous: bool = True) -> None:
        """Failure is not proof of refund. Preserve the entire reserved liability."""
        state = "AMBIGUOUS" if ambiguous else "FAILED"
        with self._transaction():
            row = self.request(request_id)
            if row is None or row["state"] in ("SETTLED", "REFUNDED", "CANCELLED"):
                raise JournalError("cannot fail an absent/finalized request")
            self._event(request_id, state, {"error": error[:300]}, now)
            self.conn.execute("UPDATE inference_requests SET state=?,error=?,updated_at=? WHERE request_id=?",
                              (state, error[:300], now, request_id))

    def cancel_prepared(self, request_id: str, now: str) -> None:
        """Only an intent whose durable SUBMITTED barrier was never crossed can be cancelled locally."""
        with self._transaction():
            row = self.request(request_id)
            if row is None or row["state"] != "PREPARED":
                raise JournalError("submitted or uncertain requests require verified final evidence")
            self._event(request_id, "CANCELLED", {}, now)
            self.conn.execute("UPDATE inference_requests SET state='CANCELLED',liability_base='0',updated_at=? WHERE request_id=?",
                              (now, request_id))

    def reconcile(self, request_id: str, evidence: dict, now: str, verifier=None) -> bool:
        """Archive imports as UNVERIFIED. Only a trusted independent verifier may finalize exposure.

        `verifier.verify(evidence)` must return FinalizedSettlement, independently sourced and final.
        No default verifier and no boolean 'trust this receipt' flag exist.
        """
        if self.request(request_id) is None:
            raise JournalError("unknown request")
        event_id = self._event(request_id, "SETTLEMENT_EVIDENCE", evidence, now, "UNVERIFIED")
        if verifier is None:
            return False
        final = verifier.verify(evidence)
        if not isinstance(final, FinalizedSettlement):
            raise JournalError("verifier did not return independently finalized settlement evidence")
        with self._transaction():
            row = self.request(request_id)
            if not row["channel_id"] or row["network"] == "unknown" or row["account"] == "unknown":
                raise JournalError("settlement attribution is incomplete; resolve historical identity first")
            if (final.network, final.account, final.channel_id) != (row["network"], row["account"], row["channel_id"]):
                raise JournalError("settlement network/account/channel binding mismatch")
            if not all((final.transaction_id, final.finalized_block, final.source)):
                raise JournalError("settlement requires transaction, finalized block and verification source")
            values = (final.spent_base, final.refunded_base, final.fees_base)
            if any(type(v) is not int or v < 0 or v >= 1 << 128 for v in values):
                raise JournalError("invalid finalized amounts")
            if final.spent_base + final.refunded_base != int(row["escrow_base"]):
                raise JournalError("settlement amount does not resolve the reserved escrow")
            if final.outcome not in ("SETTLED", "REFUNDED"):
                raise JournalError("nonterminal settlement outcome")
            if final.outcome == "REFUNDED" and final.spent_base != 0:
                raise JournalError("refund evidence also claims spend")
            if final.outcome == "SETTLED":
                receipts = self.conn.execute(
                    "SELECT payload_json FROM inference_events WHERE request_id=? AND kind='RECEIPT_READY' ORDER BY id DESC LIMIT 1",
                    (request_id,)).fetchone()
                expected = json.loads(receipts["payload_json"]) if receipts else None
                if expected is None or final.final_root != expected["final_root"] or final.spent_base != int(expected["payable"]):
                    raise JournalError("settlement differs from the persisted receipt root/amount")
            if row["state"] in ("SETTLED", "REFUNDED"):
                if (row["state"], int(row["finalized_spend_base"]), int(row["finalized_fees_base"])) != (
                        final.outcome, final.spent_base, final.fees_base):
                    raise JournalError("conflicting finalized evidence")
            elif row["state"] in ("PREPARED", "CANCELLED"):
                raise JournalError("request was never submitted")
            self.conn.execute("UPDATE inference_events SET verification='VERIFIED' WHERE id=?", (event_id,))
            self._event(request_id, "FINALIZED", asdict(final), now, "VERIFIED")
            self.conn.execute(
                "UPDATE inference_requests SET state=?,liability_base='0',finalized_spend_base=?,finalized_fees_base=?,updated_at=? WHERE request_id=?",
                (final.outcome, str(final.spent_base), str(final.fees_base), now, request_id))
            self.conn.execute("UPDATE flop_sessions SET state=? WHERE channel_id=?", (final.outcome, final.channel_id))
        return True
