from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, Tuple

import pytest

from agentscout.config import Settings
from agentscout.storage import Storage
from agentscout.technocore import TechnocoreClient

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
DID_A = "did:key:z6MkvUygP3HGGBRazZ5aaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
DID_B = "did:key:z6Mks5fqt4qcsbLEMU15bbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
DID_C = "did:key:z6Mkn5KmNqNDpB4XGUyFcccccccccccccccccccccccccccccc"


class FakeServer:
    """Routes URL paths to canned (status, body) responses; records every request."""

    def __init__(self):
        self.routes: Dict[str, Tuple[int, Dict[str, str], str]] = {}
        self.requests = []
        self.sequence: Dict[str, list] = {}

    def route(self, path_with_query: str, status: int = 200, body="", headers=None):
        if not isinstance(body, str):
            body = json.dumps(body)
        self.routes[path_with_query] = (status, {k.lower(): v for k, v in (headers or {}).items()}, body)

    def route_sequence(self, path_with_query: str, responses):
        self.sequence[path_with_query] = [(s, {k.lower(): v for k, v in (h or {}).items()}, b if isinstance(b, str) else json.dumps(b)) for s, h, b in responses]

    def fetch(self, url: str, timeout: int):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        self.requests.append(path)
        if path in self.sequence and self.sequence[path]:
            return self.sequence[path].pop(0)
        if path in self.routes:
            return self.routes[path]
        return 404, {}, "not found"


def room_json(room, msgs, first_seq=None, last_seq=None):
    return {"room": room, "count": len(msgs), "first_seq": first_seq if first_seq is not None else (msgs[0]["seq"] if msgs else None),
            "last_seq": last_seq if last_seq is not None else (msgs[-1]["seq"] if msgs else 0), "messages": msgs}


def msg(seq, ts, frm, text, nonce=None):
    m = {"seq": seq, "ts": ts, "from": frm, "text": text}
    if nonce is not None:
        m["nonce"] = nonce
    return m


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def client(server):
    return TechnocoreClient("https://example.test", max_reads_per_minute=1000, timeout=5, fetcher=server.fetch, sleep=lambda s: None)


@pytest.fixture
def storage(tmp_path):
    st = Storage(str(tmp_path / "t.db"))
    yield st
    st.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(watch_rooms=["lobby", "builders"], db_path=str(tmp_path / "t.db"), notes_per_cycle=50, artifact_checks_per_cycle=10)
