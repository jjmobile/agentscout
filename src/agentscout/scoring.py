"""Deterministic score + confidence. Everything here is documented in SCORING.md; keep them in sync."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict

from .census import AgentFacts


@dataclass(frozen=True)
class Weights:
    active_days: int = 30      # min(days_seen, 14) / 14
    rooms: int = 15            # min(rooms with >= 2 msgs, 6) / 6
    replies: int = 30          # min(replies_weighted, 10) / 10
    artifacts: int = 25        # min(2*min(owned_rooms,1) + artifacts_ok, 5) / 5
    note_fields: int = 0       # world-writable → label only

    def total(self) -> int:
        return self.active_days + self.rooms + self.replies + self.artifacts + self.note_fields


@dataclass(frozen=True)
class Penalties:
    duplicates: int = 25          # dup_ratio > 0.5
    burst: int = 15               # > 30 msgs in one UTC hour
    cross_room: int = 15          # same text in ≥ 3 rooms
    contract_spam: int = 20       # any contract-address / airdrop pattern
    injection: int = 20           # role-override / rank-me phrases
    opaque: int = 20              # ≥4 msgs and >50 % ciphertext/base64/hash dumps


DEFAULT_WEIGHTS = Weights()
DEFAULT_PENALTIES = Penalties()


@dataclass
class ScoreResult:
    score: int
    confidence: int
    components: Dict[str, float]
    penalties: Dict[str, int]

    def as_dict(self) -> dict:
        return {"score": self.score, "confidence": self.confidence, "components": self.components, "penalties": self.penalties}


def _cap(x: float, cap: float) -> float:
    return min(max(x, 0.0), cap) / cap


def score(f: AgentFacts, w: Weights = DEFAULT_WEIGHTS, p: Penalties = DEFAULT_PENALTIES) -> ScoreResult:
    assert w.total() == 100, "weights must sum to 100 (LLM blending rescales later, Milestone C)"
    comps = {
        "active_days": round(w.active_days * _cap(f.days_seen, 14), 2),
        "rooms": round(w.rooms * _cap(len(f.rooms_active), 6), 2),
        "replies": round(w.replies * _cap(f.replies_weighted, 10), 2),
        "artifacts": round(w.artifacts * _cap(2 * min(len(f.owned_rooms), 1) + f.artifacts_ok, 5), 2),
        "note_fields": 0.0,
    }
    pens: Dict[str, int] = {}
    if f.signed_msgs >= 4 and f.dup_ratio > 0.5:
        pens["duplicates"] = p.duplicates
    if f.max_per_hour > 30:
        pens["burst"] = p.burst
    if f.cross_room_identical >= 1:
        pens["cross_room"] = p.cross_room
    if f.contract_spam_msgs >= 1:
        pens["contract_spam"] = p.contract_spam
    if f.injection_msgs >= 1:
        pens["injection"] = p.injection
    if f.signed_msgs >= 4 and f.opaque_ratio > 0.5:
        pens["opaque"] = p.opaque
    raw = sum(comps.values()) - sum(pens.values())
    s = int(round(min(99.0, max(0.0, raw))))
    return ScoreResult(score=s, confidence=confidence(f), components=comps, penalties=pens)


def confidence(f: AgentFacts) -> int:
    """How well-observed the agent is — independent of how good it looks."""
    c = (
        25 * _cap(f.days_seen, 4)
        + 25 * _cap(f.signed_msgs, 20)
        + 25 * _cap(len(f.rooms), 3)
        + 24 * _cap(f.days_since_first_seen, 7)
    )
    return int(round(min(99.0, max(0.0, c))))


def weights_doc() -> dict:
    return {"weights": asdict(DEFAULT_WEIGHTS), "penalties": asdict(DEFAULT_PENALTIES)}
