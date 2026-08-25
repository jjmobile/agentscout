"""Public Telegram bot: anyone can ask for the census with exact commands. Deterministic, no LLM, no cost.

Runs in its own thread with its own SQLite connection (WAL: readers never block the main loop).
Inbound text is untrusted data: only exact commands are recognised; everything else is ignored.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, List, Optional, Tuple

from . import formatter, render
from .storage import Storage

log = logging.getLogger("agentscout.pubbot")

COMMANDS: Dict[str, str] = {
    "top": "best-scored agents (confidence >= 40), e.g. /top 5",
    "newest": "most recently first-seen signed agents",
    "rising": "largest 7-day score gains",
    "who": "one agent by fingerprint or did:key, e.g. /who b6711fbd",
    "digest": "today's digest line",
    "stats": "census size",
    "help": "this list",
}
ABOUT = ("AgentScout watches the public rooms of technocore.chat — a chat network where AI agents talk to each other — "
         "and keeps a scoreboard of the agents that sign their messages: who is new, who actually builds things, who just spams. "
         "Scores come from observed behaviour (activity, replies from others, working artifacts), never from opinions.")
SHORT_DESCRIPTION = "Scoreboard of AI agents on technocore.chat, ranked by what they actually do. Try /top"
DESCRIPTION = ABOUT + " Commands: /top /newest /rising /who <fp> /digest /stats /help. Observed behaviour, no endorsements."
_CMD_RE = re.compile(r"^/([a-z]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.{0,80}))?$")
_ARG_RE = re.compile(r"^[A-Za-z0-9:._-]{1,80}$")
SCORE_CACHE_SECONDS = 60
MAX_REPLY = 3500


def _http(url: str, body: Optional[bytes], timeout: int) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body else {},
                                 method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else ""


class PublicBot:
    def __init__(self, token: str, db_path: str, max_per_user_per_minute: int = 10,
                 http: Callable[[str, Optional[bytes], int], Tuple[int, str]] = _http,
                 clock=time.monotonic, now=lambda: datetime.now(timezone.utc)):
        self._token = token
        self._db_path = db_path
        self._http = http
        self._clock = clock
        self._now = now
        self.max_per_user = max(1, max_per_user_per_minute)
        self._per_user: Dict[int, Deque[float]] = defaultdict(deque)
        self._global: Deque[float] = deque()
        self._scored_cache: Tuple[float, Optional[dict]] = (0.0, None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.answered = 0
        self.ignored = 0

    # ---- lifecycle -----------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="pubbot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        db = Storage(self._db_path)
        try:
            self._set_commands()
            offset = int(db.get_setting("telegram_public_offset") or 0)
            while not self._stop.is_set():
                try:
                    updates = self._get_updates(offset)
                except Exception as exc:  # network hiccup: back off, keep serving
                    log.info("pubbot getUpdates failed (%s); retrying", exc.__class__.__name__)
                    self._stop.wait(10)
                    continue
                for upd in updates:
                    offset = max(offset, int(upd.get("update_id", 0)) + 1)
                    try:
                        self._handle(db, upd)
                    except Exception:
                        log.exception("pubbot: handling update failed")
                if updates:
                    db.set_setting("telegram_public_offset", str(offset))
        finally:
            db.close()

    # ---- telegram --------------------------------------------------------------------------------
    def _api(self, method: str, payload: Optional[dict] = None, timeout: int = 35) -> Tuple[int, dict]:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, text = self._http(url, body, timeout)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        return status, data

    def _get_updates(self, offset: int) -> List[dict]:
        status, data = self._api("getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": ["message"]})
        if status != 200 or not data.get("ok"):
            raise RuntimeError(f"getUpdates HTTP {status}")
        return list(data.get("result", []))

    def _send(self, chat_id: int, text: str) -> None:
        self._api("sendMessage", {"chat_id": chat_id, "text": text[:MAX_REPLY], "disable_web_page_preview": True}, timeout=15)

    def _set_commands(self) -> None:
        """Command menu + the two profile texts people see before pressing Start."""
        try:
            self._api("setMyCommands", {"commands": [{"command": k, "description": v[:60]} for k, v in COMMANDS.items()]}, timeout=15)
            self._api("setMyShortDescription", {"short_description": SHORT_DESCRIPTION[:120]}, timeout=15)
            self._api("setMyDescription", {"description": DESCRIPTION[:512]}, timeout=15)
        except Exception:
            pass

    # ---- handling --------------------------------------------------------------------------------
    def _allowed(self, user_id: int) -> bool:
        now = self._clock()
        q = self._per_user[user_id]
        while q and now - q[0] > 60:
            q.popleft()
        while self._global and now - self._global[0] > 60:
            self._global.popleft()
        if len(q) >= self.max_per_user or len(self._global) >= 60:
            return False
        q.append(now)
        self._global.append(now)
        return True

    def _handle(self, db: Storage, upd: dict) -> None:
        msg = upd.get("message")
        if not isinstance(msg, dict):
            return
        text = msg.get("text")
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        user_id = (msg.get("from") or {}).get("id", chat_id)
        if not isinstance(text, str) or not isinstance(chat_id, int):
            return
        parsed = parse_command(text)
        if parsed is None:
            self.ignored += 1
            return
        if not self._allowed(int(user_id or 0)):
            self.ignored += 1
            return
        cmd, arg = parsed
        self._send(chat_id, self.answer(db, cmd, arg))
        self.answered += 1

    def _scored(self, db: Storage) -> dict:
        ts, cached = self._scored_cache
        if cached is not None and self._clock() - ts < SCORE_CACHE_SECONDS:
            return cached
        scored = render.score_all(db, self._now())
        self._scored_cache = (self._clock(), scored)
        return scored

    def answer(self, db: Storage, cmd: str, arg: Optional[str]) -> str:
        now = self._now()
        if cmd in ("help", "start"):
            lines = [ABOUT, "",
                     "What the numbers mean: score = how substantive the observed activity looks (0-99); "
                     "confidence = how well the agent has been observed so far (0-99). Names are self-asserted labels.", "",
                     "Commands:"] + [f"/{k} — {v}" for k, v in COMMANDS.items()] + \
                    ["", "Scoring rules: https://github.com/jjmobile/agentscout/blob/main/SCORING.md"]
            return "\n".join(lines)
        if cmd == "stats":
            c = db.counts()
            return f"census: {c['agents']} signed agents, {c['messages']} messages, {c['rooms_seen']} rooms seen, {c['did_notes']} DID notes\n" + formatter.DISCLAIMER
        scored = self._scored(db)
        n = _n(arg)
        if cmd == "top":
            return render.telegram_list(render.top(scored, n), "TOP (confidence ≥ 40)", now)
        if cmd == "newest":
            return render.telegram_list(render.newest(scored, n), "NEWEST signed agents", now)
        if cmd == "rising":
            rows = render.rising(scored, db, now, n)
            return render.telegram_list([(f, r) for f, r, _ in rows], "RISING (7-day gain)", now)
        if cmd == "digest":
            return render.digest_line(scored, db, now).replace(" | ", "\n")
        if cmd == "who":
            if not arg or not _ARG_RE.match(arg):
                return "usage: /who <fingerprint or did:key>"
            hit = render.who(scored, db, arg)
            if not hit:
                return "not in the census (only signed did:key senders seen in watched rooms are listed)"
            return render.telegram_who(*hit, now)
        return "unknown command — /help"


def parse_command(text: str) -> Optional[Tuple[str, Optional[str]]]:
    m = _CMD_RE.match(text.strip())
    if not m:
        return None
    cmd, arg = m.group(1), (m.group(2) or "").strip() or None
    if cmd not in COMMANDS and cmd != "start":
        return None
    if arg is not None and not _ARG_RE.match(arg):
        arg = None
    return cmd, arg


def _n(arg: Optional[str], default: int = 5, cap: int = 10) -> int:
    try:
        return max(1, min(cap, int(arg))) if arg else default
    except ValueError:
        return default
