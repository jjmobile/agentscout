import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentscout.airdrop_status import build_status, cli


def test_unknown_is_not_zero_and_nonexistent_db_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="does not exist"):
        build_status(missing)
    assert not missing.exists()
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    before = path.read_bytes()
    status = build_status(path)
    assert status["local_sessions"]["states"] is None
    assert status["inference_journal"]["available"] is False
    assert status["official_allocation"]["credited_allocation_base"] is None
    assert path.read_bytes() == before


def test_session_labels_and_paper_deals_do_not_become_credited_spend(tmp_path):
    path = tmp_path / "agent.db"
    conn = sqlite3.connect(path)
    for table in ("flop_sessions", "payer_deals", "worker_deals"):
        conn.execute(f"CREATE TABLE {table} (state TEXT)")
    conn.execute("INSERT INTO flop_sessions VALUES ('SETTLED')")
    conn.execute("INSERT INTO payer_deals VALUES ('claimed')")
    conn.execute("INSERT INTO worker_deals VALUES ('refunded')")
    conn.commit()
    conn.close()
    report = build_status(path)
    assert report["local_sessions"]["states"] == {"SETTLED": 1}
    assert report["paper_commerce"]["payer"]["states"] == {"claimed": 1}
    assert report["inference_journal"]["available"] is False
    assert report["official_allocation"]["credited_allocation_base"] is None


def test_networks_stay_separate_and_u128_amounts_do_not_round(tmp_path):
    path = tmp_path / "agent.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE inference_requests (
        provider TEXT,network TEXT,account TEXT,asset TEXT,decimals INTEGER,state TEXT,escrow_base TEXT,fee_reserve_base TEXT,
        liability_base TEXT,finalized_spend_base TEXT,finalized_fees_base TEXT)""")
    huge = 2 ** 100 + 1
    conn.executemany("INSERT INTO inference_requests VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        ("flop", "testnet", "our-account", "FLOP", 18, "FINALIZED", str(huge), "3", "0", str(huge), "2"),
        ("flop", "testnet", "our-account", "FLOP", 18, "AMBIGUOUS", "10", "3", "13", "0", "0"),
        ("flop", "mainnet", "our-account", "FLOP", 18, "RESERVED", "20", "3", "23", "0", "0"),
    ])
    conn.commit()
    conn.close()
    groups = {group["network"]: group for group in build_status(path)["inference_journal"]["groups"]}
    assert groups["testnet"]["finalized_spend_base"] == str(huge)
    assert groups["testnet"]["reserved_escrow_base"] == str(huge + 10)
    assert groups["testnet"]["outstanding_liability_base"] == "13"
    assert groups["mainnet"]["outstanding_liability_base"] == "23"
    assert groups["testnet"]["credited_allocation_base"] is None


def test_recorded_heartbeat_does_not_claim_current_process_liveness(tmp_path):
    path = tmp_path / "agent.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT)")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.executemany("INSERT INTO settings VALUES (?,?)", [("runtime_heartbeat_at", yesterday),
                      ("own_did", "did:key:existing"), ("not_for_status", "private data")])
    conn.commit()
    conn.close()
    runtime = build_status(path)["runtime"]
    assert runtime["heartbeat_age_seconds"] >= 86400
    assert runtime["recorded"]["own_did"] == "did:key:existing"
    assert runtime["recorded"]["deployed_source_revision"] is None
    assert "not_for_status" not in runtime["recorded"]
    assert runtime["running_verified"] is None


def test_legacy_rows_are_liability_floor_with_unknown_fees(storage):
    storage.conn.execute("""INSERT INTO inference_requests
        (request_id,provider,asset,decimals,network,account,day,state,escrow_base,fee_reserve_base,
         liability_base,intent_json,created_at,updated_at)
        VALUES ('legacy:one','legacy-flop','FLOP',18,'unknown','unknown','2026-10-19',
                'LEGACY_UNVERIFIED','100','0','100','{}','2026-10-19','2026-10-19')""")
    report = build_status(storage.path)
    group = report["inference_journal"]["groups"][0]
    assert group["outstanding_liability_base"] == "100"
    assert group["reserved_fee_base"] is None
    assert group["liability_is_lower_bound"] is True


def test_supplied_allocation_cannot_bless_itself_as_official(tmp_path):
    path = tmp_path / "agent.db"
    sqlite3.connect(path).close()
    evidence = tmp_path / "allocation.json"
    evidence.write_text(json.dumps({"source_url": "https://flop.finance/airdrop/", "verified": True,
                                    "account": "not-our-account", "allocation_base": "1000000"}))
    allocation = build_status(path, evidence)["official_allocation"]
    assert allocation["verification"] == "UNVERIFIED_SOURCE_PROVIDED"
    assert allocation["claimed_allocation_base"] == "1000000"
    assert allocation["credited_allocation_base"] is None
    assert allocation["claimable_base"] is None


def test_bad_evidence_returns_error_without_mutating_database(tmp_path, capsys):
    path = tmp_path / "agent.db"
    sqlite3.connect(path).close()
    evidence = tmp_path / "allocation.json"
    evidence.write_text('{"allocation_base": 1.5}')
    before = path.read_bytes()
    assert cli(["--db", str(path), "--allocation-evidence", str(evidence)]) == 1
    assert "nonnegative integer" in capsys.readouterr().err
    assert path.read_bytes() == before
