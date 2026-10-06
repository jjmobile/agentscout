"""W5 — the FLOP chain seam: open compute channels as our account, read what settled.

Yellow Paper §12.1 / App. G.1: a streaming session touches the chain only at OPEN (`open_channel`, signed
by the agent account) and SETTLE (`settle`, signed by the miner with our receipt attached). This module is
the thin, swappable layer between AgentScout and a Substrate node:

* `NullChain` — no RPC configured (today): every call raises `ChainUnavailable`, so the provider reports
  "unavailable" honestly and nothing pretends to spend.
* `SubstrateChain` — `substrate-interface` behind a duck-typed `iface` + `keypair`, so the composition is
  unit-tested with fakes and goes live the day a testnet RPC URL exists (`FLOP_RPC_URL`). The pallet and
  call names default to the paper's (`ComputeChannel.open_channel`) and are configurable because the
  runtime's metadata, not the paper, is the final word on spelling.

The account is our ed25519 identity key (§6.5: accepted via `MultiSignature`); `channel_id` is derived
locally per F.1 and cross-checked against the `ChannelOpened` event when the runtime emits one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .flopchannel import channel_id_v1

log = logging.getLogger("agentscout.flopchain")

BASE_UNITS_PER_FLOP = 10 ** 18       # F.3: payable/escrow are u128 counts of 10^-18 FLOP
DEFAULT_PALLET = "ComputeChannel"
DEFAULT_SS58_PREFIX = 42             # generic Substrate; the FLOP prefixes are unpublished (§9.3 R9.8)


class ChainUnavailable(RuntimeError):
    """No usable chain: not configured, unreachable, or the dependency is missing."""


class ChainError(RuntimeError):
    """The node answered and the call failed (dispatch error, bad params, insufficient balance)."""


@dataclass(frozen=True)
class OpenChannelParams:
    """`open_channel(miner, model_hash, measured_root, decode_policy_hash, precision, enclave_key, agent_key,
    sla, escrow, nonce, settlement_class)` — App. G.1. Hashes/keys are raw 32-byte strings; `escrow` is in
    base units; `sla` is passed through to the runtime's SLA struct as the node's metadata defines it."""
    miner: bytes
    model_hash: bytes
    measured_root: bytes
    decode_policy_hash: bytes
    precision: Any
    enclave_key: bytes
    agent_key: bytes
    sla: Dict[str, Any]
    escrow: int
    nonce: int
    settlement_class: Any

    def __post_init__(self) -> None:
        for name in ("miner", "model_hash", "measured_root", "decode_policy_hash", "enclave_key", "agent_key"):
            if len(getattr(self, name)) != 32:
                raise ValueError(f"{name} must be 32 bytes")
        if self.escrow <= 0 or self.escrow >= 1 << 128:
            raise ValueError("escrow must be a positive u128 count of base units")
        if not 0 <= self.nonce < 1 << 64:
            raise ValueError("nonce must fit u64")


@dataclass(frozen=True)
class ChannelOpened:
    channel_id: bytes
    tx_hash: str
    block_hash: Optional[str] = None
    events: list = field(default_factory=list)


class NullChain:
    """The seam while no RPC is configured. Deliberately loud: W5 must never spend in the dark."""

    name = "null"

    def __init__(self, reason: str = "no FLOP RPC configured (FLOP_RPC_URL is empty)"):
        self.reason = reason

    def genesis_hash(self) -> bytes:
        raise ChainUnavailable(self.reason)

    def account_id(self) -> bytes:
        raise ChainUnavailable(self.reason)

    def free_balance(self) -> int:
        raise ChainUnavailable(self.reason)

    def open_channel(self, params: OpenChannelParams) -> ChannelOpened:
        raise ChainUnavailable(self.reason)

    def channel(self, channel_id: bytes) -> Optional[Dict[str, Any]]:
        raise ChainUnavailable(self.reason)


class SubstrateChain:
    """substrate-interface wrapper. `iface` is a `SubstrateInterface` (or a test double with the same
    methods: get_block_hash, query, compose_call, create_signed_extrinsic, submit_extrinsic); `keypair`
    exposes `.public_key` (32 bytes) and is what signs our extrinsics."""

    name = "substrate"

    def __init__(self, iface, keypair, pallet: str = DEFAULT_PALLET):
        self.iface = iface
        self.keypair = keypair
        self.pallet = pallet
        self._genesis: Optional[bytes] = None

    @classmethod
    def connect(cls, url: str, identity, pallet: str = DEFAULT_PALLET, ss58_prefix: int = DEFAULT_SS58_PREFIX) -> "SubstrateChain":
        """Open a websocket/http connection with substrate-interface, signing as our ed25519 identity."""
        try:
            from substrateinterface import Keypair, KeypairType, SubstrateInterface  # type: ignore
        except ImportError as exc:
            raise ChainUnavailable("substrate-interface is not installed") from exc
        try:
            iface = SubstrateInterface(url=url, ss58_format=ss58_prefix)
        except Exception as exc:  # noqa: BLE001 — connection/metadata failures are "unavailable", not bugs
            raise ChainUnavailable(f"cannot connect to {url}: {exc.__class__.__name__}: {str(exc)[:160]}") from exc
        keypair = Keypair.create_from_seed(identity.private_seed().hex(), ss58_format=ss58_prefix, crypto_type=KeypairType.ED25519)
        if bytes(keypair.public_key) != identity.public_bytes():
            raise ChainUnavailable("substrate keypair does not reproduce the identity's public key")
        chain = cls(iface, keypair, pallet)
        log.info("flop chain connected: %s chain=%s account=%s", url, getattr(iface, "chain", "?"), keypair.ss58_address)
        return chain

    # ---- reads ------------------------------------------------------------------------
    def genesis_hash(self) -> bytes:
        if self._genesis is None:
            h = self.iface.get_block_hash(0)
            self._genesis = bytes.fromhex(h[2:] if h.startswith("0x") else h)
            if len(self._genesis) != 32:
                raise ChainError("genesis hash is not 32 bytes")
        return self._genesis

    def account_id(self) -> bytes:
        return bytes(self.keypair.public_key)

    def free_balance(self) -> int:
        """Free balance in base units (10^-18 FLOP)."""
        res = self.iface.query("System", "Account", [self.keypair.ss58_address])
        value = getattr(res, "value", res)
        try:
            return int(value["data"]["free"])
        except (KeyError, TypeError) as exc:
            raise ChainError(f"unexpected System.Account shape: {value!r}"[:200]) from exc

    def channel(self, channel_id: bytes) -> Optional[Dict[str, Any]]:
        res = self.iface.query(self.pallet, "Channels", ["0x" + channel_id.hex()])
        value = getattr(res, "value", res)
        return dict(value) if value else None

    # ---- writes -----------------------------------------------------------------------
    def open_channel(self, params: OpenChannelParams) -> ChannelOpened:
        """Submit `open_channel`, wait for inclusion, return the channel id. The id is derived locally
        (F.1: genesis ‖ agent ‖ miner ‖ nonce) and must match the `ChannelOpened` event when present —
        a mismatch means our encoder and the runtime disagree, which is a stop-the-world finding."""
        expected = channel_id_v1(self.genesis_hash(), self.account_id(), params.miner, params.nonce)
        call = self.iface.compose_call(
            call_module=self.pallet,
            call_function="open_channel",
            call_params={
                "miner": "0x" + params.miner.hex(),
                "model_hash": "0x" + params.model_hash.hex(),
                "measured_root": "0x" + params.measured_root.hex(),
                "decode_policy_hash": "0x" + params.decode_policy_hash.hex(),
                "precision": params.precision,
                "enclave_key": "0x" + params.enclave_key.hex(),
                "agent_key": "0x" + params.agent_key.hex(),
                "sla": params.sla,
                "escrow": params.escrow,
                "nonce": params.nonce,
                "settlement_class": params.settlement_class,
            },
        )
        extrinsic = self.iface.create_signed_extrinsic(call=call, keypair=self.keypair)
        try:
            receipt = self.iface.submit_extrinsic(extrinsic, wait_for_inclusion=True)
        except Exception as exc:  # noqa: BLE001 — node/transport failures
            raise ChainError(f"open_channel submit failed: {exc.__class__.__name__}: {str(exc)[:200]}") from exc
        if not getattr(receipt, "is_success", False):
            raise ChainError(f"open_channel failed: {getattr(receipt, 'error_message', None)}")
        events = list(getattr(receipt, "triggered_events", []) or [])
        emitted = _channel_id_from_events(events, self.pallet)
        if emitted is not None and emitted != expected:
            raise ChainError(f"ChannelOpened id {emitted.hex()} != locally derived {expected.hex()} (F.1 mismatch)")
        return ChannelOpened(expected, str(getattr(receipt, "extrinsic_hash", "")), getattr(receipt, "block_hash", None), events)


def _channel_id_from_events(events, pallet: str) -> Optional[bytes]:
    """Find `ChannelOpened { channel_id }` among triggered events, tolerant of substrate-interface's shapes."""
    for ev in events:
        value = getattr(ev, "value", ev)
        try:
            module = value.get("module_id") or value.get("event", {}).get("module_id")
            name = value.get("event_id") or value.get("event", {}).get("event_id")
            attrs = value.get("attributes") if "attributes" in value else value.get("event", {}).get("attributes")
        except AttributeError:
            continue
        if module != pallet or name != "ChannelOpened":
            continue
        cid = attrs.get("channel_id") if isinstance(attrs, dict) else (attrs[0] if attrs else None)
        if isinstance(cid, str):
            return bytes.fromhex(cid[2:] if cid.startswith("0x") else cid)
        if isinstance(cid, (bytes, bytearray)):
            return bytes(cid)
    return None


def make_chain(settings, identity):
    """`FLOP_RPC_URL` empty → NullChain; otherwise a live SubstrateChain (connection errors become
    ChainUnavailable so the caller can keep the summarizer off without crashing the agent)."""
    url = getattr(settings, "flop_rpc_url", "") or ""
    if not url:
        return NullChain()
    return SubstrateChain.connect(url, identity, pallet=getattr(settings, "flop_pallet", DEFAULT_PALLET),
                                  ss58_prefix=getattr(settings, "flop_ss58_prefix", DEFAULT_SS58_PREFIX))
