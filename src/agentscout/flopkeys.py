"""W5 — the agent's sr25519 session key for compute channels.

Yellow Paper §6.5 fixes the roles: the *account* is our ed25519 identity key (already a FLOP account,
`identity.ss58_address`), while the per-channel `agent_key` passed to `open_channel` and used for the
per-turn acks and the agent receipt v1 is **sr25519** (F.0: Substrate `b"substrate"` signing context).
§6.2 bounds a session key's lifetime to ≤ 864,000 blocks (~10 days), so the key here rotates on its own
after `max_age_days` (default 9) — a new channel simply binds the new public key.

The curve comes from `py-sr25519-bindings` (the same wheel substrate-interface uses); it is imported
lazily so the rest of AgentScout never depends on it. When it is missing, `KeysUnavailable` is raised
and the Flop provider reports itself unavailable instead of pretending to spend.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("agentscout.flopkeys")

SUBSTRATE_CONTEXT = b"substrate"
MAX_SESSION_KEY_DAYS = 10          # §6.2 SessionKeysMaxDuration at the 1 s target cadence


class KeysUnavailable(RuntimeError):
    """sr25519 support is not installed; nothing can be signed for a channel."""


def _backend():
    try:
        import sr25519  # type: ignore
    except ImportError as exc:
        raise KeysUnavailable("py-sr25519-bindings is not installed") from exc
    return sr25519


def verify(public_key: bytes, message: bytes, signature: bytes, backend=None) -> bool:
    """sr25519 verification under the Substrate context (what every F.3 signature uses)."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        return bool((backend or _backend()).verify(signature, message, public_key))
    except Exception:  # noqa: BLE001 — a malformed point is "not a valid signature", never a crash
        return False


class SessionKey:
    """One sr25519 keypair with a creation time, persisted as `{"seed": b64, "created_at": iso}` (0600)."""

    def __init__(self, seed: bytes, created_at: datetime, backend=None):
        if len(seed) != 32:
            raise ValueError("session key seed must be 32 bytes")
        self._backend = backend or _backend()
        self.public, self._secret = self._backend.pair_from_seed(seed)
        self.public = bytes(self.public)
        self.created_at = created_at

    @classmethod
    def load_or_create(cls, path: str, now: datetime, max_age_days: int = 9, backend=None) -> "SessionKey":
        """Load the key at `path`; create (or rotate) when absent, unreadable, or older than `max_age_days`.
        `max_age_days` is clamped under the protocol's 10-day ceiling. Rotation never deletes: the previous
        file is kept as `<path>.prev` so an in-flight channel can still be settled under its agent key."""
        max_age_days = max(1, min(max_age_days, MAX_SESSION_KEY_DAYS - 1))
        p = Path(path)
        if p.exists():
            try:
                data = json.loads(p.read_text())
                seed = base64.b64decode(data["seed"])
                created = datetime.fromisoformat(data["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if now - created < timedelta(days=max_age_days):
                    return cls(seed, created, backend)
                log.info("flop session key is %d days old: rotating", (now - created).days)
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("flop session key at %s unreadable (%s): creating a new one", path, exc)
            os.replace(str(p), str(p) + ".prev")
        seed = os.urandom(32)
        key = cls(seed, now, backend)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"seed": base64.b64encode(seed).decode("ascii"), "created_at": now.isoformat()}) + "\n")
        try:
            os.chmod(str(p), stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return key

    def age_days(self, now: datetime) -> float:
        return (now - self.created_at).total_seconds() / 86400

    def expires_at(self) -> datetime:
        return self.created_at + timedelta(days=MAX_SESSION_KEY_DAYS)

    def sign(self, message: bytes) -> bytes:
        """64-byte sr25519 signature under the Substrate context."""
        return bytes(self._backend.sign((self.public, self._secret), message))

    def verify(self, message: bytes, signature: bytes) -> bool:
        return verify(self.public, message, signature, self._backend)

    def __repr__(self) -> str:  # never expose key material
        return f"SessionKey(public={self.public.hex()[:16]}…, created_at={self.created_at.isoformat()})"


def public_key_hex(key: Optional[SessionKey]) -> str:
    return key.public.hex() if key else ""
