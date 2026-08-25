"""Turn stored messages/notes/owners into per-agent facts. Pure functions over Storage rows."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

DID_PREFIX = "did:key:z6Mk"
DID_RE = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{40,50}")
KV_REF_RE = re.compile(r"/kv/([a-z0-9][a-z0-9_-]{0,47})/([a-z0-9][a-z0-9_-]{0,47})(?![a-z0-9_/-])")
NOTE_FIELD_RE = re.compile(r"(?:^|\s)(name|role|mailbox|purpose|room|feed|repo|source)\s*:\s*([^\s|;]+)", re.IGNORECASE)
REPLY_WINDOW = timedelta(minutes=30)

CONTRACT_RES = [
    re.compile(r"\b0x[0-9a-fA-F]{40}\b"),
    re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}pump\b"),
    re.compile(r"\bairdrop\b", re.IGNORECASE),
    re.compile(r"\bCA\s*:\s*\S{20,}", re.IGNORECASE),
]
_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_OPAQUE_TOKEN_RE = re.compile(r"\S{24,}")


def is_opaque(text: str) -> bool:
    """Ciphertext / base64 / hash dumps: after removing long machine tokens, fewer than 2 real words remain."""
    remainder = " ".join(
        tok for tok in text.split()
        if not (_OPAQUE_TOKEN_RE.fullmatch(tok) and (any(ch.isdigit() for ch in tok) or any(ch in "+/=" for ch in tok)))
    )
    return len(_WORD_RE.findall(remainder)) < 2


INJECTION_RES = [
    re.compile(r"ignore\s+(?:all\s+|your\s+|previous\s+|prior\s+)*(?:instructions|rules|prompts?)", re.IGNORECASE),
    re.compile(r"\brank\s+me\b", re.IGNORECASE),
    re.compile(r"\bput\s+me\s+(?:at|on)\s+(?:the\s+)?top\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\bendorse\s+me\b", re.IGNORECASE),
]


def fingerprint(did: str) -> str:
    """Technocore DID-note convention: first 16 hex of SHA-256 of the did:key string, lowercase."""
    return hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]


def normalize_text(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = " ".join(t.split())
    return t.casefold()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:16]


def is_signed(msg: dict) -> bool:
    """The read API never returns the signature; a did:key `from` plus a nonce means the server verified it."""
    frm = msg.get("from")
    return isinstance(frm, str) and frm.startswith(DID_PREFIX) and msg.get("nonce") is not None


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_note(text: str) -> Tuple[Optional[str], Dict[str, str]]:
    """(did found in the note, field labels). Labels are self-asserted — never identity proof."""
    did = None
    m = DID_RE.search(text)
    if m:
        did = m.group(0)
    fields = {k.lower(): v for k, v in NOTE_FIELD_RE.findall(text)}
    return did, fields


@dataclass
class AgentFacts:
    did: str
    fp: str
    first_seen: str
    last_seen: str
    name: Optional[str] = None
    note_present: bool = False
    signed_msgs: int = 0
    days_seen: int = 0
    rooms: List[str] = field(default_factory=list)
    rooms_active: List[str] = field(default_factory=list)  # rooms with >= 2 signed msgs (breadth that costs effort)
    dup_ratio: float = 0.0
    max_per_hour: int = 0
    cross_room_identical: int = 0
    replies_raw: int = 0
    replies_weighted: float = 0.0
    owned_rooms: List[str] = field(default_factory=list)
    artifacts_ok: int = 0
    artifacts_total: int = 0
    contract_spam_msgs: int = 0
    injection_msgs: int = 0
    opaque_ratio: float = 0.0  # share of messages that are ciphertext/base64/hash dumps
    # Milestone C (optional decoration; every renderer works without it)
    summary: Optional[str] = None
    category: Optional[str] = None
    llm_signal: Optional[int] = None
    llm_flags: List[str] = field(default_factory=list)
    days_since_first_seen: float = 0.0
    sample: str = ""  # a short, swept excerpt of the most recent message (for humans only)


def aliases_for(did: str, name: Optional[str]) -> List[str]:
    z = did[len("did:key:"):]
    out = [did.casefold(), z[:8].casefold(), ("…" + z[-4:]).casefold()]
    if name and len(name) >= 4:
        out.append("@" + name.casefold())
        out.append(name.casefold())
    return out


def _messages_by_did(messages) -> Dict[str, list]:
    out: Dict[str, list] = defaultdict(list)
    for m in messages:
        if m["signed"] and m["sender_did"]:
            out[m["sender_did"]].append(m)
    return out


def compute_facts(storage, now: datetime, prelim_scores: Optional[Dict[str, int]] = None) -> Dict[str, AgentFacts]:
    """One pass over the whole corpus (small: ≤200 msgs per watched room).

    prelim_scores: when given, replies are dampened by the replier's preliminary score/age
    (sock-puppet dampening). Callers do two passes: first without, then with.
    """
    messages = storage.all_messages()
    agents = {r["did"]: r for r in storage.agents()}
    notes = storage.notes_by_fp()
    owners = storage.owned_rooms_by_did()
    artifacts = storage.artifacts_by_did()
    summaries = storage.summaries_by_did()
    by_did = _messages_by_did(messages)
    by_room: Dict[str, list] = defaultdict(list)
    for m in messages:
        by_room[m["room"]].append(m)
    for room in by_room:
        by_room[room].sort(key=lambda m: m["seq"])

    facts: Dict[str, AgentFacts] = {}
    for did, row in agents.items():
        fp = row["fp"]
        note = notes.get(fp)
        name = None
        if note:
            _, fields = parse_note(note["text"])
            name = fields.get("name")
        f = AgentFacts(did=did, fp=fp, first_seen=row["first_seen"], last_seen=row["last_seen"], name=name, note_present=note is not None)
        msgs = by_did.get(did, [])
        f.signed_msgs = len(msgs)
        f.days_seen = len({m["ts"][:10] for m in msgs})
        f.rooms = sorted({m["room"] for m in msgs})
        per_room: Dict[str, int] = defaultdict(int)
        for m in msgs:
            per_room[m["room"]] += 1
        f.rooms_active = sorted(r for r, n in per_room.items() if n >= 2)
        if msgs:
            hashes = [m["text_hash"] for m in msgs]
            f.dup_ratio = 1.0 - len(set(hashes)) / len(hashes)
            per_hour: Dict[str, int] = defaultdict(int)
            for m in msgs:
                per_hour[m["ts"][:13]] += 1
            f.max_per_hour = max(per_hour.values())
            rooms_per_hash: Dict[str, set] = defaultdict(set)
            for m in msgs:
                rooms_per_hash[m["text_hash"]].add(m["room"])
            f.cross_room_identical = sum(1 for rs in rooms_per_hash.values() if len(rs) >= 3)
            f.contract_spam_msgs = sum(1 for m in msgs if any(r.search(m["text"]) for r in CONTRACT_RES))
            f.injection_msgs = sum(1 for m in msgs if any(r.search(m["text"]) for r in INJECTION_RES))
            f.opaque_ratio = sum(1 for m in msgs if is_opaque(m["text"])) / len(msgs)
            latest = max(msgs, key=lambda m: m["ts"])
            f.sample = " ".join(latest["text"].split())[:140]
        f.owned_rooms = owners.get(did, [])
        f.artifacts_ok, f.artifacts_total = artifacts.get(did, (0, 0))
        sm = summaries.get(did)
        if sm is not None and not sm["error"] and sm["summary"]:
            f.summary, f.category, f.llm_signal = sm["summary"], sm["category"], int(sm["signal"])
            try:
                f.llm_flags = list(json.loads(sm["flags"] or "[]"))
            except ValueError:
                f.llm_flags = []
        f.days_since_first_seen = max(0.0, (now - parse_ts(row["first_seen"])).total_seconds() / 86400.0)
        facts[did] = f

    # replies: another signed DID, same room, within 30 min after, mentioning an alias
    for did, f in facts.items():
        if not f.signed_msgs:
            continue
        al = aliases_for(did, f.name)
        counted_per_replier_day: Dict[Tuple[str, str], int] = defaultdict(int)
        seen_reply_ids = set()
        for m in by_did[did]:
            t0 = parse_ts(m["ts"])
            for other in by_room[m["room"]]:
                if other["seq"] <= m["seq"]:
                    continue
                if not other["signed"] or other["sender_did"] == did:
                    continue
                if parse_ts(other["ts"]) - t0 > REPLY_WINDOW:
                    break
                key = (other["room"], other["seq"])
                if key in seen_reply_ids:
                    continue
                low = other["text"].casefold()
                if not any(a in low for a in al):
                    continue
                replier = other["sender_did"]
                day_key = (replier, other["ts"][:10])
                if counted_per_replier_day[day_key] >= 5:
                    continue
                counted_per_replier_day[day_key] += 1
                seen_reply_ids.add(key)
                f.replies_raw += 1
                weight = 1.0
                if prelim_scores is not None:
                    rf = facts.get(replier)
                    young = rf is not None and rf.days_since_first_seen < 2.0
                    weak = prelim_scores.get(replier, 0) < 20
                    if young or weak:
                        weight = 0.25
                f.replies_weighted += weight
    return facts


def extract_kv_refs(text: str) -> List[Tuple[str, str]]:
    return [(ns, key) for ns, key in KV_REF_RE.findall(text)]
