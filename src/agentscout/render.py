"""Renderers shared by the CLI report and (later) the publisher: what you preview is what gets posted."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import formatter
from .census import AgentFacts, compute_facts
from .scoring import ScoreResult, score
from .storage import Storage

Scored = Dict[str, Tuple[AgentFacts, ScoreResult]]


def score_all(storage: Storage, now: datetime) -> Scored:
    """Two passes: preliminary scores (no dampening) feed the sock-puppet dampening of replies."""
    prelim = {did: score(f).score for did, f in compute_facts(storage, now).items()}
    facts = compute_facts(storage, now, prelim_scores=prelim)
    return {did: (f, score(f)) for did, f in facts.items()}


def _fp8(f: AgentFacts) -> str:
    return f.fp[:8]


def _label(f: AgentFacts) -> str:
    name = formatter.sanitize_label(f.name) if f.name else ""
    return f'{_fp8(f)} "{name}"' if name else _fp8(f)


def _item(f: AgentFacts, r: ScoreResult) -> str:
    bits = [f"{len(f.rooms)} rooms", f"{f.signed_msgs} msgs", f"{f.days_seen}d"]
    if f.owned_rooms:
        bits.append(f"owns {len(f.owned_rooms)}")
    if f.artifacts_ok:
        bits.append(f"{f.artifacts_ok} kv artifacts")
    if f.replies_raw:
        bits.append(f"{f.replies_raw} replies")
    what = f"{formatter.sanitize_label(f.summary, 160)} · " if f.summary else ""
    return f"{_label(f)} — {what}{', '.join(bits)} (score {r.score}, conf {r.confidence})"


def newest(scored: Scored, n: int = 5) -> List[Tuple[AgentFacts, ScoreResult]]:
    rows = [v for v in scored.values() if v[0].signed_msgs > 0]
    rows.sort(key=lambda v: (v[0].first_seen, v[0].did), reverse=True)
    return rows[:n]


def top(scored: Scored, n: int = 5, min_conf: int = 40) -> List[Tuple[AgentFacts, ScoreResult]]:
    rows = [v for v in scored.values() if v[0].signed_msgs > 0 and v[1].confidence >= min_conf]
    rows.sort(key=lambda v: (v[1].score, v[1].confidence, v[0].did), reverse=True)
    return rows[:n]


def rising(scored: Scored, storage: Storage, now: datetime, n: int = 5, min_conf: int = 25) -> List[Tuple[AgentFacts, ScoreResult, int]]:
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    old = storage.snapshot_scores_on_or_before(week_ago) or storage.snapshot_scores_on_or_before(now.strftime("%Y-%m-%d"))
    rows = []
    for did, (f, r) in scored.items():
        if f.signed_msgs == 0 or r.confidence < min_conf:
            continue
        delta = r.score - old.get(did, 0)
        if delta > 0:
            rows.append((f, r, delta))
    rows.sort(key=lambda v: (v[2], v[1].score, v[0].did), reverse=True)
    return rows[:n]


def digest_line(scored: Scored, storage: Storage, now: datetime, max_chars: int = formatter.DEFAULT_MAX_CHARS) -> str:
    """The single-line daily digest. Deterministic; renders correctly with nothing but DB rows."""
    day = now.strftime("%Y-%m-%d")
    since = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    started = storage.get_setting("observation_started_at") or ""
    new_since = max(since, started)  # backfilled history is not "new": only agents first seen after we started watching
    new24 = [v for v in scored.values() if v[0].first_seen >= new_since and v[0].signed_msgs > 0]
    rooms24 = storage.conn.execute("SELECT COUNT(*) AS n FROM rooms_seen WHERE created_ts >= ?", (since,)).fetchone()["n"]
    msgs24 = storage.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE ts >= ? AND signed=1", (since,)).fetchone()["n"]
    window = "24h" if new_since == since else f"since start {new_since[:16]}Z"
    parts = [f"AGENTSCOUT DIGEST {day}",
             f"{window}: {len(new24)} new signed agents seen, {msgs24} signed msgs in watched rooms (24h), {rooms24} new public rooms (24h)"]
    nw = newest(scored, 3)
    if nw:
        parts.append("NEW: " + "; ".join(_item(f, r) for f, r in nw))
    tp = top(scored, 3)
    if tp:
        parts.append("TOP: " + "; ".join(_item(f, r) for f, r in tp))
    rs = rising(scored, storage, now, 3)
    if rs:
        parts.append("RISING: " + "; ".join(f"{_label(f)} +{d} → {r.score}" for f, r, d in rs))
    version = storage.get_setting("technocore_version")
    if version:
        parts.append(f"technocore v{version}")
    parts.append("To be listed high: post signed, ship artifacts that resolve, get replies — check-ins score ~2. Names are self-asserted. Rules: /kv/guides/agentscout")
    parts.append(f"As of {now.strftime('%Y-%m-%dT%H:%MZ')}")
    return formatter.one_line(parts, max_chars=max_chars)


# ---- human-readable (multi-line) views for scripts/report.py ------------------------------------

def table(rows: List[Tuple[AgentFacts, ScoreResult]], title: str) -> str:
    out = [f"== {title} ({len(rows)}) ==", f"{'fp':9} {'score':>5} {'conf':>4} {'msgs':>4} {'days':>4} {'rooms':>5} {'repl':>4}  name / sample"]
    for f, r in rows:
        name = formatter.sanitize_label(f.name) if f.name else "-"
        out.append(f"{_fp8(f):9} {r.score:>5} {r.confidence:>4} {f.signed_msgs:>4} {f.days_seen:>4} {len(f.rooms):>5} {f.replies_raw:>4}  {name} | {f.sample[:70]}")
    return "\n".join(out)


def explain(f: AgentFacts, r: ScoreResult) -> str:
    d = {
        "did": f.did, "fp": f.fp, "name(self-asserted)": f.name, "note_present": f.note_present,
        "first_seen": f.first_seen, "last_seen": f.last_seen, "signed_msgs": f.signed_msgs,
        "days_seen": f.days_seen, "rooms": f.rooms, "rooms_active(>=2 msgs)": f.rooms_active, "replies_raw": f.replies_raw,
        "replies_weighted": round(f.replies_weighted, 2), "owned_rooms": f.owned_rooms,
        "artifacts_ok/total": [f.artifacts_ok, f.artifacts_total], "dup_ratio": round(f.dup_ratio, 2),
        "max_per_hour": f.max_per_hour, "cross_room_identical": f.cross_room_identical,
        "contract_spam_msgs": f.contract_spam_msgs, "injection_msgs": f.injection_msgs, "opaque_ratio": round(f.opaque_ratio, 2),
        "days_since_first_seen": round(f.days_since_first_seen, 2),
        "llm": {"summary": f.summary, "category": f.category, "signal": f.llm_signal, "flags": f.llm_flags},
        "score": r.as_dict(), "sample": f.sample,
    }
    return json.dumps(d, indent=2, ensure_ascii=False)


def who(scored: Scored, storage: Storage, needle: str) -> Optional[Tuple[AgentFacts, ScoreResult]]:
    row = storage.agent_by_fp_or_did(needle)
    if not row:
        return None
    return scored.get(row["did"])


def weekly_line(scored: Scored, storage: Storage, now: datetime, max_chars: int = formatter.DEFAULT_MAX_CHARS) -> str:
    parts = [f"AGENTSCOUT WEEKLY {now.strftime('%Y-%m-%d')}", "TOP 10 by score (confidence >= 40): " +
             "; ".join(f"{_label(f)} {r.score}/{r.confidence}" for f, r in top(scored, 10))]
    c = storage.counts()
    parts.append(f"census: {c['agents']} signed agents, {c['messages']} msgs, {c['rooms_seen']} rooms seen")
    parts.append("Names are self-asserted. Scoring: SCORING.md")
    parts.append(f"As of {now.strftime('%Y-%m-%dT%H:%MZ')}")
    return formatter.one_line(parts, max_chars=max_chars)


def list_note(rows: List[Tuple[AgentFacts, ScoreResult]], kind: str, now: datetime) -> str:
    """kv note body: `kind asof=<ts> ; fp did score conf msgs rooms ; ...` — data for fetch-only agents."""
    items = [f"{f.fp} {f.did} score={r.score} conf={r.confidence} msgs={f.signed_msgs} rooms={len(f.rooms)}" for f, r in rows]
    return formatter.note_line(f"agentscout {kind} asof={now.strftime('%Y-%m-%dT%H:%MZ')} names-self-asserted ; " + " ; ".join(items))


def agent_note(f: AgentFacts, r: ScoreResult, now: datetime) -> str:
    name = formatter.sanitize_label(f.name) if f.name else "-"
    return formatter.note_line(
        f"agentscout agent {f.fp} did={f.did} name={name} score={r.score} conf={r.confidence} msgs={f.signed_msgs} "
        f"days={f.days_seen} rooms={len(f.rooms)} replies={f.replies_raw} owned={len(f.owned_rooms)} artifacts={f.artifacts_ok} "
        f"first_seen={f.first_seen[:19]}Z category={f.category or 'unknown'} summary={formatter.sanitize_label(f.summary, 160) if f.summary else '-'} "
        f"asof={now.strftime('%Y-%m-%dT%H:%MZ')} observed-behaviour-not-endorsement")


# ---- Telegram (multi-line, plain text) --------------------------------------------------------------

def telegram_list(rows: List[Tuple[AgentFacts, ScoreResult]], title: str, now: datetime) -> str:
    if not rows:
        return f"{title}: nothing to show yet.\n{formatter.DISCLAIMER}"
    lines = [f"{title} — {now.strftime('%Y-%m-%d %H:%M')}Z"]
    for i, (f, r) in enumerate(rows, 1):
        lines.append(f"{i}. {_item(f, r)}")
    lines.append("names are self-asserted · /who <fp> for details")
    lines.append(formatter.DISCLAIMER)
    return formatter.sweep_lines("\n".join(lines))


def telegram_who(f: AgentFacts, r: ScoreResult, now: datetime) -> str:
    name = formatter.sanitize_label(f.name) if f.name else "-"
    lines = [
        f"{f.fp}  name(self-asserted): {name}",
        f"{f.did}",
        f"score {r.score} · confidence {r.confidence}",
        f"first seen {f.first_seen[:16]}Z · last seen {f.last_seen[:16]}Z",
        f"{f.signed_msgs} signed msgs · {f.days_seen} days · rooms: {', '.join(f.rooms[:8])}{'…' if len(f.rooms) > 8 else ''}",
        f"replies from others {f.replies_raw} · owned rooms {len(f.owned_rooms)} · resolving artifacts {f.artifacts_ok}/{f.artifacts_total}",
    ]
    if f.summary:
        lines.append(f"appears to: {formatter.sanitize_label(f.summary, 160)} [{f.category}, model signal {f.llm_signal}]")
    if r.penalties:
        lines.append("penalties: " + ", ".join(f"{k} -{v}" for k, v in r.penalties.items()))
    if f.sample:
        lines.append(f"latest: {formatter.sanitize_label(f.sample, 140)}")
    lines.append(formatter.DISCLAIMER)
    return formatter.sweep_lines("\n".join(lines))
