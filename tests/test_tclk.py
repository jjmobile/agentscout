import json
from datetime import datetime, timedelta, timezone

import pytest

from agentscout import tclk
from agentscout.commerce import Commerce
from agentscout.config import Settings
from agentscout.identity import Identity
from agentscout.publisher import Publisher
from conftest import DID_A, NOW

# Golden vectors generated with the reference implementation (@flop-labs/tclk, 2026-09-03):
# byte-identical wire lines and ids, or two conforming implementations are on different deals.
GOLD_OFFER_FIELDS = dict(
    type="offer", role="payer", lock="hash", amount="1000000", asset="FLOP", rails=["paper"],
    claimByMs=1788480000000, refundAfterMs=1788487200000, expiresMs=1788415200000,
    nonce="9f2c81d04c9e1f7a", job={"id": "t6f5ea80cde", "proto": "a2a"},
)
GOLD_PAYER = "did:key:z6MkwNoeDd24jWouuvbQkuCwf3a1o14ToqJiKezPcBQc3A7q"
GOLD_PAYEE = "did:key:z6MkucThNe5Cq2g6Ln1V54BAZSp8KNQXXgF5xEo3T1E4bKze"
GOLD_OFFER_ID = "0xa344113338ed8365855b0441c8061fd3200fdbe622a7660792b83421d469a356"
GOLD_CONTRACT = "0x95966ddf633b21d186d814fc9f2bd464644b48e4b51de16cf0f91885b89d6925"
GOLD_OFFER_LINE = ('tclk1 {"amount":"1000000","asset":"FLOP","claimByMs":1788480000000,'
                   '"expiresMs":1788415200000,"from":"did:key:z6MkwNoeDd24jWouuvbQkuCwf3a1o14ToqJiKezPcBQc3A7q",'
                   '"id":"0xa344113338ed8365855b0441c8061fd3200fdbe622a7660792b83421d469a356",'
                   '"job":{"id":"t6f5ea80cde","proto":"a2a"},"lock":"hash","nonce":"9f2c81d04c9e1f7a",'
                   '"rails":["paper"],"refundAfterMs":1788487200000,"role":"payer","type":"offer"}')


def gold_offer():
    return tclk.make_offer(GOLD_PAYER, "1000000", expires_ms=1788415200000,
                           claim_by_ms=1788480000000, refund_after_ms=1788487200000,
                           job_id="t6f5ea80cde", nonce="9f2c81d04c9e1f7a")


def gold_accept():
    offer = gold_offer()
    core = {"from": GOLD_PAYEE, "ref": offer["id"], "statement": "0x" + "ab" * 32,
            "nonce": "0011223344556677"}
    return dict({"type": "accept"}, **core, contract=tclk.contract_id(offer, core))


def test_golden_vectors_match_reference_library():
    offer = gold_offer()
    assert offer["id"] == GOLD_OFFER_ID
    assert tclk.encode_frame(offer) == GOLD_OFFER_LINE
    accept = gold_accept()
    assert accept["contract"] == GOLD_CONTRACT
    assert tclk.decode_frame(tclk.encode_frame(accept)) == accept


def test_decode_is_fail_closed():
    with pytest.raises(tclk.TclkError):
        tclk.decode_frame("tclk1 {\"type\":\"offer\"}")                    # missing fields
    with pytest.raises(tclk.TclkError):
        tclk.decode_frame(tclk.encode_frame(gold_offer()).replace('"amount":"1000000"', '"amount":"1000001"'))  # id no longer hashes
    bad = json.loads(tclk.encode_frame(gold_accept())[len(tclk.PREFIX):])
    bad["extra"] = 1
    with pytest.raises(tclk.TclkError):
        tclk.validate_frame(bad)                                           # unknown key rejects


def test_state_machine_happy_path_and_guards():
    offer = gold_offer()
    preimage, statement = tclk.generate_hash_lock()
    core = {"from": GOLD_PAYEE, "ref": offer["id"], "statement": statement, "nonce": "0011223344556677"}
    accept = dict({"type": "accept"}, **core, contract=tclk.contract_id(offer, core))
    t0 = offer["expiresMs"] - 1000
    state = tclk.open_contract(offer)
    state, ok, _ = tclk.apply_frame(state, accept, t0)
    assert ok and state["state"] == "accepted"
    # self-accept, wrong contract id, late accept all reject without state change
    self_acc = dict(accept, **{"from": GOLD_PAYER})
    assert tclk.apply_frame(tclk.open_contract(offer), self_acc, t0)[1] is False
    assert tclk.apply_frame(tclk.open_contract(offer), dict(accept, contract="0x" + "0" * 64), t0)[1] is False
    assert tclk.apply_frame(tclk.open_contract(offer), accept, offer["expiresMs"])[1] is False
    lock = tclk.make_frame("lock", GOLD_PAYER, state["contract"], rail="paper", ref=state["contract"])
    state, ok, _ = tclk.apply_frame(state, lock, t0)
    assert ok and state["state"] == "locked"
    # payee reveals the right secret before the refund window
    bad_reveal = tclk.make_frame("reveal", GOLD_PAYEE, state["contract"], secret="0x" + "11" * 32)
    assert tclk.apply_frame(state, bad_reveal, t0)[1] is False
    reveal = tclk.make_frame("reveal", GOLD_PAYEE, state["contract"], secret=preimage)
    assert tclk.apply_frame(state, reveal, offer["refundAfterMs"])[1] is False   # too late
    state, ok, _ = tclk.apply_frame(state, reveal, t0)
    assert ok and state["state"] == "claimed" and state["secret"] == preimage
    # terminal: nothing applies
    assert tclk.apply_frame(state, reveal, t0)[1] is False


