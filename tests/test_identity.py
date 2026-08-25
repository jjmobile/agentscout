import logging
import os
import stat

from agentscout.identity import Identity, b58decode, b58encode, did_from_public_key, fingerprint, public_key_from_did

SPEC_DID = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"  # W3C did:key spec Ed25519 example


def test_base58_roundtrip_and_leading_zeros():
    for raw in (b"", b"\x00", b"\x00\x00\x01", os.urandom(34), b"\xff" * 34):
        assert b58decode(b58encode(raw)) == raw


def test_spec_vector_decodes_and_reencodes():
    pub = public_key_from_did(SPEC_DID)
    assert did_from_public_key(pub) == SPEC_DID
    raw = b58decode(SPEC_DID[len("did:key:z"):])
    assert raw[:2] == b"\xed\x01" and len(raw) == 34


def test_generated_did_has_technocore_shape(tmp_path):
    ident, created = Identity.load_or_create(str(tmp_path / "identity.key"))
    assert created and ident.did.startswith("did:key:z6Mk")
    assert all(ch in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for ch in ident.did[len("did:key:z"):])
    assert public_key_from_did(ident.did)


def test_identity_persists_and_is_private(tmp_path):
    path = str(tmp_path / "identity.key")
    a, created_a = Identity.load_or_create(path)
    b, created_b = Identity.load_or_create(path)
    assert created_a and not created_b and a.did == b.did
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert fingerprint(a.did) == a.fp and len(a.fp) == 16


def test_signature_is_86_char_base64url_and_verifies(tmp_path):
    ident, _ = Identity.load_or_create(str(tmp_path / "k"))
    payload = "lobby|1787615867344|hello".encode()
    sig = ident.sign(payload)
    assert len(sig) == 86 and "=" not in sig
    import base64
    public_key_from_did(ident.did).verify(base64.urlsafe_b64decode(sig + "=="), payload)


def test_private_key_never_in_repr_or_logs(tmp_path, caplog):
    path = str(tmp_path / "identity.key")
    with caplog.at_level(logging.DEBUG):
        ident, _ = Identity.load_or_create(path)
    seed_b64 = open(path).read().strip()
    assert seed_b64 not in repr(ident) and seed_b64 not in caplog.text
