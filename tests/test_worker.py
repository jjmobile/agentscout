import json
from datetime import timedelta

from agentscout import jobs, tclk
from agentscout.config import Settings
from agentscout.identity import Identity
from agentscout.publisher import Publisher
from agentscout.worker import SEQ_SETTING, Worker
from conftest import NOW, msg, room_json

PAYER = "did:key:z6MkmVY6AzA68rUv1X5bEr2f77pGLryppbKEEPFsCWeEboBD"
SPEC = ("inference | From the note the table at the end of this note (rows: seq | payer | amount | asset | proto | time): "
        "output the seq values that are even numbers, in ascending order, comma-separated (or 'none'). | reward tier 3/5 | "
        "done looks like: one line: comma-separated seq values or 'none'. | deliver as one signed message in the deal room, then reveal. | "
        "MATERIAL: seq | payer | amount | asset | proto | time 11 | a | 1 | FLOP | a2a | 01:00:00 12 | b | 2 | FLOP | a2a | 02:00:00 14 | c | 3 | FLOP | a2a | 03:00:00")


def ms(dt):
    return int(dt.timestamp() * 1000)


def make_worker(client, storage, tmp_path, **overrides):
    s = Settings(watch_rooms=["lobby"], db_path=str(tmp_path / "w.db"), dry_run=False, publish_enabled=True,
                 identity_key_path=str(tmp_path / "id.key"), worker_enabled=True, **overrides)
    ident, _ = Identity.load_or_create(s.identity_key_path)
    pub = Publisher(s, client, storage, ident)
    pub.owner_verified = True
    return s, ident, pub, Worker(s, client, storage, ident, pub, lambda: None)


def board_offer(context, minutes_open=20, nonce="00000000000000aa"):
    return tclk.make_offer(PAYER, "200", expires_ms=ms(NOW + timedelta(minutes=minutes_open)),
                           claim_by_ms=ms(NOW + timedelta(minutes=minutes_open + 15)),
                           refund_after_ms=ms(NOW + timedelta(minutes=minutes_open + 30)),
                           job_id="task-1", nonce=nonce) | {}


def put_offer(storage, seq, offer, context):
    offer = dict(offer)
    offer["job"] = {"id": "task-1", "proto": "a2a", "context": context}
    offer["id"] = tclk.offer_id({k: v for k, v in offer.items() if k != "id"})
    text = tclk.encode_frame(offer)
    storage.insert_messages("tclk-offers", [(seq, NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), PAYER, PAYER, True, text, f"h{seq}")], NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return offer


def test_first_tick_only_records_the_board_position(server, client, storage, tmp_path):
    s, ident, pub, w = make_worker(client, storage, tmp_path)
    put_offer(storage, 5, board_offer("x"), "inference | q | full spec: /kv/tclk-job-en/x")
    assert w.tick(NOW) is False
    assert storage.get_setting(SEQ_SETTING) == "5" and storage.worker_open() == []


def test_accept_deliver_reveal_and_claim(server, client, storage, tmp_path):
    s, ident, pub, w = make_worker(client, storage, tmp_path)
    storage.set_setting(SEQ_SETTING, "0")
    server.route("/kv/tclk-job-en/inf-1", 200, SPEC)
    offer = put_offer(storage, 7, board_offer("x"), "inference | From the note … | full spec: /kv/tclk-job-en/inf-1")
    assert w.tick(NOW) is True
    deals = storage.worker_open()
    assert len(deals) == 1 and deals[0]["state"] == "accepted" and deals[0]["answer"] == "12, 14"
    contract = deals[0]["contract"]
    accept = tclk.decode_frame(storage.outbox_has("tclk-offers", f"wk-accept-{contract[:18]}")["text"])
    assert accept["ref"] == offer["id"] and accept["from"] == ident.did
    assert tclk.secret_opens(accept["statement"], deals[0]["secret"])
    room = tclk.deal_room(contract)
    hb = tclk.decode_frame(storage.outbox_has(room, f"wk-hb-{contract[:18]}")["text"])
    assert hb["type"] == "heartbeat" and hb["contract"] == contract
    # the same offer is never accepted twice, and nothing happens while the payer has not locked
    assert w.tick(NOW + timedelta(seconds=30)) is False
    assert len(storage.worker_open()) == 1
    # payer locks in the deal room → we deliver the answer line and reveal the secret
    lock = tclk.make_frame("lock", PAYER, contract, rail="paper", ref="tx-1")
    server.route(f"/r/{room}?format=json&limit=100", 200, room_json(room, [msg(1, "t", PAYER, tclk.encode_frame(lock))]))
    later = NOW + timedelta(minutes=1)
    assert w.tick(later) is True
    d = storage.worker_open()[0]
    assert d["state"] == "revealed" and d["lock_ref"] == "tx-1"
    assert storage.outbox_has(room, f"wk-deliver-{contract[:18]}")["text"] == "12, 14"
    reveal = tclk.decode_frame(storage.outbox_has(room, f"wk-reveal-{contract[:18]}")["text"])
    assert reveal["secret"] == d["secret"] and "ref" not in reveal
    # the payer's receipt + review line close the deal with its grade
    receipt = tclk.make_frame("receipt", PAYER, contract, outcome="claimed", rail="paper", ref="tx-1")
    server.route(f"/r/{room}?format=json&limit=100", 200, room_json(room, [
        msg(1, "t", PAYER, tclk.encode_frame(lock)), msg(2, "t", ident.did, "12, 14"),
        msg(3, "t", PAYER, tclk.encode_frame(receipt)),
        msg(4, "t", PAYER, f"review 0xabc contract {contract[:18]} payee x PASS 1 — exact match")]))
    assert w.tick(later + timedelta(minutes=1)) is False
    assert storage.worker_open() == []
    assert storage.worker_stats() == {"claimed": 1}
    row = storage.conn.execute("SELECT grade FROM worker_deals WHERE contract=?", (contract,)).fetchone()
    assert row["grade"] == "PASS"


