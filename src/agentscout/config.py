from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}: expected a boolean, got {raw!r}")


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: expected an integer, got {raw!r}") from exc
    if not lo <= val <= hi:
        raise ConfigError(f"{name}: {val} outside [{lo}, {hi}]")
    return val


ROOM_NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,47}$"


def validate_base_url(url: str) -> str:
    """Only https, host only, no path/query/fragment/userinfo — a typo must not make a generic client."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise ConfigError(f"TECHNOCORE_BASE_URL must use https, got {url!r}")
    if not parts.hostname or parts.username or parts.password:
        raise ConfigError(f"TECHNOCORE_BASE_URL must be a bare https host, got {url!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError(f"TECHNOCORE_BASE_URL must not contain a path or query, got {url!r}")
    return f"https://{parts.netloc}"


def _room(name: str, default: str) -> str:
    import re

    val = os.environ.get(name, default).strip() or default
    if not re.match(ROOM_NAME_RE, val):
        raise ConfigError(f"{name}: invalid name {val!r}")
    return val


def _rooms(raw: str) -> List[str]:
    import re

    rooms = [r.strip() for r in raw.split(",") if r.strip()]
    for r in rooms:
        if not re.match(ROOM_NAME_RE, r):
            raise ConfigError(f"SCOUT_WATCH_ROOMS: invalid room name {r!r}")
    return rooms


@dataclass
class Settings:
    technocore_base_url: str = "https://technocore.chat"
    watch_rooms: List[str] = field(
        default_factory=lambda: [
            "lobby", "general", "introductions", "welcome", "builders",
            "technocore", "meta", "infra", "ai", "alpha",
        ]
    )
    new_room_watch_hours: int = 6
    max_event_rooms: int = 30
    event_room_poll_seconds: int = 120
    poll_seconds: int = 15
    did_scan_hours: int = 6
    notes_per_cycle: int = 10
    owners_per_cycle: int = 10
    artifact_checks_per_cycle: int = 10
    note_refresh_days: int = 7
    digest_utc_hour: int = 6
    max_reads_per_minute: int = 120
    process_backlog: bool = True
    dry_run: bool = True
    llm_enabled: bool = False
    replies_enabled: bool = False
    freetext_queries: bool = False
    db_path: str = "/data/agentscout.db"
    identity_key_path: str = "/data/identity.key"
    # Milestone B — publishing
    publish_enabled: bool = False
    feed_room: str = "d-agentscout-feed"
    kv_ns: str = "agentscout"
    kv_top_n: int = 50
    keepalive_note_hours: int = 72
    repo_url: str = "https://github.com/jjmobile/agentscout"
    # Telegram reporting (outbound only)
    telegram_token_file: str = "/run/secrets/telegram_bot_token"
    telegram_chat_id: str = ""
    telegram_max_per_hour: int = 20
    # Public Telegram bot (inbound commands, deterministic answers from the census)
    telegram_public_token_file: str = "/run/secrets/telegram_public_bot_token"
    telegram_public_max_per_user_per_minute: int = 10
    log_level: str = "INFO"
    http_timeout: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            technocore_base_url=validate_base_url(os.environ.get("TECHNOCORE_BASE_URL", "https://technocore.chat")),
            watch_rooms=_rooms(os.environ.get("SCOUT_WATCH_ROOMS", ",".join(cls().watch_rooms))),
            new_room_watch_hours=_int("SCOUT_NEW_ROOM_WATCH_HOURS", 6, 0, 24 * 30),
            max_event_rooms=_int("SCOUT_MAX_EVENT_ROOMS", 30, 0, 500),
            event_room_poll_seconds=_int("SCOUT_EVENT_ROOM_POLL_SECONDS", 120, 15, 3600),
            poll_seconds=_int("SCOUT_POLL_SECONDS", 15, 5, 3600),
            did_scan_hours=_int("SCOUT_DID_SCAN_HOURS", 6, 1, 24 * 7),
            notes_per_cycle=_int("SCOUT_NOTES_PER_CYCLE", 10, 0, 200),
            owners_per_cycle=_int("SCOUT_OWNERS_PER_CYCLE", 10, 0, 200),
            artifact_checks_per_cycle=_int("SCOUT_ARTIFACT_CHECKS_PER_CYCLE", 10, 0, 100),
            note_refresh_days=_int("SCOUT_NOTE_REFRESH_DAYS", 7, 1, 30),
            digest_utc_hour=_int("SCOUT_DIGEST_UTC_HOUR", 6, 0, 23),
            max_reads_per_minute=_int("SCOUT_MAX_READS_PER_MINUTE", 120, 1, 600),
            process_backlog=_bool("PROCESS_BACKLOG_ON_FIRST_START", True),
            dry_run=_bool("DRY_RUN", True),
            llm_enabled=_bool("SCOUT_LLM_ENABLED", False),
            replies_enabled=_bool("SCOUT_REPLIES_ENABLED", False),
            freetext_queries=_bool("SCOUT_FREETEXT_QUERIES", False),
            db_path=os.environ.get("AGENTSCOUT_DB", "/data/agentscout.db"),
            identity_key_path=os.environ.get("AGENTSCOUT_IDENTITY_KEY", "/data/identity.key"),
            publish_enabled=_bool("SCOUT_PUBLISH_ENABLED", False),
            feed_room=_room("SCOUT_FEED_ROOM", "d-agentscout-feed"),
            kv_ns=_room("SCOUT_KV_NS", "agentscout"),
            kv_top_n=_int("SCOUT_KV_TOP_N", 50, 0, 500),
            keepalive_note_hours=_int("KEEPALIVE_NOTE_HOURS", 72, 1, 24 * 6),
            repo_url=os.environ.get("SCOUT_REPO_URL", "https://github.com/jjmobile/agentscout").strip(),
            telegram_token_file=os.environ.get("TELEGRAM_BOT_TOKEN_FILE", "/run/secrets/telegram_bot_token"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            telegram_max_per_hour=_int("TELEGRAM_MAX_PER_HOUR", 20, 1, 500),
            telegram_public_token_file=os.environ.get("TELEGRAM_PUBLIC_BOT_TOKEN_FILE", "/run/secrets/telegram_public_bot_token"),
            telegram_public_max_per_user_per_minute=_int("TELEGRAM_PUBLIC_MAX_PER_USER_PER_MINUTE", 10, 1, 100),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            http_timeout=_int("HTTP_TIMEOUT_SECONDS", 20, 5, 120),
        )
        # Milestones C–E are not built: refuse to start with their flags on.
        if s.llm_enabled or s.replies_enabled or s.freetext_queries:
            raise ConfigError("Milestone B build: SCOUT_LLM_ENABLED / SCOUT_REPLIES_ENABLED / SCOUT_FREETEXT_QUERIES must be false.")
        if s.publish_enabled and s.dry_run:
            raise ConfigError("SCOUT_PUBLISH_ENABLED=true requires DRY_RUN=false.")
        if not s.feed_room.startswith("d-"):
            raise ConfigError("SCOUT_FEED_ROOM must be an ownable d- room.")
        return s

    @property
    def will_publish(self) -> bool:
        return self.publish_enabled and not self.dry_run
