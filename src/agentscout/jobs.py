"""Deterministic solvers for the technocore task mill (W1 of AGENTSCOUT_EVOLUTION.md).

The mill posts tclk/1 payer offers whose job context is a one-line spec note:

    <family> | <question> | reward tier n/5 | done looks like: <format> | deliver … | PROTOCOL: … |
    CREDIT: … | MATERIAL: <header cols> <row fields> <row fields> …

Everything here is pure: parse the spec, recognise one of the known question templates, compute
the one-line answer from the MATERIAL table. Anything unrecognised returns None — the worker then
leaves that offer alone. No LLM, ever: an answer we cannot compute exactly is an answer we do not give.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

SUPPORTED_FAMILIES = ("census", "inference", "verification", "attest")

# The "attest" family has no material: the deliverable is a line we post in the deal room first.
ATTEST_ANSWER = "__attest__"

_DID = r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{40,50}"


@dataclass
class Spec:
    family: str
    question: str
    done: str
    columns: List[str]
    rows: List[List[str]] = field(default_factory=list)

    def col(self, name: str) -> int:
        return self.columns.index(name)


def parse_spec(text: str) -> Optional[Spec]:
    """The spec note (or the inline context) → Spec. None when the shape is not the mill's."""
    if not text or " | " not in text:
        return None
    text = " ".join(text.split())
    family, rest = text.split(" | ", 1)
    family = family.strip().lower()
    if not family or " " in family:
        return None
    q_end = rest.find(" | reward tier")
    question = rest[:q_end].strip() if q_end >= 0 else rest.split(" | full spec:")[0].strip()
    m = re.search(r"done looks like:\s*(.*?)(?:\s\|\s|$)", rest)
    done = m.group(1).strip() if m else ""
    columns: List[str] = []
    rows: List[List[str]] = []
    if " MATERIAL:" in rest or rest.startswith("MATERIAL:"):
        material = rest.split("MATERIAL:", 1)[1].strip()
        columns, rows = _parse_table(material)
    else:
        hm = re.search(r"(?:rows|one (?:offer|frame) per line): ((?:[a-z]+ \| )+[a-z]+)\)", question)
        if hm:
            columns = [c.strip() for c in hm.group(1).split("|")]
    return Spec(family=family, question=question, done=done, columns=columns, rows=rows)


def _parse_table(material: str) -> Tuple[List[str], List[List[str]]]:
    """'seq | payer | amount 123 | abc | 5 456 | def | 6' → header + rows. Field values never contain
    whitespace or '|', so the flat token stream chunks cleanly by the header width."""
    tokens = [t for t in re.split(r"\s*\|\s*|\s+", material.strip()) if t]
    header: List[str] = []
    for t in tokens:
        if re.fullmatch(r"[a-z_]+", t) and t not in header:
            header.append(t)
        else:
            break
    if len(header) < 2:
        return [], []
    body = tokens[len(header):]
    width = len(header)
    rows = [body[i:i + width] for i in range(0, len(body) - len(body) % width, width)]
    return header, rows


# ---- solvers -------------------------------------------------------------------------------------

def _ints(spec: Spec, col: str) -> List[int]:
    i = spec.col(col)
    return [int(r[i]) for r in spec.rows]


def _fmt_list(values) -> str:
    return ", ".join(str(v) for v in values)


def _alpha_key(name: str):
    """'ties: alphabetically first' in the mill's grader is case-insensitive (verified against graded
    deals on 2026-09-08: ASCII order was marked FAIL); 'ASCII-smaller' templates keep plain order."""
    return (name.casefold(), name)


def _inf_largest(spec: Spec, n: int) -> str:
    si, ai = spec.col("seq"), spec.col("amount")
    ordered = sorted(spec.rows, key=lambda r: (-int(r[ai]), int(r[si])))
    return _fmt_list(r[si] for r in ordered[:n])