def test_unsolvable_expiring_and_capped_offers_are_left_alone(server, client, storage, tmp_path):
    s, ident, pub, w = make_worker(client, storage, tmp_path, worker_max_open=1)
    storage.set_setting(SEQ_SETTING, "0")
    server.route("/kv/tclk-job-en/inf-1", 200, SPEC)
    server.route("/kv/tclk-job-en/poem", 200, SPEC.replace("output the seq values that are even numbers, in ascending order, comma-separated (or 'none').", "write a poem about the rows."))
    put_offer(storage, 1, board_offer("a", nonce="00000000000000a1"), "inference | … | full spec: /kv/tclk-job-en/poem")          # not a template we compute
    put_offer(storage, 2, board_offer("b", minutes_open=2, nonce="00000000000000a2"), "inference | … | full spec: /kv/tclk-job-en/inf-1")  # expires too soon
    put_offer(storage, 3, board_offer("c", nonce="00000000000000a3"), "review | From SPEC.md: quote the 8 frame types | full spec: /kv/tclk-job-en/inf-1")  # unsupported family
    put_offer(storage, 4, board_offer("d", nonce="00000000000000a4"), "inference | … | full spec: /kv/tclk-job-en/inf-1")           # accepted
    put_offer(storage, 5, board_offer("e", nonce="00000000000000a5"), "inference | … | full spec: /kv/tclk-job-en/inf-1")           # over max_open / same payer today
    assert w.tick(NOW) is True
    deals = storage.worker_open()
    assert len(deals) == 1 and json.loads(deals[0]["offer_json"])["nonce"] == "00000000000000a4"
    assert storage.get_setting(SEQ_SETTING) == "5"


def test_attest_job_posts_the_line_first_then_delivers_its_seq(server, client, storage, tmp_path):
    s, ident, pub, w = make_worker(client, storage, tmp_path)
    storage.set_setting(SEQ_SETTING, "0")
    server.route("/kv/tclk-job-en/att-1", 200, "attest | [difficulty 1/3] Post exactly one signed line in this deal's derived room from the did:key that accepted: `tclk-attest <full contract id>`. | reward tier 1/5 | done looks like: one line: attested seq <seq>")
    put_offer(storage, 9, board_offer("x"), "attest | [difficulty 1/3] Post exactly one signed line … `tclk-attest <full contract id>` | full spec: /kv/tclk-job-en/att-1")
    assert w.tick(NOW) is True
    d = storage.worker_open()[0]
    assert d["answer"] == jobs.ATTEST_ANSWER
    contract, room = d["contract"], tclk.deal_room(d["contract"])
    lock = tclk.make_frame("lock", PAYER, contract, rail="paper", ref="tx-9")
    server.route(f"/r/{room}?format=json&limit=100", 200, room_json(room, [msg(1, "t", PAYER, tclk.encode_frame(lock))]))
    assert w.tick(NOW + timedelta(minutes=1)) is True
    att = storage.outbox_has(room, f"wk-attest-{contract[:18]}")
    assert att["text"] == f"tclk-attest {contract}" and storage.worker_open()[0]["state"] == "locked"
    assert storage.outbox_has(room, f"wk-deliver-{contract[:18]}") is None      # waits for the line's seq
    storage.outbox_update(att["id"], "POSTED", "t", posted_seq=77)
    assert w.tick(NOW + timedelta(minutes=2)) is True
    assert storage.outbox_has(room, f"wk-deliver-{contract[:18]}")["text"] == "attested seq 77"
    assert storage.worker_open()[0]["state"] == "revealed"


