"""Read-only official-source monitor. Standalone Python 3.9+, standard library only.

Run this file directly; it deliberately imports no agent settings, keys or clients.
Page changes are evidence for human review, never instructions or launch authority.
"""
from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import fcntl
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

INTERVAL = 7200
HEARTBEAT = 600
MAX_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 20
PARTICIPATION_STATUS = "awaiting_official_onboarding"
ALLOWED_HOSTS = {"flop.finance", "ask.flop.finance", "raw.githubusercontent.com", "api.github.com",
                 "faucet.flop.network"}
DEFAULT_STATE_DIR = Path(__file__).resolve().parents[2] / ".launchwatch"
# Optional phone alert via the existing ops bot; token file and chat id stay outside this module's output.
TELEGRAM_TOKEN_FILE = Path(__file__).resolve().parents[2] / "secrets" / "telegram_bot_token.txt"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@dataclass(frozen=True)
class Source:
    key: str
    url: str
    kind: str = "html"
    required: tuple = ()


SOURCES = (
    Source("airdrop", "https://flop.finance/airdrop/", required=("genesis", "airdrop")),
    Source("testnet", "https://flop.finance/testnet/", required=("testnet", "FLOP")),
    Source("timeline", "https://ask.flop.finance/docs/testnet-and-timeline", required=("testnet", "timeline")),
    Source("network_status", "https://ask.flop.finance/docs/network-testnet", required=("testnet", "status")),
    Source("agent_onboarding", "https://ask.flop.finance/docs/agent-integration", required=("agent", "FLOP")),
    Source("operator_hub", "https://ask.flop.finance/operators", required=("operator", "network")),
    Source("yellowpaper_web", "https://flop.finance/intro/yellowpaper/", required=("R8.4", "E.38")),
    Source("yellowpaper_mirror", "https://raw.githubusercontent.com/flop-labs/yellowpaper/main/yellowpaper.md",
           "text", ("R8.4", "E.38")),
    Source("official_repositories", "https://api.github.com/orgs/flop-labs/repos?per_page=100", "repos"),
    Source("yellowpaper_releases", "https://api.github.com/repos/flop-labs/yellowpaper/releases?per_page=20", "releases"),
    Source("technocore_releases", "https://api.github.com/repos/flop-labs/technocore-chat/releases?per_page=20", "releases"),
    # Public faucet chain descriptor: flips network/genesis/rpc when the faucet moves from devnet to testnet.
    Source("faucet_chain", "https://faucet.flop.network/chain", "json", ("network", "genesisHash")),
)


def iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


class AlreadyRunning(Exception):
    pass


@contextlib.contextmanager
def instance_lock(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "watch.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("another launchwatch process holds the lock") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def load_state(directory: Path, now: float) -> dict:
    path = directory / "state.json"
    if not path.exists():
        return {"schema": 1, "created_at": iso(now), "sources": {}, "history": [],
                "participation_status": PARTICIPATION_STATUS}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != 1 or not isinstance(state.get("sources"), dict) or not isinstance(state.get("history"), list):
        raise ValueError("invalid state schema; preserve state.json and investigate")
    return state


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("refusing URL outside the fixed HTTPS official-source hosts")


class OfficialRedirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_http(url: str, timeout: int = FETCH_TIMEOUT) -> dict:
    """Called in a killable child: bounded body and socket timeout, no credentials."""
    validate_url(url)
    request = Request(url, headers={"User-Agent": "AgentScout-LaunchWatch/1.0 (read-only)",
                                   "Accept": "application/json,text/html,text/plain;q=0.9",
                                   "Accept-Encoding": "identity"})
    with build_opener(OfficialRedirects()).open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"unexpected HTTP status {response.status}")
        if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
            raise ValueError("unexpected content encoding")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("response exceeds 2 MiB limit")
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "text/plain", "application/json", "application/octet-stream"}:
            raise ValueError("unexpected response content type")
        return {"body": body.decode("utf-8", errors="strict"), "url": response.geturl(),
                "content_type": content_type, "http_status": response.status}


