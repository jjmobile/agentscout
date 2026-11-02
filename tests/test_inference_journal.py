"""Offline failure/restart/finality tests. A fake verifier is explicit; no live chain is contacted."""
import dataclasses
import json
import sqlite3
from datetime import timedelta

import pytest

from agentscout import flopsession
from agentscout.inference_journal import FinalizedSettlement, InferenceJournal, JournalError, base_units
from agentscout.storage import Storage
from test_flopsession import FakeChain, FakeMiner, NOW, _session

STAMP = "2026-10-20T12:00:00.000Z"


def reserve(journal, request_id="r", **over):
    args = dict(request_id=request_id, provider="official-adapter-not-yet-built", network="network", account="account",
                escrow_base=base_units("1"), fee_reserve_base=base_units("0.1"), intent={"input_hash": "abc"},
                now=STAMP, max_requests=5, max_daily_base=base_units("3"), max_open_base=base_units("3"))
    args.update(over)
    return journal.reserve(**args)


def test_exact_base_units_and_invalid_money():
    assert base_units("0.100000000000000001") == 100000000000000001
    assert base_units("12345678901234567890.123456789012345678") == 12345678901234567890123456789012345678
    for value in ("NaN", "Infinity", "-1", "0.0000000000000000001"):
        with pytest.raises(JournalError):
            base_units(value)


def test_reservations_are_idempotent_and_exposure_survives_restart(storage, tmp_path):
    journal = InferenceJournal(storage)
    assert reserve(journal) is True and reserve(journal) is False
    with pytest.raises(JournalError, match="different intent"):
        reserve(journal, intent={"input_hash": "changed"})
    journal.bind_channel("r", "channel", STAMP)
    journal.record("r", "OPEN_INTENT", {}, STAMP, "OPEN_SUBMITTED")
    journal.fail("r", "lost response", STAMP)
    reopened = Storage(str(tmp_path / "t.db"))
    try:
        recovered = InferenceJournal(reopened)
        assert recovered.request("r")["state"] == "AMBIGUOUS"
        assert recovered.totals(STAMP[:10])["open_liability_base"] == base_units("1.1")
        with pytest.raises(JournalError, match="open FLOP exposure"):
            reserve(recovered, "next-day", now="2026-10-21T00:00:00Z", max_open_base=base_units("2"))
        with pytest.raises(JournalError, match="daily FLOP exposure"):
            reserve(recovered, "more-work", max_daily_base=base_units("2"))
        with pytest.raises(JournalError, match="require verified"):
            recovered.cancel_prepared("r", STAMP)
    finally:
        reopened.close()


def test_local_cancellation_is_only_before_submission(storage):
    journal = InferenceJournal(storage)
    reserve(journal)
    journal.cancel_prepared("r", STAMP)
    assert journal.request("r")["state"] == "CANCELLED"
    assert journal.totals(STAMP[:10])["open_liability_base"] == 0
    assert reserve(journal) is False     # cancelled IDs are also never replayed
    with pytest.raises(JournalError, match="terminal states"):
        journal.record("r", "pretend", {}, STAMP, "SETTLED")


def test_failed_session_cannot_bypass_count_cap(storage, tmp_path):
    chain = FakeChain()
    session, _, _ = _session(tmp_path, storage, chain=chain, miner=FakeMiner(tamper_output=True), flop_max_sessions_per_day=1)
    with pytest.raises(flopsession.SessionError, match="h_out"):
        session.run(["first task"])
    with pytest.raises(flopsession.SessionError, match="daily session cap"):
        session.run(["second task"])
    assert len(chain.opened) == 1 and storage.flop_sessions_today(STAMP[:10]) == 1
    assert session.journal.totals(STAMP[:10])["open_liability_base"] == base_units("2.1")


