"""Read-only reconciliation: paper activity, local session evidence, and allocation are separate.

This report never queries a chain, signs, submits, infers eligibility, or marks an allocation credited.
An optional allocation file is an unverified source claim even if its own fields say 'verified'.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

RUNTIME_KEYS = ("own_did", "deployed_source_revision", "inference_provider", "flop_live_enabled",
                "runtime_started_at", "runtime_heartbeat_at")


def _runtime(conn, tables: set, now: datetime) -> dict:
    recorded = {key: None for key in RUNTIME_KEYS}
    if "settings" in tables:
        placeholders = ",".join("?" for _ in RUNTIME_KEYS)
        for row in conn.execute(f"SELECT key,value FROM settings WHERE key IN ({placeholders})", RUNTIME_KEYS):
            recorded[row[0]] = row[1]
    age = None
    if recorded["runtime_heartbeat_at"]:
        try:
            timestamp = datetime.fromisoformat(recorded["runtime_heartbeat_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is not None:
                age = int((now - timestamp).total_seconds())
        except ValueError:
            pass
    return {"recorded": recorded, "heartbeat_age_seconds": age,
            "running_verified": None,
            "reason": "recorded settings and heartbeat are historical evidence, not a live process probe"}


def _counts(conn, tables: set, name: str) -> dict:
    if name not in tables:
        return {"available": False, "states": None}
    states = {row[0]: row[1] for row in conn.execute(f"SELECT state,COUNT(*) FROM {name} GROUP BY state")}
    return {"available": True, "states": states}


def _amount(value) -> int:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("amount must be a nonnegative integer in base units")
    text = str(value)
    if not text.isascii() or not text.isdigit():
        raise ValueError("amount must be a nonnegative integer in base units")
    return int(text)


def allocation_claim(path: Path) -> dict:
    raw = path.read_bytes()
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("allocation evidence must be a JSON object")
    # A source URL, hash or a caller's verified=true flag does not authenticate an allocation.
    return {"verification": "UNVERIFIED_SOURCE_PROVIDED", "sha256": hashlib.sha256(raw).hexdigest(),
            "source_url_provided": doc.get("source_url"), "network_provided": doc.get("network"),
            "account_provided": doc.get("account"), "snapshot_provided": doc.get("snapshot"),
            "claimed_allocation_base": str(_amount(doc["allocation_base"]))
            if "allocation_base" in doc else None,
            "credited_allocation_base": None, "claimable_base": None,
            "reason": "source authenticity, account match, snapshot and claim status not independently verified"}


def build_status(path: Path, evidence: Path = None) -> dict:
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("agent database does not exist")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        now = datetime.now(timezone.utc)
        report = {"schema": "agentscout-airdrop-status-v1",
                  "generated_at": now.isoformat(),
                  "database": str(path),
                  "runtime": _runtime(conn, tables, now),
                  "paper_commerce": {"payer": _counts(conn, tables, "payer_deals"),
                                     "worker": _counts(conn, tables, "worker_deals"),
                                     "allocation_credit": "not inferred from paper deals"},
                  "local_sessions": _counts(conn, tables, "flop_sessions"),
                  "inference_journal": {"available": "inference_requests" in tables, "groups": []},
                  "official_allocation": {"verification": "UNKNOWN", "credited_allocation_base": None,
                                          "claimable_base": None}}
        report["local_sessions"]["warning"] = "CLOSED or SETTLED labels alone do not prove finality or airdrop credit"
        if "inference_requests" in tables:
            groups = {}
            for row in conn.execute("""SELECT provider,network,account,asset,decimals,state,escrow_base,fee_reserve_base,
                                       liability_base,finalized_spend_base,finalized_fees_base
                                       FROM inference_requests"""):
                key = (row["provider"], row["network"], row["account"], row["asset"], row["decimals"])
                if key not in groups:
                    groups[key] = {"provider": key[0], "network": key[1], "account": key[2], "states": {},
                                   "asset": key[3], "decimals": key[4], "legacy_rows": 0,
                                   "reserved_escrow_base": 0, "reserved_fee_base": 0,
                                   "outstanding_liability_base": 0, "finalized_spend_base": 0,
                                   "finalized_fees_base": 0}
                group = groups[key]
                state = row["state"]
                group["states"][state] = group["states"].get(state, 0) + 1
                if state == "LEGACY_UNVERIFIED":
                    group["legacy_rows"] += 1
                for output, column in (("reserved_escrow_base", "escrow_base"),
                                       ("reserved_fee_base", "fee_reserve_base"),
                                       ("outstanding_liability_base", "liability_base"),
                                       ("finalized_spend_base", "finalized_spend_base"),
                                       ("finalized_fees_base", "finalized_fees_base")):
                    group[output] += _amount(row[column])
            for group in groups.values():
                for key in tuple(group):
                    if key.endswith("_base"):
                        group[key] = str(group[key])
                group["unit"] = f"10^-{group['decimals']} {group['asset']} (local journal)"
                group["liability_is_lower_bound"] = group["legacy_rows"] > 0
                if group["legacy_rows"]:
                    group["reserved_fee_base"] = None
                    group["legacy_warning"] = "legacy escrow retained; historical fees unknown, not zero"
                group["finality_basis"] = "trusted evidence recorded by the local journal; not independently re-queried"
                group["credited_allocation_base"] = None
            report["inference_journal"]["groups"] = list(groups.values())
        else:
            report["inference_journal"]["reason"] = "journal absent; spend and liabilities unknown, not zero"
        if evidence is not None:
            report["official_allocation"] = allocation_claim(Path(evidence))
        return report
    finally:
        conn.close()


def cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--allocation-evidence", type=Path,
                        help="optional supplied source claim; always displayed as unverified")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_status(args.db, args.allocation_evidence), indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"airdrop-status: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
