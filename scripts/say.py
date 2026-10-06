#!/usr/bin/env python3
"""Operator-run: queue ONE signed line from AgentScout's DID into a room, or write ONE kv note.

The running agent posts queued lines from its outbox on the next cycle (~2 min) with the same
nonce ordering, landed-check and retries as every other line. Used for contest entries.

    docker exec -i agentscout-agent python - --room d-foo --text "..."        < scripts/say.py   # dry
    docker exec -i agentscout-agent python - --room d-foo --text "..." --yes  < scripts/say.py   # queue
    docker exec -i agentscout-agent python - --note ns key "value" --yes      < scripts/say.py   # kv write
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/src")

from agentscout import formatter  # noqa: E402
from agentscout.config import Settings  # noqa: E402
from agentscout.identity import Identity  # noqa: E402
from agentscout.storage import Storage  # noqa: E402
from agentscout.technocore import TechnocoreClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", help="room name without /r/")
    ap.add_argument("--text", help="message text (swept to one line before signing)")
    ap.add_argument("--note", nargs=3, metavar=("NS", "KEY", "VALUE"), help="write a world-writable kv note")
    ap.add_argument("--yes", action="store_true", help="actually queue / write")
    a = ap.parse_args()
    s = Settings.from_env()
    ident, _ = Identity.load_or_create(s.identity_key_path)
    now = datetime.now(timezone.utc)
    if a.note:
        ns, key, value = a.note
        print(f"note /kv/{ns}/{key} <- {value!r} ({len(value)} chars) as {ident.did}")
        if not a.yes:
            print("dry run; add --yes to write")
            return 0
        c = TechnocoreClient(s.technocore_base_url, s.max_reads_per_minute, s.http_timeout)
        status, body = c.write_note(ns, key, value)
        print(f"HTTP {status} {body.strip()[:200]}")
        return 0 if status == 200 else 1
    if not (a.room and a.text):
        ap.error("--room and --text, or --note")
    text = formatter.sweep(a.text)
    marker = text[:60]
    print(f"/r/{a.room} <- {text!r} ({len(text)} chars) as {ident.did}")
    if not a.yes:
        print("dry run; add --yes to queue")
        return 0
    db = Storage(s.db_path)
    rid = db.enqueue(a.room, "say", marker, text, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    db.close()
    print(f"queued (outbox id {rid}); posted on the next cycle" if rid else "already in the outbox (same room+marker)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
