"""Experimental channel adapter with durable intent and exposure accounting; disabled by default.

The wire codec follows the reference corpus, but transport and runtime integration remain provisional.
Official signed-HTTP onboarding may replace this entire adapter. A locally CLOSED session is never
treated as settled or credited. Independent finality verification must be supplied by a future trusted
adapter; this module does not implement automatic claim, timeout recovery, refund or finality polling.
"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import flopchannel as fc
from .flopchain import BASE_UNITS_PER_FLOP, ChainError, OpenChannelParams
from .inference_journal import InferenceJournal, JournalError, base_units

log = logging.getLogger("agentscout.flopsession")

STATE_OPEN = "OPEN"
STATE_CLOSED = "CLOSED"          # receipt handed over; settlement is the miner's extrinsic
STATE_SETTLED = "SETTLED"        # seen on chain (channel(...) reports settled) — set by a later check
STATE_FAILED = "FAILED"


class SessionError(RuntimeError):
    """The session could not complete; the channel (if opened) is left for timeout/force paths."""


@dataclass(frozen=True)
class MinerHello:
    """What a miner advertises before a channel is opened (everything `open_channel` needs)."""
    miner: bytes                 # AccountId32
    enclave_key: bytes           # sr25519 session key the leaves are signed with (SOFT: miner session key)
    model_hash: bytes
    measured_root: bytes
    decode_policy_hash: bytes
    precision: Any
    settlement_class: Any
    sla: Dict[str, Any]
    model: str = ""


@dataclass(frozen=True)
class TurnReply:
    turn: fc.Turn                # unacked, as the miner signed it
    output: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SessionResult:
    channel_id: bytes
    output: str
    turns: int
    aggregate_gn: int
    final_root: bytes
    payable: int
    tx_hash: str
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str = ""


class HttpMinerTransport:
    """PROVISIONAL miner API (JSON over HTTPS, stdlib urllib). Shapes:
    POST {base}/v1/hello                      → {miner, enclave_key, model_hash, measured_root, decode_policy_hash, precision, settlement_class, sla, model}
    POST {base}/v1/channels/{cid}/turns       {turn_index, input, agent_send_ms} → {leaf: {...F.3 fields hex...}, output, input_tokens, output_tokens}
    POST {base}/v1/channels/{cid}/acks        {turn_index, agent_send_ms, agent_recv_ms, agent_sig}
    POST {base}/v1/channels/{cid}/close       {final_root, aggregate_gn, payable, receipt_sig, transcript} → {status, tx_hash}
    Hex fields are lowercase without 0x. Replace this class when Labs publishes the stream protocol."""

    def __init__(self, base_url: str, timeout: int = 60, allow_provisional: bool = False):
        if not allow_provisional:
            raise SessionError("provisional miner transport is disabled; no official API has been implemented")
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": "agentscout-w5/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SessionError(f"miner {path} HTTP {exc.code}: {exc.read()[:200]!r}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SessionError(f"miner {path} failed: {exc.__class__.__name__}: {str(exc)[:160]}") from exc

    def hello(self) -> MinerHello:
        d = self._post("/v1/hello", {})
        h = bytes.fromhex
        return MinerHello(h(d["miner"]), h(d["enclave_key"]), h(d["model_hash"]), h(d["measured_root"]),
                          h(d["decode_policy_hash"]), d.get("precision", "Bf16"), d.get("settlement_class", "Cooperative"),
                          dict(d.get("sla") or {}), str(d.get("model", "")))

    def turn(self, channel_id: bytes, turn_index: int, prompt: str, agent_send_ms: int) -> TurnReply:
        d = self._post(f"/v1/channels/{channel_id.hex()}/turns", {"turn_index": turn_index, "input": prompt, "agent_send_ms": agent_send_ms})
        leaf = d["leaf"]
        h = bytes.fromhex

        def opt(key: str) -> Optional[bytes]:
            v = leaf.get(key)
            return h(v) if v else None

        turn = fc.Turn(fc.LeafVersion(int(leaf["version"])), int(leaf["turn_index"]), h(leaf["h_in"]), h(leaf["h_out"]),
                       int(leaf["g_n"]), opt("decode_policy_hash"), opt("h_ids"), opt("toploc_commitment_hash"),
                       int(leaf["miner_recv_ms"]), int(leaf["miner_done_ms"]), int(leaf["latency_ms"]), h(leaf["enclave_sig"]))
        return TurnReply(turn, str(d.get("output", "")), int(d.get("input_tokens", 0)), int(d.get("output_tokens", 0)))

    def ack(self, channel_id: bytes, acked: fc.Turn) -> None:
        self._post(f"/v1/channels/{channel_id.hex()}/acks", {"turn_index": acked.turn_index, "agent_send_ms": acked.agent_send_ms,
                                                             "agent_recv_ms": acked.agent_recv_ms, "agent_sig": (acked.agent_sig or b"").hex()})

    def close(self, channel_id: bytes, final_root: bytes, aggregate_gn: int, payable: int, receipt_sig: bytes, transcript: bytes) -> str:
        d = self._post(f"/v1/channels/{channel_id.hex()}/close", {"final_root": final_root.hex(), "aggregate_gn": str(aggregate_gn),
                                                                  "payable": str(payable), "receipt_sig": receipt_sig.hex(),
                                                                  "transcript": transcript.hex()})
        return str(d.get("tx_hash", ""))


class FlopSession:
    """Runs sessions. `chain` is a flopchain client, `key` a flopkeys.SessionKey, `transport` a miner
    transport (duck-typed as HttpMinerTransport), `storage` the agent DB. Guards: per-day session cap,
    minimum free balance (escrow + identity stake headroom), strictly consecutive turns, every signature."""

    def __init__(self, settings, chain, key, transport, storage, verify: Callable[[bytes, bytes, bytes], bool],
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.s = settings
        self.chain = chain
        self.key = key
        self.transport = transport
        self.db = storage
        self._verify = verify
        self._now = now
        self.journal = InferenceJournal(storage)

    # ---- guards -----------------------------------------------------------------------
    def escrow_base_units(self) -> int:
        return base_units(self.s.flop_escrow_flop)

    def _check_balance(self, fees: int) -> None:
        free = self.chain.free_balance()
        need = self.escrow_base_units() + fees + base_units(self.s.flop_min_balance_flop)
        if free < need:
            raise SessionError(f"free balance {free / BASE_UNITS_PER_FLOP:.4f} FLOP < escrow+headroom {need / BASE_UNITS_PER_FLOP:.4f}")

    # ---- the session ------------------------------------------------------------------
    def run(self, prompts: List[str], request_id: Optional[str] = None) -> SessionResult:
        """One durably journaled attempt. Reusing a request never submits another channel.

        Without an explicit business request ID, identical prompts under this account/network dedupe
        across restarts and UTC days. Supply a new ID only for genuinely new work, never a blind retry.
        CLOSED is a delivery acknowledgement; only independent reconciliation can prove settlement.
        """
        if not prompts:
            raise SessionError("no prompts")
        now = self._now()
        network, account = self.chain.genesis_hash().hex(), self.chain.account_id().hex()
        prompt_hashes = [fc.blake2_256(p.encode("utf-8")).hex() for p in prompts]
        request_id = request_id or "flop:" + hashlib.sha256(json.dumps([network, account, prompt_hashes]).encode()).hexdigest()
        if self.journal.request(request_id) is not None:
            raise SessionError(f"request {request_id} already recorded; reconcile it before any retry")
        try:
            fees = base_units(getattr(self.s, "flop_fee_reserve_flop", "0.1"))
            self._check_balance(fees)
        except JournalError as exc:
            raise SessionError(str(exc)) from exc
        hello = self.transport.hello()
        escrow = self.escrow_base_units()
        intent = {"prompt_hashes": prompt_hashes, "miner": hello.miner.hex(), "model": hello.model,
                  "model_hash": hello.model_hash.hex(), "measured_root": hello.measured_root.hex(),
                  "decode_policy_hash": hello.decode_policy_hash.hex(), "agent_key": self.key.public.hex(),
                  "enclave_key": hello.enclave_key.hex()}
        try:
            created = self.journal.reserve(request_id, "flop-provisional", network, account, escrow, fees, intent, _iso(now),
                                           int(self.s.flop_max_sessions_per_day),
                                           base_units(getattr(self.s, "flop_max_daily_exposure_flop", "1.1")),
                                           base_units(getattr(self.s, "flop_max_open_exposure_flop", "1.1")))
            if not created:
                raise SessionError(f"request {request_id} already recorded; reconcile it before any retry")
        except JournalError as exc:
            raise SessionError(str(exc)) from exc
        nonce = self.db.flop_next_channel_nonce()
        params = OpenChannelParams(hello.miner, hello.model_hash, hello.measured_root, hello.decode_policy_hash, hello.precision,
                                   hello.enclave_key, self.key.public, hello.sla, escrow, nonce, hello.settlement_class)
        cid = fc.channel_id_v1(bytes.fromhex(network), bytes.fromhex(account), hello.miner, nonce)
        self.journal.bind_channel(request_id, cid.hex(), _iso(now))
        self.db.flop_session_open(cid.hex(), hello.miner.hex(), hello.model, escrow, nonce, "", self.key.public.hex(), _iso(now), "PREPARED")
        self.journal.record(request_id, "OPEN_INTENT", {**intent, "nonce": nonce, "escrow_base": str(escrow),
                            "sla": hello.sla, "precision": hello.precision, "settlement_class": hello.settlement_class},
                            _iso(now), state="OPEN_SUBMITTED")
        try:
            opened = self.chain.open_channel(params)
            if opened.channel_id != cid:
                raise ChainError("opened channel differs from the durable intent")
        except Exception as exc:  # submit failures can be ambiguous even when no receipt arrived
            self.journal.fail(request_id, f"open_channel: {exc}", _iso(self._now()))
            self.db.flop_session_fail(cid.hex(), str(exc)[:300], _iso(self._now()), "AMBIGUOUS")
            raise SessionError(f"open_channel: {exc}") from exc
        self.journal.record(request_id, "OPEN_RESPONSE", {"tx_hash": opened.tx_hash, "block_hash": opened.block_hash}, _iso(self._now()), "OPEN")
        self.db.flop_session_opened(cid.hex(), opened.tx_hash)
        log.info("flop channel %s opened with miner %s… escrow %.4f FLOP (tx %s)", cid.hex()[:16], hello.miner.hex()[:12],
                 escrow / BASE_UNITS_PER_FLOP, opened.tx_hash)
        tr = fc.Transcript(cid, hello.enclave_key, self.key.public, self.key.sign, self._verify, hello.decode_policy_hash)
        output, in_tok, out_tok = "", 0, 0
        try:
            for i, prompt in enumerate(prompts):
                send_ms = _ms(self._now())
                self.journal.record(request_id, "TURN_INTENT", {"turn_index": i, "input_hash": prompt_hashes[i], "agent_send_ms": send_ms}, _iso(self._now()))
                reply = self.transport.turn(cid, i, prompt, send_ms)
                recv_ms = max(_ms(self._now()), send_ms)
                t = reply.turn
                self.journal.record(request_id, "TURN_RESPONSE", {"turn_index": i,
                                    "transcript": fc.encode_transcript(cid, [t]).hex(),
                                    "delivered_output_hash": fc.blake2_256(reply.output.encode("utf-8")).hex()}, _iso(self._now()))
                if t.h_in != fc.blake2_256(prompt.encode("utf-8")):
                    raise fc.WireError(f"turn {i}: h_in is not the hash of our prompt")
                if t.h_out != fc.blake2_256(reply.output.encode("utf-8")):
                    raise fc.WireError(f"turn {i}: h_out is not the hash of the delivered output")
                acked = tr.accept(t, send_ms, recv_ms)          # verifies the leaf under the enclave key, extends the root
                self.journal.record(request_id, "ACK_INTENT", {"turn_index": i, "transcript": tr.blob().hex()}, _iso(self._now()))
                self.transport.ack(cid, acked)
                output, in_tok, out_tok = reply.output, in_tok + reply.input_tokens, out_tok + reply.output_tokens
            payable = escrow                                     # cooperative settle pays the full reserved escrow (R12.1a)
            receipt = tr.receipt(payable)
            self.journal.record(request_id, "RECEIPT_READY", {"final_root": tr.root.hex(), "aggregate_gn": str(tr.aggregate_gn),
                                "payable": str(payable), "receipt_sig": receipt.hex(), "transcript": tr.blob().hex()},
                                _iso(self._now()), "RECEIPT_READY")
            self.db.flop_session_receipt(cid.hex(), len(tr.turns), str(tr.aggregate_gn), tr.root.hex(), str(payable), receipt.hex())
            self.journal.record(request_id, "CLOSE_INTENT", {}, _iso(self._now()), "CLOSE_SUBMITTED")
            tx = self.transport.close(cid, tr.root, tr.aggregate_gn, payable, receipt, tr.blob())
        except fc.WireError as exc:
            self.journal.fail(request_id, f"wire: {exc}", _iso(self._now()), ambiguous=False)
            self.db.flop_session_fail(cid.hex(), f"wire: {exc}", _iso(self._now()))
            raise SessionError(f"channel {cid.hex()[:16]}: {exc}") from exc
        except Exception as exc:
            self.journal.fail(request_id, str(exc), _iso(self._now()))
            self.db.flop_session_fail(cid.hex(), str(exc)[:300], _iso(self._now()), "AMBIGUOUS")
            raise SessionError(f"channel {cid.hex()[:16]} uncertain: {exc}") from exc
        self.journal.record(request_id, "CLOSE_RESPONSE", {"tx_hash": tx}, _iso(self._now()), "CLOSED")
        self.db.flop_session_close(cid.hex(), len(tr.turns), str(tr.aggregate_gn), tr.root.hex(), str(payable), receipt.hex(), tx, _iso(self._now()))
        log.info("flop channel %s closed: %d turn(s), G_n %s, payable %.4f FLOP, settle tx %s", cid.hex()[:16], len(tr.turns),
                 tr.aggregate_gn, payable / BASE_UNITS_PER_FLOP, tx or "(miner pending)")
        return SessionResult(cid, output, len(tr.turns), tr.aggregate_gn, tr.root, payable, tx, in_tok, out_tok, request_id)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
