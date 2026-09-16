from datetime import timedelta

from agentscout import radar
from conftest import NOW

OLD_LLMS = "## READ\nGET /r/<room>\n## LIMITS\ntwo token buckets\n"
NEW_LLMS = "## READ\nGET /r/<room>\n## LIMITS\nthree token buckets\n## FAUCET\nGET /faucet/<did> hands testnet FLOP to a did:key\n"
OLD_CARD = {"version": "0.7.0", "limits": {"reads_per_minute_per_ip": 600, "message_chars": 4096}, "endpoints": ["/r", "/kv"]}
NEW_CARD = {"version": "0.9.7", "limits": {"reads_per_minute_per_ip": 900, "message_chars": 4096, "note_chars": 8192}, "endpoints": ["/r", "/kv", "/faucet"]}


def test_compare_is_deterministic_and_names_what_moved():
    c = radar.compare(NOW, OLD_LLMS, NEW_LLMS, OLD_CARD, NEW_CARD)
    assert (c.old_version, c.new_version) == ("0.7.0", "0.9.7")
    assert c.card_changed == ["endpoints [\"/r\", \"/kv\"]→[\"/r\", \"/kv\", \"/faucet\"]", "limits.reads_per_minute_per_ip 600→900"]
    assert c.card_added == ["limits.note_chars=8192"] and c.card_removed == []
    assert c.sections_added == ["FAUCET"] and c.sections_removed == []
    assert (c.lines_added, c.lines_removed) == (3, 1)          # "three token buckets" + 2 faucet lines ; "two token buckets"
    assert c.keywords_new == ["faucet", "flop", "testnet"]
    assert c.summary() == "agent.json v0.7.0 → v0.9.7; 3 agent.json fields changed; llms.txt +FAUCET; NEW KEYWORDS: faucet,flop,testnet"
    assert radar.compare(NOW, OLD_LLMS, NEW_LLMS, OLD_CARD, NEW_CARD).to_json() == c.to_json()
    assert radar.DocChange.from_json(c.to_json()) == c
    assert radar.compare(NOW, OLD_LLMS, OLD_LLMS, OLD_CARD, OLD_CARD).is_empty


def test_feed_line_and_note_are_bounded_and_carry_the_marker():
    c = radar.compare(NOW, OLD_LLMS, NEW_LLMS, OLD_CARD, NEW_CARD)
    line = radar.feed_line(c, "agentscout")
    assert line.startswith("TECHNOCORE CHANGE 2026-08-25T12:00Z | agent.json v0.7.0 → v0.9.7")
    assert "Δ limits.reads_per_minute_per_ip 600→900" in line and "/kv/agentscout/protocol" in line
    assert "\n" not in line and line.endswith("Observed behaviour, not endorsement.")
    assert radar.marker(c) == "TECHNOCORE CHANGE 2026-08-25T12:00Z"
    later = radar.compare(NOW + timedelta(hours=6), NEW_LLMS, NEW_LLMS + "## WALLET\nlink a wallet\n", NEW_CARD, NEW_CARD)
    note = radar.protocol_note([later, c], "0.9.7", "2026-08-25T06:00:00Z", NOW + timedelta(hours=6))
    assert note.startswith("agentscout protocol asof=2026-08-25T18:00Z agent.json-version=0.9.7 watching=llms.txt,/.well-known/agent.json baseline=2026-08-25T06:00:00Z changes=2 ; 2026-08-25T18:00Z llms.txt +WALLET; NEW KEYWORDS: wallet")
    assert len(note) <= 3800 and "\n" not in note


def test_watch_docs_records_a_change_only_when_a_previous_copy_exists(server, settings, client, storage):
    import json
    from agentscout.ingest import Ingestor as Ingester
    server.route("/llms.txt", body=OLD_LLMS)
    server.route("/.well-known/agent.json", body=OLD_CARD)
    ing = Ingester(settings, client, storage)
    assert ing.watch_docs(NOW) is False                                            # baseline: snapshots stored
    assert storage.doc_snapshot("llms.txt")["text"] == OLD_LLMS and json.loads(storage.doc_snapshot("agent.json")["text"]) == OLD_CARD
    server.route("/llms.txt", body=NEW_LLMS)
    server.route("/.well-known/agent.json", body=NEW_CARD)
    assert ing.watch_docs(NOW + timedelta(hours=7)) is True
    rows = storage.protocol_changes()
    assert len(rows) == 1 and rows[0]["old_version"] == "0.7.0" and rows[0]["new_version"] == "0.9.7"
    assert rows[0]["summary"].startswith("agent.json v0.7.0 → v0.9.7; 3 agent.json fields changed")
    assert storage.unpublished_protocol_changes()[0]["id"] == rows[0]["id"]
    assert ing.watch_docs(NOW + timedelta(hours=14)) is False and len(storage.protocol_changes()) == 1


