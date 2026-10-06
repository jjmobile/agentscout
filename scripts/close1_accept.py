#!/usr/bin/env python3
"""Operator-run, close-1 contest: countersign ONE open offer from /r/close1 and queue the signed trade.

    docker exec -i agentscout-agent python - --side buy --max-qty 3            < scripts/close1_accept.py   # dry
    docker exec -i agentscout-agent python - --side buy --max-qty 3 --yes      < scripts/close1_accept.py   # queue
    docker exec -i agentscout-agent python - --seconds 90 ...              (watch the room longer)

`--side` is OUR side. Picks the open offer (taker "any", no taker_sig, maker_sig verifies, price inside the
referee's limits for the next sweep, qty <= --max-qty, until >= next sweep) with the best price for us, signs
`close-1|accept|<terms>|<our did>` and hands the trade line to the outbox; the agent posts it on its next cycle.
Settlement shows up in /r/d-close1-flow at the next sweep as settled or void (with a reason). Paper POLF only.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, "/app/src")

from cryptography.exceptions import InvalidSignature  # noqa: E402

from agentscout.config import Settings  # noqa: E402
from agentscout.identity import Identity, public_key_from_did  # noqa: E402
from agentscout.storage import Storage  # noqa: E402
from agentscout.technocore import TechnocoreClient  # noqa: E402

SEASON = "close-1"


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def maker_sig_ok(j: dict) -> bool:
    try:
        public_key_from_did(j["terms"]["maker"]).verify(b64d(j["maker_sig"]), f"{SEASON}|terms|{canon(j['terms'])}".encode())
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=("buy", "sell"), required=True, help="OUR side of the trade")
    ap.add_argument("--max-qty", type=Decimal, default=Decimal("3"))
    ap.add_argument("--room", default="close1")
    ap.add_argument("--seconds", type=float, default=45.0, help="how long to watch the room for open offers")
    ap.add_argument("--skip", default="", help="comma-separated trade ids never to accept again")
    ap.add_argument("--skip-makers", default="", help="comma-separated maker DIDs to ignore (offers that void on funds)")
    ap.add_argument("--max-off-ref", type=Decimal, default=Decimal("0.05"),
                    help="worst price accepted, as a fraction of the reference (0.003 = 0.3%% worse than ref)")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    skip = {x for x in a.skip.split(",") if x}
    skip_makers = {x for x in a.skip_makers.split(",") if x}
    s = Settings.from_env()
    ident, _ = Identity.load_or_create(s.identity_key_path)
    c = TechnocoreClient(s.technocore_base_url, s.max_reads_per_minute, s.http_timeout)

    price = json.loads(c.read_room("d-close1-price", limit=1)["messages"][-1]["text"])
    nxt, lo, hi = int(price["for"]), Decimal(price["limits"][0]), Decimal(price["limits"][1])
    ref = Decimal(price["ref"]["px"])
    # our own worst-price guard, tighter than the referee's band: buy no higher / sell no lower than ref ± max_off_ref
    if a.side == "buy":
        hi = min(hi, (ref * (1 + a.max_off_ref)).quantize(Decimal("0.01")))
    else:
        lo = max(lo, (ref * (1 - a.max_off_ref)).quantize(Decimal("0.01")))
    print(f"next sweep {nxt}: ref {ref} (age {price.get('age_s')}s), accepting {lo}-{hi}")

    # The room is a firehose and since-reads return only the newest 200 after the cursor (no paging back),
    # so poll forward for --seconds and accumulate what passes by.
    seen, msgs = set(), []
    cursor = max(0, int(c.read_room(a.room, limit=1)["last_seq"]) - 200)
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.seconds:
        page = c.read_room(a.room, since=cursor, limit=200)["messages"]
        for m in page:
            if m["seq"] not in seen:
                seen.add(m["seq"]); msgs.append(m)
        if page:
            cursor = max(cursor, max(m["seq"] for m in page))
        time.sleep(1.0)
    want_maker_side = "sell" if a.side == "buy" else "buy"
    offers = []
    for m in msgs:
        try:
            j = json.loads(m["text"])
        except ValueError:
            continue
        if j.get("t") != "trade" or j.get("season") != SEASON or j.get("taker_sig") or j.get("taker") != "any":
            continue
        t = j.get("terms") or {}
        if t.get("taker") != "any" or t.get("side") != want_maker_side or t.get("maker") == ident.did or t.get("id") in skip or t.get("maker") in skip_makers:
            continue
        try:
            px, qty, until = Decimal(t["px"]), Decimal(t["qty"]), int(t["until"])
        except (KeyError, ValueError, TypeError):
            continue
        if not (lo <= px <= hi) or qty > a.max_qty or qty < Decimal("0.1") or until < nxt:
            continue
        if m["from"] != t["maker"] or not maker_sig_ok(j):
            continue
        offers.append((px, qty, m["seq"], j))
    if not offers:
        print(f"no acceptable open offers (watched {len(msgs)} msgs over {a.seconds:.0f}s)")
        return 1
    offers.sort(key=lambda o: (o[0] if a.side == "buy" else -o[0], -o[2]))   # best price for us, then newest
    for px, qty, seq, j in offers[:5]:
        print(f"  offer seq {seq}: maker {j['terms']['maker'][8:20]} {j['terms']['side']} {qty} @ {px} until {j['terms']['until']} id {j['terms']['id']}")
    px, qty, seq, j = offers[0]
    terms = j["terms"]
    taker_sig = ident.sign(f"{SEASON}|accept|{canon(terms)}|{ident.did}".encode())
    trade = {"t": "trade", "season": SEASON, "terms": terms, "taker": ident.did,
             "maker_sig": j["maker_sig"], "taker_sig": taker_sig}
    text = canon(trade)
    print(f"\nWE {a.side} {qty} @ {px} (collateral/value {qty * px} POLF, fee ~{(qty * px * Decimal('0.01')).quantize(Decimal('0.01'))}) — trade id {terms['id']}")
    print(text)
    if not a.yes:
        print("dry run; add --yes to queue")
        return 0
    db = Storage(s.db_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rid = db.enqueue(a.room, "trade", f"close1 trade {terms['id']}", text, now)
    db.close()
    print(f"queued (outbox id {rid}); check /r/d-close1-flow at sweep {nxt} for id {terms['id']}" if rid else "already queued")
    return 0


if __name__ == "__main__":
    sys.exit(main())