def fetch_bounded(source: Source) -> dict:
    """A wall-clock deadline also stops slow-drip reads and stuck DNS resolution."""
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_fetch", "--url", source.url],
                            capture_output=True, text=True, timeout=FETCH_TIMEOUT + 5, check=False)
    if result.returncode:
        # Child errors contain classifications only, never server bodies or exception URLs.
        raise ValueError(result.stdout.strip()[:160] or "fetch child failed")
    return json.loads(result.stdout)


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header", "noscript"):
            self.hidden += 1
        if not self.hidden and tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "br", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header", "noscript"):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data + " ")


def normalize(source: Source, body: str) -> str:
    if source.kind in ("repos", "releases"):
        value = json.loads(body)
        if not isinstance(value, list):
            raise ValueError("expected GitHub list, received error or invalid response")
        # Fail visibly rather than silently miss older pages if the org outgrows one page.
        if source.kind == "repos" and len(value) >= 100:
            raise ValueError("repository pagination limit reached; update watcher before trusting discovery")
        if source.kind == "repos":
            fields = ("full_name", "html_url", "default_branch", "archived", "pushed_at")
            if any(not isinstance(row, dict) or not str(row.get("full_name", "")).startswith("flop-labs/") for row in value):
                raise ValueError("invalid official repository record")
            rows = [{key: row.get(key) for key in fields} for row in value]
            rows.sort(key=lambda row: row["full_name"])
        else:
            fields = ("tag_name", "name", "html_url", "published_at", "draft", "prerelease", "target_commitish", "body")
            if any(not isinstance(row, dict) or not row.get("tag_name") for row in value):
                raise ValueError("invalid release record")
            rows = [{key: row.get(key) for key in fields} for row in value]
            for original, normalized in zip(value, rows):
                assets = original.get("assets", [])
                if not isinstance(assets, list) or any(not isinstance(asset, dict) for asset in assets):
                    raise ValueError("invalid release asset list")
                normalized["assets"] = sorted(
                    [{key: asset.get(key) for key in ("name", "browser_download_url", "digest", "size", "updated_at")}
                     for asset in assets], key=lambda asset: str(asset["name"]))
            rows.sort(key=lambda row: row["tag_name"])
        return json.dumps(rows, indent=2, sort_keys=True, ensure_ascii=False)
    if source.kind == "json":
        value = json.loads(body)
        if not isinstance(value, dict) or any(key not in value for key in source.required):
            raise ValueError("unexpected JSON document; baseline was not replaced")
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    if source.kind == "html":
        parser = VisibleText()
        parser.feed(body)
        body = "".join(parser.parts)
    lines = [re.sub(r"\s+", " ", line).strip() for line in body.splitlines()]
    text = "\n".join(line for line in lines if line)
    if len(text) < 60 or any(word.casefold() not in text.casefold() for word in source.required):
        raise ValueError("page lacks expected content; baseline was not replaced")
    return text


def change_summary(old: str, new: str) -> str:
    # Deterministic excerpts, not an LLM interpretation or launch detector.
    old_lines, new_lines = old.splitlines(), new.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, n=0))
    added = [line[1:] for line in diff if line.startswith("+") and not line.startswith("+++")]
    removed = [line[1:] for line in diff if line.startswith("-") and not line.startswith("---")]
    excerpt = "; ".join(line[:180] for line in (added or removed)[:3])
    return f"text +{len(added)}/-{len(removed)} lines; review required; excerpt: {excerpt}"


def safe_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "fetch deadline exceeded"
    if isinstance(exc, URLError):
        return "network/DNS/TLS connection failed"
    if isinstance(exc, (UnicodeError, json.JSONDecodeError)):
        return "invalid document encoding or JSON"
    # ValueError messages from this module are fixed, except fetch child's fixed output.
    if isinstance(exc, ValueError):
        return str(exc)[:180]
    return type(exc).__name__


