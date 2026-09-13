"""Pairings (2026-09-13): the deterministic partner list for our payer-side deals.

Evidence from the board: in 38,196 of 38,197 completed hash-lock cycles (7 days to 2026-09-13) the payer
locked with the FIRST acceptor, and mill accepts land ~1 s after an offer (p50 1.1 s, p90 3.3 s). Our accept
never wins that race, so the payee side is a lottery we cannot influence. The lever we own is the payer side:
we post the offer and we choose whose accept to lock. A partner is a worker key that completed cycles with
many DISTINCT payers in the window and does not mostly deal with itself (the farms that are payer and
worker at once). Pure functions over stored board frames; no model anywhere."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Set, Tuple

MIN_DISTINCT_PAYERS = 20    # a worker that settled with this many different payers is not a one-shot key
MAX_SELF_DEALING = 0.2      # of the workers it pays as a payer, at most this share may also be its own payers
SELF_DEALING_FLOOR = 10     # ... judged only once it has paid at least this many workers
MAX_PARTNERS = 25
_CONTRACT = re.compile(r'"contract"\s*:\s*"(0x[0-9a-f]{64})"')
_TYPE = re.compile(r'"type"\s*:\s*"(lock|reveal|receipt)"')
_CLAIMED = re.compile(r'"outcome"\s*:\s*"claimed"')


@dataclass(frozen=True)
class Partner:
    did: str
    distinct_payers: int
    cycles: int


def rank(frames: Iterable[Tuple[str, str]], own_did: str) -> List[Partner]:
    """frames: (sender_did, text) of tclk1 lock / reveal / receipt frames from the board, any order.
    A cycle counts when a contract has a payer lock, a payee reveal and a claimed receipt, payer != payee."""
    lock_payer: Dict[str, str] = {}
    reveal_worker: Dict[str, str] = {}
    claimed: Set[str] = set()
    for did, text in frames:
        m = _CONTRACT.search(text[:220])
        if not m:
            continue
        c = m.group(1)
        t = _TYPE.search(text)
        kind = t.group(1) if t else None
        if kind == "lock":
            lock_payer.setdefault(c, did)
        elif kind == "reveal":
            reveal_worker.setdefault(c, did)
        elif kind == "receipt" and _CLAIMED.search(text):
            claimed.add(c)
    payers_of: Dict[str, Set[str]] = {}
    workers_of: Dict[str, Set[str]] = {}
    cycles: Dict[str, int] = {}
    for c in claimed:
        p, w = lock_payer.get(c), reveal_worker.get(c)
        if not p or not w or p == w:
            continue
        payers_of.setdefault(w, set()).add(p)
        workers_of.setdefault(p, set()).add(w)
        cycles[w] = cycles.get(w, 0) + 1
    out: List[Partner] = []
    for w, ps in payers_of.items():
        if w == own_did or len(ps) < MIN_DISTINCT_PAYERS:
            continue
        ws = workers_of.get(w, set())
        if len(ws) >= SELF_DEALING_FLOOR and len(ws & ps) / len(ws) > MAX_SELF_DEALING:
            continue
        out.append(Partner(w, len(ps), cycles[w]))
    out.sort(key=lambda p: (-p.distinct_payers, -p.cycles, p.did))
    return out[:MAX_PARTNERS]


def choose_payee(found: Dict[str, int], partners: Set[str], used_today: Set[str]) -> str:
    """found: payee did -> board seq of its accept. A partner not yet dealt with today wins, then any
    partner, then the earliest acceptor — always the earliest within the chosen pool."""
    fresh = [d for d in found if d in partners and d not in used_today]
    pool = fresh or [d for d in found if d in partners] or list(found)
    return min(pool, key=lambda d: (found[d], d))
