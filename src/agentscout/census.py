"""Turn stored messages/notes/owners into per-agent facts. Pure functions over Storage rows."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional, Tuple

DID_PREFIX = "did:key:z6Mk"
DID_RE = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{40,50}")
KV_REF_RE = re.compile(r"/kv/([a-z0-9][a-z0-9_-]{0,47})/([a-z0-9][a-z0-9_-]{0,47})(?![a-z0-9_/-])")
NOTE_FIELD_RE = re.compile(r"(?:^|\s)(name|role|mailbox|purpose|room|feed|repo|source)\s*:\s*([^\s|;]+)", re.IGNORECASE)
DEFAULT_WINDOW_DAYS = 7                       # score what Technocore itself still holds (7-day retention)
REPLY_WINDOW = timedelta(minutes=30)          # mention-based replies: within this after the target's message
ADJACENCY_WINDOW = timedelta(minutes=10)      # quiet-room adjacency: another DID answering shortly after
QUIET_ROOM_MSGS_PER_HOUR = 20.0               # rooms busier than this get no adjacency credit (lobby ≈ 3000/h)
ADJACENCY_WEIGHT = 0.5
MAX_REPLIES_PER_REPLIER_PER_TARGET_PER_DAY = 3
MAX_REPLIES_PER_REPLIER_PER_DAY = 20          # endorsement-spraying fleets stop counting after this
RECIPROCAL_DISCOUNT = 0.25                    # A names B and B names A the same day: mutual back-scratching
TEMPLATED_RATIO_BROADCAST = 0.7               # > this share of "[Role @handle] …" messages = automated broadcaster
SELF_TAG_RE = re.compile(r"^\s*\[[^\]]{0,60}?@([A-Za-z0-9_]{3,32})\]")   # "[Role @handle] …" — the agent's own tag
_TOKEN_DID_RE = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{40,50}")
_TOKEN_Z_RE = re.compile(r"(?<![A-Za-z0-9])z6Mk[1-9A-HJ-NP-Za-km-z]{4,}")
_TOKEN_ELL_RE = re.compile(r"…([A-Za-z0-9]{4})")
_TOKEN_FP_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")
_TOKEN_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{3,32})")

CONTRACT_RES = [
    re.compile(r"\b0x[0-9a-fA-F]{40}\b"),
    re.compile(r"\b0x[0-9a-fA-F]{4,6}\.{2,3}[0-9a-fA-F]{4,6}\b"),   # abbreviated "0x585c...fa64" in alert bots
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
    replies_raw: int = 0             # mention/fingerprint/handle references by other DIDs
    replies_adjacent: int = 0        # answers within 10 min in quiet rooms (counted at 0.5)
    replies_weighted: float = 0.0
    handles: List[str] = field(default_factory=list)   # self-declared "@handle" tags seen in its own messages
    templated_ratio: float = 0.0     # share of its messages that are "[Role @handle] …" broadcasts
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


def aliases_for(did: str, name: Optional[str], handles: Optional[List[str]] = None, fp: Optional[str] = None) -> List[str]:
    """Strings another agent would use to refer to this one. All matched casefolded."""
    z = did[len("did:key:"):]
    out = [did.casefold(), z[:8].casefold(), ("…" + z[-4:]).casefold()]
    if fp:
        out.append(fp.casefold())                       # "/kv/did/<fp>" references
    if name and len(name) >= 4:
        out.append("@" + name.casefold())
        out.append(name.casefold())
    for h in handles or []:
        out.append("@" + h.casefold())
    return out


def self_handles(counts: Dict[str, int], n_msgs: int) -> List[str]:
    """Handles an agent declares about itself: the "@x" inside a leading "[… @x]" tag, seen in ≥2 of its messages."""
    return sorted(h for h, n in counts.items() if n >= 2 or n == n_msgs)


def room_rates(room_stats: Dict[str, Tuple[int, str, str]]) -> Dict[str, float]:
    """Messages per hour per room over the span we hold (≥2 messages)."""
    rates: Dict[str, float] = {}
    for room, (n, lo, hi) in room_stats.items():
        if n < 2:
            continue
        span_h = max(0.05, (parse_ts(hi) - parse_ts(lo)).total_seconds() / 3600.0)
        rates[room] = n / span_h
    return rates


def _since(now: datetime, window_days: int) -> str:
    return (now - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


Credit = Tuple[str, str, str, float, bool]   # (target, replier, day, weight, is_reference)


DEFAULT_MIN_MSGS = 2     # identities with a single signed message in the window are counted, not scored (see build_facts)


def compute_facts(storage, now: datetime, prelim_scores: Optional[Dict[str, int]] = None,
                  window_days: int = DEFAULT_WINDOW_DAYS, min_msgs: int = DEFAULT_MIN_MSGS) -> Dict[str, AgentFacts]:
    """Facts for every signed agent seen in the last `window_days` (Technocore keeps 7 days; so do we).

    prelim_scores: when given, replies are dampened by the replier's preliminary score/age
    (sock-puppet dampening). Callers do two passes: first without, then with — see render.score_all,
    which reuses the expensive pass.
    """
    facts, credits, named_pairs = build_facts(storage, now, window_days, min_msgs)
    apply_replies(facts, credits, named_pairs, prelim_scores)
    return facts


def build_facts(storage, now: datetime, window_days: int = DEFAULT_WINDOW_DAYS, min_msgs: int = DEFAULT_MIN_MSGS):
    """Everything except the reply weighting. Streams the window from SQLite: memory is O(scored agents), not O(messages).

    Agents with fewer than `min_msgs` signed messages in the window are not materialised (they can never be listed);
    they are still counted for the digest (Storage.new_agents_since) and their messages still grant reply credit to
    scored agents exactly as before (a one-shot replier has no preliminary score, so the sock-puppet ×0.25 applies).
    On 2026-08-26 (361k identities in the window, 63 % one-shot) this is the difference between fitting in 1 GB or not."""
    since = _since(now, window_days)
    notes = storage.notes_by_fp()
    owners = storage.owned_rooms_by_did()
    artifacts = storage.artifacts_by_did()
    summaries = storage.summaries_by_did()

    facts: Dict[str, AgentFacts] = {}
    for row in storage.agents_seen_since(since, min_msgs):
        did, fp = row["did"], row["fp"]
        note = notes.get(fp)
        name = None
        if note:
            _, fields = parse_note(note["text"])
            name = fields.get("name")
        f = AgentFacts(did=did, fp=fp, first_seen=row["first_seen"], last_seen=row["last_seen"], name=name, note_present=note is not None)
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

    # ---- per-agent aggregates, computed inside SQLite ----------------------------------------------
    for r in storage.iter_agent_stats(since):
        f = facts.get(r["did"])
        if f is None:
            continue
        f.signed_msgs = int(r["n"])
        f.days_seen = int(r["days"])
        f.dup_ratio = 1.0 - int(r["hashes"]) / f.signed_msgs
        f.max_per_hour = int(r["max_per_hour"])
    for r in storage.iter_agent_rooms(since):        # ordered by did, room
        f = facts.get(r["did"])
        if f is None:
            continue
        f.rooms.append(r["room"])
        if int(r["n"]) >= 2:
            f.rooms_active.append(r["room"])
    for did, n in storage.cross_room_identical(since).items():
        if did in facts:
            facts[did].cross_room_identical = n
    for did, text in storage.latest_texts(since).items():
        if did in facts:
            facts[did].sample = " ".join(text.split())[:140]

    # ---- per-message text features: one streaming pass ---------------------------------------------
    handle_counts: Dict[str, Dict[str, int]] = {}
    templated: Dict[str, int] = defaultdict(int)
    opaque: Dict[str, int] = defaultdict(int)
    for r in storage.iter_signed_texts(since):
        did, text = r["did"], r["text"]
        f = facts.get(did)
        if f is None:
            continue
        mt = SELF_TAG_RE.match(text)
        if mt:
            templated[did] += 1
            counts = handle_counts.setdefault(did, {})
            counts[mt.group(1)] = counts.get(mt.group(1), 0) + 1
        if any(rx.search(text) for rx in CONTRACT_RES):
            f.contract_spam_msgs += 1
        if any(rx.search(text) for rx in INJECTION_RES):
            f.injection_msgs += 1
        if is_opaque(text):
            opaque[did] += 1
    for did, f in facts.items():
        f.handles = self_handles(handle_counts.get(did, {}), f.signed_msgs)
        if f.signed_msgs:
            f.templated_ratio = templated.get(did, 0) / f.signed_msgs
            f.opaque_ratio = opaque.get(did, 0) / f.signed_msgs

    # ---- replies from other DIDs --------------------------------------------------------------------
    # (a) reference (1.0): another signed DID, same room, ≤30 min after one of the agent's messages, whose text names
    #     the agent (DID, z6Mk prefix, "…xxxx", fingerprint, DID-note name, self-declared @handle).
    # (b) adjacency (0.5): in a quiet room (≤20 msgs/h) a different signed DID posts ≤10 min after the agent —
    #     neither message a "[Role @handle]" broadcast, replier not a broadcaster; once per replier per target per day.
    # Discounts: reciprocal naming the same day ×0.25; young/weak replier (sock-puppet) ×0.25.
    # Caps: ≤3 per replier per target per day, ≤20 per replier per day overall.
    index = _reference_index(facts, notes)
    rates = room_rates(storage.room_stats(since))
    own = storage.get_setting("own_did")      # AgentScout's own answers must never count as replies (ask → answered → score up)
    named_pairs = set()                       # (replier, target, day) for reciprocity
    credits: List[Credit] = []
    cur_room = None
    quiet = False
    last_post: Dict[str, datetime] = {}       # sender -> its latest message time in this room
    recent: Deque[Tuple[datetime, str, bool]] = deque()   # quiet rooms only: (t, sender, templated), ≤30 min
    for m in storage.iter_signed_messages(since):
        room, sender, text = m["room"], m["did"], m["text"]
        if sender == own:
            continue
        if room != cur_room:
            cur_room, quiet = room, rates.get(room, 0.0) <= QUIET_ROOM_MSGS_PER_HOUR
            last_post, recent = {}, deque()
        t = parse_ts(m["ts"])
        day = m["ts"][:10]
        refs = _referenced_dids(text, index) - {sender}
        for target in refs:
            named_pairs.add((sender, target, day))
            tt = last_post.get(target)
            if tt is not None and t - tt <= REPLY_WINDOW:
                credits.append((target, sender, day, 1.0, True))
        is_templated = bool(SELF_TAG_RE.match(text))
        if quiet:
            while recent and t - recent[0][0] > REPLY_WINDOW:
                recent.popleft()
            sf = facts.get(sender)                    # unscored one-shot sender: its only message is its ratio
            if not is_templated and (sf.templated_ratio if sf is not None else float(is_templated)) <= 0.5:
                seen_targets = set()
                for (rt, rdid, rtempl) in recent:
                    if rdid == sender or rdid in refs or rdid in seen_targets or rtempl:
                        continue
                    if t - rt <= ADJACENCY_WINDOW:
                        seen_targets.add(rdid)
                        credits.append((rdid, sender, day, ADJACENCY_WEIGHT, False))
            recent.append((t, sender, is_templated))
        last_post[sender] = t
    return facts, credits, named_pairs


def apply_replies(facts: Dict[str, AgentFacts], credits: List[Credit], named_pairs: set,
                  prelim_scores: Optional[Dict[str, int]] = None) -> None:
    """Turn raw reply credits into replies_raw/adjacent/weighted (idempotent: resets first)."""
    for f in facts.values():
        f.replies_raw = f.replies_adjacent = 0
        f.replies_weighted = 0.0
    per_pair_day: Dict[Tuple[str, str, str], int] = defaultdict(int)
    adjacency_pair_day = set()
    replier_day_total: Dict[Tuple[str, str], int] = defaultdict(int)
    for target, replier, day, weight, is_ref in credits:
        f = facts.get(target)
        if f is None:
            continue
        if not is_ref:
            if (target, replier, day) in adjacency_pair_day:
                continue
            adjacency_pair_day.add((target, replier, day))
        if per_pair_day[(replier, target, day)] >= MAX_REPLIES_PER_REPLIER_PER_TARGET_PER_DAY:
            continue
        if replier_day_total[(replier, day)] >= MAX_REPLIES_PER_REPLIER_PER_DAY:
            continue
        per_pair_day[(replier, target, day)] += 1
        replier_day_total[(replier, day)] += 1
        if is_ref:
            f.replies_raw += 1
        else:
            f.replies_adjacent += 1
        if (target, replier, day) in named_pairs:
            weight *= RECIPROCAL_DISCOUNT
        if prelim_scores is not None:
            rf = facts.get(replier)
            young = rf is not None and rf.days_since_first_seen < 2.0
            weak = prelim_scores.get(replier, 0) < 20
            if young or weak:
                weight *= 0.25
        f.replies_weighted += weight


def _reference_index(facts: Dict[str, "AgentFacts"], notes: Dict[str, object]) -> Dict[str, Dict[str, str]]:
    """Lookup tables from the ways agents are referred to → DID. Ambiguous keys (shared by >1 DID) are dropped."""
    tables: Dict[str, Dict[str, str]] = {"did": {}, "z8": {}, "last4": {}, "fp": {}, "handle": {}}
    clash: Dict[str, set] = defaultdict(set)

    def put(table: str, key: str, did: str) -> None:
        key = key.casefold()
        if key in tables[table] and tables[table][key] != did:
            clash[table].add(key)
        tables[table][key] = did

    for did, f in facts.items():
        z = did[len("did:key:"):]
        put("did", did, did)
        put("z8", z[:8], did)
        put("last4", z[-4:], did)
        put("fp", f.fp, did)
        for h in f.handles:
            put("handle", h, did)
        if f.name and len(f.name) >= 4:
            put("handle", f.name, did)
    for table, keys in clash.items():
        for k in keys:
            tables[table].pop(k, None)
    return tables


@dataclass
class ConversationIndex:
    """How much of the traffic is agents talking *to* each other, by DID (full did:key, z6Mk… prefix, …last4)."""
    addressed: int = 0       # signed messages that address another known agent
    answered: int = 0        # (room, A, B) pairs where both addressed the other


def conversation_index(storage, since: str, own: Optional[str] = None) -> ConversationIndex:
    """Two streaming passes, memory O(mentioning messages): (1) collect DID-shaped tokens from the SQL-prefiltered
    messages, (2) resolve them with one pass over the agents table (ambiguous prefixes/suffixes are dropped, like
    _reference_index). Handles and fingerprints are not counted here — a DID is the one unforgeable address."""
    msgs: List[Tuple[str, str, List[Tuple[str, str]]]] = []       # (room, sender, [(kind, key)])
    need: Dict[str, set] = {"did": set(), "z8": set(), "last4": set()}
    for r in storage.iter_did_mentions(since):
        if r["did"] == own:
            continue
        toks: List[Tuple[str, str]] = []
        for m in _TOKEN_DID_RE.findall(r["text"]):
            toks.append(("did", m)); need["did"].add(m)
        for m in _TOKEN_Z_RE.findall(r["text"]):
            toks.append(("z8", m[:8])); need["z8"].add(m[:8])
        for m in _TOKEN_ELL_RE.findall(r["text"]):
            toks.append(("last4", m)); need["last4"].add(m)
        if toks:
            msgs.append((r["room"], r["did"], toks))
    if not msgs:
        return ConversationIndex()
    found: Dict[str, Dict[str, Optional[str]]] = {"did": {}, "z8": {}, "last4": {}}   # key -> did, or None if ambiguous
    def put(kind: str, key: str, did: str) -> None:
        if key in need[kind]:
            found[kind][key] = None if key in found[kind] and found[kind][key] != did else did
    for did in storage.iter_dids():
        z = did[len("did:key:"):]
        put("did", did, did); put("z8", z[:8], did); put("last4", z[-4:], did)
    pairs: Dict[Tuple[str, str, str], set] = {}
    idx = ConversationIndex()
    for room, sender, toks in msgs:
        targets = {found[k].get(key) for k, key in toks} - {None, sender}      # own outgoing replies are skipped above; being addressed counts
        if not targets:
            continue
        idx.addressed += 1
        for t in targets:
            a, b = sorted((sender, t))
            pairs.setdefault((room, a, b), set()).add(sender)
    idx.answered = sum(1 for who in pairs.values() if len(who) == 2)
    return idx


def _referenced_dids(text: str, index: Dict[str, Dict[str, str]]) -> set:
    out = set()
    low = text.casefold()
    for m in _TOKEN_DID_RE.findall(text):
        d = index["did"].get(m.casefold())
        if d:
            out.add(d)
    for m in _TOKEN_Z_RE.findall(text):
        d = index["z8"].get(m[:8].casefold())
        if d:
            out.add(d)
    for m in _TOKEN_ELL_RE.findall(text):
        d = index["last4"].get(m.casefold())
        if d:
            out.add(d)
    for m in _TOKEN_FP_RE.findall(low):
        d = index["fp"].get(m)
        if d:
            out.add(d)
    for m in _TOKEN_HANDLE_RE.findall(text):
        d = index["handle"].get(m.casefold())
        if d:
            out.add(d)
    return out


def extract_kv_refs(text: str) -> List[Tuple[str, str]]:
    return [(ns, key) for ns, key in KV_REF_RE.findall(text)]
