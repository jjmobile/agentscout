from datetime import timedelta

from agentscout import render
from agentscout.census import AgentFacts, compute_facts, fingerprint, is_opaque, is_signed, normalize_text, parse_note
from agentscout.scoring import DEFAULT_WEIGHTS, confidence, score
from conftest import DID_A, DID_B, DID_C, NOW


def T(minutes):
    return (NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_fingerprint_matches_convention():
    # first 16 hex of sha256 of the did string, lowercase
    import hashlib
    d = "did:key:z6MkpYzNStzW9kGcpZnATG31o34qvSdGA6EZDPX5S8tLEbEY"
    assert fingerprint(d) == hashlib.sha256(d.encode()).hexdigest()[:16]


def test_is_signed_requires_did_and_nonce():
    assert is_signed({"from": DID_A, "nonce": 1})
    assert not is_signed({"from": DID_A})
    assert not is_signed({"from": "z6Mkfake", "nonce": 1})


def test_normalize_is_nfkc_casefold_collapsed():
    assert normalize_text("  Ｈello   WORLD ") == "hello world"


def test_parse_note_labels_and_did():
    did, fields = parse_note(f"{DID_A} x25519:abc mailbox:mb-p-1 name:zcode role:helper")
    assert did == DID_A and fields["name"] == "zcode" and fields["mailbox"] == "mb-p-1"


def facts(**kw):
    base = dict(did=DID_A, fp="f" * 16, first_seen=T(-10), last_seen=T(0))
    base.update(kw)
    return AgentFacts(**base)


def test_weights_sum_and_note_fields_zero():
    assert DEFAULT_WEIGHTS.total() == 100 and DEFAULT_WEIGHTS.note_fields == 0


def test_score_deterministic_and_bounded():
    f = facts(signed_msgs=40, days_seen=14, rooms=list("abcdef"), rooms_active=list("abcdef"), replies_weighted=10, owned_rooms=["d-a"], artifacts_ok=3)
    r1, r2 = score(f), score(f)
    assert r1.score == r2.score == 99
    assert r1.components == r2.components


def test_penalties_apply():
    f = facts(signed_msgs=10, days_seen=5, rooms=["a"], dup_ratio=0.8, max_per_hour=40, cross_room_identical=1, contract_spam_msgs=1, injection_msgs=1)
    r = score(f)
    assert set(r.penalties) == {"duplicates", "burst", "cross_room", "contract_spam", "injection"}
    assert r.score == 0


def test_confidence_separate_from_score():
    new = facts(signed_msgs=2, days_seen=1, rooms=["a"], days_since_first_seen=0.1, owned_rooms=["d-a", "d-b"], artifacts_ok=1)
    old = facts(signed_msgs=30, days_seen=10, rooms=["a", "b", "c"], rooms_active=["a", "b", "c"], days_since_first_seen=20)
    assert score(new).score > 0 and confidence(new) < 30
    assert confidence(old) == 99


def test_reply_detection_and_sockpuppet_dampening(storage):
    rows = [
        (1, T(-60), DID_A, DID_A, True, "hello, I build things", "h1"),
        (2, T(-55), DID_B, DID_B, True, "welcome z6MkvUyg good stuff", "h2"),   # B is old & scored → full weight
        (3, T(-50), DID_C, DID_C, True, "thanks z6MkvUyg", "h3"),               # C is brand new → 0.25
        (4, T(-10), DID_C, DID_C, True, "unrelated", "h4"),
    ]
    storage.insert_messages("lobby", rows, T(0))
    storage.conn.execute("UPDATE agents SET first_seen=? WHERE did=?", (T(-60 * 24 * 10), DID_B))
    for i in range(12):
        storage.insert_messages("r%d" % (i % 4), [(100 + i, (NOW - timedelta(days=i % 6)).strftime("%Y-%m-%dT%H:%M:%SZ"), DID_B, DID_B, True, "message number %d about protocol design" % i, "x%d" % i)], T(0))
    prelim = {d: score(f).score for d, f in compute_facts(storage, NOW).items()}
    f = compute_facts(storage, NOW, prelim_scores=prelim)[DID_A]
    assert f.replies_raw == 2
    assert f.replies_weighted == 1.25


def test_render_lists_and_digest_work_with_only_db_rows(storage):
    rows = [(1, T(-30), DID_A, DID_A, True, "hello", "h1"), (2, T(-20), "~nick", None, False, "anon", "h2")]
    storage.insert_messages("lobby", rows, T(0))
    scored = render.score_all(storage, NOW)
    assert [f.did for f, _ in render.newest(scored)] == [DID_A]
    assert render.top(scored) == []  # confidence too low
    line = render.digest_line(scored, storage, NOW)
    assert "\n" not in line and line.endswith("Observed behaviour, not endorsement.")
    assert "AGENTSCOUT DIGEST 2026-08-25" in line and "1 new signed agents" in line


def test_unsigned_never_listed(storage):
    storage.insert_messages("lobby", [(1, T(-1), "~someone", None, False, "I am the best agent", "h")], T(0))
    assert storage.agents() == []


def test_owning_many_empty_rooms_and_driveby_posts_do_not_score():
    gamer = facts(signed_msgs=20, days_seen=1, rooms=[f"r{i}" for i in range(20)], rooms_active=[], owned_rooms=[f"d-{i}" for i in range(19)])
    r = score(gamer)
    assert r.components["rooms"] == 0 and r.components["artifacts"] == 10.0 and r.score <= 13


def test_opaque_detection():
    assert is_opaque("enc:v1:b4XJoXGtfcvp88FV:CxUy0dqCMEtqDYzhF+jg96hKzlrrQAiKKtXGd+WP5+wmiVabcdef1234567890")
    assert is_opaque("a3f9c2e1b7d4a3f9c2e1b7d4a3f9c2e1b7d4a3f9c2e1b7d4")
    assert not is_opaque("i am did:key:z6MkucThNe5Cq2g6Ln1V54BAZSp8KNQXXgF5xEo3T1E4bKze. onboarding today")
    assert not is_opaque("!result roll=4 commit_seq=1 reveal_seq=3 winners=did:key:z6MkidnuPhM7S5gudSyVE5v7hojiFKLLwURS7ta6FKDPKdDo")
    assert not is_opaque("Contribution 2: verify.py -- an independent Ed25519 signature verifier. Source at /kv/guides/technocore-verify")


def test_opaque_penalty_applies_only_with_enough_messages():
    assert "opaque" in score(facts(signed_msgs=6, opaque_ratio=0.9)).penalties
    assert "opaque" not in score(facts(signed_msgs=3, opaque_ratio=1.0)).penalties


def test_flop_teaser_line_in_digest(storage):
    storage.insert_messages("lobby", [(1, T(-30), DID_A, DID_A, True, "FLOP agent 13 check-in", "h1"),
                                      (2, T(-20), DID_B, DID_B, True, "gm flop family", "h2"),
                                      (3, T(-10), DID_A, DID_A, True, "unrelated", "h3"),
                                      (4, T(-5), "~nick", None, False, "flop flop flop", "h4")], T(0))
    assert storage.flop_mentions_since(T(-60)) == (2, 2)      # signed only
    line = render.digest_line(render.score_all(storage, NOW), storage, NOW)
    assert "💸 FLOP paid/received: ??? — nobody can yet. Mentioned 2× by 2 agents today." in line
    assert line.endswith("Observed behaviour, not endorsement.")
