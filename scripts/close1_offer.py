#!/usr/bin/env python3
"""Operator-run, close-1 contest: post ONE open offer (maker side) in /r/close1, taker "any".

    docker exec -i agentscout-agent python - --side buy --qty 2 --px 225.03        < scripts/close1_offer.py   # dry
    docker exec -i agentscout-agent python - --side buy --qty 2 --px 225.03 --yes  < scripts/close1_offer.py   # queue

Signs `close-1|terms|<terms>` with our key and queues the offer line (maker_sig only) into the outbox; any
owner may countersign and post it, and the referee settles it at the next sweep if it is still inside the
limits. Check /r/d-close1-flow for the trade id. Paper POLF only.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, "/app/src")

from agentscout.config import Settings  # noqa: E402
from agentscout.identity import Identity  # noqa: E402
from agentscout.storage import Storage  # noqa: E402
from agentscout.technocore import TechnocoreClient  # noqa: E402

SEASON = "close-1"


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=("buy", "sell"), required=True, help="our (maker) side")
    ap.add_argument("--qty", required=True, help="contracts, step 0.01, at least 0.1")
    ap.add_argument("--px", help="price, step 0.01; default = the referee's current reference")
    ap.add_argument("--until", type=int, help="last sweep it may settle in; default = next sweep + 12 (one hour)")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    s = Settings.from_env()
    ident, _ = Identity.load_or_create(s.identity_key_path)
    c = TechnocoreClient(s.technocore_base_url, s.max_reads_per_minute, s.http_timeout)
    price = json.loads(c.read_room("d-close1-price", limit=1)["messages"][-1]["text"])
    nxt, lo, hi = int(price["for"]), Decimal(price["limits"][0]), Decimal(price["limits"][1])
    px = Decimal(a.px) if a.px else Decimal(price["ref"]["px"])
    qty = Decimal(a.qty)
    until = a.until or nxt + 12
    print(f"next sweep {nxt}: ref {price['ref']['px']} (age {price.get('age_s')}s), limits {lo}-{hi}")
    if not (lo <= px <= hi) or qty < Decimal("0.1") or px != px.quantize(Decimal("0.01")) or qty != qty.quantize(Decimal("0.01")):
        print("price outside the limits or bad step", file=sys.stderr)
        return 1
    terms = {"id": "as-" + secrets.token_hex(4), "maker": ident.did, "px": str(px), "qty": str(qty),
             "side": a.side, "taker": "any", "until": until}
    maker_sig = ident.sign(f"{SEASON}|terms|{canon(terms)}".encode())
    offer = {"t": "trade", "season": SEASON, "terms": terms, "taker": "any", "maker_sig": maker_sig, "taker_sig": ""}
    text = canon(offer)
    print(f"OFFER {a.side} {qty} @ {px} until sweep {until} (value {qty * px} POLF, fee ~{(qty * px * Decimal('0.01')).quantize(Decimal('0.01'))}) id {terms['id']}")
    print(text)
    if not a.yes:
        print("dry run; add --yes to queue")
        return 0
    db = Storage(s.db_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rid = db.enqueue("close1", "offer", f"close1 offer {terms['id']}", text, now)
    db.close()
    print(f"queued (outbox id {rid}); watch /r/d-close1-flow for id {terms['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
