"""P6 — the inference seam. AgentScout's cosmetic LLM summaries are the one place it *spends*
on inference, and the $FLOP airdrop rewards spending testnet FLOP on inference. So the provider
behind `summarizer.client.messages.parse(...)` is made pluggable: today it is Anthropic; the day
the Flop network publishes an inference endpoint (the radar watches for it), the same call routes
through Flop and pays in FLOP, turning work we already do into airdrop-qualifying activity.

The seam sits at provider *selection*, not inside the summarizer — the summarizer keeps speaking
`messages.parse(**kw)`, so every guard (cost cap, hourly cap, refusal skip, error disable) and every
test is unchanged. Inference is never in the critical path: if the chosen provider is unavailable,
the startup smoke fails, summaries are simply skipped, and the census is unaffected.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("agentscout.inference")

# Doc terms that mean "spendable inference is arriving" — the radar warns when they appear, so we
# implement FlopProvider.parse the day they do rather than discovering it late (see ingest.watch_docs).
INFERENCE_KEYWORDS = ("inference", "compute", "gpu", "miner", "mining", "rail", "settle", "x402")


class InferenceUnavailable(RuntimeError):
    """The selected provider has no endpoint to call yet. Raised during smoke so the summarizer
    disables LLM cleanly (summaries are cosmetic) instead of erroring every cycle."""


class _FlopMessages:
    def __init__(self, provider: "FlopProvider"):
        self._p = provider

    def parse(self, **kwargs):
        endpoint = self._p.endpoint()
        if endpoint is None:
            raise InferenceUnavailable(
                "no Flop inference endpoint discovered yet (agent.json advertises none); "
                "keeping LLM off until the marketplace lands")
        # ── Implement HERE the day `endpoint` is non-None (radar will have warned): ──────────────
        #   POST the request to `endpoint`, priced and paid in FLOP over the settlement rail, then
        #   return an object exposing the same attributes the summarizer reads from an Anthropic
        #   response: `.parsed_output` (matching kwargs["output_format"]), `.stop_reason`, `.id`,
        #   and `.usage` with input/output token counts. The FLOP amount spent goes to the usage
        #   ledger (a new column or a FLOP-priced row). Until then this path is unreachable because
        #   `endpoint()` is None and smoke() has already disabled the summarizer.
        raise InferenceUnavailable(f"Flop inference endpoint {endpoint} known but client not implemented yet")


class FlopProvider:
    """Routes inference through the Flop network. A deliberate, honest stub until the network
    publishes an inference API — it reports unavailable so nothing pretends to spend FLOP that
    cannot be spent. `model` and the `.messages.parse` shape mirror the Anthropic client so the
    summarizer needs no knowledge of which provider it holds."""

    name = "flop"

    def __init__(self, storage, model: str = "flop-inference"):
        self._db = storage
        self.model = model
        self.messages = _FlopMessages(self)

    def endpoint(self) -> Optional[str]:
        """The inference endpoint, once technocore.chat/agent.json advertises one. Discovered from
        the stored doc snapshot the radar already keeps; None until it exists."""
        try:
            import json
            snap = self._db.doc_snapshot("agent.json")
            if not snap:
                return None
            card = json.loads(snap["text"])
            eps = card.get("endpoints", {}) if isinstance(card, dict) else {}
            return eps.get("inference") or eps.get("compute") or None
        except (ValueError, KeyError, TypeError):
            return None


def make_provider(settings, storage, api_key: Optional[str]):
    """Pick the inference provider. Default 'anthropic' returns the real SDK client (unchanged
    behaviour); 'flop' returns the FlopProvider seam. None when anthropic is chosen but no key."""
    provider = getattr(settings, "inference_provider", "anthropic")
    if provider == "flop":
        log.info("inference provider: flop (endpoint discovery via agent.json; LLM off until it lands)")
        return FlopProvider(storage, model=settings.model)
    if not api_key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=api_key, timeout=90.0, max_retries=2)
