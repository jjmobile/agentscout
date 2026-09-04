"""Milestone C: one Claude call per qualified agent → schema-validated one-line summary + category + signal.

Claude is never in the critical path: every list, digest and reply renders without this module.
Cost guard, hourly cap and a startup smoke check all sit in front of every call.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from . import formatter
from .census import AgentFacts
from .config import Settings
from .storage import Storage

log = logging.getLogger("agentscout.summarizer")

CATEGORIES = ("infra", "trading-crypto", "research-security", "tooling-libraries", "social-community",
              "marketing-token", "art-games", "unknown")
FLAGS = ("spam", "self-promotion", "injection-attempt", "impersonation-claim", "none")
DATA_DIR = Path(__file__).parent / "data"
EVIDENCE_MSGS = 12
EVIDENCE_MSG_CHARS = 220


class AgentSummary(BaseModel):
    summary: str = Field(max_length=200)
    category: Literal["infra", "trading-crypto", "research-security", "tooling-libraries", "social-community",
                      "marketing-token", "art-games", "unknown"]
    signal: int = Field(ge=0, le=99)
    rationale: str = Field(max_length=240)
    flags: List[Literal["spam", "self-promotion", "injection-attempt", "impersonation-claim", "none"]]


class SmokeCheck(BaseModel):
    ok: bool
    word: str = Field(max_length=20)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_pricing() -> Dict[str, Dict[str, float]]:
    return json.loads((DATA_DIR / "pricing.json").read_text())


def load_system_prompt() -> str:
    return (DATA_DIR / "system_prompt.txt").read_text().strip()


def estimate_usd(model: str, pricing: dict, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    p = pricing.get(model) or pricing["_default"]
    return (input_tokens * p["input"] + output_tokens * p["output"] + cache_read * p["cache_read"] + cache_write * p["cache_write"]) / 1_000_000


def qualifies(f: AgentFacts) -> bool:
    return (f.signed_msgs >= 3 and f.days_seen >= 2) or bool(f.owned_rooms) or f.artifacts_ok >= 1


def build_evidence(f: AgentFacts, messages: List[dict], note_text: Optional[str]) -> str:
    """Delimited, swept, size-bounded. Deterministic numbers first; raw strings clearly marked as untrusted."""
    lines = [
        "<facts>",
        f"fp={f.fp} signed_msgs={f.signed_msgs} days_seen={f.days_seen} rooms={len(f.rooms)} rooms_active={len(f.rooms_active)} "
        f"replies_from_others={f.replies_raw} owned_rooms={len(f.owned_rooms)} artifacts_resolving={f.artifacts_ok}/{f.artifacts_total} "
        f"dup_ratio={f.dup_ratio:.2f} opaque_ratio={f.opaque_ratio:.2f} contract_spam_msgs={f.contract_spam_msgs} injection_msgs={f.injection_msgs}",
        "</facts>",
        "<evidence>",
        f"rooms: {', '.join(f.rooms[:12])}",
    ]
    if note_text:
        lines.append(f"did_note: {formatter.sweep(note_text)[:400]}")
    for m in messages[:EVIDENCE_MSGS]:
        lines.append(f"[{m['ts'][:16]}Z {m['room']}] {formatter.sweep(m['text'])[:EVIDENCE_MSG_CHARS]}")
    lines.append("</evidence>")
    return "\n".join(lines)


class Summarizer:
    def __init__(self, settings: Settings, storage: Storage, client, notify=None, pricing: Optional[dict] = None,
                 system_prompt: Optional[str] = None):
        self.s = settings
        self.db = storage
        self.client = client  # anthropic.Anthropic or a test double with .messages.parse(**kw)
        self.notify = notify
        self.pricing = pricing or load_pricing()
        self.system_prompt = system_prompt or load_system_prompt()
        self.enabled = client is not None and settings.llm_enabled
        self.disabled_reason: Optional[str] = None

    # ---- guards ------------------------------------------------------------------------------------
    def smoke(self) -> bool:
        """One tiny structured call. Any failure disables LLM features for this run — never the agent."""
        if not self.enabled:
            return False
        try:
            resp = self._parse(SmokeCheck, "Reply with ok=true and word='ready'.", max_tokens=64)
            out = resp.parsed_output
            self._ledger("smoke", None, resp, "OK", datetime.now(timezone.utc))
            if not (out and out.ok):
                raise ValueError("unexpected smoke output")
            log.info("claude smoke check passed (model %s)", self.s.model)
            return True
        except Exception as exc:  # noqa: BLE001
            self.disable(f"smoke check failed: {exc.__class__.__name__}: {str(exc)[:160]}")
            return False

    def disable(self, reason: str) -> None:
        self.enabled = False
        self.disabled_reason = reason
        log.error("LLM summaries disabled: %s", reason)

    def spent_today_usd(self, now: datetime) -> float:
        return self.db.usage_usd_since(now.strftime("%Y-%m-%dT00:00:00Z"))

    def cost_guard_ok(self, now: datetime) -> bool:
        if not self.s.cost_guard_enabled:
            return True
        spent = self.spent_today_usd(now)
        if spent >= self.s.max_daily_cost_usd:
            if self.db.get_setting("cost_guard_notified") != now.strftime("%Y-%m-%d"):
                self.db.set_setting("cost_guard_notified", now.strftime("%Y-%m-%d"))
                log.warning("cost guard reached: $%.2f of $%.2f today — no more LLM calls until UTC midnight", spent, self.s.max_daily_cost_usd)
            return False
        return True

    def hourly_ok(self, now: datetime) -> bool:
        return self.db.summaries_since(iso(now - timedelta(hours=1))) < self.s.max_summaries_per_hour

    # ---- work ------------------------------------------------------------------------------------
    def due(self, facts: Dict[str, AgentFacts], now: datetime, priority: Optional[set] = None) -> List[str]:
        stale_before = iso(now - timedelta(days=self.s.resummary_days))
        existing = self.db.summaries_by_did()
        out = []
        for did, f in facts.items():
            if not qualifies(f):
                continue
            row = existing.get(did)
            if row is None or row["created_at"] < stale_before:
                out.append(did)
        # agents shown in the lists first (that is where a missing summary is visible), then most active
        pri = priority or set()
        out.sort(key=lambda d: (d in pri, facts[d].signed_msgs, facts[d].last_seen), reverse=True)
        return out

    def tick(self, facts: Dict[str, AgentFacts], now: datetime, priority: Optional[set] = None) -> int:
        if not self.enabled:
            return 0
        done = 0
        for did in self.due(facts, now, priority)[: self.s.summaries_per_cycle]:
            if not self.cost_guard_ok(now) or not self.hourly_ok(now):
                break
            self.summarize(did, facts[did], now)
            done += 1
        return done

    def summarize(self, did: str, f: AgentFacts, now: datetime) -> str:
        msgs = self.db.recent_messages_for(did, EVIDENCE_MSGS)
        note = self.db.note_for_fp(f.fp)
        evidence = build_evidence(f, msgs, note["text"] if note else None)
        try:
            resp = self._parse(AgentSummary, evidence, max_tokens=self.s.max_tokens)
        except Exception as exc:  # noqa: BLE001 — SDK errors are many; none may kill the loop
            name = exc.__class__.__name__
            status = getattr(exc, "status_code", None)
            self.db.record_summary_error(did, f"{name} {status or ''}".strip(), iso(now))
            self.db.usage_insert(iso(now), "summary", did, self.s.model, 0, 0, 0, 0, 0.0, f"ERROR {name}")
            if name in ("AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError"):
                self.disable(f"{name}: {str(exc)[:160]}")
            else:
                log.warning("summary for %s failed: %s", f.fp, name)
            return "ERROR"
        if getattr(resp, "stop_reason", None) == "refusal" or resp.parsed_output is None:
            self._ledger("summary", did, resp, "SKIPPED_REFUSAL", now)
            self.db.record_summary_error(did, "refusal", iso(now))
            log.info("summary for %s skipped (refusal)", f.fp)
            return "SKIPPED_REFUSAL"
        out: AgentSummary = resp.parsed_output
        flags = [x for x in out.flags if x != "none"]
        self.db.upsert_summary(did, iso(now), self.s.model, formatter.sanitize_label(out.summary, 160), out.category,
                               min(99, max(0, int(out.signal))), formatter.sanitize_label(out.rationale, 200), flags,
                               getattr(resp, "id", None))
        usd = self._ledger("summary", did, resp, "OK", now)
        log.info("summary %s: %s / signal %d / %s ($%.4f)", f.fp, out.category, out.signal, ",".join(flags) or "-", usd)
        return "OK"

    # ---- SDK ---------------------------------------------------------------------------------------
    def _parse(self, schema, user_text: str, max_tokens: int):
        kwargs = dict(
            model=self.s.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_text}],
            output_format=schema,
        )
        if self.s.effort:
            kwargs["output_config"] = {"effort": self.s.effort}
        return self.client.messages.parse(**kwargs)

    def _ledger(self, purpose: str, did: Optional[str], resp, status: str, now: datetime) -> float:
        u = getattr(resp, "usage", None)
        it = int(getattr(u, "input_tokens", 0) or 0)
        ot = int(getattr(u, "output_tokens", 0) or 0)
        cr = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        cw = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        usd = estimate_usd(self.s.model, self.pricing, it, ot, cr, cw)
        self.db.usage_insert(iso(now), purpose, did, self.s.model, it, ot, cr, cw, usd, status)
        return usd


