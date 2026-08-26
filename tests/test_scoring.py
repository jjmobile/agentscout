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
    rows = [(1, T(-30), DID_A, DID_A, True, "hello", "h1"), (2, T(-20), "~nick", None, False, "anon", "h2"),
            (3, T(-25), DID_A, DID_A, True, "second", "h3"), (4, T(-15), DID_B, DID_B, True, "drive-by", "h4")]
    storage.insert_messages("lobby", rows, T(0))
    storage.insert_messages("builders", [(1, T(-10), DID_A, DID_A, True, "third, elsewhere", "h5")], T(0))
    scored = render.score_all(storage, NOW)
    assert [f.did for f, _ in render.newest(scored)] == [DID_A]      # B has one message: not "newest", not news
    assert render.top(scored) == []  # confidence too low
    line = render.digest_line(scored, storage, NOW)
    assert "\n" not in line and line.endswith("Observed behaviour, not endorsement.")
    assert "AGENTSCOUT DIGEST 2026-08-25" in line and "2 new signed identities, 1 of them active" in line
    assert "Ask me" not in line and "SCOUT: me" in render.digest_line(scored, storage, NOW, ask_rooms=["builders", "agentscout"])


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


def _put(storage, room, seq, minutes, did, text, signed=True):
    storage.insert_messages(room, [(seq, T(minutes), did if signed else "~n", did if signed else None, signed, text, f"h{room}{seq}")], T(0))


def test_reply_by_self_declared_handle_and_fingerprint(storage):
    from agentscout.census import fingerprint
    _put(storage, "builders", 1, -300, DID_A, "[Mint Verifier @mint_tracker_d390] Validated stages")
    _put(storage, "builders", 2, -200, DID_A, "[Mint Verifier @mint_tracker_d390] Validated again")
    _put(storage, "builders", 3, -190, DID_B, "[Web-of-Trust @observer_10] Endorsed peer @mint_tracker_d390 for accuracy")
    _put(storage, "builders", 4, -180, DID_C, "see the note at /kv/did/" + fingerprint(DID_A) + " for details")
    _put(storage, "builders", 5, -170, DID_B, "unrelated chatter about nothing")
    f = compute_facts(storage, NOW)[DID_A]
    assert f.handles == ["mint_tracker_d390"]
    assert f.replies_raw == 2 and f.replies_adjacent == 0


def test_adjacency_counts_only_in_quiet_rooms(storage):
    # quiet room: 3 messages over an hour
    _put(storage, "small-room", 1, -60, DID_A, "does anyone know how nonces work here?")
    _put(storage, "small-room", 2, -55, DID_B, "yes, they must increase per key per room")
    _put(storage, "small-room", 3, -50, DID_B, "and a millisecond clock works fine")   # same replier, same hour: counted once
    # busy room: 300 messages in 5 minutes, one of them right after A
    _put(storage, "busy", 1, -30, DID_A, "hello busy room")
    for i in range(2, 302):
        _put(storage, "busy", i, -30 + i * 0.01, DID_C if i % 2 else DID_B, f"noise {i}")
    f = compute_facts(storage, NOW)[DID_A]
    assert f.replies_adjacent == 1 and f.replies_raw == 0
    assert f.replies_weighted == 0.5


def test_fleet_caps_limit_endorsement_spray(storage):
    _put(storage, "r", 1, -100, DID_A, "[Sentinel @target_x] status ok")
    _put(storage, "r", 2, -99, DID_A, "[Sentinel @target_x] status ok again")
    for i in range(3, 15):
        _put(storage, "r", i, -98 + i * 0.1, DID_B, f"[Web-of-Trust @wot_{i}] Endorsed peer @target_x #{i}")
    f = compute_facts(storage, NOW)[DID_A]
    assert f.replies_raw == 3          # per replier per target per day cap


def test_abbreviated_contract_addresses_count_as_contract_spam(storage):
    _put(storage, "builders", 1, -10, DID_A, "[LIVE MINT ACTIVE] morphora on INK (0x585c...fa64) | Stage: Public Live | Supply: 2311")
    assert compute_facts(storage, NOW)[DID_A].contract_spam_msgs == 1


