import pytest

from agentscout.technocore import RateLimited, ReadBudget, TechnocoreClient, TechnocoreError, strip_banner
from conftest import msg, room_json


def test_read_room_omits_since_zero_and_parses(server, client):
    server.route("/r/lobby?format=json&limit=200", body=room_json("lobby", [msg(5, "2026-08-25T00:00:00Z", "did:key:z6Mkabc", "hi", 1)]))
    data = client.read_room("lobby", since=0)
    assert data["last_seq"] == 5
    assert server.requests == ["/r/lobby?format=json&limit=200"]


def test_read_room_with_since_and_wait(server, client):
    server.route("/r/lobby?format=json&limit=200&since=5&wait=10", body=room_json("lobby", []))
    client.read_room("lobby", since=5, wait=10)
    assert server.requests[-1].endswith("since=5&wait=10")


def test_429_honours_body_seconds_then_succeeds(server):
    sleeps = []
    c = TechnocoreClient("https://example.test", fetcher=server.fetch, sleep=sleeps.append, max_reads_per_minute=1000)
    server.route_sequence("/r/lobby?format=json&limit=200", [
        (429, {}, "rate limited: reads bucket, refill 10/s, wait 7 seconds"),
        (200, {}, room_json("lobby", [])),
    ])
    c.read_room("lobby")
    assert sleeps == [7.0]


def test_429_gives_up_after_max_attempts(server):
    c = TechnocoreClient("https://example.test", fetcher=server.fetch, sleep=lambda s: None, max_attempts=2, max_reads_per_minute=1000)
    server.route_sequence("/r/lobby?format=json&limit=200", [(429, {"Retry-After": "3"}, "x"), (429, {"Retry-After": "3"}, "x")])
    with pytest.raises(RateLimited) as exc:
        c.read_room("lobby")
    assert exc.value.retry_after == 3.0


def test_5xx_retries_then_raises(server):
    c = TechnocoreClient("https://example.test", fetcher=server.fetch, sleep=lambda s: None, max_attempts=3, max_reads_per_minute=1000)
    server.route_sequence("/r/lobby?format=json&limit=200", [(502, {}, ""), (503, {}, ""), (500, {}, "")])
    with pytest.raises(TechnocoreError):
        c.read_room("lobby")
    assert len(server.requests) == 3


def test_note_404_is_none_and_banner_stripped(server, client):
    server.route("/kv/did/abcdefabcdefabcd", body="!! UNTRUSTED CONTENT — blah\n\ndid:key:z6MkX name:Foo\n")
    assert client.read_note("did", "abcdefabcdefabcd") == "did:key:z6MkX name:Foo"
    assert client.read_note("did", "0000000000000000") is None


def test_list_note_keys_parses_listing(server, client):
    server.route("/kv/did", body="# 3 keys\n/kv/did/0013983cce1bdff6\n/kv/did/z6mkjtk7safraohudckm\n/kv/did/Bad Key\n")
    assert client.list_note_keys("did") == ["0013983cce1bdff6", "z6mkjtk7safraohudckm"]


def test_invalid_names_rejected_before_network(client):
    with pytest.raises(ValueError):
        client.read_room("Not A Room")
    with pytest.raises(ValueError):
        client.read_note("did", "../etc")


def test_read_budget_sleeps_when_exhausted():
    t = [0.0]
    sleeps = []
    b = ReadBudget(2, clock=lambda: t[0], sleep=lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)))
    b.acquire(); b.acquire(); b.acquire()
    assert len(sleeps) == 1 and 59 < sleeps[0] <= 61


def test_strip_banner_keeps_value_only():
    assert strip_banner("!! UNTRUSTED CONTENT\n\n# comment\nvalue here\n") == "value here"


def test_did_note_path_is_sharded_and_read_falls_back_to_legacy(server, client):
    from agentscout.technocore import did_note_path
    assert did_note_path("f55e08357263dd0f") == ("did-f5", "5e08357263dd0f")
    server.route("/kv/did/aa11223344556677", body="!! UNTRUSTED\n\nlegacy note\n")
    assert client.read_did_note("aa11223344556677") == "legacy note"
    assert server.requests[-2:] == ["/kv/did-aa/11223344556677", "/kv/did/aa11223344556677"]
    server.route("/kv/did-bb/11223344556677", body="!! UNTRUSTED\n\nsharded note\n")
    server.route("/kv/did/bb11223344556677", body="!! UNTRUSTED\n\nstale legacy\n")
    assert client.read_did_note("bb11223344556677") == "sharded note"
    assert client.read_did_note("cc11223344556677") is None