YP_OLD = ('<span>Version<b>0.5.0 (draft)</b></span>\n<span>Updated<b>2026-09-05</b></span>\n\n'
          '**E.36 — External-work attestor classes `[TBD]`.** Market-grade attestor sets.\n\n'
          '**E.38 — Genesis allocation & airdrop vesting `[TBD]`.** The path that distributes\ngenesis_supply has no normative section.\n\n'
          '**E.39 — Validator-reward liquidity `[RATIFY]`.** Unrelated.\n\n'
          '**E.40 — Agent & staker leg distribution `[TBD]`.** Specify how the pools are paid out.\n')
YP_NEW = (YP_OLD.replace("0.5.0 (draft)", "0.6.0 (draft)").replace("2026-09-05", "2026-10-02")
          .replace("airdrop vesting `[TBD]`.** The path that distributes\ngenesis_supply has no normative section.",
                   "airdrop vesting `[RATIFY]`.** Tier set: settled inference spend on testnet, sublinear; claim path §9.4."))


def test_yellowpaper_facts_and_summary_track_the_open_items():
    old, new = radar.yellowpaper_facts(YP_OLD), radar.yellowpaper_facts(YP_NEW)
    assert old["version"] == "0.5.0 (draft)" and old["updated"] == "2026-09-05"
    assert {t: i["status"] for t, i in old["items"].items()} == {"E.36": "TBD", "E.38": "TBD", "E.40": "TBD"}
    assert radar.yellowpaper_summary(old, old) == "text changed outside version/date/E.36,E.38,E.40"
    assert radar.yellowpaper_summary(old, new) == "version 0.5.0 (draft) → 0.6.0 (draft); updated 2026-09-05 → 2026-10-02; E.38 [TBD] → [RATIFY]"
    reworded = YP_OLD.replace("Specify how the pools", "Specify how both pools")
    assert radar.yellowpaper_summary(old, radar.yellowpaper_facts(reworded)) == "E.40 text changed (still [TBD])"
    gone = YP_OLD.replace("**E.40 — Agent & staker leg distribution `[TBD]`.**", "**E.40 — Agent leg.**")
    assert radar.yellowpaper_summary(old, radar.yellowpaper_facts(gone)) == "E.40 GONE (was [TBD]; ratified into a section?)"


def test_watch_yellowpaper_warns_once_per_change_and_never_publishes(server, settings, client, storage, caplog):
    import logging
    from agentscout.ingest import Ingestor
    path = "/flop-labs/yellowpaper/main/yellowpaper.md"
    server.route(path, body=YP_OLD)
    ing = Ingestor(settings, client, storage)
    with caplog.at_level(logging.INFO):
        assert ing.watch_yellowpaper(NOW) is False                                # baseline
        assert ing.watch_yellowpaper(NOW + timedelta(hours=1)) is False           # not due
        assert server.requests.count(path) == 1
        assert storage.doc_snapshot("yellowpaper.md")["text"] == YP_OLD
        server.route(path, body=YP_NEW)
        assert ing.watch_yellowpaper(NOW + timedelta(hours=7)) is True
        assert ing.watch_yellowpaper(NOW + timedelta(hours=14)) is False          # unchanged: quiet
        server.route(path, status=503, body="")
        assert ing.watch_yellowpaper(NOW + timedelta(hours=21)) is False          # outage: no snapshot churn
    assert storage.doc_snapshot("yellowpaper.md")["text"] == YP_NEW
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "YELLOW PAPER CHANGED" in r.getMessage()]
    assert len(warn) == 1 and "E.38 [TBD] → [RATIFY] | now E.36=[TBD] E.38=[RATIFY] E.40=[TBD]" in warn[0]
    assert storage.protocol_changes() == [] and storage.unpublished_protocol_changes() == []
    settings.yellowpaper_url = ""
    assert ing.watch_yellowpaper(NOW + timedelta(days=2)) is False and server.requests.count(path) == 4
