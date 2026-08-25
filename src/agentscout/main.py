"""Milestone A loop: ingest → census → score snapshot → digest preview. Never writes to Technocore."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from . import __version__, render
from .config import ConfigError, Settings
from .identity import Identity
from .notify import OpsCounter, TelegramLogHandler, TelegramNotifier, load_token
from .pubbot import PublicBot
from .summarizer import Summarizer, make_client
from .publisher import Publisher
from .ingest import Ingestor
from .logging_config import configure_logging
from .storage import Storage
from .technocore import TechnocoreClient

log = logging.getLogger("agentscout.main")


class Runner:
    def __init__(self, settings: Settings, client: TechnocoreClient, storage: Storage, sleep=time.sleep,
                 clock=lambda: datetime.now(timezone.utc), notifier: Optional[TelegramNotifier] = None):
        self.s = settings
        self.db = storage
        self.client = client
        self.ing = Ingestor(settings, client, storage)
        self.notify = notifier or TelegramNotifier(None, None)
        self.ops = OpsCounter()
        self.publisher: Optional[Publisher] = None
        self.summarizer: Optional[Summarizer] = None
        self._sleep = sleep
        self._now = clock
        self.stop = False

    def startup(self) -> None:
        log.info("agentscout %s starting: DRY_RUN=%s publish=%s llm=%s replies=%s db=%s rooms=%s",
                 __version__, self.s.dry_run, self.s.will_publish, self.s.llm_enabled, self.s.replies_enabled,
                 self.s.db_path, ",".join(self.s.watch_rooms))
        ident, created = Identity.load_or_create(self.s.identity_key_path)
        log.info("identity: did=%s fp=%s (%s)", ident.did, ident.fp, "CREATED — back up %s now" % self.s.identity_key_path if created else "loaded")
        self.db.set_setting("own_did", ident.did)
        self.publisher = Publisher(self.s, self.client, self.db, ident, notify=self.notify)
        if self.s.will_publish:
            self.publisher.verify_ownership()
        if self.summarizer is not None and self.summarizer.enabled and self.s.claude_startup_smoke:
            self.summarizer.smoke()
        llm = "on (%s, effort %s, cap $%.2f/day)" % (self.s.model, self.s.effort or "default", self.s.max_daily_cost_usd) \
            if (self.summarizer is not None and self.summarizer.enabled) else "off"
        mode = "LIVE publishing to /r/%s" % self.s.feed_room if (self.s.will_publish and self.publisher.owner_verified) else "dry-run (nothing is posted)"
        self.notify.send(f"AgentScout {__version__} started — {mode}\nDID {ident.did}\nfp {ident.fp}\nClaude summaries {llm}\ntelegram reporting on")
        self.ing.discover_limits()
        now = self._now()
        if not self.db.get_setting("observation_started_at"):
            self.db.set_setting("observation_started_at", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.ing.ensure_config_rooms(now)

    def cycle(self) -> None:
        now = self._now()
        deadline = time.monotonic() + self.s.cycle_budget_seconds
        if self.publisher is not None:           # queued signed posts go out before any slow reading
            self.publisher.flush_outbox(now)
        new_rooms = self.ing.poll_events(now)
        if new_rooms:
            log.info("events: %d new public rooms announced", new_rooms)
        self.ing.poll_rooms(now, deadline=deadline)
        if time.monotonic() < deadline:
            self.ing.scan_notes(now)
            self.ing.check_artifacts(now)
        scored = self.maybe_snapshot(now)
        if self.publisher is not None:
            if scored is None and self._digest_due(now):
                scored = render.score_all(self.db, now)
            self.publisher.tick(now, scored)
        if self.summarizer is not None and self.summarizer.enabled:
            if scored is None:
                scored = render.score_all(self.db, now)
            listed = {f.did for f, _r in render.top(scored, 10)} | {f.did for f, _r in render.newest(scored, 10)} \
                | {f.did for f, _r, _d in render.rising(scored, self.db, now, 10)}
            self.summarizer.tick({did: f for did, (f, _r) in scored.items()}, now, priority=listed)

    def _digest_due(self, now: datetime) -> bool:
        day = now.strftime("%Y-%m-%d")
        return now.hour >= self.s.digest_utc_hour and self.db.outbox_has(self.s.feed_room, f"AGENTSCOUT DIGEST {day}") is None

    def maybe_snapshot(self, now: datetime):
        day = now.strftime("%Y-%m-%d")
        if self.db.has_snapshot(day):
            return None
        scored = render.score_all(self.db, now)
        self.db.save_snapshot(day, ((did, r.score, r.confidence, r.as_dict()) for did, (f, r) in scored.items()))
        c = self.db.counts()
        log.info("snapshot %s: %d agents scored; counts=%s", day, len(scored), c)
        preview = render.digest_line(scored, self.db, now)
        log.info("DIGEST PREVIEW: %s", preview)
        self.notify.send(f"📊 daily snapshot {day}: {len(scored)} agents scored\nops last 24h: {self.ops.summary_and_reset()}\n{preview}")
        return scored

    def run(self, once: bool = False) -> None:
        self.startup()
        while not self.stop:
            started = time.monotonic()
            try:
                self.cycle()
            except Exception:  # keep observing; never die on one bad cycle
                log.exception("cycle failed")
            if once:
                break
            elapsed = time.monotonic() - started
            self._sleep(max(1.0, self.s.poll_seconds - elapsed))
        log.info("agentscout stopped")


def cli(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agentscout", description="Read-only Technocore observer (Milestone A)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = ap.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    token = load_token(settings.telegram_token_file, os.environ.get("TELEGRAM_BOT_TOKEN"))
    notifier = TelegramNotifier(token, settings.telegram_chat_id or None, settings.telegram_max_per_hour)
    storage = Storage(settings.db_path)
    client = TechnocoreClient(settings.technocore_base_url, settings.max_reads_per_minute, settings.http_timeout)
    runner = Runner(settings, client, storage, notifier=notifier)
    if notifier.enabled:
        logging.getLogger().addHandler(TelegramLogHandler(notifier, runner.ops))
        log.info("telegram reporting enabled (chat %s)", settings.telegram_chat_id)
    else:
        log.info("telegram reporting disabled (no token/chat id)")
    if settings.llm_enabled:
        api_key = load_token(settings.anthropic_key_file, os.environ.get("ANTHROPIC_API_KEY"))
        runner.summarizer = Summarizer(settings, storage, make_client(api_key), notify=notifier)
        if api_key is None:
            runner.summarizer.disable("SCOUT_LLM_ENABLED=true but no API key at %s" % settings.anthropic_key_file)
    public_token = load_token(settings.telegram_public_token_file, os.environ.get("TELEGRAM_PUBLIC_BOT_TOKEN"))
    pubbot = None
    if public_token:
        pubbot = PublicBot(public_token, settings.db_path, settings.telegram_public_max_per_user_per_minute)
        pubbot.start()
        log.info("public telegram bot enabled")
    else:
        log.info("public telegram bot disabled (no token)")

    def _stop(signum, _frame):
        log.info("signal %s received; finishing cycle", signum)
        runner.stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        runner.run(once=args.once)
    finally:
        if pubbot:
            pubbot.stop()
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(cli())
