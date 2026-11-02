#!/usr/bin/env python3
"""Generate a launchd job; explicit flags activate it for this login or future logins."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LABEL = "chat.technocore.agentscout.launchwatch"


def make_plist(python: Path, script: Path, directory: Path, notify=False) -> dict:
    return {"Label": LABEL,
            "ProgramArguments": [str(python), str(script), "tick", "--state-dir", str(directory)] + (["--notify"] if notify else []),
            "WorkingDirectory": str(ROOT), "RunAtLoad": True, "StartInterval": 600,
            "ProcessType": "Background", "LowPriorityIO": True,
            "StandardOutPath": str(directory / "launchd.stdout.log"),
            "StandardErrorPath": str(directory / "launchd.stderr.log")}


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".launchwatch")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="bootstrap the repo plist for this login only")
    mode.add_argument("--install-at-login", action="store_true",
                      help="explicitly write ~/Library/LaunchAgents and bootstrap for this and future logins")
    parser.add_argument("--notify", action="store_true", help="enable local native macOS notifications")
    mode.add_argument("--remove", action="store_true", help="unload this job, preserving all files")
    args = parser.parse_args(argv)
    directory = args.state_dir.expanduser().resolve()
    python = args.python.expanduser().resolve()
    script = ROOT / "src" / "agentscout" / "launchwatch.py"
    if not python.is_file() or not os.access(python, os.X_OK):
        parser.error("--python must name an executable Python 3.9+ interpreter")
    if (args.apply or args.install_at_login or args.remove) and sys.platform != "darwin":
        parser.error("service activation/removal requires macOS launchd")
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    if args.remove:
        subprocess.run(["launchctl", "bootout", target], capture_output=True, text=True, check=True)
        print("Unloaded this job; evidence and any login plist are preserved.")
        return 0
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / f"{LABEL}.plist"
    data = plistlib.dumps(make_plist(python, script, directory, notify=args.notify))
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    temporary.replace(destination)
    print(f"Generated {destination}")
    if args.install_at_login:
        # The sole opt-in write outside the checkout. Never reached by the default or --apply.
        destination = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        temporary.replace(destination)
        print(f"Saved login plist {destination}; activation still needs verification.")
    else:
        print("Repo-only mode: re-run --apply after macOS logout/reboot.")
        print("For future logins, explicitly run --install-at-login; it writes ~/Library/LaunchAgents.")
    if args.apply or args.install_at_login:
        # Idempotent update of this one known label, without affecting other jobs.
        existing = subprocess.run(["launchctl", "print", target], capture_output=True, check=False)
        if existing.returncode == 0:
            subprocess.run(["launchctl", "bootout", target], capture_output=True, text=True, check=True)
        subprocess.run(["launchctl", "enable", target], capture_output=True, text=True, check=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(destination)], capture_output=True, text=True, check=True)
        subprocess.run(["launchctl", "print", target], capture_output=True, text=True, check=True)
        print(f"Loaded and verified {target}; RunAtLoad requests the initial source check.")
        print("Fetch health is separate: inspect status after the first check completes.")
    else:
        command = [str(python), str(Path(__file__).resolve()), "--state-dir", str(directory), "--apply"]
        if args.notify:
            command.append("--notify")
        print("Not installed. To load: " + shlex.join(command))
    print("Status: " + shlex.join([str(python), str(script), "status", "--state-dir", str(directory)]))
    return 0


def main(argv=None) -> int:
    try:
        return _main(argv)
    except subprocess.CalledProcessError as exc:
        operation = " ".join(str(part) for part in exc.cmd[:2])
        reason = (exc.stderr or exc.stdout or "").strip().splitlines()
        detail = ": " + reason[0][:240] if reason else ""
        print(f"Activation/removal not confirmed: {operation} exited {exc.returncode}{detail}", file=sys.stderr)
        print("Generated files are preserved. No alternative execution path was attempted.", file=sys.stderr)
        return exc.returncode or 1
    except OSError as exc:
        print(f"Installation not confirmed: {exc.strerror or type(exc).__name__}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
