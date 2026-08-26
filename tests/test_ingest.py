from datetime import timedelta

from agentscout.ingest import Ingestor
from conftest import DID_A, DID_B, NOW, msg, room_json


def T(minutes):
    return (NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def setup(server, settings, client, storage):
    server.route("/.well-known/agent.json", body={"version": "0.7.0", "limits": {"reads_per_minute_per_ip": 600}})
    server.route("/r/events?format=json&limit=200", body=room_json("events", [msg(1, T(-100), "server", "created newroom")]))
    server.route("/r/lobby?format=json&limit=200", body=room_json("lobby", [
        msg(10, T(-50), DID_A, "hello from A", 1),
        msg(11, T(-49), "~anon", "unsigned nick"),
        msg(12, T(-40), DID_B, "welcome z6MkvUyg, nice", 2),
    ]))
    server.route("/r/builders?format=json&limit=200", body=room_json("builders", []))
    server.route("/r/newroom?format=json&limit=200", body=room_json("newroom", [msg(1, T(-90), DID_A, "see /kv/guides/technocore-verify", 3)]))
    server.route("/kv/did", body="/kv/did/" + __import__("agentscout.census", fromlist=["fingerprint"]).fingerprint(DID_A) + "\n")
    server.route("/kv/did/" + __import__("agentscout.census", fromlist=["fingerprint"]).fingerprint(DID_A), body=f"!! UNTRUSTED\n\n{DID_A} name:Emeth role:arb\n")
    server.route("/kv/room-owners", body="/kv/room-owners/d-x\n")
    server.route("/kv/room-owners/d-x", body=f"!! UNTRUSTED\n\n{DID_B}\n")
    server.route("/kv/guides/technocore-verify", body="!! UNTRUSTED\n\nsome code\n")
    ing = Ingestor(settings, client, storage)
    ing.discover_limits()
    ing.ensure_config_rooms(NOW)
    return ing


def test_full_cycle_populates_census(server, settings, client, storage):
    ing = setup(server, settings, client, storage)
    assert ing.poll_events(NOW) == 1
    assert "newroom" in storage.rooms_to_poll(NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
    inserted = ing.poll_rooms(NOW)
    assert inserted == 4
    dids = {r["did"] for r in storage.agents()}
    assert dids == {DID_A, DID_B}          # unsigned nick never becomes an agent
    ing.scan_notes(NOW)
    assert storage.note_for_fp(__import__("agentscout.census", fromlist=["fingerprint"]).fingerprint(DID_A))["text"].startswith(DID_A)
    assert storage.owned_rooms_by_did()[DID_B] == ["d-x"]
    assert ing.check_artifacts(NOW) == 1
    assert storage.artifacts_by_did()[DID_A] == (1, 1)
    # cursor persisted; re-poll uses since and inserts nothing new
    server.route("/r/lobby?format=json&limit=200&since=12", body=room_json("lobby", [msg(12, T(-40), DID_B, "dup", 2)], first_seq=12, last_seq=12))
    assert ing.poll_rooms(NOW) == 0


def test_gap_detection_and_dedupe(server, settings, client, storage):
    ing = setup(server, settings, client, storage)
    ing.poll_events(NOW)
    ing.poll_rooms(NOW)
    server.route("/r/lobby?format=json&limit=200&since=12", body=room_json("lobby", [msg(20, T(-1), DID_A, "later", 4)], first_seq=20, last_seq=20))
    ing.poll_rooms(NOW)
    gaps = storage.conn.execute("SELECT * FROM sequence_gaps").fetchall()
    assert len(gaps) == 1 and gaps[0]["expected_seq"] == 13 and gaps[0]["first_available_seq"] == 20
    assert storage.room_state("lobby")["last_seen_seq"] == 20


def test_read_budget_capped_from_agent_json(server, settings, client, storage):
    client.budget.per_minute = 1000
    setup(server, settings, client, storage)
    assert client.budget.per_minute == 120


def test_new_room_watch_expires(server, settings, client, storage):
    ing = setup(server, settings, client, storage)
    ing.poll_events(NOW)
    later = (NOW + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "newroom" not in storage.rooms_to_poll(later)
    assert "lobby" in storage.rooms_to_poll(later)


def test_events_backlog_is_capped_to_newest_rooms(server, settings, client, storage):
    settings.max_event_rooms = 3
    events = [msg(i, T(-200 + i), "server", f"created r{i}") for i in range(1, 51)]
    setup(server, settings, client, storage)
    server.route("/r/events?format=json&limit=200", body=room_json("events", events))
    assert ing_poll(settings, client, storage) == 50
    polled = storage.rooms_to_poll(NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert [r for r in polled if r.startswith("r")] == ["r48", "r49", "r50"]
    assert storage.counts()["rooms_seen"] == 50           # all recorded, few watched


def test_event_rooms_polled_on_slow_cadence(server, settings, client, storage):
    ing = setup(server, settings, client, storage)
    ing.poll_events(NOW)
    ing.poll_rooms(NOW)
    n = len(server.requests)
    ing.poll_rooms(NOW + timedelta(seconds=30))          # config rooms only; newroom skipped
    assert "/r/newroom?format=json&limit=200&since=1" not in server.requests[n:]
    ing.poll_rooms(NOW + timedelta(seconds=200))
    assert any(p.startswith("/r/newroom?") for p in server.requests[n:])


def ing_poll(settings, client, storage):
    from agentscout.ingest import Ingestor
    ing = Ingestor(settings, client, storage)
    ing.ensure_config_rooms(NOW)
    return ing.poll_events(NOW)


def test_cycle_budget_stops_polling_and_rotates(server, settings, client, storage):
    ing = setup(server, settings, client, storage)
    ing.poll_events(NOW)
    import time as _t
    assert ing.poll_rooms(NOW, deadline=_t.monotonic() - 1) == 0          # budget already spent: nothing polled
    n = ing.poll_rooms(NOW, deadline=_t.monotonic() + 60)
    assert n == 4
    storage.touch_room("lobby", (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))   # lobby is now the stalest
    order = storage.rooms_to_poll((NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert order[0] == "lobby"                                            # least-recently-polled first, not alphabetical


def test_docs_watch_warns_once_per_change_and_names_new_keywords(server, settings, client, storage, caplog):
    import logging
    ing = setup(server, settings, client, storage)
    server.route("/llms.txt", body="READ: GET /r/<room>\nLIMITS: two token buckets\n")
    with caplog.at_level(logging.INFO):
        assert ing.watch_docs(NOW) is False                          # baseline, no warning
        assert ing.watch_docs(NOW + timedelta(hours=1)) is False      # not due yet
        assert server.requests.count("/llms.txt") == 1
        server.route("/llms.txt", body="READ: GET /r/<room>\nFAUCET: GET /faucet/<did> hands testnet FLOP to a did:key\n")
        assert ing.watch_docs(NOW + timedelta(hours=7)) is True
    warn = [r for r in caplog.records if r.levelno == logging.WARNING and "DOCS CHANGED" in r.getMessage()]
    assert len(warn) == 1 and "new keywords faucet,flop,testnet" in warn[0].getMessage()
    assert ing.watch_docs(NOW + timedelta(hours=14)) is False          # unchanged since: quiet


def test_sharded_did_notes_are_discovered_without_a_message(server, settings, client, storage):
    """The flat /kv/did is full; a note published only at /kv/did-<2>/<14> by an agent that never
    posted in a watched room must still enter the census. One shard is listed per cycle."""
    from agentscout.census import fingerprint
    from agentscout.ingest import DID_SHARDS

    did_c = "did:key:z6MkshardedOnlyAgentNeverSeenInARoom"
    fp = fingerprint(did_c)
    server.route(f"/kv/did-{fp[:2]}", body=f"/kv/did-{fp[:2]}/{fp[2:]}\n/kv/did-{fp[:2]}/not-a-fingerprint\n")
    server.route(f"/kv/did-{fp[:2]}/{fp[2:]}", body=f"!! UNTRUSTED\n\n{did_c} name:Shardy role:test\n")
    ing = setup(server, settings, client, storage)
    ing.poll_events(NOW)
    ing.poll_rooms(NOW)
    for _ in range(DID_SHARDS):                      # one cycle per shard; unrouted shards 404 -> empty
        ing.scan_notes(NOW)
    assert storage.note_for_fp(fp)["text"].startswith(did_c)
    assert storage.get_setting("did_sharded_keys") == "1"
    assert storage.get_setting("did_namespace_keys") == "1"
    ing.scan_notes(NOW)                              # sweep finished: no further shard listing until the next refresh
    assert ing._shard_cursor == DID_SHARDS
