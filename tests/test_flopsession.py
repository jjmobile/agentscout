"""W5 end to end with fakes: chain double, miner double that signs F.3 leaves, real storage and codec."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from agentscout import flopchannel as fc
from agentscout import flopsession, inference
from agentscout.config import Settings
from agentscout.flopchain import BASE_UNITS_PER_FLOP, ChainUnavailable, ChannelOpened, NullChain
from agentscout.flopkeys import SessionKey
from agentscout.identity import Identity
from agentscout.summarizer import SmokeCheck
from test_flopkeys_chain import FakeSr25519

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
GENESIS = b"\x01" * 32
BACKEND = FakeSr25519()


def verify(pk, msg, sig):
    return BACKEND.verify(sig, msg, pk)


class FakeChain:
    def __init__(self, free=100 * BASE_UNITS_PER_FLOP, pub=b"\x0a" * 32):
        self.free = free
        self.pub = pub
        self.opened = []

    def genesis_hash(self):
        return GENESIS

    def account_id(self):
        return self.pub

    def free_balance(self):
        return self.free

    def open_channel(self, params):
        self.opened.append(params)
        cid = fc.channel_id_v1(GENESIS, self.pub, params.miner, params.nonce)
        return ChannelOpened(cid, "0xopen%d" % len(self.opened))


class FakeMiner:
    """Signs V3 leaves with its own fake-sr25519 session key; records acks; 'settles' on close."""

    def __init__(self, tamper_output=False, bad_sig=False, policy=b"\x66" * 32):
        self.pub, self.sec = BACKEND.pair_from_seed(b"\x42" * 32)
        self.miner = b"\x33" * 32
        self.policy = policy
        self.acks = []
        self.closed = None
        self.tamper_output = tamper_output
        self.bad_sig = bad_sig
        self.channel_policy = policy

    def hello(self):
        self.acks = []                                               # a new channel: acks are per channel
        return flopsession.MinerHello(self.miner, self.pub, b"\x44" * 32, b"\x55" * 32, self.policy, "Bf16", "Cooperative",
                                      {"max_latency_ms": 2000}, "fake-7b")

    def turn(self, channel_id, turn_index, prompt, agent_send_ms):
        output = '{"ok": true, "word": "ready"}'
        h_in = fc.blake2_256(prompt.encode())
        h_out = fc.blake2_256(output.encode())
        t = fc.Turn(fc.LeafVersion.V3, turn_index, h_in, h_out, 1000 + turn_index, self.channel_policy, fc.h_ids([1, 2], [3]),
                    b"\x77" * 32, agent_send_ms + 5, agent_send_ms + 50, 45, bytes(64))
        sig = BACKEND.sign((self.pub, self.sec), fc.leaf_hash(channel_id, t))
        if self.bad_sig:
            sig = bytes(64)
        t = dataclasses.replace(t, enclave_sig=sig)
        if self.tamper_output:
            output = output + " "
        return flopsession.TurnReply(t, output, 12, 7)

    def ack(self, channel_id, acked):
        fc.verify_ack(channel_id, acked, self._agent_key, verify)   # what the miner's record_ack does
        self.acks.append(acked)

    def close(self, channel_id, final_root, aggregate_gn, payable, receipt_sig, transcript):
        fc.verify_receipt(channel_id, final_root, aggregate_gn, payable, self._agent_key, receipt_sig, verify)
        cid, turns = fc.decode_transcript(transcript)
        assert cid == channel_id and len(turns) == len(self.acks)
        self.closed = (final_root, aggregate_gn, payable)
        return "0xsettle"


def _settings(tmp_path, **over):
    base = dict(watch_rooms=["lobby"], db_path=str(tmp_path / "t.db"), inference_provider="flop",
                flop_rpc_url="ws://fake", flop_miner_url="http://miner.fake", flop_escrow_flop=2.0,
                flop_session_key_path=str(tmp_path / "sk.json"))
    base.update(over)
    return Settings(**base)


def _session(tmp_path, storage, chain=None, miner=None, **over):
    s = _settings(tmp_path, **over)
    key = SessionKey.load_or_create(s.flop_session_key_path, NOW, backend=BACKEND)
    miner = miner or FakeMiner()
    miner._agent_key = key.public
    sess = flopsession.FlopSession(s, chain or FakeChain(), key, miner, storage, verify, now=lambda: NOW)
    return sess, miner, key


def test_session_happy_path_records_a_closed_channel(storage, tmp_path):
    sess, miner, key = _session(tmp_path, storage)
    res = sess.run(["summarise: hello"])
    assert res.output == '{"ok": true, "word": "ready"}' and res.turns == 1 and res.aggregate_gn == 1000
    assert res.payable == 2 * BASE_UNITS_PER_FLOP and res.tx_hash == "0xsettle" and (res.input_tokens, res.output_tokens) == (12, 7)
    assert miner.closed == (res.final_root, 1000, 2 * BASE_UNITS_PER_FLOP) and len(miner.acks) == 1
    params = sess.chain.opened[0]
    assert params.agent_key == key.public and params.enclave_key == miner.pub and params.nonce == 1 and params.escrow == 2 * BASE_UNITS_PER_FLOP
    row = storage.flop_session(res.channel_id.hex())
    assert row["state"] == "CLOSED" and row["turns"] == 1 and row["aggregate_gn"] == "1000" and row["settle_tx"] == "0xsettle"
    assert row["payable"] == str(2 * BASE_UNITS_PER_FLOP) and row["final_root"] == res.final_root.hex() and row["day"] == "2026-10-20"
    assert storage.flop_sessions_today("2026-10-20") == 1
    res2 = sess.run(["a", "b"])                                   # two turns, next nonce
    assert res2.turns == 2 and res2.aggregate_gn == 1000 + 1001 and sess.chain.opened[1].nonce == 2
    assert storage.flop_session(res2.channel_id.hex())["turns"] == 2


def test_session_rejects_tampered_output_and_bad_leaf_signature(storage, tmp_path):
    sess, miner, _ = _session(tmp_path, storage, miner=FakeMiner(tamper_output=True))
    with pytest.raises(flopsession.SessionError, match="h_out"):
        sess.run(["x"])
    cid = sess.chain.opened[0]
    row = storage.flop_sessions_recent(1)[0]
    assert row["state"] == "FAILED" and "h_out" in row["error"] and miner.acks == [] and miner.closed is None
    assert storage.flop_sessions_today("2026-10-20") == 0          # failed channels do not burn the daily cap
    sess2, miner2, _ = _session(tmp_path, storage, miner=FakeMiner(bad_sig=True))
    with pytest.raises(flopsession.SessionError, match="enclave signature"):
        sess2.run(["x"])
    assert storage.flop_sessions_recent(1)[0]["state"] == "FAILED" and cid.nonce == 1


def test_session_guards_balance_and_daily_cap(storage, tmp_path):
    sess, _, _ = _session(tmp_path, storage, chain=FakeChain(free=12 * BASE_UNITS_PER_FLOP))
    with pytest.raises(flopsession.SessionError, match="free balance"):
        sess.run(["x"])                                           # 12 < escrow 2 + headroom 11
    sess, _, _ = _session(tmp_path, storage, flop_max_sessions_per_day=1)
    sess.run(["x"])
    with pytest.raises(flopsession.SessionError, match="daily session cap"):
        sess.run(["x"])
    assert sess.chain.opened and len(sess.chain.opened) == 1


def test_session_policy_mismatch_is_fail_closed(storage, tmp_path):
    miner = FakeMiner()
    miner.channel_policy = b"\x99" * 32                            # leaves carry a policy other than the one opened with
    sess, _, _ = _session(tmp_path, storage, miner=miner)
    with pytest.raises(flopsession.SessionError, match="decode policy"):
        sess.run(["x"])


def test_provider_parse_round_trip_and_unavailable_reasons(storage, tmp_path):
    s = _settings(tmp_path)
    p = inference.make_provider(s, storage, None)
    assert isinstance(p, inference.FlopProvider) and p.endpoint() == "http://miner.fake"
    with pytest.raises(inference.InferenceUnavailable, match="no identity"):
        p.messages.parse(model="x", max_tokens=10, messages=[])
    ident, _ = Identity.load_or_create(str(tmp_path / "id.key"))
    p.bind_identity(ident)
    p._chain = NullChain()                                         # what make_chain returns without an RPC URL
    with pytest.raises(inference.InferenceUnavailable, match="no Flop RPC"):
        p.messages.parse(model="x", max_tokens=10, messages=[])
    sess, miner, _ = _session(tmp_path, storage)
    p.session = lambda: sess                                       # the wired session, chain and miner faked
    resp = p.messages.parse(model="x", max_tokens=64, system=[{"type": "text", "text": "You are terse."}],
                            messages=[{"role": "user", "content": "Reply with ok=true and word='ready'."}], output_format=SmokeCheck)
    assert isinstance(resp.parsed_output, SmokeCheck) and resp.parsed_output.ok and resp.parsed_output.word == "ready"
    assert resp.stop_reason == "end_turn" and resp.id.startswith("flop:") and resp.usage.input_tokens == 12
    prompt = inference._render_prompt([{"type": "text", "text": "sys"}], [{"role": "user", "content": "hi"}], SmokeCheck)
    assert prompt.startswith("sys\n\nuser: hi\n\nRespond with a single JSON object") and '"word"' in prompt
    assert inference._parse_output(SmokeCheck, '```json\n{"ok": false, "word": "x"}\n```').ok is False
    with pytest.raises(ValueError):
        inference._parse_output(SmokeCheck, "not json")


def test_provider_session_key_rotation_and_chain_unavailable(storage, tmp_path, monkeypatch):
    s = _settings(tmp_path, flop_rpc_url="ws://unreachable.fake")
    p = inference.make_provider(s, storage, None)
    ident, _ = Identity.load_or_create(str(tmp_path / "id.key"))
    p.bind_identity(ident)
    from agentscout import flopchain

    def boom(settings, identity):
        raise ChainUnavailable("cannot connect")

    monkeypatch.setattr(flopchain, "make_chain", boom)
    with pytest.raises(inference.InferenceUnavailable, match="cannot connect"):
        p.session()
    p._chain = FakeChain()
    monkeypatch.setattr("agentscout.flopkeys._backend", lambda: BACKEND)
    sess = p.session()
    assert isinstance(sess, flopsession.FlopSession) and sess.key.public == p._key.public
    first = p._key
    p._key = SessionKey(b"\x07" * 32, datetime.now(timezone.utc) - timedelta(days=20), backend=BACKEND)   # stale → reload/rotate
    sess = p.session()
    assert sess.key is not first and sess.key.age_days(datetime.now(timezone.utc)) < 1
