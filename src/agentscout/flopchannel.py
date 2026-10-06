"""W5 — FLOP compute-channel wire format (Yellow Paper Appendix F, profile `flop-wire-v1`).

The agent side of `pallet_compute_channel` (§12.1): every inference turn arrives as a signed transcript
leaf; the agent MUST recompute the leaf hash, check the miner/enclave signature, extend the running Merkle
root, and counter-sign a per-turn ack before accepting output. At close it signs the agent receipt v1 that
authorises `settle`. Everything here is pure stdlib and deterministic: the byte layouts are those of F.0/F.3,
cross-checked against the public vector corpus (`tests/data/flop-wire-format-v1.json`, status
public-canonical) and the Labs reference encoder `evidence/compute-channel.py`.

Signatures are sr25519 (Substrate `b"substrate"` context); this module never signs or verifies itself —
callers inject `sign(msg) -> sig64` / `verify(pubkey32, msg, sig64) -> bool` (see `flopkeys`). Fail-closed
everywhere: unknown tags, inconsistent optional fields, truncation, trailing bytes and non-canonical
compacts raise `WireError`. Hashes are BLAKE2b-256; `decode_policy_hash` is SHA-256 (F.1).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, List, Optional, Sequence, Tuple

CHANNEL_ID_DOMAIN_V1 = b"FLOP/COMPUTE_CHANNEL/ID"
RECEIPT_DOMAIN_V1 = b"FLOP/COMPUTE_CHANNEL/RECEIPT"
TRANSCRIPT_BLOB_MAGIC = b"FCC4"
ZERO32 = bytes(32)
U128_MAX = (1 << 128) - 1

Hash = bytes
Verifier = Callable[[bytes, bytes, bytes], bool]   # (public_key32, message, signature64) -> ok
Signer = Callable[[bytes], bytes]                   # message -> signature64


class WireError(ValueError):
    """Any malformed or inconsistent Appendix F object. Never partially decoded."""


class LeafVersion(IntEnum):
    V0 = 0
    V1 = 1
    V2 = 2
    V3 = 3


# ── F.0 codec ─────────────────────────────────────────────────────────────────────────────────────

def blake2_256(data: bytes) -> Hash:
    return hashlib.blake2b(data, digest_size=32).digest()


def _fixed(value: bytes, length: int, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != length:
        raise WireError(f"{name} must be {length} bytes")
    return bytes(value)


def _uint_le(value: int, length: int, name: str) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= 1 << (8 * length):
        raise WireError(f"{name} does not fit u{8 * length}")
    return value.to_bytes(length, "little")


def compact_u32(value: int) -> bytes:
    """Canonical SCALE Compact<u32>: 1 byte below 2^6, 2 below 2^14, 4 below 2^30, else 03 ‖ u32LE."""
    _uint_le(value, 4, "compact value")
    if value < 1 << 6:
        return bytes([value << 2])
    if value < 1 << 14:
        return ((value << 2) | 1).to_bytes(2, "little")
    if value < 1 << 30:
        return ((value << 2) | 2).to_bytes(4, "little")
    return b"\x03" + value.to_bytes(4, "little")


def decode_compact_u32(data: bytes, offset: int = 0) -> Tuple[int, int]:
    """Returns (value, next_offset); rejects truncated, overlong and wider-than-u32 encodings."""
    if offset >= len(data):
        raise WireError("truncated compact integer")
    first = data[offset]
    mode = first & 3
    size = (1, 2, 4, (first >> 2) + 5)[mode]
    end = offset + size
    if end > len(data):
        raise WireError("truncated compact integer")
    if mode == 0:
        value = first >> 2
    elif mode in (1, 2):
        value = int.from_bytes(data[offset:end], "little") >> 2
    else:
        if size != 5:
            raise WireError("compact integer exceeds u32")
        value = int.from_bytes(data[offset + 1:end], "little")
    if data[offset:end] != compact_u32(value):
        raise WireError("non-canonical compact integer")
    return value, end


# ── F.1 binding preimages ─────────────────────────────────────────────────────────────────────────

def channel_id_v1(genesis_hash: Hash, agent: bytes, miner: bytes, nonce: int) -> Hash:
    """blake2_256("FLOP/COMPUTE_CHANNEL/ID" ‖ 01 ‖ genesis ‖ agent32 ‖ miner32 ‖ nonce:u64LE).
    Binds the deployment (genesis) and the session (agent, miner, nonce): a wrong network or a wrong
    counterparty yields a different id, so nothing signed under one channel can be replayed on another."""
    return blake2_256(
        CHANNEL_ID_DOMAIN_V1 + b"\x01"
        + _fixed(genesis_hash, 32, "genesis_hash")
        + _fixed(agent, 32, "agent")
        + _fixed(miner, 32, "miner")
        + _uint_le(nonce, 8, "nonce")
    )


def h_ids(prompt_ids: Sequence[int], generated_ids: Sequence[int]) -> Hash:
    """V3 token-id binding: blake2_256(len:u32 ‖ ids:u32… ‖ len:u32 ‖ ids:u32…)."""
    pre = _uint_le(len(prompt_ids), 4, "prompt length") + b"".join(_uint_le(t, 4, "token id") for t in prompt_ids)
    pre += _uint_le(len(generated_ids), 4, "generated length") + b"".join(_uint_le(t, 4, "token id") for t in generated_ids)
    return blake2_256(pre)


# ── F.3 transcript leaves ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Turn:
    """One transcript turn as the miner reports it (plus our ack once we have co-signed it)."""
    version: LeafVersion
    turn_index: int
    h_in: Hash
    h_out: Hash
    g_n: int
    decode_policy_hash: Optional[Hash]
    h_ids: Optional[Hash]
    toploc_commitment_hash: Optional[Hash]
    miner_recv_ms: int
    miner_done_ms: int
    latency_ms: int
    enclave_sig: bytes
    agent_send_ms: Optional[int] = None
    agent_recv_ms: Optional[int] = None
    agent_sig: Optional[bytes] = None

    def check(self) -> None:
        """FCC4 version/field consistency (fail-closed): V0/V1 carry no policy and zero V3 fields; V2 a
        policy and zero V3 fields; V3 a policy and a non-zero h_ids."""
        if not isinstance(self.version, LeafVersion):
            raise WireError("unsupported leaf version")
        ids_nonzero = self.h_ids is not None and any(self.h_ids)
        toploc_nonzero = self.toploc_commitment_hash is not None and any(self.toploc_commitment_hash)
        if self.version in (LeafVersion.V0, LeafVersion.V1):
            if self.decode_policy_hash is not None or ids_nonzero or toploc_nonzero:
                raise WireError("V0/V1 fields are inconsistent")
        elif self.version == LeafVersion.V2:
            if self.decode_policy_hash is None or ids_nonzero or toploc_nonzero:
                raise WireError("V2 fields are inconsistent")
        elif self.decode_policy_hash is None or not ids_nonzero:
            raise WireError("V3 fields are inconsistent")
        if (self.agent_sig is None) != (self.agent_send_ms is None) or (self.agent_sig is None) != (self.agent_recv_ms is None):
            raise WireError("agent ack fields are inconsistent")


def leaf_preimage(channel_id: Hash, turn: Turn) -> bytes:
    """V3: channel_id ‖ idx:u32 ‖ h_in ‖ h_out ‖ g_n:u128 ‖ policy ‖ h_ids ‖ toploc ‖ recv:u64 ‖ done:u64 ‖ latency:u64
    (236 B); V2 drops h_ids+toploc (172 B); V1 drops the policy (140 B); V0 drops the timings (116 B)."""
    turn.check()
    v = _fixed(channel_id, 32, "channel_id") + _uint_le(turn.turn_index, 4, "turn_index")
    v += _fixed(turn.h_in, 32, "h_in") + _fixed(turn.h_out, 32, "h_out") + _uint_le(turn.g_n, 16, "g_n")
    if turn.version >= LeafVersion.V2:
        v += _fixed(turn.decode_policy_hash, 32, "decode_policy_hash")  # type: ignore[arg-type]
    if turn.version == LeafVersion.V3:
        v += _fixed(turn.h_ids, 32, "h_ids") + _fixed(turn.toploc_commitment_hash or ZERO32, 32, "toploc")  # type: ignore[arg-type]
    if turn.version >= LeafVersion.V1:
        v += _uint_le(turn.miner_recv_ms, 8, "miner_recv_ms") + _uint_le(turn.miner_done_ms, 8, "miner_done_ms")
        v += _uint_le(turn.latency_ms, 8, "latency_ms")
    return v


def leaf_hash(channel_id: Hash, turn: Turn) -> Hash:
    return blake2_256(leaf_preimage(channel_id, turn))


def ack_message(channel_id: Hash, turn_index: int, leaf: Hash, agent_send_ms: int, agent_recv_ms: int) -> bytes:
    """Per-turn agent ack (F.0): channel_id ‖ turn_index:u32LE ‖ leaf_hash ‖ send:u64LE ‖ recv:u64LE. No domain."""
    return (_fixed(channel_id, 32, "channel_id") + _uint_le(turn_index, 4, "turn_index") + _fixed(leaf, 32, "leaf_hash")
            + _uint_le(agent_send_ms, 8, "agent_send_ms") + _uint_le(agent_recv_ms, 8, "agent_recv_ms"))


def receipt_message_v1(channel_id: Hash, final_root: Hash, aggregate_gn: int, payable: int) -> bytes:
    """Agent receipt v1: "FLOP/COMPUTE_CHANNEL/RECEIPT" ‖ 01 ‖ channel_id ‖ final_root ‖ gn:u128LE ‖ payable:u128LE.
    `payable` is in 10^-18 FLOP base units and is the full reserved escrow on the cooperative `settle` path."""
    return (RECEIPT_DOMAIN_V1 + b"\x01" + _fixed(channel_id, 32, "channel_id") + _fixed(final_root, 32, "final_root")
            + _uint_le(aggregate_gn, 16, "aggregate_gn") + _uint_le(payable, 16, "payable"))


# ── F.3 Merkle accumulator ────────────────────────────────────────────────────────────────────────

def hash_pair(left: Hash, right: Hash) -> Hash:
    return blake2_256(_fixed(left, 32, "left") + _fixed(right, 32, "right"))


def merkle_root(leaves: Sequence[Hash]) -> Hash:
    """Leaf order = turn order; odd last node duplicated; empty root = 00×32; one leaf = the leaf."""
    if not leaves:
        return ZERO32
    level = [_fixed(leaf, 32, "leaf") for leaf in leaves]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_path(leaves: Sequence[Hash], index: int) -> List[Tuple[Hash, bool]]:
    """Path for leaf `index` as (sibling_hash, sibling_is_left) items, bottom-up."""
    if index < 0 or index >= len(leaves):
        raise WireError("leaf index out of range")
    level = [_fixed(leaf, 32, "leaf") for leaf in leaves]
    path: List[Tuple[Hash, bool]] = []
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sibling = index - 1 if index % 2 else index + 1
        path.append((level[sibling], index % 2 == 1))
        level = [hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        index //= 2
    return path


def root_from_path(leaf: Hash, path: Sequence[Tuple[Hash, bool]]) -> Hash:
    current = _fixed(leaf, 32, "leaf")
    for sibling, sibling_is_left in path:
        current = hash_pair(sibling, current) if sibling_is_left else hash_pair(current, sibling)
    return current


# ── FCC4 transcript container and the SCALE VerifiedTurn ─────────────────────────────────────────

def encode_transcript(channel_id: Hash, turns: Sequence[Turn]) -> bytes:
    """FCC4 ‖ channel_id ‖ count:u32LE ‖ turns… (F.3 "DA transcript container")."""
    out = TRANSCRIPT_BLOB_MAGIC + _fixed(channel_id, 32, "channel_id") + _uint_le(len(turns), 4, "turn count")
    for t in turns:
        t.check()
        out += bytes([t.version]) + _uint_le(t.turn_index, 4, "turn_index")
        out += _fixed(t.h_in, 32, "h_in") + _fixed(t.h_out, 32, "h_out") + _uint_le(t.g_n, 16, "g_n")
        out += b"\x01" + _fixed(t.decode_policy_hash, 32, "decode_policy_hash") if t.decode_policy_hash is not None else b"\x00"
        out += _fixed(t.h_ids or ZERO32, 32, "h_ids") + _fixed(t.toploc_commitment_hash or ZERO32, 32, "toploc_commitment_hash")
        out += _uint_le(t.miner_recv_ms, 8, "miner_recv_ms") + _uint_le(t.miner_done_ms, 8, "miner_done_ms")
        out += _uint_le(t.latency_ms, 8, "latency_ms") + _fixed(t.enclave_sig, 64, "enclave_sig")
        if t.agent_sig is None:
            out += b"\x00"
        else:
            out += b"\x01" + _uint_le(t.agent_send_ms, 8, "agent_send_ms") + _uint_le(t.agent_recv_ms, 8, "agent_recv_ms")  # type: ignore[arg-type]
            out += _fixed(t.agent_sig, 64, "agent_sig")
    return out


def decode_transcript(data: bytes) -> Tuple[Hash, List[Turn]]:
    """Exact FCC4 decode. Acks are parsed, not verified — call `verify_ack` under the channel's agent key."""
    pos = 0

    def take(n: int) -> bytes:
        nonlocal pos
        if pos + n > len(data):
            raise WireError("truncated transcript blob")
        chunk = data[pos:pos + n]
        pos += n
        return chunk

    def flag() -> bool:
        b = take(1)[0]
        if b not in (0, 1):
            raise WireError("invalid option tag")
        return b == 1

    if take(4) != TRANSCRIPT_BLOB_MAGIC:
        raise WireError("unsupported transcript blob version")
    channel_id = take(32)
    count = int.from_bytes(take(4), "little")
    turns: List[Turn] = []
    for _ in range(count):
        tag = take(1)[0]
        try:
            version = LeafVersion(tag)
        except ValueError:
            raise WireError("unsupported leaf version") from None
        turn_index = int.from_bytes(take(4), "little")
        h_in, h_out = take(32), take(32)
        g_n = int.from_bytes(take(16), "little")
        policy = take(32) if flag() else None
        ids, toploc = take(32), take(32)
        recv, done, lat = (int.from_bytes(take(8), "little") for _ in range(3))
        enclave_sig = take(64)
        send = recv_a = sig = None
        if flag():
            send, recv_a = int.from_bytes(take(8), "little"), int.from_bytes(take(8), "little")
            sig = take(64)
        turn = Turn(version, turn_index, h_in, h_out, g_n, policy, ids if any(ids) else None,
                    toploc if any(toploc) else None, recv, done, lat, enclave_sig, send, recv_a, sig)
        turn.check()
        turns.append(turn)
    if pos != len(data):
        raise WireError("trailing transcript blob bytes")
    return channel_id, turns


