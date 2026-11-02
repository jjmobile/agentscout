#!/usr/bin/env python3
"""Read an existing key/backup and prove its public DID without printing secrets."""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentscout.identity import Identity, public_key_from_did  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, help="existing key or backup path")
    parser.add_argument("--expected-did", required=True, help="public identity to verify")
    args = parser.parse_args(argv)
    try:
        metadata = Path(args.key).stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("key must be a regular file with no group or other access")
        identity, _ = Identity.load_or_create(args.key, allow_create=False, expected_did=args.expected_did)
        challenge = b"AgentScout local identity recovery check\x00" + os.urandom(32)
        signature = base64.urlsafe_b64decode(identity.sign(challenge) + "==")
        public_key_from_did(args.expected_did).verify(signature, challenge)
    except (OSError, ValueError) as exc:
        print(f"Identity check failed: {exc}", file=sys.stderr)
        return 1
    print(f"Existing identity verified: {identity.did}")
    print("Public key matches and local signing succeeded. No key was created or changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
