#!/usr/bin/env python3
"""Local, read-only view of the census. Same renderer as the (future) publisher.

usage: report.py [newest|top|rising|who <fp|did>|digest-preview|explain <fp|did>|stats] [-n N]
env:   AGENTSCOUT_DB (default ./data/agentscout.db)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from agentscout import render  # noqa: E402
from agentscout.storage import Storage  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="digest-preview",
                    choices=["newest", "top", "rising", "who", "digest-preview", "explain", "stats"])
    ap.add_argument("needle", nargs="?")
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--db", default=os.environ.get("AGENTSCOUT_DB", "./data/agentscout.db"))
    a = ap.parse_args()
    if not os.path.exists(a.db):
        print(f"no database at {a.db}", file=sys.stderr)
        return 1
    db = Storage(a.db)
    now = datetime.now(timezone.utc)
    if a.command == "stats":
        print(db.counts())
        return 0
    scored = render.score_all(db, now)
    if a.command == "newest":
        print(render.table(render.newest(scored, a.n), "NEWEST signed agents (first seen desc)"))
    elif a.command == "top":
        print(render.table(render.top(scored, a.n), "TOP by score (confidence >= 40)"))
    elif a.command == "rising":
        rows = render.rising(scored, db, now, a.n)
        print(render.table([(f, r) for f, r, _ in rows], "RISING (7-day score delta)"))
        for f, r, d in rows:
            print(f"  {f.fp[:8]} +{d}")
    elif a.command in ("who", "explain"):
        if not a.needle:
            print("need a fingerprint or did:key", file=sys.stderr)
            return 2
        hit = render.who(scored, db, a.needle)
        if not hit:
            print("not in census (only signed did:key senders in watched rooms are listed)")
            return 1
        print(render.explain(*hit) if a.command == "explain" else render.table([hit], "WHO"))
    else:
        print(render.digest_line(scored, db, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
