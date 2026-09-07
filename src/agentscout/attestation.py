"""C1 — portable reputation attestations (AGENT_COMMERCE.md, spec in C1_ATTESTATION_FORMAT.md).

A reputation attestation is a signed, pinned, REPRODUCIBLE computation over public data — not an
opinion. A verifier trusts (a) the detached Ed25519 signature proves AgentScout issued it, and
(b) recomputing the cited method over the pinned inputs reproduces the signals. It verifies with
only the object + the issuer did:key — no Technocore access — so a reputation observed here travels
to any substrate the same `did:key` subject appears on.

Two integrity properties are structural, not promised:
  * deterministic ⇒ non-purchasable — for a given (subject, asof, window) the signals/read/digest
    are a pure function of public data; the only per-issue randomness is `nonce`. You cannot buy a
    better grade; requesting one just triggers the computation.
  * negative is first-class — `band` can be "flagged"/"insufficient"; a trust layer must say "no".

No LLM anywhere: signals, band, id, and signature are all deterministic / cryptographic.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidSignature

from .census import AgentFacts
from .identity import fingerprint, public_key_from_did
from .scoring import ScoreResult, score
from .tclk import canonical

DOMAIN = "agentcommerce::attestation::v1"
METHOD = "github.com/jjmobile/agentscout/blob/main/SCORING.md@v1"

# Penalty names (from scoring.score) that mean "counterparty risk", not just "low quality".
_SERIOUS = ("contract_spam", "injection", "opaque", "broadcast")
_INSUFFICIENT_MSGS = 5          # below this we won't grade — the read is "insufficient", not "bad"
_INSUFFICIENT_CONF = 40
_ESTABLISHED_SCORE = 60
_FLAGGED_SCORE = 40             # a serious flag only makes the BAND "flagged" when the score is this low
                                # too; a stray flag on a strong agent stays established with the flag shown.


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _sha(payload: str) -> str:
    return "0x" + hashlib.sha256(payload.encode("ascii")).hexdigest()


def _att_id(core: Dict) -> str:
    """Domain-tagged sha256 over the object minus id and sig."""
    return _sha(f"{DOMAIN}|attestation|{canonical(core)}")


def _sig_payload(without_sig: Dict) -> bytes:
    """The bytes the detached signature covers: everything except `sig` (so it commits to `id`)."""
    return f"{DOMAIN}|{canonical(without_sig)}".encode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def serialize(obj: Dict) -> str:
    """The portable wire form: the exact single-line canonical bytes the id/sig cover."""
    return canonical(obj)


def daily_nonce(fp: str, day: str) -> str:
    """A per-(subject, day) nonce so a published attestation note is idempotent within a day (it
    only rewrites when the underlying signals change), while still unique across subjects and days."""
    return hashlib.sha256(f"{DOMAIN}|nonce|{fp}|{day}".encode("ascii")).hexdigest()[:16]


def _flags(r: ScoreResult) -> List[str]:
    return sorted(k for k in _SERIOUS if k in r.penalties)


def _band(f: AgentFacts, r: ScoreResult, flags: List[str]) -> str:
    # `flags` is surfaced independently of `band`, so a strong agent with a stray incident still
    # shows the flag. The band only turns "flagged" when a serious flag coincides with a low score —
    # i.e. the behaviour actually dragged the agent down — so "flagged" stays a real scam warning
    # and doesn't get pinned on the #1 agent for one contract-address mention out of hundreds.
    if flags and r.score < _FLAGGED_SCORE:
        return "flagged"
    if f.signed_msgs < _INSUFFICIENT_MSGS or r.confidence < _INSUFFICIENT_CONF:
        return "insufficient"
    return "established" if r.score >= _ESTABLISHED_SCORE else "emerging"


def attest(identity, facts: AgentFacts, asof: str, *, window_days: int = 7, method: str = METHOD,
           evidence: Optional[List[str]] = None, credence_verified: Optional[int] = None,
           multiday_pairs: Optional[int] = None, deals_completed: Optional[int] = None,
           deals_defaulted: Optional[int] = None, valid_until: Optional[str] = None,
           nonce: Optional[str] = None) -> Dict:
    """Build a signed reputation attestation about `facts.did`. Content is a pure function of
    `facts` (+ the optional commerce counters the caller has); only `nonce` is random."""
    r = score(facts)
    signals: Dict[str, object] = {
        "signed_msgs": facts.signed_msgs,
        "days_seen": facts.days_seen,
        "rooms_active": len(facts.rooms_active),
        "replies_from_others": facts.replies_raw,
        "templated_ratio": round(facts.templated_ratio, 3),
        "owned_rooms": len(facts.owned_rooms),
        "artifacts_ok": facts.artifacts_ok,
        "artifacts_total": facts.artifacts_total,
        "opaque_ratio": round(facts.opaque_ratio, 3),
        "contract_spam_msgs": facts.contract_spam_msgs,
        "injection_msgs": facts.injection_msgs,
    }
    for k, v in (("credence_verified", credence_verified), ("multiday_pairs", multiday_pairs),
                 ("deals_completed", deals_completed), ("deals_defaulted", deals_defaulted)):
        if v is not None:
            signals[k] = v                                 # commerce counters only when the caller has them
    flags = _flags(r)
    core = {
        "type": "reputation", "v": 1,
        "issuer": identity.did,
        "subject": facts.did,
        "subject_fp": facts.fp,
        "asof": asof,
        "window_days": window_days,
        "valid_until": valid_until or _iso(_parse(asof) + timedelta(days=window_days)),
        "method": method,
        "inputs_digest": _sha(canonical(signals)),
        "signals": signals,
        "read": {"score": r.score, "confidence": r.confidence, "band": _band(facts, r, flags)},
        "flags": flags,
        "evidence": list(evidence) if evidence else [],
        "disclaimer": "observed-behaviour-not-endorsement",
        "nonce": nonce or token_hex(8),
    }
    obj = dict(core)
    obj["id"] = _att_id(core)                              # id over core (no id, no sig)
    obj["sig"] = identity.sign(_sig_payload(obj))          # sig over everything incl id, excl sig
    return obj


@dataclass
class Verdict:
    authentic: bool                 # signature + id + subject_fp all check out
    expired: bool
    reason: str                     # first failure, or "ok"
    issuer: Optional[str] = None
    subject: Optional[str] = None
    band: Optional[str] = None
    flags: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.authentic and not self.expired


def verify(obj: Dict, now: Optional[datetime] = None) -> Verdict:
    """Check an attestation with only the object + the issuer did:key. No network, no LLM."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(obj, dict) or not all(k in obj for k in ("issuer", "subject", "id", "sig", "subject_fp", "valid_until")):
        return Verdict(False, False, "malformed: missing fields")
    issuer, subject = obj["issuer"], obj["subject"]
    core = {k: v for k, v in obj.items() if k not in ("id", "sig")}
    if _att_id(core) != obj["id"]:
        return Verdict(False, False, "id mismatch", issuer, subject)
    signed = {k: v for k, v in obj.items() if k != "sig"}
    try:
        public_key_from_did(issuer).verify(_b64url_decode(obj["sig"]), _sig_payload(signed))
    except (InvalidSignature, ValueError, binascii.Error):
        return Verdict(False, False, "bad signature", issuer, subject)
    if fingerprint(subject) != obj["subject_fp"]:
        return Verdict(False, False, "subject fp mismatch", issuer, subject)
    expired = _parse(obj["valid_until"]) <= now
    band = (obj.get("read") or {}).get("band")
    return Verdict(True, expired, "expired" if expired else "ok", issuer, subject, band, tuple(obj.get("flags", ())))
