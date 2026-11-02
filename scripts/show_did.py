#!/usr/bin/env python3
"""Print AgentScout's existing public DID and fingerprint; never create a key.

env: AGENTSCOUT_IDENTITY_KEY (default /data/identity.key)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from agentscout.identity import Identity  # noqa: E402


def main() -> int:
    path = os.environ.get("AGENTSCOUT_IDENTITY_KEY", "/data/identity.key")
    ident, _ = Identity.load_or_create(path, allow_create=False,
                                     expected_did=os.environ.get("AGENTSCOUT_EXPECTED_DID", ""))
    print(f"did:         {ident.did}")
    print(f"fingerprint: {ident.fp}")
    print(f"did note:    /kv/did-{ident.fp[:2]}/{ident.fp[2:]}")
    print(f"key file:    {path} (existing) — back it up, never share it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