def encode_verified_turn(turn: Turn, path: Sequence[Tuple[Hash, bool]]) -> bytes:
    """SCALE `VerifiedTurn` (F.3): the settle/dispute argument. Optional hashes are zero-filled, not Option<>."""
    turn.check()
    out = bytes([turn.version]) + _uint_le(turn.turn_index, 4, "turn_index")
    out += _fixed(turn.h_in, 32, "h_in") + _fixed(turn.h_out, 32, "h_out") + _uint_le(turn.g_n, 16, "g_n")
    out += _fixed(turn.decode_policy_hash or ZERO32, 32, "decode_policy_hash")
    out += _fixed(turn.h_ids or ZERO32, 32, "h_ids") + _fixed(turn.toploc_commitment_hash or ZERO32, 32, "toploc_commitment_hash")
    out += _uint_le(turn.miner_recv_ms, 8, "miner_recv_ms") + _uint_le(turn.miner_done_ms, 8, "miner_done_ms")
    out += _uint_le(turn.latency_ms, 8, "latency_ms") + _fixed(turn.enclave_sig, 64, "enclave_sig") + compact_u32(len(path))
    for sibling, sibling_is_left in path:
        out += _fixed(sibling, 32, "path sibling") + (b"\x01" if sibling_is_left else b"\x00")
    return out


