"""W5 session key (sr25519 seam with a fake backend) and chain seam (substrate-interface double)."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from agentscout import flopchain, flopkeys
from agentscout.flopchannel import channel_id_v1
from agentscout.identity import Identity

NOW = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)


class FakeSr25519:
    """Deterministic stand-in for py-sr25519-bindings: pub = blake2(seed), sig = blake2(pub ‖ msg)."""

    def pair_from_seed(self, seed):
        pub = hashlib.blake2b(b"pub" + seed, digest_size=32).digest()
        return pub, hashlib.blake2b(b"sec" + seed, digest_size=64).digest()

    def sign(self, pair, msg):
        pub, _ = pair
        return hashlib.blake2b(pub + msg, digest_size=64).digest()

    def verify(self, sig, msg, pub):
        return sig == hashlib.blake2b(pub + msg, digest_size=64).digest()


def test_session_key_create_load_rotate(tmp_path):
    be = FakeSr25519()
    path = str(tmp_path / "flop_session.key")
    k1 = flopkeys.SessionKey.load_or_create(path, NOW, max_age_days=9, backend=be)
    assert os.stat(path).st_mode & 0o777 == 0o600
    data = json.loads(open(path).read())
    assert set(data) == {"seed", "created_at"} and data["created_at"] == NOW.isoformat()
    sig = k1.sign(b"turn")
    assert len(k1.public) == 32 and len(sig) == 64 and k1.verify(b"turn", sig) and not k1.verify(b"other", sig)
    assert flopkeys.verify(k1.public, b"turn", sig, backend=be) and not flopkeys.verify(k1.public[:31], b"turn", sig, backend=be)
    k2 = flopkeys.SessionKey.load_or_create(path, NOW + timedelta(days=8), max_age_days=9, backend=be)
    assert k2.public == k1.public and k2.created_at == NOW
    k3 = flopkeys.SessionKey.load_or_create(path, NOW + timedelta(days=9), max_age_days=9, backend=be)
    assert k3.public != k1.public and k3.created_at == NOW + timedelta(days=9)
    assert os.path.exists(path + ".prev")
    assert k3.expires_at() == k3.created_at + timedelta(days=10)
    k4 = flopkeys.SessionKey.load_or_create(path, NOW + timedelta(days=30), max_age_days=99, backend=be)   # clamped under 10 d
    assert k4.public != k3.public


def test_session_key_unreadable_file_is_replaced(tmp_path):
    path = tmp_path / "k"
    path.write_text("garbage")
    k = flopkeys.SessionKey.load_or_create(str(path), NOW, backend=FakeSr25519())
    assert len(k.public) == 32 and (tmp_path / "k.prev").read_text() == "garbage"


def test_missing_binding_is_keys_unavailable(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "sr25519":
            raise ImportError("nope")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(flopkeys.KeysUnavailable):
        flopkeys.SessionKey(bytes(32), NOW)


# ── chain seam ────────────────────────────────────────────────────────────────────────────────────

GENESIS = bytes(range(32))


class FakeKeypair:
    def __init__(self, pub):
        self.public_key = pub
        self.ss58_address = "5Fake" + pub.hex()[:8]


class FakeReceipt:
    def __init__(self, ok, events=None, err=None):
        self.is_success = ok
        self.triggered_events = events or []
        self.error_message = err
        self.extrinsic_hash = "0xabc"
        self.block_hash = "0xdef"


class FakeIface:
    chain = "flop-volta"

    def __init__(self, receipt, free=5 * 10 ** 18):
        self.receipt = receipt
        self.calls = []
        self.free = free

    def get_block_hash(self, n):
        assert n == 0
        return "0x" + GENESIS.hex()

    def query(self, module, storage, params):
        self.calls.append(("query", module, storage, params))
        if (module, storage) == ("System", "Account"):
            return type("R", (), {"value": {"data": {"free": self.free}}})()
        return type("R", (), {"value": None})()

    def compose_call(self, call_module, call_function, call_params):
        self.calls.append(("compose", call_module, call_function, call_params))
        return ("call", call_module, call_function)

    def create_signed_extrinsic(self, call, keypair):
        self.calls.append(("sign", call, keypair.ss58_address))
        return ("xt", call)

    def submit_extrinsic(self, xt, wait_for_inclusion):
        self.calls.append(("submit", xt, wait_for_inclusion))
        return self.receipt


def _params(agent_key=b"\x22" * 32, nonce=7):
    return flopchain.OpenChannelParams(
        miner=b"\x33" * 32, model_hash=b"\x44" * 32, measured_root=b"\x55" * 32, decode_policy_hash=b"\x66" * 32,
        precision="Bf16", enclave_key=b"\x77" * 32, agent_key=agent_key, sla={"max_latency_ms": 2000},
        escrow=2 * 10 ** 18, nonce=nonce, settlement_class="Cooperative")


def test_null_chain_is_loud():
    chain = flopchain.NullChain()
    for fn in (chain.genesis_hash, chain.account_id, chain.free_balance):
        with pytest.raises(flopchain.ChainUnavailable):
            fn()
    with pytest.raises(flopchain.ChainUnavailable):
        chain.open_channel(_params())


def test_open_channel_composes_and_cross_checks_event():
    pub = b"\x11" * 32
    expected = channel_id_v1(GENESIS, pub, b"\x33" * 32, 7)
    ev = {"module_id": "ComputeChannel", "event_id": "ChannelOpened", "attributes": {"channel_id": "0x" + expected.hex()}}
    iface = FakeIface(FakeReceipt(True, [ev]))
    chain = flopchain.SubstrateChain(iface, FakeKeypair(pub))
    assert chain.genesis_hash() == GENESIS and chain.account_id() == pub and chain.free_balance() == 5 * 10 ** 18
    opened = chain.open_channel(_params())
    assert opened.channel_id == expected and opened.tx_hash == "0xabc" and opened.block_hash == "0xdef"
    compose = next(c for c in iface.calls if c[0] == "compose")
    assert compose[1:3] == ("ComputeChannel", "open_channel")
    p = compose[3]
    assert list(p) == ["miner", "model_hash", "measured_root", "decode_policy_hash", "precision", "enclave_key", "agent_key", "sla", "escrow", "nonce", "settlement_class"]
    assert p["miner"] == "0x" + "33" * 32 and p["escrow"] == 2 * 10 ** 18 and p["nonce"] == 7 and p["sla"] == {"max_latency_ms": 2000}
    assert ("submit", ("xt", ("call", "ComputeChannel", "open_channel")), True) in iface.calls


def test_open_channel_event_mismatch_and_failures():
    pub = b"\x11" * 32
    bad_ev = {"module_id": "ComputeChannel", "event_id": "ChannelOpened", "attributes": {"channel_id": "0x" + "ee" * 32}}
    with pytest.raises(flopchain.ChainError, match="F.1 mismatch"):
        flopchain.SubstrateChain(FakeIface(FakeReceipt(True, [bad_ev])), FakeKeypair(pub)).open_channel(_params())
    with pytest.raises(flopchain.ChainError, match="InsufficientBalance"):
        flopchain.SubstrateChain(FakeIface(FakeReceipt(False, err={"name": "InsufficientBalance"})), FakeKeypair(pub)).open_channel(_params())
    no_event = flopchain.SubstrateChain(FakeIface(FakeReceipt(True, [])), FakeKeypair(pub)).open_channel(_params(nonce=8))
    assert no_event.channel_id == channel_id_v1(GENESIS, pub, b"\x33" * 32, 8)
    with pytest.raises(ValueError):
        _params(agent_key=b"\x22" * 31)
    with pytest.raises(ValueError):
        flopchain.OpenChannelParams(**{**_params().__dict__, "escrow": 0})


def test_make_chain_null_without_url_and_identity_seed_round_trip(tmp_path):
    ident, _ = Identity.load_or_create(str(tmp_path / "id.key"))
    assert len(ident.public_bytes()) == 32 and len(ident.private_seed()) == 32

    class S:
        flop_rpc_url = ""

    assert isinstance(flopchain.make_chain(S(), ident), flopchain.NullChain)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    assert Identity(Ed25519PrivateKey.from_private_bytes(ident.private_seed())).did == ident.did
