"""Milestone A loop: ingest → census → score snapshot → digest preview. Never writes to Technocore."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from . import __version__, render
from .config import ConfigError, Settings
from .identity import Identity
from .ingest import Ingestor
from .logging_config import configure_logging
from .storage import Storage
from .technocore import TechnocoreClient

log = logging.getLogger("agentscout.main")


class Runner:
    def __init__(self, settings: Settings, client: TechnocoreClient, storage: Storage, sleep=time.sleep, clock=lambda: datetime.now(timezone.utc)):
        self.s = settings
        self.db = storage
        self.client = client
        self.ing = Ingestor(settings, client, storage)
        self._sleep = sleep
        self._now = clock
        self.stop = False

    def startup(self) -> None:
        log.info("agentscout %s starting: DRY_RUN=%s llm=%s replies=%s db=%s rooms=%s",
                 __version__, self.s.dry_run, self.s.llm_enabled, self.s.replies_enabled, self.s.db_path, ",".join(self.s.watch_rooms))
        ident, created = Identity.load_or_create(self.s.identity_key_path)
        log.info("identity: did=%s fp=%s (%s)", ident.did, ident.fp, "CREATED — back up %s now" % self.s.identity_key_path if created else "loaded")
        self.db.set_setting("own_did", ident.did)
        self.ing.discover_limits()
        now = self._now()
        if not self.db.get_setting("observation_started_at"):
            self.db.set_setting("observation_started_at", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.ing.ensure_config_rooms(now)

    def cycle(self) -> None:
        now = self._now()
        new_rooms = self.ing.poll_events(now)
        if new_rooms:
            log.info("events: %d new public rooms announced", new_rooms)
        self.ing.poll_rooms(now)
        self.ing.scan_notes(now)
        self.ing.check_artifacts(now)
        self.maybe_snapshot(now)

    def maybe_snapshot(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if self.db.has_snapshot(day):
            return
        scored = render.score_all(self.db, now)
        self.db.save_snapshot(day, ((did, r.score, r.confidence, r.as_dict()) for did, (f, r) in scored.items()))
        c = self.db.counts()
        log.info("snapshot %s: %d agents scored; counts=%s", day, len(scored), c)
        if now.hour >= self.s.digest_utc_hour or self.s.dry_run:
            log.info("DIGEST PREVIEW (would post to feed room; DRY_RUN): %s", render.digest_line(scored, self.db, now))

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
    storage = Storage(settings.db_path)
    client = TechnocoreClient(settings.technocore_base_url, settings.max_reads_per_minute, settings.http_timeout)
    runner = Runner(settings, client, storage)

    def _stop(signum, _frame):
        log.info("signal %s received; finishing cycle", signum)
        runner.stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        runner.run(once=args.once)
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(cli())
