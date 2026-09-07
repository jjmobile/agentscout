from datetime import timedelta

import pytest

from agentscout import attestation
from agentscout.census import AgentFacts
from agentscout.identity import Identity, fingerprint
from conftest import DID_A, DID_B, NOW


def facts(did=DID_A, **over):
    base = dict(did=did, fp=fingerprint(did), first_seen="2026-08-17T00:00:00Z",
                last_seen="2026-08-25T00:00:00Z", signed_msgs=200, days_seen=8,
                rooms=["a", "b", "c"], rooms_active=["a", "b", "c"], replies_raw=40,
                replies_weighted=10.0, owned_rooms=["r"], artifacts_ok=3, artifacts_total=3,
                days_since_first_seen=8.0)
    base.update(over)
    return AgentFacts(**base)


def ident(tmp_path):
    i, _ = Identity.load_or_create(str(tmp_path / "id.key"))
    return i


ASOF = "2026-08-25T12:00:00Z"


def test_attest_round_trips_and_is_tamper_evident(tmp_path):
    i = ident(tmp_path)
    a = attestation.attest(i, facts(), ASOF, nonce="0011223344556677")
    v = attestation.verify(a, now=NOW)
    assert v.ok and v.authentic and not v.expired and v.issuer == i.did and v.subject == DID_A
    # tamper a signal → signature no longer covers the bytes
    bad = dict(a); bad["signals"] = dict(a["signals"], signed_msgs=999999)
    assert attestation.verify(bad, now=NOW).reason in ("id mismatch", "bad signature")
    # tamper the id
    assert attestation.verify(dict(a, id="0xdeadbeef"), now=NOW).reason == "id mismatch"
    # lie about the subject fingerprint
    assert attestation.verify(dict(a, subject_fp="0000000000000000"), now=NOW).reason in ("id mismatch", "subject fp mismatch")


def test_content_is_deterministic_and_non_purchasable(tmp_path):
    i = ident(tmp_path)
    a = attestation.attest(i, facts(), ASOF, nonce="aaaaaaaaaaaaaaaa")
    b = attestation.attest(i, facts(), ASOF, nonce="bbbbbbbbbbbbbbbb")
    # same public inputs ⇒ identical verdict + signals; only nonce and its derivatives (id, sig) differ
    assert a["signals"] == b["signals"] and a["read"] == b["read"] and a["inputs_digest"] == b["inputs_digest"]
    assert a["nonce"] != b["nonce"] and a["id"] != b["id"] and a["sig"] != b["sig"]
    # byte-identical when the nonce is pinned too
    assert attestation.attest(i, facts(), ASOF, nonce="cccccccccccccccc") == \
           attestation.attest(i, facts(), ASOF, nonce="cccccccccccccccc")


def test_established_band_for_a_solid_agent(tmp_path):
    a = attestation.attest(ident(tmp_path), facts(), ASOF)
    assert a["read"]["band"] == "established" and a["flags"] == []
    assert attestation.verify(a, now=NOW).band == "established"


def test_flagged_first_class_for_a_low_scoring_scammer(tmp_path):
    # thin, malicious agent → the serious flag coincides with a low score → band "flagged"
    a = attestation.attest(ident(tmp_path), facts(signed_msgs=6, days_seen=1, rooms=["a"],
                                                  rooms_active=[], replies_raw=0, replies_weighted=0.0,
                                                  owned_rooms=[], artifacts_ok=0, artifacts_total=0,
                                                  days_since_first_seen=1.0, injection_msgs=2), ASOF)
    assert a["read"]["band"] == "flagged" and "injection" in a["flags"]
    v = attestation.verify(a, now=NOW)
    assert v.ok and "injection" in v.flags


def test_strong_agent_with_a_stray_flag_is_not_branded_flagged(tmp_path):
    # a well-established agent with ONE contract-address mention: flag is surfaced for the reader,
    # but the band follows the (still-high) score — "flagged" is not pinned on the #1 agent.
    a = attestation.attest(ident(tmp_path), facts(contract_spam_msgs=1), ASOF)
    assert "contract_spam" in a["flags"] and a["read"]["band"] != "flagged"
    assert a["read"]["band"] in ("established", "emerging")


def test_insufficient_for_thin_evidence(tmp_path):
    a = attestation.attest(ident(tmp_path), facts(signed_msgs=2, days_seen=1, rooms=["a"],
                                                  rooms_active=[], replies_raw=0, replies_weighted=0.0,
                                                  owned_rooms=[], artifacts_ok=0, artifacts_total=0,
                                                  days_since_first_seen=1.0), ASOF)
    assert a["read"]["band"] == "insufficient"


def test_expiry_rejected(tmp_path):
    a = attestation.attest(ident(tmp_path), facts(), ASOF, window_days=7)
    fresh = attestation.verify(a, now=NOW)                       # NOW == asof-ish, inside window
    late = attestation.verify(a, now=NOW + timedelta(days=8))
    assert fresh.ok and not late.ok and late.expired and late.authentic and late.reason == "expired"


def test_forged_content_with_a_fixed_id_still_fails_on_signature(tmp_path):
    # The id is a public hash anyone can recompute; the real protection is the signature. An attacker
    # bumps a signal AND recomputes a valid id (so the id check passes) — without the issuer key the
    # signature can't be reforged.
    i = ident(tmp_path)
    a = attestation.attest(i, facts(), ASOF, nonce="0011223344556677")
    forged = dict(a); forged["signals"] = dict(a["signals"], signed_msgs=999999)
    core = {k: v for k, v in forged.items() if k not in ("id", "sig")}
    forged["id"] = attestation._att_id(core)                    # attacker fixes the id
    assert attestation.verify(forged, now=NOW).reason == "bad signature"


def test_portable_wrong_issuer_key_fails(tmp_path):
    a = attestation.attest(ident(tmp_path), facts(), ASOF)
    forged = dict(a, issuer=DID_B)                              # claim a different issuer, keep the sig
    assert attestation.verify(forged, now=NOW).reason in ("id mismatch", "bad signature")
