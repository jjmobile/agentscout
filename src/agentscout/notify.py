"""Telegram reporter — outbound only (sendMessage). No polling, no commands, no inbound surface.

The bot token is read from a file (Docker secret) or env, kept in memory, never logged.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Callable, Deque, Optional

log = logging.getLogger("agentscout.notify")

TELEGRAM_MAX_CHARS = 4000  # API limit 4096


def _urllib_post(url: str, body: bytes, timeout: int) -> int:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


class TelegramNotifier:
    def __init__(self, token: Optional[str], chat_id: Optional[str], max_per_hour: int = 20,
                 poster: Callable[[str, bytes, int], int] = _urllib_post, clock=time.monotonic, timeout: int = 15):
        self.enabled = bool(token and chat_id)
        self._token = token or ""
        self._chat_id = chat_id or ""
        self.max_per_hour = max(1, max_per_hour)
        self._post = poster
        self._clock = clock
        self._timeout = timeout
        self._sent: Deque[float] = deque()
        self.dropped = 0
        self.sent_total = 0

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        now = self._clock()
        while self._sent and now - self._sent[0] > 3600:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_hour:
            self.dropped += 1
            if self.dropped in (1, 10, 100):
                log.warning("telegram: hourly cap %d reached; dropped %d message(s)", self.max_per_hour, self.dropped)
            return False
        text = text if len(text) <= TELEGRAM_MAX_CHARS else text[: TELEGRAM_MAX_CHARS - 1] + "…"
        body = json.dumps({"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            status = self._post(url, body, self._timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log.warning("telegram: send failed (%s)", exc.__class__.__name__)
            return False
        self._sent.append(now)
        if status != 200:
            log.warning("telegram: sendMessage HTTP %d", status)  # token never logged
            return False
        self.sent_total += 1
        return True


import re as _re
from collections import Counter

_TRANSIENT_RE = _re.compile(r"HTTP 5\d\d|sequence gap|429|connection error|unreachable|Internal Server Error", _re.IGNORECASE)


class OpsCounter:
    """Counts noisy-but-expected warnings so the daily report can say '37 transient 500s, 4 gaps'."""

    def __init__(self):
        self.counts: Counter = Counter()

    def bucket(self, record: logging.LogRecord) -> Optional[str]:
        msg = record.getMessage()
        if "sequence gap" in msg:
            return "ring_gaps"
        if _TRANSIENT_RE.search(msg):
            return "transient_http_errors"
        return None

    def summary_and_reset(self) -> str:
        parts = [f"{v} {k.replace('_', ' ')}" for k, v in sorted(self.counts.items())] or ["no transient errors"]
        self.counts.clear()
        return ", ".join(parts)


class TelegramLogHandler(logging.Handler):
    """Forwards only actionable records: ERROR+ from anywhere, WARNING from the publisher (ownership, tamper,
    post failures), and ingest warnings about protocol drift. Transient 5xx/429/gaps are counted, not sent."""

    def __init__(self, notifier: TelegramNotifier, counter: Optional[OpsCounter] = None, level: int = logging.WARNING):
        super().__init__(level)
        self.notifier = notifier
        self.counter = counter or OpsCounter()

    def should_forward(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("agentscout.notify"):
            return False
        if self.counter.bucket(record):
            self.counter.counts[self.counter.bucket(record)] += 1
            return False
        if record.levelno >= logging.ERROR:
            return True
        if record.name.startswith("agentscout.publisher"):
            return True
        msg = record.getMessage()
        return "VERSION CHANGED" in msg or "NOTE_TAMPERED" in msg

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.should_forward(record):
                self.notifier.send(f"⚠️ {record.levelname} {record.name}: {record.getMessage()}"[:TELEGRAM_MAX_CHARS])
        except Exception:  # a reporter must never break the agent
            pass


def load_token(path: Optional[str], env_value: Optional[str]) -> Optional[str]:
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tok = fh.read().strip()
                if tok:
                    return tok
        except OSError:
            pass
    return env_value.strip() if env_value and env_value.strip() else None