def notify_macos(message: str) -> None:
    """Fixed AppleScript; message is argv data, never code or a shell command."""
    if sys.platform != "darwin":
        raise ValueError("native notifications require macOS")
    script = 'on run argv\ndisplay notification (item 1 of argv) with title "AgentScout FLOP monitor"\nend run'
    subprocess.run(["/usr/bin/osascript", "-e", script, message], capture_output=True,
                   timeout=10, check=True)


def notify_telegram(message: str) -> None:
    """POST a fixed-label message to the configured ops chat. Raises if not configured or not accepted."""
    token = TELEGRAM_TOKEN_FILE.read_text().strip()
    chat = ""
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("TELEGRAM_CHAT_ID="):
            chat = line.split("=", 1)[1].strip()
    if not token or not chat:
        raise ValueError("telegram not configured")
    data = json.dumps({"chat_id": chat, "text": "AgentScout FLOP monitor: " + message}).encode()
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                      headers={"Content-Type": "application/json"})
    with build_opener().open(request, timeout=FETCH_TIMEOUT) as response:
        if response.status != 200:
            raise ValueError("telegram rejected message")


def notify_all(message: str) -> None:
    """Phone first (the operator is often away from the Mac); succeed if either channel accepts."""
    errors = []
    for channel in (notify_telegram, notify_macos):
        try:
            channel(message)
        except Exception as exc:
            errors.append(exc)
    if len(errors) == 2:
        raise errors[0]


def health(state: dict, now: float, sources=SOURCES) -> dict:
    statuses = {}
    for source in sources:
        row = state.get("sources", {}).get(source.key, {})
        success = row.get("last_success_at")
        failures = row.get("consecutive_failures", 0)
        age = now - timestamp(success) if success else None
        if failures >= 2 or (age is not None and age >= 2 * INTERVAL):
            status = "stale"
        elif failures:
            status = "degraded"
        elif success:
            status = "healthy"
        else:
            status = "pending"
        statuses[source.key] = {"health": status, "last_success_at": success,
                                "consecutive_failures": failures, "last_error": row.get("last_error"),
                                "sha256": row.get("sha256"), "last_change_at": row.get("last_change_at"),
                                "change_summary": row.get("change_summary"), "url": source.url}
    last_tick = state.get("last_tick_at")
    scheduler_stale = not last_tick or now - timestamp(last_tick) >= 2 * HEARTBEAT + 60
    levels = {row["health"] for row in statuses.values()}
    overall = "stale" if scheduler_stale or "stale" in levels else "degraded" if "degraded" in levels else "pending" if "pending" in levels else "healthy"
    return {"as_of": iso(now), "health": overall, "scheduler_stale": scheduler_stale,
            "last_tick_at": last_tick, "last_poll_completed_at": state.get("last_poll_completed_at"),
            "next_poll_at": state.get("next_poll_at"), "sources": statuses,
            "last_notification": state.get("last_notification"),
            "participation_status": PARTICIPATION_STATUS,
            "automatic_spending": False, "note": "Document changes require review; this watcher cannot enable participation."}


