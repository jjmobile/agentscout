"""Milestone A loop: ingest → census → score snapshot → digest preview. Never writes to Technocore."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import __version__, render
from .ask import Asker
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
        self.pubbot: Optional[PublicBot] = None
        self.asker: Optional[Asker] = None
        self._sleep = sleep
        self._now = clock
        self.stop = False
        self._scored: Optional[dict] = None
        self._scored_at: Optional[datetime] = None

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
            self.asker = Asker(self.s, self.db, ident.did, live=self.s.replies_enabled)
            for room in self.s.ask_rooms:
                if room != self.s.ask_room:               # the dedicated room is only polled once the server let us create it
                    self.db.ensure_room(room, "config", None, iso_now(self._now()))
            log.info("ask rooms %s: %s", ",".join(self.s.ask_rooms), "LIVE replies" if self.s.replies_enabled else "log-only (SCOUT_REPLIES_ENABLED=false)")
        if self.summarizer is not None and self.summarizer.enabled and self.s.claude_startup_smoke:
            self.summarizer.smoke()
        llm = "on (%s, effort %s, cap $%.2f/day)" % (self.s.model, self.s.effort or "default", self.s.max_daily_cost_usd) \
            if (self.summarizer is not None and self.summarizer.enabled) else "off"
        mode = "LIVE publishing to /r/%s" % self.s.feed_room if (self.s.will_publish and self.publisher.owner_verified) else "dry-run (nothing is posted)"
        now = self._now()
        self._start_notice(now, f"AgentScout {__version__} started — {mode}\nDID {ident.did}\nfp {ident.fp}\nClaude summaries {llm}\ntelegram reporting on")
        self.ing.discover_limits()
        if not self.db.get_setting("observation_started_at"):
            self.db.set_setting("observation_started_at", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.ing.ensure_config_rooms(now)

    def _start_notice(self, now: datetime, text: str) -> None:
        """One Telegram line per start — but at most one per hour, so a restart loop cannot flood the chat."""
        last = self.db.get_setting("start_notice_at")
        suppressed = int(self.db.get_setting("start_notices_suppressed") or 0)
        if last and now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < timedelta(hours=1):
            self.db.set_setting("start_notices_suppressed", str(suppressed + 1))
            log.info("start notice suppressed (%d restart(s) since %s)", suppressed + 1, last)
            return
        if suppressed:
            text += f"\n⚠️ {suppressed} unannounced restart(s) since the previous notice — check the container logs"
        self.notify.send(text)
        self.db.set_setting("start_notice_at", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.db.set_setting("start_notices_suppressed", "0")

    def scored(self, now: datetime, fresh: bool = False) -> dict:
        """The census, re-scored at most every score_interval_minutes (scoring walks the whole window)."""
        if not fresh and self._scored is not None and self._scored_at is not None \
                and now - self._scored_at < timedelta(minutes=self.s.score_interval_minutes):
            return self._scored
        started = time.monotonic()
        self._scored = None                      # release the previous census first: peak memory = one census, not two
        if self.pubbot is not None:
            self.pubbot.share_scored(None)
        self._scored = render.score_all(self.db, now, self.s.score_window_days, self.s.score_min_msgs)
        self._scored_at = now
        log.info("scored %d agents seen in the last %dd in %.1fs", len(self._scored), self.s.score_window_days, time.monotonic() - started)
        if self.pubbot is not None:
            self.pubbot.share_scored(self._scored)
        return self._scored

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
            self.ing.watch_docs(now)
        scored = self.maybe_snapshot(now)
        if self.publisher is not None:
            if scored is None and self._digest_due(now):
                scored = self.scored(now, fresh=True)      # the daily digest is always freshly scored
            elif scored is None and self.publisher.notes_catchup_due(now):
                scored = self.scored(now)
            self.publisher.tick(now, scored)
        if self.asker is not None:
            self.asker.ensure_room(now)
            if self.asker.tick(now, lambda: self.scored(now)) and self.publisher is not None:
                self.publisher.flush_outbox(now)          # answer within the same cycle
        if self.summarizer is not None and self.summarizer.enabled:
            if scored is None:
                scored = self.scored(now)
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
        scored = self.scored(now, fresh=True)
        self.db.save_snapshot(day, ((did, r.score, r.confidence, r.as_dict()) for did, (f, r) in scored.items()))
        cutoff = (now - timedelta(days=self.s.score_window_days + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pruned = self.db.prune_messages(cutoff)
        if pruned:
            log.info("retention: pruned %d messages older than %s", pruned, cutoff)
        c = self.db.counts()
        log.info("snapshot %s: %d agents scored; counts=%s", day, len(scored), c)
        preview = render.digest_line(scored, self.db, now)
        log.info("DIGEST PREVIEW: %s", preview)
        yesterday = self.db.counters((now - timedelta(days=1)).strftime("%Y-%m-%d"))
        usage = ", ".join(f"{k}={v}" for k, v in sorted(yesterday.items())) or "none"
        self.notify.send(f"📊 daily snapshot {day}: {len(scored)} agents scored\nops last 24h: {self.ops.summary_and_reset()}\nusage yesterday: {usage}\n{preview}")
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


def iso_now(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        pubbot = PublicBot(public_token, settings.db_path, settings.telegram_public_max_per_user_per_minute,
                           window_days=settings.score_window_days, min_msgs=settings.score_min_msgs)
        pubbot.start()
        runner.pubbot = pubbot                   # the main loop hands its scoring over; the bot rarely scores itself
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
