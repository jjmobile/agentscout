"""Offline tests for monitoring persistence, failure health and read-only boundaries."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from agentscout import launchwatch as lw

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp()
SOURCE = lw.Source("testnet", "https://flop.finance/testnet/", required=("testnet", "FLOP"))
OLD = "<main><h1>FLOP testnet</h1><p>Status: planned. Readiness criteria and official onboarding remain unpublished.</p></main>"
NEW = OLD.replace("planned", "claimed live on this page")


def response(body=OLD):
    return {"body": body, "url": SOURCE.url}


def test_baseline_change_unchanged_and_process_restart(tmp_path):
    first = lw.tick(tmp_path, now=NOW, sources=(SOURCE,), fetcher=lambda _: response())
    assert first["health"] == "healthy"
    state = lw.load_state(tmp_path, NOW)
    assert len(state["history"]) == 1
    old_hash = state["sources"]["testnet"]["sha256"]
    # Reload from disk, not a persistent object in the process.
    second = lw.tick(tmp_path, now=NOW + lw.INTERVAL, sources=(SOURCE,), fetcher=lambda _: response(NEW))
    assert second["participation_status"] == "awaiting_official_onboarding"
    assert second["automatic_spending"] is False
    state = lw.load_state(tmp_path, NOW)
    assert len(state["history"]) == 2
    assert state["history"][-1]["previous_sha256"] == old_hash
    assert "review required" in state["history"][-1]["summary"]
    third = lw.tick(tmp_path, now=NOW + 2 * lw.INTERVAL, sources=(SOURCE,), fetcher=lambda _: response(NEW))
    assert third["health"] == "healthy"
    assert len(lw.load_state(tmp_path, NOW)["history"]) == 2
    assert len(list((tmp_path / "snapshots" / "testnet").glob("*.txt"))) == 2


def test_failure_preserves_last_good_evidence_and_stales_after_two_misses(tmp_path):
    lw.tick(tmp_path, now=NOW, sources=(SOURCE,), fetcher=lambda _: response())
    saved = lw.load_state(tmp_path, NOW)["sources"]["testnet"].copy()

    def unavailable(_):
        raise TimeoutError()

    first = lw.tick(tmp_path, now=NOW + lw.INTERVAL, sources=(SOURCE,), fetcher=unavailable)
    assert first["health"] == "degraded"
    second = lw.tick(tmp_path, now=NOW + 2 * lw.INTERVAL, sources=(SOURCE,), fetcher=unavailable)
    assert second["health"] == "stale"
    row = lw.load_state(tmp_path, NOW)["sources"]["testnet"]
    assert row["sha256"] == saved["sha256"]
    assert row["last_success_at"] == saved["last_success_at"]
    assert row["consecutive_failures"] == 2
    assert row["last_error"] == "fetch deadline exceeded"
    recovered = lw.tick(tmp_path, now=NOW + 3 * lw.INTERVAL, sources=(SOURCE,), fetcher=lambda _: response())
    assert recovered["health"] == "healthy"
    assert recovered["sources"]["testnet"]["consecutive_failures"] == 0
    assert lw.load_state(tmp_path, NOW)["sources"]["testnet"]["total_failures"] == 2


def test_scheduler_heartbeat_does_not_fetch_before_due_and_status_ages(tmp_path):
    calls = []

    def fetch(source):
        calls.append(source.key)
        return response()

    lw.tick(tmp_path, now=NOW, sources=(SOURCE,), fetcher=fetch)
    heartbeat = lw.tick(tmp_path, now=NOW + lw.HEARTBEAT, sources=(SOURCE,), fetcher=fetch)
    assert heartbeat["action"] == "heartbeat"
    assert calls == ["testnet"]
    state = lw.load_state(tmp_path, NOW)
    stale = lw.health(state, NOW + 4 * lw.HEARTBEAT, sources=(SOURCE,))
    assert stale["scheduler_stale"] is True
    assert stale["health"] == "stale"
    assert stale["sources"]["testnet"]["health"] == "healthy"
    lw.tick(tmp_path, now=NOW + lw.HEARTBEAT + 1, force=True, sources=(SOURCE,), fetcher=fetch)
    assert calls == ["testnet", "testnet"]


def test_bad_pages_are_failed_fetches_and_cannot_erase_baseline(tmp_path):
    lw.tick(tmp_path, now=NOW, sources=(SOURCE,), fetcher=lambda _: response())
    old = lw.load_state(tmp_path, NOW)["sources"]["testnet"]["sha256"]
    result = lw.tick(tmp_path, now=NOW + lw.INTERVAL, sources=(SOURCE,), fetcher=lambda _: response("<html>Please sign in.</html>"))
    assert result["health"] == "degraded"
    assert result["sources"]["testnet"]["sha256"] == old
    assert len(lw.load_state(tmp_path, NOW)["history"]) == 1


def test_one_source_failure_does_not_hide_other_updates(tmp_path):
    other = lw.Source("airdrop", "https://flop.finance/airdrop/", "text", ("airdrop",))

    def fetch(source):
        if source.key == "testnet":
            raise TimeoutError()
        return response("FLOP airdrop rules remain a draft; official eligibility and settled spend records must be verified.")

    result = lw.tick(tmp_path, now=NOW, sources=(SOURCE, other), fetcher=fetch)
    assert result["sources"]["testnet"]["health"] == "degraded"
    assert result["sources"]["airdrop"]["health"] == "healthy"


def test_lock_prevents_simultaneous_instance(tmp_path):
    with lw.instance_lock(tmp_path):
        with pytest.raises(lw.AlreadyRunning):
            lw.tick(tmp_path, now=NOW, sources=(SOURCE,), fetcher=lambda _: pytest.fail("no fetch permitted"))
    assert not (tmp_path / "state.json").exists()


def test_corrupt_state_is_not_overwritten_and_atomic_write_keeps_original_on_failure(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        lw.tick(tmp_path, now=NOW, sources=(SOURCE,), fetcher=lambda _: response())
    assert state_path.read_text() == "{corrupt"

    def fail_replace(*args):
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr(lw.os, "replace", fail_replace)
    with pytest.raises(OSError):
        lw.atomic_write(state_path, "replacement")
    assert state_path.read_text() == "{corrupt"
    assert not list(tmp_path.glob(".tmp-*"))


def test_html_noise_and_github_volatile_counters_do_not_change_hash():
    assert lw.normalize(SOURCE, OLD) == lw.normalize(SOURCE, "<nav>new menu</nav><script>alert('x')</script>" + OLD + "<footer>new date</footer>")
    repos = lw.Source("repos", "https://api.github.com/orgs/flop-labs/repos", "repos")
    body = [{"full_name": "flop-labs/yellowpaper", "stargazers_count": 1, "pushed_at": "2026-10-19"}]
    old = lw.normalize(repos, json.dumps(body))
    body[0]["stargazers_count"] = 999
    assert old == lw.normalize(repos, json.dumps(body))
    releases = lw.Source("releases", "https://api.github.com/repos/flop-labs/yellowpaper/releases", "releases")
    assert lw.normalize(releases, "[]") == "[]"  # no releases is a valid baseline
    with pytest.raises(ValueError):
        lw.normalize(releases, '{"message":"API rate limit exceeded"}')
    release = [{"tag_name": "v1", "assets": [{"name": "node", "digest": "sha256:old", "download_count": 1}]}]
    before = lw.normalize(releases, json.dumps(release))
    release[0]["assets"][0]["download_count"] = 2
    assert before == lw.normalize(releases, json.dumps(release))
    release[0]["assets"][0]["digest"] = "sha256:new"
    assert before != lw.normalize(releases, json.dumps(release))


@pytest.mark.parametrize("url", ["http://flop.finance/testnet/", "https://evil.test/", "https://user:secret@flop.finance/", "https://flop.finance:444/", "file:///tmp/doc"])
def test_redirects_cannot_escape_official_https_hosts(url):
    with pytest.raises(ValueError):
        lw.validate_url(url)


def test_bounded_fetch_enforces_wall_deadline(monkeypatch):
    def run(command, **kwargs):
        assert kwargs["timeout"] == lw.FETCH_TIMEOUT + 5
        assert command[-1] == SOURCE.url
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(lw.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        lw.fetch_bounded(SOURCE)


def test_optional_local_notifications_are_quiet_on_baseline_and_deduplicated(tmp_path):
    messages = []
    lw.tick(tmp_path, now=NOW, sources=(SOURCE,), fetcher=lambda _: response(), notifier=messages.append)
    assert messages == []
    lw.tick(tmp_path, now=NOW + lw.INTERVAL, sources=(SOURCE,), fetcher=lambda _: response(NEW), notifier=messages.append)
    assert len(messages) == 1 and "Official documents changed: testnet" in messages[0]
    assert "claimed live" not in messages[0]
    lw.tick(tmp_path, now=NOW + 2 * lw.INTERVAL, sources=(SOURCE,), fetcher=lambda _: response(NEW), notifier=messages.append)
    assert len(messages) == 1
    # A failed notification does not lose health state or retry on every heartbeat.
    def fail_notify(message):
        raise TimeoutError()
    result = lw.tick(tmp_path, now=NOW + 3 * lw.INTERVAL, sources=(SOURCE,), fetcher=lambda _: response("bad"), notifier=fail_notify)
    assert result["last_notification"]["delivery"] == "failed"
    lw.tick(tmp_path, now=NOW + 3 * lw.INTERVAL + 10, sources=(SOURCE,), fetcher=lambda _: response(), notifier=messages.append)
    assert len(messages) == 1


def test_status_is_read_only_and_reports_stopped_scheduler(tmp_path, capsys):
    assert lw.cli(["status", "--state-dir", str(tmp_path)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["scheduler_stale"] is True
    assert result["automatic_spending"] is False
    assert list(tmp_path.iterdir()) == []


def installer_module():
    installer_path = Path(__file__).resolve().parents[1] / "scripts" / "install_launchwatch.py"
    spec = importlib.util.spec_from_file_location("install_launchwatch", installer_path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return installer


def test_plist_uses_standalone_script_and_no_shell_or_secrets():
    installer = installer_module()
    plist = installer.make_plist(Path("/path with spaces/python3"), Path("/repo/launchwatch.py"), Path("/repo/.launchwatch"))
    assert plist["ProgramArguments"] == ["/path with spaces/python3", "/repo/launchwatch.py", "tick", "--state-dir", "/repo/.launchwatch"]
    assert plist["StartInterval"] == lw.HEARTBEAT
    assert plist["RunAtLoad"] is True
    assert "EnvironmentVariables" not in plist
    assert installer.make_plist(Path("/python"), Path("/script"), Path("/state"), notify=True)["ProgramArguments"][-1] == "--notify"


def test_installer_only_writes_login_directory_with_explicit_flag(tmp_path, monkeypatch, capsys):
    installer = installer_module()
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        code = 1 if command[1] == "print" and len(calls) == 1 else 0
        return subprocess.CompletedProcess(command, code, stdout="", stderr="")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    state = tmp_path / "state"
    assert installer.main(["--state-dir", str(state), "--notify"]) == 0
    assert not fake_home.exists() and calls == []
    assert installer.main(["--state-dir", str(state), "--install-at-login", "--notify"]) == 0
    plist = fake_home / "Library" / "LaunchAgents" / f"{installer.LABEL}.plist"
    assert plist.exists()
    assert ["launchctl", "bootstrap", f"gui/{installer.os.getuid()}", str(plist)] in calls
    assert calls[-1] == ["launchctl", "print", f"gui/{installer.os.getuid()}/{installer.LABEL}"]
    assert "Loaded and verified" in capsys.readouterr().out


def test_installer_bootstrap_failure_is_concise_and_does_not_claim_success(tmp_path, monkeypatch, capsys):
    installer = installer_module()
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "bootstrap":
            raise subprocess.CalledProcessError(5, command, stderr="Bootstrap failed: 5: Input/output error\n")
        return subprocess.CompletedProcess(command, 1 if command[1] == "print" else 0, stdout="", stderr="")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    assert installer.main(["--state-dir", str(tmp_path / "state"), "--apply"]) == 5
    output = capsys.readouterr()
    assert "launchctl bootstrap exited 5" in output.err
    assert "Traceback" not in output.err and "Loaded" not in output.out
    assert calls[-1][1] == "bootstrap"
    assert not fake_home.exists()


def test_faucet_chain_json_is_canonical_and_validated():
    source = lw.Source("faucet_chain", "https://faucet.flop.network/chain", "json", ("network", "genesisHash"))
    a = lw.normalize(source, '{"network":"devnet","genesisHash":"0x1"}')
    assert a == lw.normalize(source, '{"genesisHash":"0x1", "network":"devnet"}')
    assert a != lw.normalize(source, '{"network":"testnet","genesisHash":"0x2"}')
    with pytest.raises(ValueError):
        lw.normalize(source, '{"error":"down"}')
    lw.validate_url("https://faucet.flop.network/chain")


def test_notify_all_succeeds_if_one_channel_works(monkeypatch):
    sent = []
    monkeypatch.setattr(lw, "notify_telegram", lambda m: (_ for _ in ()).throw(ValueError("x")))
    monkeypatch.setattr(lw, "notify_macos", sent.append)
    lw.notify_all("hi")
    assert sent == ["hi"]
    monkeypatch.setattr(lw, "notify_macos", lambda m: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(ValueError):
        lw.notify_all("hi")


def test_commit_feed_tracks_sha_and_rejects_errors():
    source = lw.Source("yellowpaper_commits", "https://api.github.com/repos/flop-labs/yellowpaper/commits", "commits")
    row = {"sha": "a1", "commit": {"committer": {"date": "2026-11-05"}, "message": "sync\n\nbody"}}
    a = lw.normalize(source, json.dumps([row]))
    assert '"message": "sync"' in a and a != lw.normalize(source, json.dumps([dict(row, sha="b2")]))
    with pytest.raises(ValueError):
        lw.normalize(source, '{"message": "API rate limit exceeded"}')
