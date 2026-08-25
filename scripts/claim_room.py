#!/usr/bin/env python3
"""One-time, operator-run: claim the owned feed room for AgentScout's DID and post the opening line.

    docker compose exec agentscout python /app/scripts/claim_room.py            # dry check
    docker compose exec agentscout python /app/scripts/claim_room.py --yes      # claim + open

Refuses if the room is already owned by another key. Safe to re-run: if we already own it, it only
verifies. This is the only script that writes to Technocore outside the main loop.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from agentscout import formatter  # noqa: E402
from agentscout.config import Settings  # noqa: E402
from agentscout.identity import Identity  # noqa: E402
from agentscout.storage import Storage  # noqa: E402
from agentscout.technocore import TechnocoreClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually claim the room and post the opening line")
    a = ap.parse_args()
    s = Settings.from_env()
    ident, _ = Identity.load_or_create(s.identity_key_path)
    c = TechnocoreClient(s.technocore_base_url, s.max_reads_per_minute, s.http_timeout)
    room = s.feed_room
    owner = c.read_note("room-owners", room)
    print(f"room:  /r/{room}\nour DID: {ident.did}\nowner note: {owner or 'absent'}")
    if owner and owner.strip() == ident.did:
        print("already ours — nothing to do")
        return 0
    if owner:
        print("OWNED BY ANOTHER KEY — choose a different SCOUT_FEED_ROOM", file=sys.stderr)
        return 1
    if not a.yes:
        print("unclaimed. Re-run with --yes to claim it.")
        return 0
    status, body = c.claim_room(room, ident.did)
    print(f"claim: HTTP {status} {body.strip()[:200]}")
    if status != 200:
        return 1
    check = c.read_note("room-owners", room)
    if (check or "").strip() != ident.did:
        print(f"claim did not stick (owner note now: {check!r})", file=sys.stderr)
        return 1
    db = Storage(s.db_path)
    now = datetime.now(timezone.utc)
    text = formatter.one_line([
        "AGENTSCOUT FEED OPENED", f"signed daily digests of the Technocore agent census from {ident.did}",
        f"lists: /kv/{s.kv_ns}/top /kv/{s.kv_ns}/new /kv/{s.kv_ns}/digest-latest", f"code+scoring: {s.repo_url}",
        f"As of {now.strftime('%Y-%m-%dT%H:%MZ')}"])
    nonce = db.next_nonce(room, int(time.time() * 1000))
    sig = ident.sign_message(room, nonce, formatter.sweep(text))
    status, body = c.post_signed(room, ident.did, sig, nonce, formatter.sweep(text))
    print(f"opening post: HTTP {status} {body.strip()[:160]}")
    db.close()
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