def tick(directory: Path, *, force=False, now=None, fetcher=fetch_bounded, sources=SOURCES, notifier=None) -> dict:
    now = time.time() if now is None else now
    with instance_lock(directory):
        state = load_state(directory, now)
        old_health = state.get("last_health")
        changes = []
        state["last_tick_at"] = iso(now)
        state["participation_status"] = PARTICIPATION_STATUS
        due = force or not state.get("next_poll_at") or now >= timestamp(state["next_poll_at"])
        state["last_action"] = "poll" if due else "heartbeat"
        if due:
            state["last_poll_started_at"] = iso(now)
            # If interrupted, the incomplete poll is immediately due on the next tick.
            state.pop("next_poll_at", None)
            save_json(directory / "state.json", state)
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {source.key: pool.submit(fetcher, source) for source in sources}
                for source in sources:
                    row = state["sources"].setdefault(source.key, {"url": source.url, "total_failures": 0})
                    row["last_attempt_at"] = iso(now)
                    try:
                        response = futures[source.key].result()
                        text = normalize(source, response["body"])
                        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        previous = row.get("sha256")
                        snapshot = directory / "snapshots" / source.key / f"{digest}.txt"
                        atomic_write(snapshot, text + "\n")
                        if previous != digest:
                            old_path = directory / "snapshots" / source.key / f"{previous}.txt"
                            summary = change_summary(old_path.read_text(encoding="utf-8"), text) if previous and old_path.exists() else "baseline captured; review source before relying on it"
                            row.update(last_change_at=iso(now), change_summary=summary)
                            state["history"].append({"at": iso(now), "source": source.key, "sha256": digest,
                                                     "previous_sha256": previous, "summary": summary})
                            state["history"] = state["history"][-100:]
                            if previous:
                                changes.append(source.key)
                        row.update(sha256=digest, last_success_at=iso(now), consecutive_failures=0,
                                   last_error=None, final_url=response.get("url", source.url))
                    except Exception as exc:
                        row["consecutive_failures"] = row.get("consecutive_failures", 0) + 1
                        row["total_failures"] = row.get("total_failures", 0) + 1
                        row["last_error"] = safe_error(exc)
                    # Preserve completed source results if the host exits during this batch.
                    save_json(directory / "state.json", state)
            state["last_poll_completed_at"] = iso(now)
            state["next_poll_at"] = iso(now + INTERVAL)
        status = health(state, now, sources)
        current_health = status["health"]
        state["last_health"] = current_health
        messages = []
        if changes:
            messages.append("Official documents changed: " + ", ".join(changes) + ". Review local evidence before acting.")
        if current_health != old_health and (old_health or current_health != "healthy"):
            messages.append("Monitoring health: " + current_health + ". Check the local launchwatch status.")
        if messages:
            # Fixed labels and source keys only; page text never enters a notification.
            message = " ".join(messages)
            event = {"at": iso(now), "message": message, "delivery": "local_status_only"}
            state["last_notification"] = event
            # Commit transition before notification: restarts cannot repeatedly notify it.
            save_json(directory / "state.json", state)
            if notifier:
                try:
                    notifier(message)
                    event["delivery"] = "native_notification_requested"
                except Exception as exc:
                    event["delivery"] = "failed"
                    event["error"] = safe_error(exc)
        status = health(state, now, sources)
        save_json(directory / "state.json", state)
        save_json(directory / "status.json", status)
        # Keep disk growth bounded; state + last 100 history references remain recoverable.
        keep = {(item["source"], item["sha256"]) for item in state["history"]}
        keep |= {(item["source"], item["previous_sha256"]) for item in state["history"] if item.get("previous_sha256")}
        keep |= {(key, row["sha256"]) for key, row in state["sources"].items() if row.get("sha256")}
        for source in sources:
            for path in (directory / "snapshots" / source.key).glob("*.txt"):
                if (source.key, path.stem) not in keep:
                    path.unlink()
        return {"action": state["last_action"], **status}


def cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("tick", "check", "status", "_fetch"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--notify", action="store_true", help="request Telegram + macOS notifications for changes and health transitions")
    parser.add_argument("--url", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "_fetch":
            print(json.dumps(fetch_http(args.url)))
            return 0
        if args.command == "status":
            result = health(load_state(args.state_dir, time.time()), time.time())
        else:
            result = tick(args.state_dir, force=args.command == "check", notifier=notify_all if args.notify else None)
        if args.command == "tick":
            print(json.dumps({key: result[key] for key in ("as_of", "action", "health", "participation_status")}))
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["health"] == "healthy" else 2
    except AlreadyRunning:
        print(json.dumps({"action": "skipped", "reason": "another watcher is running"}))
        return 0
    except Exception as exc:
        print(json.dumps({"error": safe_error(exc), "health": "unavailable",
                          "participation_status": PARTICIPATION_STATUS}))
        return 3


if __name__ == "__main__":
    raise SystemExit(cli())