def test_own_did_is_never_listed(storage):
    storage.set_setting("own_did", DID_A)
    storage.insert_messages("lobby", [(1, T(-30), DID_A, DID_A, True, "AGENTSCOUT DIGEST ...", "h1"),
                                      (2, T(-20), DID_B, DID_B, True, "hello from B", "h2"), (3, T(-19), DID_B, DID_B, True, "again", "h3")], T(0))
    storage.insert_messages("builders", [(1, T(-18), DID_B, DID_B, True, "and here", "h4")], T(0))
    scored = render.score_all(storage, NOW)
    assert DID_A not in scored and DID_B in scored
    assert [f.did for f, _r in render.newest(scored)] == [DID_B]


# ---- scale: windowed, streamed census (2026-08-25 OOM incident) ---------------------------------------------

def test_window_limits_agents_and_messages(storage):
    old = (NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    storage.insert_messages("lobby", [(1, old, DID_B, DID_B, True, "ancient history", "h0"), (2, T(-5), DID_A, DID_A, True, "fresh", "h1")], T(0))
    facts = compute_facts(storage, NOW)                     # default 7-day window
    assert set(facts) == {DID_A}
    facts = compute_facts(storage, NOW, window_days=30)
    assert set(facts) == {DID_A, DID_B} and facts[DID_B].signed_msgs == 1


def test_reference_reply_in_busy_room_uses_30_minute_window(storage):
    rows = [(i, T(-120 + i), DID_C, DID_C, True, "noise %d" % i, "n%d" % i) for i in range(100)]   # 100 msgs in 100 min: not quiet
    rows += [(200, T(-40), DID_A, DID_A, True, "I shipped a thing", "ha"),
             (201, T(-35), DID_B, DID_B, True, "nice work z6MkvUyg", "hb"),          # 5 min after A → counts
             (202, T(-5), DID_B, DID_B, True, "again z6MkvUyg", "hc")]                # 35 min after A → too late
    storage.insert_messages("lobby", rows, T(0))
    f = compute_facts(storage, NOW)[DID_A]
    assert f.replies_raw == 1 and f.replies_adjacent == 0 and f.replies_weighted == 1.0


def test_adjacency_only_in_quiet_rooms(storage):
    storage.insert_messages("builders", [(1, T(-600), DID_C, DID_C, True, "morning", "h0"),      # 3 msgs in 10 h: quiet room
                                         (2, T(-30), DID_A, DID_A, True, "does anyone use the signed lane?", "h1"),
                                         (3, T(-25), DID_B, DID_B, True, "yes, works for me", "h2")], T(0))
    f = compute_facts(storage, NOW)[DID_A]
    assert f.replies_adjacent == 1 and f.replies_weighted == 0.5
    assert compute_facts(storage, NOW)[DID_B].replies_adjacent == 0        # B answered A, not the other way round


def test_prune_messages_keeps_agents(storage):
    old = (NOW - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
    storage.insert_messages("lobby", [(1, old, DID_B, DID_B, True, "old", "h0"), (2, T(-5), DID_A, DID_A, True, "new", "h1")], T(0))
    assert storage.prune_messages((NOW - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")) == 1
    assert storage.counts()["messages"] == 1 and storage.counts()["agents"] == 2


def test_agentscouts_own_answers_never_count_as_replies(storage):
    """Milestone D: asking SCOUT and being answered must not raise the asker's score."""
    storage.set_setting("own_did", DID_C)
    storage.insert_messages("builders", [(1, T(-600), DID_B, DID_B, True, "morning", "h0"),
                                         (2, T(-30), DID_A, DID_A, True, "SCOUT: me", "h1"),
                                         (3, T(-29), DID_C, DID_C, True, "AGENTSCOUT re#2 for aaaa | card z6MkvUyg", "h2")], T(0))
    f = compute_facts(storage, NOW)[DID_A]
    assert f.replies_raw == 0 and f.replies_adjacent == 0 and f.replies_weighted == 0.0
