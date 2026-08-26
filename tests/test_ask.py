from datetime import timedelta

from agentscout.ask import OPENER_MARKER, Asker, parse_ask
from agentscout.config import Settings
from conftest import DID_A, DID_B, DID_C, NOW

OWN = "did:key:z6MkwNoeDd24jWouuvbQkuCwf3a1o14ToqJiKezPcBQc3A7q"


def T(minutes):
    return (NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_parse_exact_commands_only():
    assert parse_ask("SCOUT: me") == ("me", None)
    assert parse_ask("  scout: TOP 3 ") == ("top", "3")
    assert parse_ask("SCOUT: who b6711fbd") == ("who", "b6711fbd")
    assert parse_ask("SCOUT: rising now") == ("rising", None)
    assert parse_ask("SCOUT: rank me") is None
    assert parse_ask("please SCOUT: top") is None
    assert parse_ask("SCOUT: who ../etc") is None


def seed(storage, asks):
    """DID_A is an active agent; `asks` are (seq, did, text) lines in the ask room."""
    storage.set_setting("own_did", OWN)
    storage.insert_messages("lobby", [(1, T(-120), DID_A, DID_A, True, "hello there", "h1"), (2, T(-110), DID_A, DID_A, True, "more", "h2")], T(0))
    storage.insert_messages("builders", [(1, T(-100), DID_A, DID_A, True, "third", "h3")], T(0))
    for room in ("agentscout", "builders", "meta", "general", "infra", "ai", "alpha", "introductions"):
        storage.set_setting(f"ask_last_seq:{room}", "0")
    rows = [(seq, T(-5 + i), did, did, True, text, "a%d" % seq) for i, (seq, did, text) in enumerate(asks)]
    storage.insert_messages("agentscout", rows, T(0))


def make(storage, tmp_path, live=True, **kw):
    s = Settings(db_path=str(tmp_path / "t.db"), dry_run=False, publish_enabled=True, replies_enabled=live, **kw)
    return Asker(s, storage, OWN, live=live)


def scored_for(storage):
    from agentscout import render
    return lambda: render.score_all(storage, NOW)


def test_first_run_never_answers_a_backlog(storage, tmp_path):
    storage.set_setting("own_did", OWN)
    storage.insert_messages("agentscout", [(7, T(-5), DID_A, DID_A, True, "SCOUT: me", "x")], T(0))
    asker = make(storage, tmp_path)
    assert asker.tick(NOW, scored_for(storage)) == 0 and storage.get_setting("ask_last_seq:agentscout") == "7"
    assert storage.outbox_pending() == []


def test_replies_are_signed_outbox_lines_with_marker(storage, tmp_path):
    seed(storage, [(1, DID_A, "SCOUT: me"), (2, DID_A, "not a command"), (3, DID_A, "SCOUT: top 2"), (4, OWN, "SCOUT: me")])
    asker = make(storage, tmp_path)
    assert asker.tick(NOW, scored_for(storage)) == 2
    rows = storage.outbox_pending()
    assert [r["marker"] for r in rows] == ["AGENTSCOUT re#1", "AGENTSCOUT re#3"]
    assert rows[0]["room"] == "agentscout" and rows[0]["text"].startswith(f"AGENTSCOUT re#1 for {DID_A}"[:20])
    assert "score" in rows[0]["text"] and rows[0]["text"].endswith("Observed behaviour, not endorsement.")
    assert "TOP" in rows[1]["text"]
    assert storage.counters(NOW.strftime("%Y-%m-%d")) == {"ask_replied": 2}
    assert asker.tick(NOW, scored_for(storage)) == 0        # nothing new: nothing answered twice


def test_log_only_mode_writes_nothing(storage, tmp_path):
    seed(storage, [(1, DID_A, "SCOUT: me")])
    asker = make(storage, tmp_path, live=False)
    asker.ensure_room(NOW)
    assert asker.tick(NOW, scored_for(storage)) == 0
    assert storage.outbox_pending() == []
    assert storage.counters(NOW.strftime("%Y-%m-%d")) == {"ask_would_reply": 1}


def test_quota_per_did_capacity_once_per_day_then_silence(storage, tmp_path):
    seed(storage, [(i, DID_A, "SCOUT: top %d" % i) for i in range(1, 7)])
    asker = make(storage, tmp_path, max_replies_per_did_per_hour=3)
    assert asker.tick(NOW, scored_for(storage)) == 4          # 3 answers + one CAPACITY_REACHED line
    texts = [r["text"] for r in storage.outbox_pending(limit=10)]
    assert sum("CAPACITY_REACHED" in t for t in texts) == 1
    states = [r["state"] for r in storage.conn.execute("SELECT state FROM ask_requests ORDER BY seq")]
    assert states == ["REPLIED", "REPLIED", "REPLIED", "CAPACITY", "CAPACITY_SILENT", "CAPACITY_SILENT"]
    # quotas survive a restart: a fresh Asker over the same DB stays silent
    storage.insert_messages("agentscout", [(9, T(0), DID_A, DID_A, True, "SCOUT: top 9", "a9")], T(0))
    assert make(storage, tmp_path, max_replies_per_did_per_hour=3).tick(NOW, scored_for(storage)) == 0


def test_same_command_within_an_hour_answered_once(storage, tmp_path):
    seed(storage, [(1, DID_A, "SCOUT: me"), (2, DID_A, "SCOUT: me")])
    assert make(storage, tmp_path).tick(NOW, scored_for(storage)) == 1


def test_unknown_agent_and_unsigned_are_handled(storage, tmp_path):
    seed(storage, [(1, DID_B, "SCOUT: who nobody00"), (2, DID_C, "SCOUT: me")])
    storage.insert_messages("agentscout", [(3, T(0), "~anon", None, False, "SCOUT: top", "u")], T(0))
    asker = make(storage, tmp_path)
    assert asker.tick(NOW, scored_for(storage)) == 2
    texts = [r["text"] for r in storage.outbox_pending()]
    assert "not in the census" in texts[0] and texts[0].startswith("AGENTSCOUT re#1 for ")
    assert "score" in texts[1] and "for 67f1e87e" in texts[1]     # its ask was in a watched room → it is in the census
    storage.insert_messages("agentscout", [(5, T(0), DID_B, DID_B, True, "SCOUT: top 2", "a5")], T(0))
    assert asker.tick(NOW, scored_for(storage)) == 1
    assert "TOP" in storage.outbox_pending(limit=10)[-1]["text"]                    # top works for an unknown asker


def test_ensure_room_opens_once_and_keeps_alive_weekly(storage, tmp_path):
    from agentscout.ask import ask_room_open
    asker = make(storage, tmp_path)
    asker.ensure_room(NOW)
    asker.ensure_room(NOW + timedelta(days=1))
    assert [r["kind"] for r in storage.outbox_pending(limit=10)] == ["ask-open"]     # help waits for the room to exist
    assert not ask_room_open(storage, "agentscout")
    opener = storage.outbox_has("agentscout", OPENER_MARKER)
    storage.outbox_update(opener["id"], "POSTED", T(1), posted_seq=1)
    assert ask_room_open(storage, "agentscout")
    asker.ensure_room(NOW + timedelta(days=1))
    asker.ensure_room(NOW + timedelta(days=8))
    assert [r["kind"] for r in storage.outbox_pending(limit=10)] == ["ask-help", "ask-help"]


def test_room_cap_full_parks_the_opener_and_retries_hourly(storage, tmp_path):
    asker = make(storage, tmp_path)
    asker.ensure_room(NOW)
    opener = storage.outbox_has("agentscout", OPENER_MARKER)
    storage.outbox_update(opener["id"], "WAITING_ROOM", T(0), error="400 room limit reached", bump_attempts=True)
    asker.ensure_room(NOW + timedelta(minutes=30))
    assert storage.outbox_pending() == []                                            # parked: not retried every cycle
    asker.ensure_room(NOW + timedelta(minutes=61))
    row = storage.outbox_has("agentscout", OPENER_MARKER)
    assert row["state"] == "PENDING" and row["attempts"] == 0


def test_requests_in_open_rooms_are_answered_in_the_same_room(storage, tmp_path):
    from agentscout.ask import open_ask_rooms
    seed(storage, [])
    storage.insert_messages("builders", [(50, T(-2), DID_A, DID_A, True, "SCOUT: me", "b50")], T(0))
    storage.insert_messages("lobby", [(60, T(-1), DID_A, DID_A, True, "SCOUT: top", "l60")], T(0))    # lobby is not an ask room
    asker = make(storage, tmp_path)
    assert asker.tick(NOW, scored_for(storage)) == 1
    row = storage.outbox_pending()[0]
    assert row["room"] == "builders" and row["marker"] == "AGENTSCOUT re#50"
    assert open_ask_rooms(storage, asker.s) == ["builders", "meta", "general", "infra", "ai", "alpha", "introductions"]
