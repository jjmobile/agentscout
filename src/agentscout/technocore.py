"""Read-only Technocore HTTP client (Milestone A).

Only GET. Only the configured base host. Self-imposed read budget plus honest handling of
the server's 429/5xx. Everything returned is untrusted data.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

log = logging.getLogger("agentscout.technocore")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_BANNER_PREFIXES = ("!! UNTRUSTED", "# ")
_RETRY_SECS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s(?:ec|econds)?\b", re.IGNORECASE)


class TechnocoreError(Exception):
    pass


class RateLimited(TechnocoreError):
    def __init__(self, retry_after: float, body: str = ""):
        super().__init__(f"rate limited, retry after {retry_after:.1f}s")
        self.retry_after = retry_after
        self.body = body


Fetcher = Callable[..., Tuple[int, Dict[str, str], str]]
"""(url, timeout[, body bytes]) -> (status, lowercase headers, body text). Injected in tests."""


def _urllib_fetch(url: str, timeout: int, body: Optional[bytes] = None) -> Tuple[int, Dict[str, str], str]:
    headers = {"User-Agent": "agentscout/0.1 (+network observer)"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — host validated in config
            body = resp.read().decode("utf-8", "replace")
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, body


class ReadBudget:
    """Sliding-window cap on reads per minute (self-imposed, below the server's bucket)."""

    def __init__(self, per_minute: int, clock=time.monotonic, sleep=time.sleep):
        self.per_minute = max(1, per_minute)
        self._clock = clock
        self._sleep = sleep
        self._stamps: Deque[float] = deque()

    def acquire(self) -> None:
        now = self._clock()
        while self._stamps and now - self._stamps[0] >= 60.0:
            self._stamps.popleft()
        if len(self._stamps) >= self.per_minute:
            wait = 60.0 - (now - self._stamps[0]) + 0.05
            log.debug("read budget exhausted, sleeping %.1fs", wait)
            self._sleep(wait)
            now = self._clock()
            while self._stamps and now - self._stamps[0] >= 60.0:
                self._stamps.popleft()
        self._stamps.append(self._clock())


class TechnocoreClient:
    def __init__(
        self,
        base_url: str,
        max_reads_per_minute: int = 120,
        timeout: int = 20,
        fetcher: Fetcher = _urllib_fetch,
        sleep=time.sleep,
        clock=time.monotonic,
        max_attempts: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._fetch = fetcher
        self._sleep = sleep
        self.budget = ReadBudget(max_reads_per_minute, clock=clock, sleep=sleep)
        self.max_attempts = max_attempts
        self.reads = 0
        self.writes = 0

    # ---- low level -------------------------------------------------------------------

    def _url(self, path: str, params: Optional[Dict[str, object]] = None) -> str:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def get(self, path: str, params: Optional[Dict[str, object]] = None) -> Tuple[int, str]:
        """GET with budget, 429 honouring and bounded 5xx/connection retries. Returns (status, body)."""
        url = self._url(path, params)
        attempt = 0
        while True:
            attempt += 1
            self.budget.acquire()
            self.reads += 1
            try:
                status, headers, body = self._fetch(url, self.timeout)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                if attempt >= self.max_attempts:
                    raise TechnocoreError(f"GET {path}: {exc.__class__.__name__}") from exc
                delay = min(30.0, 2.0 ** attempt) + random.uniform(0, 1)
                log.info("GET %s connection error (%s); retry %d in %.1fs", path, exc.__class__.__name__, attempt, delay)
                self._sleep(delay)
                continue
            if status == 429:
                wait = self._retry_after(headers, body)
                if attempt >= self.max_attempts:
                    raise RateLimited(wait, body)
                log.warning("GET %s 429; waiting %.1fs (%s)", path, wait, body.strip()[:120])
                self._sleep(wait)
                continue
            if status >= 500:
                if attempt >= self.max_attempts:
                    raise TechnocoreError(f"GET {path}: HTTP {status}")
                delay = min(30.0, 2.0 ** attempt) + random.uniform(0, 1)
                log.info("GET %s HTTP %d; retry %d in %.1fs", path, status, attempt, delay)
                self._sleep(delay)
                continue
            return status, body

    @staticmethod
    def _retry_after(headers: Dict[str, str], body: str) -> float:
        ra = headers.get("retry-after")
        if ra:
            try:
                return max(1.0, float(ra))
            except ValueError:
                pass
        m = _RETRY_SECS_RE.search(body or "")
        if m:
            return max(1.0, float(m.group(1)))
        return 10.0

    def get_json(self, path: str, params: Optional[Dict[str, object]] = None) -> dict:
        status, body = self.get(path, params)
        if status != 200:
            raise TechnocoreError(f"GET {path}: HTTP {status}: {body.strip()[:200]}")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise TechnocoreError(f"GET {path}: non-JSON body") from exc
        if not isinstance(data, dict):
            raise TechnocoreError(f"GET {path}: unexpected JSON shape")
        return data

    # ---- protocol surface --------------------------------------------------------------

    @staticmethod
    def _check_name(name: str, what: str) -> str:
        if not NAME_RE.match(name):
            raise ValueError(f"invalid {what} {name!r}")
        return name

    def agent_card(self) -> dict:
        return self.get_json("/.well-known/agent.json")

    def read_room(self, room: str, since: Optional[int] = None, limit: int = 200, wait: Optional[int] = None) -> dict:
        """Newest `limit` messages after `since` (oldest first). since=0 is a server error: omit it."""
        self._check_name(room, "room")
        params: Dict[str, object] = {"format": "json", "limit": max(1, min(200, limit))}
        if since is not None and since > 0:
            params["since"] = since
            if wait:
                params["wait"] = max(0, min(10, wait))
        data = self.get_json(f"/r/{room}", params)
        msgs = data.get("messages")
        if not isinstance(msgs, list):
            raise TechnocoreError(f"room {room}: missing messages list")
        return data

    def read_events(self, since: Optional[int] = None, limit: int = 200) -> dict:
        return self.read_room("events", since=since, limit=limit)

    def list_rooms(self, limit: int = 200) -> dict:
        return self.get_json("/rooms", {"format": "json", "limit": limit})

    def list_note_keys(self, ns: str) -> List[str]:
        """Keys of a namespace, parsed from the text listing (`/kv/<ns>/<key>` per line)."""
        self._check_name(ns, "namespace")
        status, body = self.get(f"/kv/{ns}")
        if status == 404:
            return []
        if status != 200:
            raise TechnocoreError(f"list /kv/{ns}: HTTP {status}")
        prefix = f"/kv/{ns}/"
        keys = []
        for line in body.splitlines():
            line = line.strip()
            if line.startswith(prefix):
                key = line[len(prefix):].strip()
                if NAME_RE.match(key):
                    keys.append(key)
        return keys

    def read_note(self, ns: str, key: str) -> Optional[str]:
        """Note value with the untrusted-content banner stripped; None when the note does not exist."""
        self._check_name(ns, "namespace")
        self._check_name(key, "key")
        status, body = self.get(f"/kv/{ns}/{key}")
        if status == 404:
            return None
        if status != 200:
            raise TechnocoreError(f"read /kv/{ns}/{key}: HTTP {status}")
        return strip_banner(body)

    # ---- write lane (Milestone B) ---------------------------------------------------------

    def post(self, path: str, body: dict) -> Tuple[int, str]:
        """POST JSON once. 429 is waited out and retried (the write did not happen); 5xx/timeouts are
        returned/raised to the caller — a signed write may have landed, so the caller decides."""
        url = self._url(path)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        attempt = 0
        while True:
            attempt += 1
            self.budget.acquire()
            self.writes += 1
            try:
                status, headers, text = self._fetch(url, self.timeout, data)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                raise TechnocoreError(f"POST {path}: {exc.__class__.__name__}") from exc
            if status == 429 and attempt < self.max_attempts:
                wait = self._retry_after(headers, text)
                log.warning("POST %s 429; waiting %.1fs", path, wait)
                self._sleep(wait)
                continue
            return status, text

    def post_signed(self, room: str, did: str, sig: str, nonce: int, text: str) -> Tuple[int, str]:
        self._check_name(room, "room")
        return self.post(f"/r/{room}", {"did": did, "sig": sig, "nonce": str(nonce), "text": text})

    def write_note(self, ns: str, key: str, value: str, if_value: Optional[str] = None, if_absent: bool = False) -> Tuple[int, str]:
        self._check_name(ns, "namespace")
        self._check_name(key, "key")
        body: Dict[str, object] = {"value": value}
        if if_value is not None:
            body["if"] = if_value
        if if_absent:
            body["if_absent"] = True
        return self.post(f"/kv/{ns}/{key}", body)

    def claim_room(self, room: str, did: str) -> Tuple[int, str]:
        """GET /kv/room-owners/<d-room>/set/<did>?if_absent=1 — the documented ownership claim."""
        self._check_name(room, "room")
        if not room.startswith("d-"):
            raise ValueError("only d- rooms can be owned")
        return self.get(f"/kv/room-owners/{room}/set/{urllib.parse.quote(did, safe='')}", {"if_absent": 1})


def strip_banner(body: str) -> str:
    """Drop the server's `!! UNTRUSTED CONTENT` banner / `#` comment lines; return the remaining text."""
    lines = [ln for ln in body.splitlines() if ln.strip() and not ln.startswith(_BANNER_PREFIXES)]
    return " ".join(ln.strip() for ln in lines)