def _inf_payer_total(spec: Spec) -> str:
    pi, ai = spec.col("payer"), spec.col("amount")
    totals: Dict[str, int] = {}
    for r in spec.rows:
        totals[r[pi]] = totals.get(r[pi], 0) + int(r[ai])
    best = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return f"{best[0]} {best[1]}"


def _inf_sort_by_payer(spec: Spec) -> str:
    pi, si = spec.col("payer"), spec.col("seq")
    ordered = sorted(spec.rows, key=lambda r: (r[pi], int(r[si])))
    return _fmt_list(r[si] for r in ordered)


def _inf_even(spec: Spec) -> str:
    evens = sorted(v for v in _ints(spec, "seq") if v % 2 == 0)
    return _fmt_list(evens) if evens else "none"


def _inf_earliest_latest(spec: Spec) -> str:
    ti, si = spec.col("time"), spec.col("seq")
    earliest = min(spec.rows, key=lambda r: (r[ti], int(r[si])))
    latest = max(spec.rows, key=lambda r: (r[ti], -int(r[si])))
    return f"{earliest[si]} {latest[si]}"


def _ver_lock_count(spec: Spec, did: str) -> str:
    ti, fi = spec.col("type"), spec.col("from")
    return str(sum(1 for r in spec.rows if r[ti] == "lock" and r[fi] == did))


def _ver_offers_and_locks(spec: Spec, did: str) -> str:
    ti, fi = spec.col("type"), spec.col("from")
    offers = sum(1 for r in spec.rows if r[ti] == "offer" and r[fi] == did)
    locks = sum(1 for r in spec.rows if r[ti] == "lock" and r[fi] == did)
    return f"offers {offers}, locks {locks}"


def _census_offers_payers(spec: Spec) -> str:
    pi = spec.col("payer")
    counts: Dict[str, int] = {}
    for r in spec.rows:
        counts[r[pi]] = counts.get(r[pi], 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], _alpha_key(kv[0])))[0]
    return f"offers={len(spec.rows)}; payers={len(counts)}; top={top[0]}:{top[1]}"


def _census_proto(spec: Spec) -> str:
    pi, ri = spec.col("proto"), spec.col("rails")
    counts: Dict[str, int] = {}
    for r in spec.rows:
        counts[r[pi]] = counts.get(r[pi], 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], _alpha_key(kv[0])))[0]
    paper_only = sum(1 for r in spec.rows if r[ri] == "paper")
    return f"proto={top[0]}:{top[1]}; paper_only={paper_only}"


def _census_assets(spec: Spec) -> str:
    ai, mi = spec.col("asset"), spec.col("amount")
    totals: Dict[str, int] = {}
    for r in spec.rows:
        totals[r[ai]] = totals.get(r[ai], 0) + int(r[mi])
    top = sorted(totals.items(), key=lambda kv: (-kv[1], _alpha_key(kv[0])))[0]
    return f"assets={len(totals)}; top_asset={top[0]}:{top[1]}"