# ── Verification (the agent's R12.1b duties) ─────────────────────────────────────────────────────

def verify_leaf(channel_id: Hash, turn: Turn, enclave_key: bytes, verify: Verifier,
                decode_policy_hash: Optional[Hash]) -> Hash:
    """Recompute the leaf hash and check the miner/enclave signature over it. With a pinned channel
    decode policy (every channel opened by this client) only explicitly tagged V2/V3 leaves with an
    equal policy hash are accepted (F.3 accepted-version cutoff; V0/V1 are legacy-only). Returns the leaf."""
    if decode_policy_hash is not None:
        if turn.version < LeafVersion.V2:
            raise WireError("legacy leaf version on a channel with a pinned decode policy")
        if turn.decode_policy_hash != decode_policy_hash:
            raise WireError("decode policy hash differs from the channel's pinned policy")
    leaf = leaf_hash(channel_id, turn)
    if not verify(_fixed(enclave_key, 32, "enclave_key"), leaf, _fixed(turn.enclave_sig, 64, "enclave_sig")):
        raise WireError("bad enclave signature on transcript leaf")
    return leaf


def verify_ack(channel_id: Hash, turn: Turn, agent_key: bytes, verify: Verifier) -> None:
    """Check a turn's agent ack under the channel's agent key (what the miner's `record_ack` checks)."""
    if turn.agent_sig is None:
        raise WireError("turn carries no agent ack")
    leaf = leaf_hash(channel_id, turn)
    msg = ack_message(channel_id, turn.turn_index, leaf, turn.agent_send_ms, turn.agent_recv_ms)  # type: ignore[arg-type]
    if not verify(_fixed(agent_key, 32, "agent_key"), msg, _fixed(turn.agent_sig, 64, "agent_sig")):
        raise WireError("bad agent ack signature")


