import json
from datetime import timedelta

from agentscout import render
from agentscout.config import Settings
from agentscout.identity import Identity, public_key_from_did
from agentscout.publisher import Publisher
from conftest import DID_A, NOW, msg, room_json


def T(minutes):
    return (NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def live_settings(tmp_path):
    return Settings(watch_rooms=["lobby"], db_path=str(tmp_path / "t.db"), dry_run=False, publish_enabled=True,
                    identity_key_path=str(tmp_path / "id.key"))


def make(server, client, storage, tmp_path, live=True):
    s = live_settings(tmp_path) if live else Settings(watch_rooms=["lobby"], db_path=str(tmp_path / "t.db"), identity_key_path=str(tmp_path / "id.key"))
    ident, _ = Identity.load_or_create(s.identity_key_path)
    pub = Publisher(s, client, storage, ident)
    pub.owner_verified = True
    return s, ident, pub


class PostCapture:
    """Intercepts POST bodies so tests can verify the signature over room|nonce|swept text."""

    def __init__(self, server, responses):
        self.server, self.responses, self.bodies = server, list(responses), []

    def __call__(self, url, timeout, body=None):
        if body is None:
            return self.server.fetch(url, timeout)
        self.bodies.append(json.loads(body))
        return self.responses.pop(0) if self.responses else (200, {}, json.dumps(room_json("d-agentscout-feed", [], last_seq=1)))


def test_signature_covers_room_nonce_swept_text(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    cap = PostCapture(server, [(200, {}, json.dumps(room_json(s.feed_room, [], last_seq=7)))])
    client._fetch = cap
    storage.enqueue(s.feed_room, "digest", "AGENTSCOUT DIGEST 2026-08-25", "AGENTSCOUT DIGEST 2026-08-25 | a\tb​c | x", T(0))
    row = storage.outbox_pending()[0]
    assert pub.post_signed_line(row, NOW) == "POSTED"
    b = cap.bodies[0]
    assert b["did"] == ident.did and b["text"] == "AGENTSCOUT DIGEST 2026-08-25 | a b c | x"
    import base64
    public_key_from_did(ident.did).verify(base64.urlsafe_b64decode(b["sig"] + "=="), f"{s.feed_room}|{b['nonce']}|{b['text']}".encode())
    done = storage.outbox_has(s.feed_room, "AGENTSCOUT DIGEST 2026-08-25")
    assert done["state"] == "POSTED" and done["posted_seq"] == 7


def test_room_cap_full_parks_the_post_instead_of_burning_attempts(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    cap = PostCapture(server, [(400, {}, "400 room limit reached (10240 is the cap, and this would be a new one). Existing rooms still accept writes")])
    client._fetch = cap
    storage.enqueue("agentscout", "ask-open", "AGENTSCOUT ASK OPENED", "AGENTSCOUT ASK OPENED | hello", T(0))
    row = storage.outbox_pending()[0]
    assert pub.post_signed_line(row, NOW) == "WAITING_ROOM"
    assert storage.outbox_has("agentscout", "AGENTSCOUT ASK OPENED")["state"] == "WAITING_ROOM"
    assert storage.outbox_pending() == []


def test_nonce_strictly_increases_even_if_clock_goes_back(storage):
    n1 = storage.next_nonce("d-x", 1_000_000)
    n2 = storage.next_nonce("d-x", 999_000)
    n3 = storage.next_nonce("d-x", 5_000_000)
    assert n1 == 1_000_000 and n2 == 1_000_001 and n3 == 5_000_000
    assert len(str(n3)) <= 19


def test_5xx_then_landed_is_not_reposted(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    marker = "AGENTSCOUT DIGEST 2026-08-25"
    cap = PostCapture(server, [(502, {}, "bad gateway")])
    client._fetch = cap
    server.route(f"/r/{s.feed_room}?format=json&limit=200", body=room_json(s.feed_room, [msg(3, T(0), ident.did, marker + " | ...", 1)]))
    storage.enqueue(s.feed_room, "digest", marker, marker + " | ...", T(0))
    row = storage.outbox_pending()[0]
    assert pub.post_signed_line(row, NOW) == "POSTED"
    assert storage.outbox_has(s.feed_room, marker)["posted_seq"] == 3
    assert len(cap.bodies) == 1  # exactly one POST


def test_5xx_not_landed_retries_with_fresh_nonce(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    marker = "AGENTSCOUT DIGEST 2026-08-25"
    cap = PostCapture(server, [(503, {}, ""), (200, {}, json.dumps(room_json(s.feed_room, [], last_seq=9)))])
    client._fetch = cap
    server.route(f"/r/{s.feed_room}?format=json&limit=200", body=room_json(s.feed_room, []))
    storage.enqueue(s.feed_room, "digest", marker, marker, T(0))
    row = storage.outbox_pending()[0]
    assert pub.post_signed_line(row, NOW) == "FAILED_RETRYABLE"
    row = storage.outbox_pending()[0]
    assert pub.post_signed_line(row, NOW + timedelta(seconds=30)) == "POSTED"
    assert int(cap.bodies[1]["nonce"]) > int(cap.bodies[0]["nonce"])
    assert cap.bodies[0]["sig"] != cap.bodies[1]["sig"]


def test_403_disables_publishing(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    client._fetch = PostCapture(server, [(403, {}, "owned room: signed by owner only")])
    storage.enqueue(s.feed_room, "digest", "m", "m", T(0))
    assert pub.post_signed_line(storage.outbox_pending()[0], NOW) == "FAILED_FINAL"
    assert pub.owner_verified is False


def test_services_note_and_credence_task_are_deterministic():
    n = render.services_note("agentscout", NOW)
    assert "svc=ask" in n and "svc=history" in n and "svc=attest" in n and "svc=referee" in n
    assert "status=intent" in n and "payment=none-yet" in n and "\n" not in n
    a = render.credence_task_line("agentscout", DID_A, NOW)
    assert a == render.credence_task_line("agentscout", DID_A, NOW.replace(hour=23, minute=59))   # one id per UTC day
    assert a.startswith("TASK v1 | t") and "Daily self-audit" in a and DID_A in a and "\n" not in a


def test_daily_credence_task_enqueued_once_per_day(server, client, storage, tmp_path):
    s = live_settings(tmp_path)
    s.credence_task_enabled = True
    ident, _ = Identity.load_or_create(s.identity_key_path)
    pub = Publisher(s, client, storage, ident)
    pub.owner_verified = True
    client._fetch = PostCapture(server, [])
    storage.insert_messages("lobby", [(1, T(-30), DID_A, DID_A, True, "hello", "h1")], T(0))
    scored = render.score_all(storage, NOW)
    at = NOW.replace(hour=7)
    pub.tick(at, scored)
    task = render.credence_task_line(s.kv_ns, ident.did, at)
    tid = task.split(" | ")[1]
    row = storage.outbox_has("credence", tid)
    assert row is not None and "Daily self-audit" in row["text"] and f"/kv/{s.kv_ns}/services" in row["text"]
    pub.tick(at + timedelta(hours=3), scored)     # same UTC day: no second task
    n = storage.conn.execute("SELECT COUNT(*) FROM outbox WHERE room='credence'").fetchone()[0]
    assert n == 1
    assert Settings(watch_rooms=["lobby"]).credence_task_enabled is False   # off unless opted in


def test_dry_run_never_posts_or_writes(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path, live=False)
    cap = PostCapture(server, [])
    client._fetch = cap
    storage.insert_messages("lobby", [(1, T(-30), DID_A, DID_A, True, "hello", "h1")], T(0))
    scored = render.score_all(storage, NOW)
    pub.tick(NOW.replace(hour=23), scored)
    assert cap.bodies == [] and storage.outbox_pending() == []
    assert storage.outbox_has(s.feed_room, "AGENTSCOUT DIGEST 2026-08-25")["state"] == "DRY_RUN"


def test_note_cas_detects_tamper_and_wins(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    cap = PostCapture(server, [(200, {}, "ok")])
    client._fetch = cap
    assert pub.write_note_cas("agentscout", "top", "v1", NOW)
    assert cap.bodies[0] == {"value": "v1", "if_absent": True}
    conflict = "409 note agentscout/top changed since you read it\n\nto retry: merge ...\ncurrent value follows (4 chars):\nEVIL\n"
    cap.responses = [(409, {}, conflict), (200, {}, "ok")]
    assert pub.write_note_cas("agentscout", "top", "v2", NOW)
    assert cap.bodies[1] == {"value": "v2", "if": "v1"}
    assert cap.bodies[2] == {"value": "v2", "if": "EVIL"}
    assert storage.published_note("agentscout", "top")["tamper_events"] == 1


def test_409_with_our_own_value_means_the_timed_out_write_landed(server, client, storage, tmp_path, caplog):
    import logging
    from agentscout.technocore import parse_conflict_value
    assert parse_conflict_value("409 note x already exists\n\nblah\ncurrent value follows (9 chars):\nalpha one\n") == "alpha one"
    assert parse_conflict_value("weird") is None
    s, ident, pub = make(server, client, storage, tmp_path)
    cap = PostCapture(server, [(409, {}, "409 note agentscout/top already exists\n\nto retry: ...\ncurrent value follows (5 chars):\nv-new\n")])
    client._fetch = cap
    with caplog.at_level(logging.WARNING):
        assert pub.write_note_cas("agentscout", "top", "v-new", NOW) is True
    assert "NOTE_TAMPERED" not in caplog.text and len(cap.bodies) == 1
    assert storage.published_note("agentscout", "top")["tamper_events"] == 0


def test_verify_ownership(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    server.route(f"/kv/room-owners/{s.feed_room}", body=f"!! UNTRUSTED\n\n{ident.did}\n")
    assert pub.verify_ownership() is True
    server.route(f"/kv/room-owners/{s.feed_room}", body=f"!! UNTRUSTED\n\n{DID_A}\n")
    assert pub.verify_ownership() is False


def test_did_note_has_no_secrets_and_is_single_line(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    v = pub.did_note_value()
    seed = open(s.identity_key_path).read().strip()
    assert ident.did in v and seed not in v and "\n" not in v and "name:AgentScout" in v


def test_posted_seq_from_text_reply_or_readback(server, client, storage, tmp_path):
    from agentscout.publisher import Publisher as P
    assert P._seq_from_post_body("# room d-x  messages 1  range 1..7\n!! UNTRUSTED\n[7] ...") == 7
    assert P._seq_from_post_body("ok") is None
    s, ident, pub = make(server, client, storage, tmp_path)
    marker = "AGENTSCOUT DIGEST 2026-08-25"
    client._fetch = PostCapture(server, [(200, {}, "ok")])
    server.route(f"/r/{s.feed_room}?format=json&limit=200", body=room_json(s.feed_room, [msg(4, T(0), ident.did, marker, 1)]))
    storage.enqueue(s.feed_room, "digest", marker, marker, T(0))
    assert pub.post_signed_line(storage.outbox_pending()[0], NOW) == "POSTED"
    assert storage.outbox_has(s.feed_room, marker)["posted_seq"] == 4


def test_stuck_posting_row_is_checked_not_reposted(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    marker = "AGENTSCOUT DIGEST 2026-08-24"
    storage.enqueue(s.feed_room, "digest", marker, marker, T(-30))
    row = storage.outbox_pending()[0]
    storage.outbox_update(row["id"], "POSTING", T(-30), nonce=5, bump_attempts=True)   # crashed here yesterday
    server.route(f"/r/{s.feed_room}?format=json&limit=200", body=room_json(s.feed_room, [msg(3, T(-29), ident.did, marker, 5)]))
    cap = PostCapture(server, [])
    client._fetch = cap
    pub.flush_outbox(NOW)
    assert storage.outbox_has(s.feed_room, marker)["state"] == "POSTED" and cap.bodies == []
    # and when it did NOT land, it goes back to retryable
    storage.enqueue(s.feed_room, "digest", "m2", "m2", T(-30))
    r2 = storage.outbox_has(s.feed_room, "m2")
    storage.outbox_update(r2["id"], "POSTING", T(-30), bump_attempts=True)
    pub.flush_outbox(NOW)
    assert storage.outbox_has(s.feed_room, "m2")["state"] in ("FAILED_RETRYABLE", "POSTED")


def test_ownership_recheck_each_cycle_after_transient_failure(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    pub.owner_verified = False
    server.route_sequence(f"/kv/room-owners/{s.feed_room}", [(500, {}, "boom"), (500, {}, "boom"), (500, {}, "boom"),
                                                             (200, {}, f"!! UNTRUSTED\n\n{ident.did}\n")])
    storage.enqueue(s.feed_room, "digest", "m", "m", T(0))
    cap = PostCapture(server, [(200, {}, json.dumps(room_json(s.feed_room, [], last_seq=2)))])
    client._fetch = cap
    pub.flush_outbox(NOW)                       # 500s: still unverified, nothing posted
    assert pub.owner_verified is False and cap.bodies == []
    pub.flush_outbox(NOW)                       # now verified, and the queued line goes out
    assert pub.owner_verified is True and len(cap.bodies) == 1


def test_failed_notes_are_retried_next_cycle(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    calls = []
    def flaky(url, timeout, body=None):
        if body is None:
            return server.fetch(url, timeout)
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("slow edge")
        return 200, {}, "ok"
    client._fetch = flaky
    pub._pending_notes[("agentscout", "top")] = "v1"
    assert pub.flush_pending_notes(NOW) == 0 and ("agentscout", "top") in pub._pending_notes
    assert pub.flush_pending_notes(NOW) == 1 and not pub._pending_notes


def test_notes_catchup_after_digest_posted_without_notes(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    assert pub.notes_catchup_due(NOW) is False                      # no digest yet
    storage.enqueue(s.feed_room, "digest", "AGENTSCOUT DIGEST 2026-08-25", "x", T(-60))
    row = storage.outbox_has(s.feed_room, "AGENTSCOUT DIGEST 2026-08-25")
    storage.outbox_update(row["id"], "POSTED", T(-59), posted_seq=2)
    assert pub.notes_catchup_due(NOW) is True                       # posted, notes never written
    storage.set_published_note("agentscout", "digest-latest", "v", T(-30))
    storage.set_published_note("agentscout", "top", "v", T(-30))
    assert pub.notes_catchup_due(NOW) is True                       # index never written (a key from a newer build)
    storage.set_published_note("agentscout", "index", "v", T(-30))
    assert pub.notes_catchup_due(NOW) is True                       # services never written (a key from a newer build)
    storage.set_published_note("agentscout", "services", "v", T(-30))
    assert pub.notes_catchup_due(NOW) is False                      # notes newer than the digest
    storage.set_published_note("agentscout", "top", "old", T(-120))
    assert pub.notes_catchup_due(NOW) is True                       # one list stale → refresh


class NoteCapture(PostCapture):
    def __init__(self, server, responses):
        super().__init__(server, responses)
        self.urls = []

    def __call__(self, url, timeout, body=None):
        if body is not None:
            self.urls.append(url.split("example.test", 1)[1])
        return super().__call__(url, timeout, body)


def test_did_note_goes_to_sharded_slot_and_failed_writes_retry_hourly(server, client, storage, tmp_path):
    s, ident, pub = make(server, client, storage, tmp_path)
    s.operator = "x:@someone"
    ns, key = "did-" + ident.fp[:2], ident.fp[2:]
    cap = NoteCapture(server, [(400, {}, "namespace full")])
    client._fetch = cap
    pub.keepalive_did_note(NOW)
    assert cap.urls == [f"/kv/{ns}/{key}"]
    assert cap.bodies[0]["if_absent"] is True and cap.bodies[0]["value"].startswith(ident.did)
    assert "operator:x:@someone" in cap.bodies[0]["value"]
    assert storage.published_note(ns, key) is None                       # a failure is never recorded as published
    pub.keepalive_did_note(NOW + timedelta(minutes=30))                  # inside the retry window: no request
    assert len(cap.urls) == 1
    cap.responses = [(200, {}, "ok")]
    pub.keepalive_did_note(NOW + timedelta(hours=2))
    assert len(cap.urls) == 2 and cap.bodies[1]["if_absent"] is True
    assert storage.published_note(ns, key)["value"] == pub.did_note_value()
    pub.keepalive_did_note(NOW + timedelta(hours=3))                     # fresh and unchanged: nothing to do
    assert len(cap.urls) == 2
    s.operator = "x:@renamed"                                            # changed text is rewritten at once, as a CAS
    cap.responses = [(200, {}, "ok")]
    pub.keepalive_did_note(NOW + timedelta(hours=4))
    assert len(cap.urls) == 3 and cap.bodies[2]["if"] == cap.bodies[1]["value"]


def test_protocol_change_is_published_once_as_a_signed_line_and_a_note(server, client, storage, tmp_path):
    from agentscout import radar
    s, ident, pub = make(server, client, storage, tmp_path)
    old = {"version": "0.7.0", "limits": {"reads_per_minute_per_ip": 600}}
    new = {"version": "0.9.7", "limits": {"reads_per_minute_per_ip": 900}}
    change = radar.compare(NOW, "## READ\n", "## READ\n## FAUCET\nGET /faucet/<did>\n", old, new)
    storage.set_doc_snapshot("agent.json", json.dumps(new), T(0))
    storage.add_protocol_change(change.ts, change.old_version, change.new_version, change.summary(), change.to_json())
    server.route("/kv/agentscout/protocol", status=500, body="nope")          # note write fails: stays pending
    assert pub.publish_protocol_change(NOW) == 1
    row = storage.outbox_has(s.feed_room, "TECHNOCORE CHANGE 2026-08-25T12:00Z")
    assert row is not None and row["kind"] == "protocol" and "v0.7.0 → v0.9.7" in row["text"] and "+FAUCET" in row["text"]
    assert ("agentscout", "protocol") in pub._pending_notes and "agent.json-version=0.9.7" in pub._pending_notes[("agentscout", "protocol")]
    assert pub.publish_protocol_change(NOW + timedelta(hours=1)) == 0        # already published: nothing new
    assert storage.unpublished_protocol_changes() == []