def test_submission_timeout_is_durable_and_never_automatically_retried(storage, tmp_path):
    class LostOpen(FakeChain):
        def open_channel(self, params):
            # Intent and liability must exist BEFORE the possibly-successful external submission.
            row = storage.conn.execute("SELECT * FROM inference_requests").fetchone()
            assert row["state"] == "OPEN_SUBMITTED" and int(row["liability_base"]) == base_units("2.1")
            super().open_channel(params)
            raise TimeoutError("node response lost after inclusion")

    chain = LostOpen()
    session, _, _ = _session(tmp_path, storage, chain=chain)
    with pytest.raises(flopsession.SessionError, match="response lost"):
        session.run(["useful request"], request_id="business-request-1")
    reopened = Storage(str(tmp_path / "t.db"))
    try:
        restarted, _, _ = _session(tmp_path, reopened, chain=chain)
        with pytest.raises(flopsession.SessionError, match="already recorded"):
            restarted.run(["useful request"], request_id="business-request-1")
        assert len(chain.opened) == 1
        assert restarted.journal.request("business-request-1")["state"] == "AMBIGUOUS"
    finally:
        reopened.close()


def test_ack_and_receipt_persist_before_send_and_close_timeout_retains_liability(storage, tmp_path):
    class LostClose(FakeMiner):
        def ack(self, channel_id, acked):
            event = storage.conn.execute("SELECT * FROM inference_events WHERE kind='ACK_INTENT'").fetchone()
            assert event and json.loads(event["payload_json"])["transcript"]
            super().ack(channel_id, acked)

        def close(self, channel_id, final_root, aggregate_gn, payable, receipt_sig, transcript):
            event = storage.conn.execute("SELECT * FROM inference_events WHERE kind='RECEIPT_READY'").fetchone()
            payload = json.loads(event["payload_json"])
            assert payload["receipt_sig"] == receipt_sig.hex() and payload["transcript"] == transcript.hex()
            assert storage.flop_session(channel_id.hex())["receipt_sig"] == receipt_sig.hex()
            super().close(channel_id, final_root, aggregate_gn, payable, receipt_sig, transcript)
            raise TimeoutError("close response lost")

    session, _, _ = _session(tmp_path, storage, miner=LostClose())
    with pytest.raises(flopsession.SessionError, match="close response lost"):
        session.run(["work"], request_id="close-lost")
    row = session.journal.request("close-lost")
    assert row["state"] == "AMBIGUOUS" and int(row["liability_base"]) == base_units("2.1")
    assert storage.flop_session(row["channel_id"])["receipt_sig"]


class IndependentVerifier:
    """Represents a future trusted adapter querying canonical finality, NOT trusting the supplied receipt."""
    def __init__(self, result):
        self.result = result

    def verify(self, untrusted_import):
        return self.result


def final_for(session, result, **over):
    row = session.journal.request(result.request_id)
    proof = FinalizedSettlement(row["network"], row["account"], row["channel_id"], "SETTLED", result.payable, 0,
                                base_units("0.01"), "canonical-tx", "canonical-final-block", "fake-independent-node", result.final_root.hex())
    return dataclasses.replace(proof, **over)


@pytest.mark.parametrize("over", [{"network": "wrong"}, {"account": "wrong"}, {"channel_id": "wrong"},
                                  {"spent_base": 1}, {"final_root": "wrong"}, {"finalized_block": ""},
                                  {"outcome": "PENDING"}])
def test_reconciliation_rejects_unbound_or_nonfinal_evidence(storage, tmp_path, over):
    session, _, _ = _session(tmp_path, storage)
    result = session.run(["useful task"])
    proof = final_for(session, result, **over)
    with pytest.raises(JournalError):
        session.journal.reconcile(result.request_id, {"miner_says": "settled"}, STAMP, IndependentVerifier(proof))
    row = session.journal.request(result.request_id)
    assert row["state"] == "CLOSED" and int(row["liability_base"]) == base_units("2.1")
    assert all(e["verification"] != "VERIFIED" for e in session.journal.events(result.request_id))


