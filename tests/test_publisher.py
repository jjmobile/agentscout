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
    cap.responses = [(409, {}, "!! UNTRUSTED\n\nEVIL\n"), (200, {}, "ok")]
    assert pub.write_note_cas("agentscout", "top", "v2", NOW)
    assert cap.bodies[1] == {"value": "v2", "if": "v1"}
    assert cap.bodies[2] == {"value": "v2", "if": "EVIL"}
    assert storage.published_note("agentscout", "top")["tamper_events"] == 1


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
