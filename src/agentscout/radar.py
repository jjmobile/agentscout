"""Protocol Radar — what changed in Technocore's own docs (llms.txt + /.well-known/agent.json).

Deterministic diff of the two documents every agent depends on; no model involved. The watcher (ingest.watch_docs)
already re-reads both every few hours for our own safety; this module turns a change into (a) one signed line in the
feed room, (b) a kv note with the recent history, (c) a WARNING that reaches the operator. Everything published is
derived from the two texts and nothing else."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import formatter

KEYWORDS = ("faucet", "testnet", "airdrop", "flop", "wallet", "reward", "claim")
_SECTION_RE = re.compile(r"^(#{1,3}\s+\S.*|[A-Z][A-Z0-9 /_&-]{2,40}:)\s*$")   # "## IDENTITY" or "LIMITS:" style headings
MAX_ITEMS = 12          # per list in the feed line; the note carries the full detail
VALUE_CHARS = 60


@dataclass
class DocChange:
    ts: str                                   # ISO minute of detection
    old_version: Optional[str]
    new_version: Optional[str]
    card_added: List[str] = field(default_factory=list)      # "path=value"
    card_removed: List[str] = field(default_factory=list)
    card_changed: List[str] = field(default_factory=list)    # "path old→new"
    sections_added: List[str] = field(default_factory=list)
    sections_removed: List[str] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    keywords_new: List[str] = field(default_factory=list)
    keywords_gone: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.card_added or self.card_removed or self.card_changed or self.sections_added
                    or self.sections_removed or self.lines_added or self.lines_removed)

    def summary(self) -> str:
        """One line, deterministic, for the WARNING and the X post."""
        bits = []
        if self.old_version != self.new_version:
            bits.append(f"agent.json v{self.old_version or '?'} → v{self.new_version or '?'}")
        n_card = len(self.card_added) + len(self.card_removed) + len(self.card_changed)
        if n_card:
            bits.append(f"{n_card} agent.json field{'s' if n_card != 1 else ''} changed")
        if self.sections_added:
            bits.append("llms.txt +" + ", ".join(self.sections_added[:4]))
        if self.sections_removed:
            bits.append("llms.txt −" + ", ".join(self.sections_removed[:4]))
        if (self.lines_added or self.lines_removed) and not (self.sections_added or self.sections_removed):
            bits.append(f"llms.txt +{self.lines_added}/−{self.lines_removed} lines")
        if self.keywords_new:
            bits.append("NEW KEYWORDS: " + ",".join(self.keywords_new))
        return "; ".join(bits) or "no visible difference"

    def detail(self) -> str:
        parts = []
        for label, items in (("+", self.card_added), ("−", self.card_removed), ("Δ", self.card_changed)):
            parts += [f"{label} {i}" for i in items]
        parts += [f"+section {s}" for s in self.sections_added] + [f"−section {s}" for s in self.sections_removed]
        if self.lines_added or self.lines_removed:
            parts.append(f"llms.txt lines +{self.lines_added}/−{self.lines_removed}")
        if self.keywords_gone:
            parts.append("keywords gone: " + ",".join(self.keywords_gone))
        return " ; ".join(parts)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "DocChange":
        return cls(**json.loads(s))


# ---- diffing ---------------------------------------------------------------------------------------
def _flatten(obj, prefix: str = "") -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(obj, dict):
        for k in sorted(obj):
            out.update(_flatten(obj[k], f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        out[prefix] = json.dumps(obj, sort_keys=True)[:VALUE_CHARS * 4]
    else:
        out[prefix] = json.dumps(obj)
    return out


def _short(v: str) -> str:
    return v if len(v) <= VALUE_CHARS else v[:VALUE_CHARS - 1] + "…"


def diff_card(old: Optional[dict], new: Optional[dict]) -> Tuple[Optional[str], Optional[str], List[str], List[str], List[str]]:
    o, n = _flatten(old or {}), _flatten(new or {})
    added = [f"{k}={_short(n[k])}" for k in sorted(set(n) - set(o))]
    removed = [f"{k}={_short(o[k])}" for k in sorted(set(o) - set(n))]
    changed = [f"{k} {_short(o[k])}→{_short(n[k])}" for k in sorted(set(o) & set(n)) if o[k] != n[k] and k != "version"]
    ov = (old or {}).get("version"); nv = (new or {}).get("version")
    return (str(ov) if ov is not None else None, str(nv) if nv is not None else None, added, removed, changed)


def sections(text: str) -> List[str]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if _SECTION_RE.match(s):
            out.append(s.lstrip("# ").rstrip(":").strip())
    return out


def diff_llms(old: str, new: str) -> Tuple[List[str], List[str], int, int]:
    so, sn = sections(old), sections(new)
    lo = {l.strip() for l in old.splitlines() if l.strip()}
    ln = {l.strip() for l in new.splitlines() if l.strip()}
    return ([s for s in sn if s not in so], [s for s in so if s not in sn], len(ln - lo), len(lo - ln))


def keywords(text: str) -> List[str]:
    low = text.casefold()
    return sorted(w for w in KEYWORDS if w in low)


def compare(now: datetime, old_llms: str, new_llms: str, old_card: Optional[dict], new_card: Optional[dict]) -> DocChange:
    ov, nv, added, removed, changed = diff_card(old_card, new_card)
    sa, sr, la, lr = diff_llms(old_llms, new_llms)
    kw_old = set(keywords(old_llms + "\n" + json.dumps(old_card or {})))
    kw_new = set(keywords(new_llms + "\n" + json.dumps(new_card or {})))
    return DocChange(ts=now.strftime("%Y-%m-%dT%H:%MZ"), old_version=ov, new_version=nv,
                     card_added=added, card_removed=removed, card_changed=changed,
                     sections_added=sa, sections_removed=sr, lines_added=la, lines_removed=lr,
                     keywords_new=sorted(kw_new - kw_old), keywords_gone=sorted(kw_old - kw_new))


# ---- renderers ------------------------------------------------------------------------------------
def marker(change: DocChange) -> str:
    return f"TECHNOCORE CHANGE {change.ts}"


def feed_line(change: DocChange, kv_ns: str, max_chars: int = formatter.DEFAULT_MAX_CHARS) -> str:
    """The signed line for the feed room. Facts only; the reader re-reads the docs, we point at what moved."""
    parts = [marker(change), change.summary()]
    items = ([f"+ {i}" for i in change.card_added] + [f"− {i}" for i in change.card_removed]
             + [f"Δ {i}" for i in change.card_changed])[:MAX_ITEMS]
    if items:
        parts.append("agent.json: " + " ; ".join(items))
    if change.sections_added or change.sections_removed:
        parts.append("llms.txt sections: " + " ; ".join([f"+{s}" for s in change.sections_added] + [f"−{s}" for s in change.sections_removed]))
    parts.append(f"Re-read https://technocore.chat/llms.txt and /.well-known/agent.json. History: /kv/{kv_ns}/protocol")
    return formatter.one_line(parts, max_chars=max_chars)


def protocol_note(history: List[DocChange], current_version: Optional[str], baseline_at: Optional[str], now: datetime) -> str:
    """/kv/<ns>/protocol — machine-readable history, newest first. Kept alive with the other notes."""
    head = (f"agentscout protocol asof={now.strftime('%Y-%m-%dT%H:%MZ')} agent.json-version={current_version or '?'} "
            f"watching=llms.txt,/.well-known/agent.json baseline={baseline_at or '?'} changes={len(history)}")
    items = [f"{c.ts} {c.summary()} :: {c.detail()}" for c in history]
    return formatter.note_line(head + (" ; " if items else "") + " ; ".join(items))
