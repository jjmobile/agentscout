#!/usr/bin/env python3
"""Seed two useful documentation jobs from this checkout; never execute inference."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentscout.workload import cli  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="separate workload SQLite file; parent must exist")
    args = parser.parse_args()
    return cli(["--db", args.db, "enqueue", str(ROOT / "workloads" / "initial_jobs.json"),
                "--source-root", str(ROOT)])


if __name__ == "__main__":
    raise SystemExit(main())