def test_manual_evidence_stays_unverified_and_only_independent_proof_releases_exposure(storage, tmp_path):
    session, _, _ = _session(tmp_path, storage)
    result = session.run(["useful task"])
    forged = {"state": "SETTLED", "finalized": True, "tx_hash": "0xanything"}
    assert session.journal.reconcile(result.request_id, forged, STAMP) is False
    assert int(session.journal.request(result.request_id)["liability_base"]) == base_units("2.1")
    assert session.journal.totals(STAMP[:10])["finalized_spend_base"] == 0
    assert session.journal.reconcile(result.request_id, forged, STAMP, IndependentVerifier(final_for(session, result)))
    row = session.journal.request(result.request_id)
    assert row["state"] == "SETTLED" and row["liability_base"] == "0"
    assert int(row["finalized_spend_base"]) == base_units("2")
    assert session.journal.totals(STAMP[:10])["daily_exposure_base"] == base_units("2.01")
    assert storage.flop_session(result.channel_id.hex())["state"] == "SETTLED"
    assert "credited" not in row.keys()        # finalized spend is not an allocation


def test_verified_refund_releases_escrow_but_counts_actual_fees(storage, tmp_path):
    session, _, _ = _session(tmp_path, storage, miner=FakeMiner(tamper_output=True))
    with pytest.raises(flopsession.SessionError):
        session.run(["bad response"], request_id="refund-me")
    row = session.journal.request("refund-me")
    proof = FinalizedSettlement(row["network"], row["account"], row["channel_id"], "REFUNDED", 0,
                                base_units("2"), base_units("0.2"), "refund-tx", "final-block", "fake-node")
    assert session.journal.reconcile("refund-me", {}, STAMP, IndependentVerifier(proof))
    assert session.journal.totals(STAMP[:10])["daily_exposure_base"] == base_units("0.2")  # actual fee overrun is retained
    assert session.journal.totals(STAMP[:10])["open_liability_base"] == 0


def test_implicit_request_id_dedupes_across_midnight(storage, tmp_path):
    session, _, _ = _session(tmp_path, storage)
    session.run(["same logical work"])
    session._now = lambda: NOW + timedelta(days=1)
    with pytest.raises(flopsession.SessionError, match="already recorded"):
        session.run(["same logical work"])
    assert len(session.chain.opened) == 1


def test_migration_preserves_all_legacy_liabilities_and_is_atomic(tmp_path, monkeypatch):
    from agentscout import storage as storage_module
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
    for index, migration in enumerate(storage_module.MIGRATIONS[:10], start=1):
        conn.executescript(migration)
        conn.execute("INSERT INTO schema_version VALUES(?)", (index,))
    for state in ("OPEN", "CLOSED", "FAILED", "SETTLED"):
        conn.execute("INSERT INTO flop_sessions(channel_id,day,miner,escrow_base,nonce,agent_key,state,opened_at) VALUES(?,?,?,?,?,?,?,?)",
                     (state, STAMP[:10], "miner", str(base_units("1")), 1, "key", state, STAMP))
    conn.close()
    migrated = Storage(path)
    journal = InferenceJournal(migrated)
    assert journal.totals(STAMP[:10])["open_liability_base"] == base_units("4")
    assert journal.totals(STAMP[:10])["finalized_spend_base"] == 0
    assert {r[0] for r in migrated.conn.execute("SELECT state FROM inference_requests")} == {"LEGACY_UNVERIFIED"}
    version = migrated.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    monkeypatch.setattr(storage_module, "MIGRATIONS", storage_module.MIGRATIONS + ["CREATE TABLE should_rollback(x); INSERT INTO absent_table VALUES(1);"])
    with pytest.raises(sqlite3.OperationalError):
        migrated._migrate()
    assert migrated.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == version
    assert migrated.conn.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
    migrated.close()
