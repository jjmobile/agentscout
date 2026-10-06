"""Appendix F wire format against the public vector corpus (profile flop-wire-v1, status public-canonical)."""
import dataclasses
import json
from pathlib import Path

import pytest

from agentscout import flopchannel as fc

CORPUS = json.loads((Path(__file__).parent / "data" / "flop-wire-format-v1.json").read_text())
CC = CORPUS["compute_channel_v1"]
H = bytes.fromhex


def _leaf_inputs():
    li = CC["leaf_inputs"]
    return dict(
        channel_id=H(li["channel_id_hex"]), turn_index=li["turn_index"], h_in=H(li["h_in_hex"]), h_out=H(li["h_out_hex"]),
        g_n=int(li["g_n"]), policy=H(li["decode_policy_hash_hex"]), ids=H(li["h_ids_hex"]), toploc=H(li["toploc_commitment_hash_hex"]),
        recv=int(li["miner_recv_ms"]), done=int(li["miner_done_ms"]), lat=li["latency_ms"],
    )


def _turn(version, sig=bytes(64), **over):
    li = _leaf_inputs()
    li.update(over)
    v = fc.LeafVersion(version)
    return fc.Turn(
        v, li["turn_index"], li["h_in"], li["h_out"], li["g_n"],
        li["policy"] if v >= fc.LeafVersion.V2 else None,
        li["ids"] if v == fc.LeafVersion.V3 else None,
        li["toploc"] if v == fc.LeafVersion.V3 else None,
        li["recv"], li["done"], li["lat"], sig,
    )


def _sr25519():
    try:
        import sr25519  # py-sr25519-bindings
    except ImportError:
        return None
    return lambda pk, msg, sig: bool(sr25519.verify(sig, msg, pk))


# ── F.0 codec ─────────────────────────────────────────────────────────────────────────────────────

