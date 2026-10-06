"""P6/W5 — the inference seam. AgentScout's cosmetic LLM summaries are the one place it *spends*
on inference, and the $FLOP airdrop rewards exactly that: compute purchased in settled sessions on the
testnet (flop.finance/airdrop, 2026-10-05). So the provider behind `summarizer.client.messages.parse(...)`
is pluggable: 'anthropic' is the SDK client; 'flop' routes the same call through a FLOP compute channel
(`flopsession`) and pays in FLOP, turning work we already do into airdrop-qualifying, non-wash demand.

The seam sits at provider *selection*, not inside the summarizer — the summarizer keeps speaking
`messages.parse(**kw)` and reading `.parsed_output`, `.stop_reason`, `.id`, `.usage`, so every guard
(cost cap, hourly cap, refusal skip, error disable) and every test is unchanged. Inference is never in
the critical path: if the Flop provider is unavailable (no RPC, no miner, no sr25519), the startup smoke
fails, summaries are simply skipped, and the census is unaffected.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("agentscout.inference")

# Doc terms that mean "spendable inference is arriving" — the radar warns when they appear.
INFERENCE_KEYWORDS = ("inference", "compute", "gpu", "miner", "mining", "rail", "settle", "x402")


class InferenceUnavailable(RuntimeError):
    """The selected provider cannot run yet. Raised during smoke so the summarizer disables LLM cleanly
    (summaries are cosmetic) instead of erroring every cycle."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FlopResponse:
    """The subset of an Anthropic `ParsedMessage` the summarizer reads."""
    parsed_output: Any
    stop_reason: str
    id: str
    usage: Usage


class _FlopMessages:
    def __init__(self, provider: "FlopProvider"):
        self._p = provider

    def parse(self, **kwargs):
        """Render system + messages into one prompt, run a one-turn paid session, validate the output
        against `output_format` (a pydantic model) exactly as the SDK's structured output would."""
        session = self._p.session()          # raises InferenceUnavailable with the precise reason
        prompt = _render_prompt(kwargs.get("system"), kwargs.get("messages") or [], kwargs.get("output_format"))
        result = session.run([prompt])
        schema = kwargs.get("output_format")
        parsed = _parse_output(schema, result.output)
        return FlopResponse(parsed, "end_turn", "flop:" + result.channel_id.hex(),
                            Usage(result.input_tokens, result.output_tokens))


class FlopProvider:
    """Routes inference through the FLOP network. Honest about readiness: `session()` raises
    `InferenceUnavailable` naming the first missing piece (sr25519, RPC, miner), so nothing pretends to
    spend FLOP that cannot be spent. `model` and `.messages.parse` mirror the Anthropic client."""

    name = "flop"

    def __init__(self, settings, storage, identity=None, model: str = "flop-inference"):
        self.s = settings
        self._db = storage
        self._identity = identity
        self.model = model
        self.messages = _FlopMessages(self)
        self._chain = None
        self._key = None

    def bind_identity(self, identity) -> None:
        """The account that opens channels (our ed25519 DID key); bound at Runner.startup once loaded."""
        self._identity = identity
        self._chain = None

    def endpoint(self) -> Optional[str]:
        """Miner endpoint: `FLOP_MINER_URL`, else whatever technocore.chat/agent.json advertises
        (`endpoints.inference` / `endpoints.compute`) once it does; None until then."""
        if getattr(self.s, "flop_miner_url", ""):
            return self.s.flop_miner_url
        try:
            snap = self._db.doc_snapshot("agent.json")
            if not snap:
                return None
            card = json.loads(snap["text"])
            eps = card.get("endpoints", {}) if isinstance(card, dict) else {}
            return eps.get("inference") or eps.get("compute") or None
        except (ValueError, KeyError, TypeError):
            return None

    def session(self):
        """Build (and cache) the chain client and session key; return a ready FlopSession."""
        from . import flopkeys
        from .flopchain import ChainUnavailable, NullChain, make_chain
        from .flopsession import FlopSession, HttpMinerTransport

        miner_url = self.endpoint()
        if not miner_url:
            raise InferenceUnavailable("no Flop miner endpoint (FLOP_MINER_URL empty and agent.json advertises none)")
        if self._identity is None:
            raise InferenceUnavailable("no identity bound to the Flop provider")
        if self._chain is None:
            try:
                self._chain = make_chain(self.s, self._identity)
            except ChainUnavailable as exc:
                raise InferenceUnavailable(f"Flop chain: {exc}") from exc
        if isinstance(self._chain, NullChain):
            raise InferenceUnavailable("no Flop RPC configured (FLOP_RPC_URL empty); keeping LLM off until the testnet lands")
        try:
            now = datetime.now(timezone.utc)
            if self._key is None or self._key.age_days(now) >= float(self.s.flop_session_key_days):
                self._key = flopkeys.SessionKey.load_or_create(self.s.flop_session_key_path, now, self.s.flop_session_key_days)
        except flopkeys.KeysUnavailable as exc:
            raise InferenceUnavailable(f"Flop session key: {exc}") from exc
        transport = HttpMinerTransport(miner_url, timeout=max(30, int(getattr(self.s, "http_timeout", 12)) * 5))
        return FlopSession(self.s, self._chain, self._key, transport, self._db, flopkeys.verify)


def _render_prompt(system, messages, schema) -> str:
    parts = []
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts.extend(str(b.get("text", "")) for b in system if isinstance(b, dict))
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            content = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        parts.append(f"{m.get('role', 'user') if isinstance(m, dict) else 'user'}: {content}")
    if schema is not None and hasattr(schema, "model_json_schema"):
        parts.append("Respond with a single JSON object matching this schema, nothing else:\n"
                     + json.dumps(schema.model_json_schema(), separators=(",", ":")))
    return "\n\n".join(p for p in parts if p)


def _parse_output(schema, text: str):
    if schema is None or not hasattr(schema, "model_validate_json"):
        return text
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("flop output is not a JSON object")
    return schema.model_validate_json(raw[start:end + 1])


def make_provider(settings, storage, api_key: Optional[str], identity=None):
    """Pick the inference provider. Default 'anthropic' returns the real SDK client (unchanged
    behaviour); 'flop' returns the FlopProvider (W5). None when anthropic is chosen but no key."""
    provider = getattr(settings, "inference_provider", "anthropic")
    if provider == "flop":
        log.info("inference provider: flop (rpc=%s miner=%s)", getattr(settings, "flop_rpc_url", "") or "-",
                 getattr(settings, "flop_miner_url", "") or "agent.json")
        return FlopProvider(settings, storage, identity=identity, model=settings.model)
    if not api_key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=api_key, timeout=90.0, max_retries=2)
