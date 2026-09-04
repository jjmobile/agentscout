import json
from datetime import timedelta

from agentscout import tclk
from agentscout.config import Settings
from agentscout.identity import Identity
from agentscout.publisher import Publisher
from agentscout.selfaudit import SelfAudit
from conftest import DID_A, DID_B, NOW


def T(minutes):
    return (NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


TOP_NOTE = ("agentscout top asof=2026-08-25T06:00Z names-self-asserted ; "
            "07a5169d2e83cf5c did:key:z6MkAAAA score=59 conf=99 msgs=71 rooms=3 why=days:8")
DIGEST_NOTE = "AGENTSCOUT DIGEST 2026-08-25 | 24h: ... | As of 2026-08-25T06:00Z | Observed behaviour, not endorsement."


def make(server, client, storage, tmp_path):
    s = Settings(watch_rooms=["lobby"], db_path=str(tmp_path / "t.db"), dry_run=False,
                 publish_enabled=True, identity_key_path=str(tmp_path / "id.key"),
                 credence_task_enabled=True)
    ident, _ = Identity.load_or_create(s.identity_key_path)
    pub = Publisher(s, client, storage, ident)
    pub.owner_verified = True
    storage.set_published_note("agentscout", "top", TOP_NOTE, T(-30))
    storage.set_published_note("agentscout", "digest-latest", DIGEST_NOTE, T(-30))
    return s, ident, SelfAudit(s, storage, ident, pub)


def submit(tid, text):
    return f"SUBMIT v1 | {tid} | {text}"


def test_honest_submission_gets_useful_and_fabrication_gets_not(server, settings, client, storage, tmp_path):
    s, ident, aud = make(server, client, storage, tmp_path)
    tid = SelfAudit.task_id(NOW.strftime("%Y-%m-%d"))
    storage.insert_messages("credence", [
        (1, T(-20), DID_A, DID_A, True,
         submit(tid, "GET both notes: HTTP 200, asof=2026-08-25T06:00Z, top #1 fp 07a5169d matches digest"), "h1"),
        (2, T(-15), DID_B, DID_B, True,
         submit(tid, "[AI-RESEARCH] GET returned HTTP 200 OK, asof=2026-08-25T00:00:00Z as expected"), "h2"),
    ], T(0))
    aud.tick(NOW)
    a = storage.outbox_has("credence", f"audit-vouch-{tid}-{DID_A[-8:]}")
    b = storage.outbox_has("credence", f"audit-vouch-{tid}-{DID_B[-8:]}")
    assert a is not None and "| useful |" in a["text"] and "ground truth is my own output" in a["text"]
    assert b is not None and "| not |" in b["text"] and "2026-08-25T00:00Z" in b["text"]
    aud.tick(NOW + timedelta(minutes=5))                        # idempotent: no duplicates
    n = storage.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='audit-vouch'").fetchone()[0]
    assert n == 2


def test_partial_evidence_gets_no_vouch_and_cap_holds(server, settings, client, storage, tmp_path):
    s, ident, aud = make(server, client, storage, tmp_path)
    tid = SelfAudit.task_id(NOW.strftime("%Y-%m-%d"))
    storage.insert_messages("credence", [
        (1, T(-20), DID_A, DID_A, True, submit(tid, "fp 07a5169d present, no timestamps to report"), "h1"),
    ], T(0))
    aud.tick(NOW)
    assert storage.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='audit-vouch'").fetchone()[0] == 0
    # unrelated SUBMITs to other tasks are ignored
    storage.insert_messages("credence", [
        (2, T(-10), DID_B, DID_B, True, submit("tsomeoneelse", "asof=2026-08-25T06:00Z 07a5169d2e83cf5c"), "h2"),
    ], T(0))
    aud.tick(NOW)
    assert storage.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='audit-vouch'").fetchone()[0] == 0
