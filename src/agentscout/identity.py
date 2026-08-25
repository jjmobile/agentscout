"""AgentScout's persistent Ed25519 identity (did:key).

The private key lives only in the persistent volume (`/data/identity.key`), is created once,
never regenerated on rebuild, never logged. The DID is public and derived from it.
Encoding follows Technocore: did:key:z + base58btc(0xed 0x01 || 32-byte public key).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import stat
from pathlib import Path
from typing import Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

log = logging.getLogger("agentscout.identity")

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ED25519_MULTICODEC = b"\xed\x01"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + out


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        idx = _B58.find(ch)
        if idx < 0:
            raise ValueError(f"invalid base58 character {ch!r}")
        n = n * 58 + idx
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + raw


def did_from_public_key(public_key: Ed25519PublicKey) -> str:
    pub = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "did:key:z" + b58encode(_ED25519_MULTICODEC + pub)


def public_key_from_did(did: str) -> Ed25519PublicKey:
    if not did.startswith("did:key:z"):
        raise ValueError("not a did:key with base58btc multibase")
    raw = b58decode(did[len("did:key:z"):])
    if len(raw) != 34 or raw[:2] != _ED25519_MULTICODEC:
        raise ValueError("not an Ed25519 did:key")
    return Ed25519PublicKey.from_public_bytes(raw[2:])


def fingerprint(did: str) -> str:
    return hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]


class Identity:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key
        self.did = did_from_public_key(private_key.public_key())
        self.fp = fingerprint(self.did)

    @classmethod
    def load_or_create(cls, path: str) -> Tuple["Identity", bool]:
        """Returns (identity, created). The file holds the 32-byte seed, base64, one line."""
        p = Path(path)
        if p.exists():
            seed = base64.b64decode(p.read_text().strip())
            if len(seed) != 32:
                raise ValueError(f"{path}: not a 32-byte Ed25519 seed")
            return cls(Ed25519PrivateKey.from_private_bytes(seed)), False
        key = Ed25519PrivateKey.generate()
        seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(base64.b64encode(seed).decode("ascii") + "\n")
        try:
            os.chmod(str(p), stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return cls(key), True

    def sign(self, payload: bytes) -> str:
        """base64url, unpadded (86 chars) — Technocore's signature encoding. Not used in Milestone A."""
        return base64.urlsafe_b64encode(self._key.sign(payload)).decode("ascii").rstrip("=")

    def sign_message(self, room: str, nonce: int, swept_text: str) -> str:
        """Technocore message signature: over `<room>|<nonce>|<text>` UTF-8, text AFTER the single-line sweep."""
        return self.sign(f"{room}|{nonce}|{swept_text}".encode("utf-8"))

    def __repr__(self) -> str:  # never expose key material
        return f"Identity(did={self.did})"