def verify_receipt(channel_id: Hash, final_root: Hash, aggregate_gn: int, payable: int,
                   agent_key: bytes, signature: bytes, verify: Verifier) -> None:
    msg = receipt_message_v1(channel_id, final_root, aggregate_gn, payable)
    if not verify(_fixed(agent_key, 32, "agent_key"), msg, _fixed(signature, 64, "signature")):
        raise WireError("bad agent receipt signature")


def verified_work(turns: Sequence[Turn]) -> int:
    """Checked sum of distinct submitted turns' g_n (`verified_work_from_turns`); duplicate indices reject."""
    seen = set()
    total = 0
    for t in turns:
        if t.turn_index in seen:
            raise WireError("duplicate turn index")
        seen.add(t.turn_index)
        total += t.g_n
    if total > U128_MAX:
        raise WireError("aggregate_gn overflows u128")
    return total


class Transcript:
    """The agent's running view of one channel: verified turns in order, cumulative root, aggregate G_n.

    `accept(turn, now_ms)` is the whole R12.1b duty for one turn: verify the leaf under the enclave key,
    require a strictly consecutive turn index, extend the root, and return the signed ack (send, recv,
    sig) to hand back to the miner. `receipt(payable)` signs the agent receipt v1 for `settle`.
    """

    def __init__(self, channel_id: Hash, enclave_key: bytes, agent_key: bytes, sign: Signer, verify: Verifier,
                 decode_policy_hash: Optional[Hash]):
        self.channel_id = _fixed(channel_id, 32, "channel_id")
        self.enclave_key = _fixed(enclave_key, 32, "enclave_key")
        self.agent_key = _fixed(agent_key, 32, "agent_key")
        self.decode_policy_hash = decode_policy_hash
        self._sign = sign
        self._verify = verify
        self.turns: List[Turn] = []
        self.leaves: List[Hash] = []

    @property
    def root(self) -> Hash:
        return merkle_root(self.leaves)

    @property
    def aggregate_gn(self) -> int:
        return verified_work(self.turns)

    def accept(self, turn: Turn, send_ms: int, recv_ms: int) -> Turn:
        if turn.turn_index != len(self.turns):
            raise WireError(f"turn index {turn.turn_index} is not the next expected {len(self.turns)}")
        if turn.agent_sig is not None:
            raise WireError("turn already carries an agent ack")
        leaf = verify_leaf(self.channel_id, turn, self.enclave_key, self._verify, self.decode_policy_hash)
        sig = self._sign(ack_message(self.channel_id, turn.turn_index, leaf, send_ms, recv_ms))
        acked = Turn(turn.version, turn.turn_index, turn.h_in, turn.h_out, turn.g_n, turn.decode_policy_hash,
                     turn.h_ids, turn.toploc_commitment_hash, turn.miner_recv_ms, turn.miner_done_ms,
                     turn.latency_ms, turn.enclave_sig, send_ms, recv_ms, _fixed(sig, 64, "agent_sig"))
        self.turns.append(acked)
        self.leaves.append(leaf)
        return acked

    def receipt(self, payable: int) -> bytes:
        """Sign agent receipt v1 over the current root and aggregate for the stated payable amount."""
        return _fixed(self._sign(receipt_message_v1(self.channel_id, self.root, self.aggregate_gn, payable)), 64, "receipt sig")

    def blob(self) -> bytes:
        return encode_transcript(self.channel_id, self.turns)

    def verified_turn(self, index: int) -> bytes:
        return encode_verified_turn(self.turns[index], merkle_path(self.leaves, index))