# (family, question regex, needed columns, solver(spec, match) -> answer)
_TEMPLATES: List[Tuple[str, re.Pattern, Tuple[str, ...], Callable[[Spec, "re.Match"], str]]] = [
    ("inference", re.compile(r"output the seq values of the (\d+) rows with the largest amount, highest first \(ties broken by lower seq first\), comma-separated"),
     ("seq", "amount"), lambda s, m: _inf_largest(s, int(m.group(1)))),
    ("inference", re.compile(r"sum the amount per payer and output the payer with the largest total and that total, as \"<payer> <total>\" \(ties: ASCII-smaller payer\)"),
     ("seq", "payer", "amount"), lambda s, m: _inf_payer_total(s)),
    ("inference", re.compile(r"sort all rows by payer \(ASCII order\), then by seq ascending, and output the seq values in that order, comma-separated"),
     ("seq", "payer"), lambda s, m: _inf_sort_by_payer(s)),
    ("inference", re.compile(r"output the seq values that are even numbers, in ascending order, comma-separated \(or 'none'\)"),
     ("seq",), lambda s, m: _inf_even(s)),
    ("inference", re.compile(r"output the seq of the row with the earliest time and the seq of the row with the latest time, as \"<earliest_seq> <latest_seq>\" \(ties: lower seq\)"),
     ("seq", "time"), lambda s, m: _inf_earliest_latest(s)),
    ("verification", re.compile(r"how many rows are offer frames posted by (" + _DID + r"), and how many are lock frames by the same sender\?"),
     ("type", "from"), lambda s, m: _ver_offers_and_locks(s, m.group(1))),
    ("verification", re.compile(r"how many rows are lock frames posted by (" + _DID + r")\? Give the count\."),
     ("type", "from"), lambda s, m: _ver_lock_count(s, m.group(1))),
    ("verification", re.compile(r"\(one line: (\d+)\+(\d+)=(\d+)\): what is the value after \"=\"\?"),
     (), lambda s, m: m.group(3)),
    ("census", re.compile(r"Census over the excerpt: how many offers, how many distinct payers, and which payer posted the most \(ties: alphabetically first\)\?"),
     ("payer",), lambda s, m: _census_offers_payers(s)),
    ("census", re.compile(r"Census over the excerpt: count offers per proto value \(\"-\" for none\) and report the most common proto with its count, and how many offers list exactly the single rail \"paper\"\."),
     ("proto", "rails"), lambda s, m: _census_proto(s)),
    ("census", re.compile(r"Census over the excerpt: the number of distinct assets, the asset with the largest total amount \(sum of amount over its rows; ties: alphabetically first\) and that total as an integer\."),
     ("asset", "amount"), lambda s, m: _census_assets(s)),
    ("attest", re.compile(r"`tclk-attest <(?:contract id|full contract id[^>]*)>`"),
     (), lambda s, m: ATTEST_ANSWER),
]


def solve(spec: Spec) -> Optional[str]:
    """One answer line, or None when the question is not one of the exact templates we compute."""
    for family, rx, cols, fn in _TEMPLATES:
        if spec.family != family:
            continue
        m = rx.search(spec.question)
        if not m:
            continue
        if cols and (not spec.rows or any(c not in spec.columns for c in cols)):
            return None
        try:
            answer = fn(spec, m)
        except (ValueError, IndexError, KeyError):
            return None
        answer = " ".join(str(answer).split())
        return answer if answer and len(answer) <= 900 else None
    return None


def material_ref(spec: Spec) -> Optional[Tuple[str, str]]:
    """(namespace, key) of the materials note the question cites, when the table is not inline."""
    if spec.rows:
        return None
    m = re.search(r"From the note /kv/([\w\-]+)/([\w\-]+)", spec.question)
    return (m.group(1), m.group(2)) if m else None


def attach_material(spec: Spec, text: str) -> Spec:
    """Parse a materials note ('seq | payer | … rows…') into the spec; unchanged when it does not fit."""
    columns, rows = _parse_table(" ".join((text or "").split()))
    if columns and rows and (not spec.columns or columns == spec.columns):
        spec.columns, spec.rows = columns, rows
    return spec


def context_family(context: str) -> Optional[str]:
    """Family named in an offer's job.context ('census | …'); None for /kv/ paths or free text."""
    if not context or context.startswith("/kv/"):
        return None
    head = context.split(" | ", 1)[0].strip().lower()
    return head if re.fullmatch(r"[a-z][a-z0-9-]{1,30}", head) else None


def context_spec_ref(context: str) -> Optional[Tuple[str, str]]:
    """(namespace, key) of the full spec note: 'full spec: /kv/ns/key' inline, or the context itself."""
    m = re.search(r"full spec:\s*(/kv/([\w\-]+)/([\w\-]+))", context or "")
    if m:
        return m.group(2), m.group(3)
    m = re.fullmatch(r"/kv/([\w\-]+)/([\w\-]+)", (context or "").strip())
    return (m.group(1), m.group(2)) if m else None