def test_compact_u32_vectors_round_trip():
    for v in CORPUS["codec"]["scale_compact_u32"]:
        assert fc.compact_u32(v["value"]).hex() == v["bytes_hex"]
        assert fc.decode_compact_u32(H(v["bytes_hex"])) == (v["value"], len(v["bytes_hex"]) // 2)


def test_malformed_compact_rejects():
    for v in CORPUS["codec"]["malformed_compact"]:
        with pytest.raises(fc.WireError):
            fc.decode_compact_u32(H(v["bytes_hex"]))


def test_fixed_integers_reject_overflow_and_negatives():
    with pytest.raises(fc.WireError):
        fc.channel_id_v1(bytes(32), bytes(32), bytes(32), 1 << 64)
    with pytest.raises(fc.WireError):
        fc.channel_id_v1(bytes(32), bytes(32), bytes(32), -1)
    with pytest.raises(fc.WireError):
        fc.channel_id_v1(bytes(31), bytes(32), bytes(32), 0)


# ── F.1 channel id ────────────────────────────────────────────────────────────────────────────────

def test_channel_id_vector_and_binding():
    v = CC["channel_id"]
    i = v["inputs"]
    cid = fc.channel_id_v1(H(i["genesis_hash_hex"]), H(i["agent_account_id32_hex"]), H(i["miner_account_id32_hex"]), i["nonce"])
    assert cid.hex() == v["hash_hex"]
    assert (fc.CHANNEL_ID_DOMAIN_V1 + b"\x01" + H(i["genesis_hash_hex"]) + H(i["agent_account_id32_hex"])
            + H(i["miner_account_id32_hex"]) + i["nonce"].to_bytes(8, "little")).hex() == v["preimage_hex"]
    neg = {n["id"]: n for n in CORPUS["negative_cases"]}
    other_genesis = fc.channel_id_v1(bytes(32), H(i["agent_account_id32_hex"]), H(i["miner_account_id32_hex"]), i["nonce"])
    assert other_genesis.hex() != v["hash_hex"] and "wrong_genesis_network" in neg
    other_agent = fc.channel_id_v1(H(i["genesis_hash_hex"]), bytes(32), H(i["miner_account_id32_hex"]), i["nonce"])
    assert other_agent.hex() != v["hash_hex"] and "wrong_session" in neg


# ── F.3 leaves, merkle, ack, receipt ──────────────────────────────────────────────────────────────

def test_leaf_preimages_and_hashes_all_versions():
    cid = _leaf_inputs()["channel_id"]
    by_version = {v["version"]: v for v in CC["leaf_versions"]}
    for name, vec in by_version.items():
        turn = _turn(int(name[1]))
        assert fc.leaf_preimage(cid, turn).hex() == vec["preimage_hex"], name
        assert fc.leaf_hash(cid, turn).hex() == vec["hash_hex"], name
        assert vec["scale_tag"] == int(turn.version)
    assert len(fc.leaf_preimage(cid, _turn(3))) == 236
    assert len(fc.leaf_preimage(cid, _turn(2))) == 172
    assert len(fc.leaf_preimage(cid, _turn(1))) == 140
    assert len(fc.leaf_preimage(cid, _turn(0))) == 116


def test_turn_field_consistency_is_fail_closed():
    li = _leaf_inputs()
    with pytest.raises(fc.WireError):   # V2 tag with V3 fields (negative case wrong_leaf_version)
        fc.Turn(fc.LeafVersion.V2, 1, li["h_in"], li["h_out"], 1, li["policy"], li["ids"], li["toploc"], 1, 2, 3, bytes(64)).check()
    with pytest.raises(fc.WireError):   # V3 without h_ids
        fc.Turn(fc.LeafVersion.V3, 1, li["h_in"], li["h_out"], 1, li["policy"], None, None, 1, 2, 3, bytes(64)).check()
    with pytest.raises(fc.WireError):   # V1 with a policy
        fc.Turn(fc.LeafVersion.V1, 1, li["h_in"], li["h_out"], 1, li["policy"], None, None, 1, 2, 3, bytes(64)).check()
    with pytest.raises(fc.WireError):   # half an ack
        fc.Turn(fc.LeafVersion.V3, 1, li["h_in"], li["h_out"], 1, li["policy"], li["ids"], None, 1, 2, 3, bytes(64), 1, None, bytes(64)).check()


def test_merkle_root_and_path_vectors():
    cid = _leaf_inputs()["channel_id"]
    m = CC["merkle"]
    leaves = [fc.leaf_hash(cid, _turn(int(n[1]))) for n in m["leaf_order"]]
    assert fc.merkle_root(leaves).hex() == m["root_hex"]
    path = fc.merkle_path(leaves, 2)
    assert [(s.hex(), left) for s, left in path] == [(p["sibling_hex"], p["sibling_is_left"]) for p in m["path_for_index_2"]]
    assert fc.root_from_path(leaves[2], path).hex() == m["root_hex"]
    flipped = [(s, not left) for s, left in path]            # negative case wrong_path_orientation
    assert fc.root_from_path(leaves[2], flipped).hex() != m["root_hex"]
    assert fc.merkle_root([]) == bytes(32)
    assert fc.merkle_root([leaves[0]]) == leaves[0]
    with pytest.raises(fc.WireError):
        fc.merkle_path(leaves, 3)


def test_ack_and_receipt_preimages():
    a = CC["fcc4_transcript_with_ack"]
    cid = _leaf_inputs()["channel_id"]
    leaf = fc.leaf_hash(cid, _turn(3))
    assert fc.ack_message(cid, _leaf_inputs()["turn_index"], leaf, int(a["agent_send_ms"]), int(a["agent_recv_ms"])).hex() == a["ack_preimage_hex"]
    r = CC["receipt"]
    i = r["inputs"]
    assert fc.receipt_message_v1(H(i["channel_id_hex"]), H(i["final_root_hex"]), i["aggregate_gn"], i["payable"]).hex() == r["preimage_hex"]
    legacy = next(n for n in CORPUS["negative_cases"] if n["id"] == "legacy_receipt_current_channel")
    assert not legacy["bytes_hex"].startswith(fc.RECEIPT_DOMAIN_V1.hex())   # the untagged 96 B form carries no domain: not v1


def test_fcc4_blob_encode_decode_and_verified_turn_scale():
    a = CC["fcc4_transcript_with_ack"]
    v3sig = H(CC["v3_leaf_signature"]["signature_hex"])
    cid = _leaf_inputs()["channel_id"]
    plain = _turn(3, sig=v3sig)
    assert fc.encode_transcript(cid, [plain]).hex() == CC["fcc4_transcript_blob_hex"]
    acked = dataclasses.replace(plain, agent_send_ms=int(a["agent_send_ms"]), agent_recv_ms=int(a["agent_recv_ms"]), agent_sig=H(a["agent_signature_hex"]))
    assert fc.encode_transcript(cid, [acked]).hex() == a["blob_hex"]
    cid2, turns = fc.decode_transcript(H(a["blob_hex"]))
    assert cid2 == cid and turns == [acked]
    assert fc.decode_transcript(H(CC["fcc4_transcript_blob_hex"]))[1] == [plain]
    leaves = [fc.leaf_hash(cid, _turn(int(n[1]), sig=v3sig if n == "V3" else bytes(64))) for n in CC["merkle"]["leaf_order"]]
    scale = fc.encode_verified_turn(plain, fc.merkle_path(leaves, 2))
    assert scale.hex() == CC["verified_turn_v3_scale_hex"]
    assert len(scale) == 269 + 1 + 33 * 2


def test_fcc4_negative_cases_reject():
    neg = {n["id"]: n for n in CORPUS["negative_cases"]}
    for case in ("truncated_fcc4", "trailing_fcc4", "unknown_fcc_version"):
        with pytest.raises(fc.WireError):
            fc.decode_transcript(H(neg[case]["bytes_hex"]))
    with pytest.raises(fc.WireError):      # unknown_leaf_enum: tag 04 inside a blob
        blob = H(CC["fcc4_transcript_blob_hex"])
        fc.decode_transcript(blob[:40] + b"\x04" + blob[41:])
    with pytest.raises(fc.WireError):      # invalid option tag
        blob = H(CC["fcc4_transcript_blob_hex"])
        fc.decode_transcript(blob[:-1] + b"\x02")


def test_verified_work_rejects_duplicate_turn_index():
    t = _turn(3)
    assert fc.verified_work([t]) == t.g_n
    with pytest.raises(fc.WireError):
        fc.verified_work([t, t])
    with pytest.raises(fc.WireError):      # u128 overflow of the checked sum
        fc.verified_work([t, dataclasses.replace(t, turn_index=7)])


# ── signatures (sr25519 under the "substrate" context) ───────────────────────────────────────────

def test_corpus_signatures_verify_under_sr25519():
    verify = _sr25519()
    if verify is None:
        pytest.skip("py-sr25519-bindings not installed")
    cid = _leaf_inputs()["channel_id"]
    v3 = CC["v3_leaf_signature"]
    leaf = fc.verify_leaf(cid, _turn(3, sig=H(v3["signature_hex"])), H(v3["public_key_hex"]), verify, _leaf_inputs()["policy"])
    assert leaf.hex() == v3["leaf_hash_hex"]
    a = CC["fcc4_transcript_with_ack"]
    _, turns = fc.decode_transcript(H(a["blob_hex"]))
    fc.verify_ack(cid, turns[0], H(a["agent_public_key_hex"]), verify)
    r = CC["receipt"]
    i = r["inputs"]
    fc.verify_receipt(H(i["channel_id_hex"]), H(i["final_root_hex"]), i["aggregate_gn"], i["payable"], H(r["public_key_hex"]), H(r["signature_hex"]), verify)
    neg = {n["id"]: n for n in CORPUS["negative_cases"]}
    with pytest.raises(fc.WireError):
        fc.verify_receipt(H(i["channel_id_hex"]), H(i["final_root_hex"]), i["aggregate_gn"], i["payable"], H(r["public_key_hex"]), H(neg["invalid_receipt_signature"]["bytes_hex"]), verify)
    with pytest.raises(fc.WireError):
        _, bad = fc.decode_transcript(H(neg["invalid_agent_ack_signature"]["bytes_hex"]))
        fc.verify_ack(cid, bad[0], H(a["agent_public_key_hex"]), verify)
    with pytest.raises(fc.WireError):      # legacy_leaf_current_channel: V1 on a channel with a pinned policy
        fc.verify_leaf(cid, _turn(1), H(v3["public_key_hex"]), verify, _leaf_inputs()["policy"])
    with pytest.raises(fc.WireError):      # policy mismatch
        fc.verify_leaf(cid, _turn(3, sig=H(v3["signature_hex"])), H(v3["public_key_hex"]), verify, bytes(32))


def test_transcript_accumulator_with_fake_signer():
    """The agent's per-turn duty with injected sign/verify: consecutive indices, root, ack, receipt."""
    import hashlib
    cid = _leaf_inputs()["channel_id"]
    enclave_key, agent_key = b"\x11" * 32, b"\x22" * 32
    sigs = {}

    def sign(msg):
        sig = hashlib.blake2b(b"agent" + msg, digest_size=64).digest()
        sigs[sig] = (agent_key, msg)
        return sig

    def verify(pk, msg, sig):
        if pk == enclave_key:
            return sig == hashlib.blake2b(b"enclave" + msg, digest_size=64).digest()
        return sigs.get(sig) == (pk, msg)

    policy = _leaf_inputs()["policy"]
    tr = fc.Transcript(cid, enclave_key, agent_key, sign, verify, policy)
    turns = []
    for i in range(3):
        t = _turn(3, turn_index=i, g_n=10 + i)
        t = dataclasses.replace(t, enclave_sig=hashlib.blake2b(b"enclave" + fc.leaf_hash(cid, t), digest_size=64).digest())
        acked = tr.accept(t, 1000 + i, 1001 + i)
        fc.verify_ack(cid, acked, agent_key, verify)
        turns.append(acked)
    assert tr.aggregate_gn == 33
    assert tr.root == fc.merkle_root([fc.leaf_hash(cid, t) for t in turns])
    with pytest.raises(fc.WireError):      # out-of-order turn
        tr.accept(_turn(3, turn_index=5, g_n=1), 1, 2)
    with pytest.raises(fc.WireError):      # bad enclave signature
        tr.accept(_turn(3, turn_index=3, g_n=1), 1, 2)
    sig = tr.receipt(payable=10 ** 18)
    fc.verify_receipt(cid, tr.root, 33, 10 ** 18, agent_key, sig, verify)
    assert fc.decode_transcript(tr.blob())[1] == turns
    assert fc.root_from_path(fc.leaf_hash(cid, turns[1]), fc.merkle_path(tr.leaves, 1)) == tr.root
    assert fc.encode_verified_turn(turns[1], fc.merkle_path(tr.leaves, 1))[:1] == b"\x03"