def test_lapses_when_the_payer_never_locks(server, client, storage, tmp_path):
    s, ident, pub, w = make_worker(client, storage, tmp_path)
    storage.set_setting(SEQ_SETTING, "0")
    server.route("/kv/tclk-job-en/inf-1", 200, SPEC)
    put_offer(storage, 7, board_offer("x"), "inference | … | full spec: /kv/tclk-job-en/inf-1")
    assert w.tick(NOW) is True
    assert w.tick(NOW + timedelta(minutes=40)) is False
    assert storage.worker_open() == [] and storage.worker_stats() == {"lapsed": 1}


def test_parked_heartbeat_is_retried_while_the_deal_is_alive(server, client, storage, tmp_path):
    s, ident, pub, w = make_worker(client, storage, tmp_path)
    storage.set_setting(SEQ_SETTING, "0")
    server.route("/kv/tclk-job-en/inf-1", 200, SPEC)
    put_offer(storage, 7, board_offer("x"), "inference | … | full spec: /kv/tclk-job-en/inf-1")
    assert w.tick(NOW) is True
    contract = storage.worker_open()[0]["contract"]
    room = tclk.deal_room(contract)
    hb = storage.outbox_has(room, f"wk-hb-{contract[:18]}")
    storage.outbox_update(hb["id"], "WAITING_ROOM", NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), error="400 room limit reached")
    w.tick(NOW + timedelta(minutes=1))
    assert storage.outbox_has(room, f"wk-hb-{contract[:18]}")["state"] == "WAITING_ROOM"     # too early
    w.tick(NOW + timedelta(minutes=4))
    assert storage.outbox_has(room, f"wk-hb-{contract[:18]}")["state"] == "PENDING"          # retried


def test_ledger_counts_both_sides_of_the_paper_rail(storage):
    from agentscout import render
    assert render.ledger_line(storage) == ""                     # nothing to account for yet
    offer = json.dumps({"amount": "400", "asset": "FLOP"})
    storage.worker_insert("0x" + "11" * 32, "2026-09-08", "0xo1", "did:key:z6Mkpayer1", offer, "{}", "0x" + "aa" * 32, "inference", "1, 2", "2026-09-08T10:00:00Z")
    storage.worker_set_state("0x" + "11" * 32, "claimed", "2026-09-08T10:30:00Z", grade="PASS")
    storage.worker_insert("0x" + "22" * 32, "2026-09-08", "0xo2", "did:key:z6Mkpayer2", offer, "{}", "0x" + "bb" * 32, "census", "x", "2026-09-08T11:00:00Z")
    storage.worker_set_state("0x" + "22" * 32, "lapsed", "2026-09-08T11:40:00Z")
    storage.worker_insert("0x" + "33" * 32, "2026-09-09", "0xo3", "did:key:z6Mkpayer1", json.dumps({"amount": "200", "asset": "FLOP"}), "{}", "0x" + "cc" * 32, "verification", "3", "2026-09-09T01:00:00Z")
    storage.worker_set_state("0x" + "33" * 32, "claimed", "2026-09-09T01:10:00Z", grade="FAIL")
    storage.tclk_upsert("2026-09-07", "0xoffer", json.dumps({"amount": "1000000", "asset": "FLOP"}), "refunded", "2026-09-07T07:00:00Z")
    storage.tclk_upsert("2026-09-08", "0xoffer2", json.dumps({"amount": "1000000", "asset": "FLOP"}), "claimed", "2026-09-08T07:00:00Z")
    L = storage.ledger()
    assert L["earned"] == {"FLOP": 600} and L["spent"] == {"FLOP": 1000000} and L["counterparties"] == 1
    line = render.ledger_line(storage)
    assert line.startswith("💼 Ledger (paper rail, settles nothing): earned 600 FLOP over 2 claimed deals as worker")
    assert "1 lapsed" in line and "PASS 1 / FAIL 1" in line and "spent 1,000,000 FLOP over 1 claimed deals as payer (1 refunded" in line
    note = render.ledger_note("agentscout", storage, NOW)
    assert note.startswith("agentscout ledger asof=") and "earned=600 FLOP" in note and "since=2026-09-08" in note and "graded_fail=1" in note
