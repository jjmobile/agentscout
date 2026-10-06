"""W5 — one paid inference session on the FLOP compute channel, end to end.

The spend path the Yellow Paper names as the only agent airdrop metric (E.38/E.40 placeholder: settled
inference spend): `open_channel` on chain (escrow IS the price, R12.1a) → stream turns with the miner,
verifying every signed leaf and co-signing every ack (R12.1b, Appendix F.3) → sign the agent receipt v1
over the final root and aggregate G_n → the miner posts `settle`. Each session is one row in
`flop_sessions`; the first settled row is the W5 definition of done.

What is spec-true here: the chain call, the channel id, every hash, leaf, ack and receipt byte, the Merkle
accumulator, the fail-closed checks. What is provisional: the miner-facing transport (`HttpMinerTransport`),
because Labs has not published the SOFT-tier stream protocol yet (E.33). It is one small class with a
JSON shape documented inline; swapping it is the only change expected when the real client spec lands.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import flopchannel as fc
from .flopchain import BASE_UNITS_PER_FLOP, ChainError, ChainUnavailable, OpenChannelParams

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


class HttpMinerTransport:
    """PROVISIONAL miner API (JSON over HTTPS, stdlib urllib). Shapes:
    POST {base}/v1/hello                      → {miner, enclave_key, model_hash, measured_root, decode_policy_hash, precision, settlement_class, sla, model}
    POST {base}/v1/channels/{cid}/turns       {turn_index, input, agent_send_ms} → {leaf: {...F.3 fields hex...}, output, input_tokens, output_tokens}
    POST {base}/v1/channels/{cid}/acks        {turn_index, agent_send_ms, agent_recv_ms, agent_sig}
    POST {base}/v1/channels/{cid}/close       {final_root, aggregate_gn, payable, receipt_sig, transcript} → {status, tx_hash}
    Hex fields are lowercase without 0x. Replace this class when Labs publishes the stream protocol."""

    def __init__(self, base_url: str, timeout: int = 60):
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

    # ---- guards -----------------------------------------------------------------------
    def escrow_base_units(self) -> int:
        return int(round(float(self.s.flop_escrow_flop) * BASE_UNITS_PER_FLOP))

    def _check_budget(self, day: str) -> None:
        n = self.db.flop_sessions_today(day)
        if n >= int(self.s.flop_max_sessions_per_day):
            raise SessionError(f"daily session cap reached ({n}/{self.s.flop_max_sessions_per_day})")
        free = self.chain.free_balance()
        need = self.escrow_base_units() + int(round(float(self.s.flop_min_balance_flop) * BASE_UNITS_PER_FLOP))
        if free < need:
            raise SessionError(f"free balance {free / BASE_UNITS_PER_FLOP:.4f} FLOP < escrow+headroom {need / BASE_UNITS_PER_FLOP:.4f}")

    # ---- the session ------------------------------------------------------------------
    def run(self, prompts: List[str]) -> SessionResult:
        """One channel, one turn per prompt, cooperative close. Returns the last turn's output."""
        if not prompts:
            raise SessionError("no prompts")
        now = self._now()
        day = now.strftime("%Y-%m-%d")
        self._check_budget(day)
        hello = self.transport.hello()
        nonce = self.db.flop_next_channel_nonce()
        escrow = self.escrow_base_units()
        params = OpenChannelParams(hello.miner, hello.model_hash, hello.measured_root, hello.decode_policy_hash, hello.precision,
                                   hello.enclave_key, self.key.public, hello.sla, escrow, nonce, hello.settlement_class)
        try:
            opened = self.chain.open_channel(params)
        except (ChainUnavailable, ChainError) as exc:
            raise SessionError(f"open_channel: {exc}") from exc
        cid = opened.channel_id
        self.db.flop_session_open(cid.hex(), hello.miner.hex(), hello.model, escrow, nonce, opened.tx_hash, self.key.public.hex(), _iso(now))
        log.info("flop channel %s opened with miner %s… escrow %.4f FLOP (tx %s)", cid.hex()[:16], hello.miner.hex()[:12],
                 escrow / BASE_UNITS_PER_FLOP, opened.tx_hash)
        tr = fc.Transcript(cid, hello.enclave_key, self.key.public, self.key.sign, self._verify, hello.decode_policy_hash)
        output, in_tok, out_tok = "", 0, 0
        try:
            for i, prompt in enumerate(prompts):
                send_ms = _ms(self._now())
                reply = self.transport.turn(cid, i, prompt, send_ms)
                recv_ms = max(_ms(self._now()), send_ms)
                t = reply.turn
                if t.h_in != fc.blake2_256(prompt.encode("utf-8")):
                    raise SessionError(f"turn {i}: h_in is not the hash of our prompt")
                if t.h_out != fc.blake2_256(reply.output.encode("utf-8")):
                    raise SessionError(f"turn {i}: h_out is not the hash of the delivered output")
                acked = tr.accept(t, send_ms, recv_ms)          # verifies the leaf under the enclave key, extends the root
                self.transport.ack(cid, acked)
                output, in_tok, out_tok = reply.output, in_tok + reply.input_tokens, out_tok + reply.output_tokens
            payable = escrow                                     # cooperative settle pays the full reserved escrow (R12.1a)
            receipt = tr.receipt(payable)
            tx = self.transport.close(cid, tr.root, tr.aggregate_gn, payable, receipt, tr.blob())
        except fc.WireError as exc:
            self.db.flop_session_fail(cid.hex(), f"wire: {exc}", _iso(self._now()))
            raise SessionError(f"channel {cid.hex()[:16]}: {exc}") from exc
        except SessionError as exc:
            self.db.flop_session_fail(cid.hex(), str(exc)[:300], _iso(self._now()))
            raise
        self.db.flop_session_close(cid.hex(), len(tr.turns), str(tr.aggregate_gn), tr.root.hex(), str(payable), receipt.hex(), tx, _iso(self._now()))
        log.info("flop channel %s closed: %d turn(s), G_n %s, payable %.4f FLOP, settle tx %s", cid.hex()[:16], len(tr.turns),
                 tr.aggregate_gn, payable / BASE_UNITS_PER_FLOP, tx or "(miner pending)")
        return SessionResult(cid, output, len(tr.turns), tr.aggregate_gn, tr.root, payable, tx, in_tok, out_tok)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