def test_refund_path():
    offer = gold_offer()
    accept = gold_accept()
    t0 = offer["expiresMs"] - 1000
    state = tclk.open_contract(offer)
    state, _, _ = tclk.apply_frame(state, accept, t0)
    lock = tclk.make_frame("lock", GOLD_PAYER, state["contract"], rail="paper", ref=state["contract"])
    state, _, _ = tclk.apply_frame(state, lock, t0)
    refund = tclk.make_frame("refund", GOLD_PAYER, state["contract"])
    assert tclk.apply_frame(state, refund, offer["refundAfterMs"] - 1)[1] is False
    state, ok, _ = tclk.apply_frame(state, refund, offer["refundAfterMs"])
    assert ok and state["state"] == "refunded"


def test_paper_record_codec_and_note_path():
    ns, key = tclk.paper_note(GOLD_CONTRACT)
    assert ns == "tclk-paper-95" and key == "966ddf633b21d1"
    line = tclk.encode_paper_record("locked", "hash", "0x" + "ab" * 32, 1788487200000)
    assert tclk.decode_paper_record(line) == {"status": "locked", "lock": "hash",
                                              "statement": "0x" + "ab" * 32, "refundAfterMs": 1788487200000}
    assert tclk.decode_paper_record("tclkpaper1 claimed hash 0x" + "ab" * 32 + " 5") is None  # claimed needs secret
    assert tclk.decode_paper_record("junk") is None
    assert tclk.deal_room(GOLD_CONTRACT) == "mb-p-tclk-95966ddf633b21d1"


def T(minutes):
    return (NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_commerce(server, client, storage, tmp_path):
    s = Settings(watch_rooms=["lobby"], db_path=str(tmp_path / "t.db"), dry_run=False,
                 publish_enabled=True, identity_key_path=str(tmp_path / "id.key"),
                 tclk_enabled=True)
    ident, _ = Identity.load_or_create(s.identity_key_path)
    pub = Publisher(s, client, storage, ident)
    pub.owner_verified = True
    return s, ident, Commerce(s, client, storage, ident, pub)


def test_commerce_opens_one_offer_per_day_and_folds_a_valid_accept(server, settings, client, storage, tmp_path):
    s, ident, com = make_commerce(server, client, storage, tmp_path)
    at = NOW.replace(hour=7)
    com.tick(at)
    row = storage.tclk_active_deal()
    assert row is not None and row["state"] == "proposed"
    offer = json.loads(row["offer_json"])
    assert offer["from"] == ident.did and offer["rails"] == ["paper"] and offer["role"] == "payer"
    outbox = storage.outbox_has(s.tclk_offers_room, offer["id"])
    assert outbox is not None and tclk.decode_frame(outbox["text"])["id"] == offer["id"]
    com.tick(at + timedelta(minutes=5))                    # same day: still one deal
    assert storage.conn.execute("SELECT COUNT(*) FROM tclk_deals").fetchone()[0] == 1
    # a valid accept lands in the (ingested) offers room
    preimage, statement = tclk.generate_hash_lock()
    core = {"from": DID_A, "ref": offer["id"], "statement": statement, "nonce": "0011223344556677"}
    accept = dict({"type": "accept"}, **core, contract=tclk.contract_id(offer, core))
    storage.insert_messages(s.tclk_offers_room,
                            [(1, T(1), DID_A, DID_A, True, tclk.encode_frame(accept), "h1")], T(1))
    com.tick(at + timedelta(minutes=10))
    row = storage.tclk_active_deal()
    assert row["state"] == "accepted" and row["contract"] == accept["contract"]


def test_commerce_folds_stored_accept_with_seen_ms_bookkeeping(server, settings, client, storage, tmp_path):
    """Regression (2026-09-03): the stored accept carries a `_seen_ms` bookkeeping key that the
    fail-closed validator must never see, or the deal wedges at `accepted` forever."""
    s, ident, com = make_commerce(server, client, storage, tmp_path)
    at = NOW.replace(hour=7)
    com.tick(at)
    offer = json.loads(storage.tclk_active_deal()["offer_json"])
    preimage, statement = tclk.generate_hash_lock()
    core = {"from": DID_A, "ref": offer["id"], "statement": statement, "nonce": "0011223344556677"}
    accept = dict({"type": "accept"}, **core, contract=tclk.contract_id(offer, core))
    storage.insert_messages(s.tclk_offers_room,
                            [(1, T(1), DID_A, DID_A, True, tclk.encode_frame(accept), "h1")], T(1))
    com.tick(at + timedelta(minutes=10))
    assert storage.tclk_active_deal()["state"] == "accepted"
    state = com._fold(storage.tclk_active_deal())
    assert state is not None and state["state"] == "accepted" and state["contract"] == accept["contract"]


def test_commerce_rejects_forged_sender(server, settings, client, storage, tmp_path):
    s, ident, com = make_commerce(server, client, storage, tmp_path)
    at = NOW.replace(hour=7)
    com.tick(at)
    offer = json.loads(storage.tclk_active_deal()["offer_json"])
    preimage, statement = tclk.generate_hash_lock()
    core = {"from": DID_A, "ref": offer["id"], "statement": statement, "nonce": "0011223344556677"}
    accept = dict({"type": "accept"}, **core, contract=tclk.contract_id(offer, core))
    other = "did:key:z6Mks5fqt4qcsbLEMU15bbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    storage.insert_messages(s.tclk_offers_room,                       # frame says DID_A, transport says other
                            [(1, T(1), other, other, True, tclk.encode_frame(accept), "h1")], T(1))
    com.tick(at + timedelta(minutes=10))
    assert storage.tclk_active_deal()["state"] == "proposed"
