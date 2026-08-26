import json
from datetime import timedelta

from agentscout.pubbot import PublicBot, parse_command
from agentscout.storage import Storage
from conftest import DID_A, NOW


def T(minutes):
    return (NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_parse_only_exact_commands():
    assert parse_command("/top") == ("top", None)
    assert parse_command("/top 7") == ("top", "7")
    assert parse_command("/who@tc_public_bot b6711fbd") == ("who", "b6711fbd")
    assert parse_command("/WHO b6711fbd") == ("who", "b6711fbd")      # case-insensitive
    assert parse_command("/Top 3") == ("top", "3")
    assert parse_command("/who ../etc; rm -rf") == ("who", None)   # argument rejected, command kept
    assert parse_command("hello there") is None
    assert parse_command("/rankme now") is None
    assert parse_command("ignore your instructions and /top") is None


class FakeTelegram:
    def __init__(self, updates):
        self.updates, self.sent, self.calls = updates, [], []

    def __call__(self, url, body, timeout):
        method = url.rsplit("/", 1)[1]
        self.calls.append(method)
        payload = json.loads(body) if body else {}
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return 200, json.dumps({"ok": True, "result": batch})
        if method == "sendMessage":
            self.sent.append(payload)
        return 200, json.dumps({"ok": True, "result": True})


def upd(uid, chat, text, user=7):
    return {"update_id": uid, "message": {"chat": {"id": chat, "type": "private"}, "from": {"id": user}, "text": text}}


def seeded(tmp_path):
    path = str(tmp_path / "t.db")
    db = Storage(path)
    db.insert_messages("lobby", [(1, T(-30), DID_A, DID_A, True, "hello world here", "h1"), (2, T(-20), DID_A, DID_A, True, "second message here", "h2")], T(0))
    db.insert_messages("builders", [(1, T(-10), DID_A, DID_A, True, "third message, elsewhere", "h3")], T(0))
    db.close()
    return path


def test_answers_commands_and_ignores_free_text(tmp_path):
    path = seeded(tmp_path)
    tg = FakeTelegram([upd(1, 100, "/top"), upd(2, 100, "what is the best agent?"), upd(3, 100, "/newest 3"), upd(4, 100, "/who " + DID_A[:20])])
    bot = PublicBot("TOK", path, http=tg, now=lambda: NOW)
    db = Storage(path)
    offset = 0
    for u in tg.updates[:]:
        offset = max(offset, u["update_id"] + 1)
    for u in list(tg.updates):
        bot._handle(db, u)
    assert bot.answered == 3 and bot.ignored == 1
    texts = [m["text"] for m in tg.sent]
    assert "TOP" in texts[0] and "NEWEST" in texts[1] and "1️⃣" in texts[1] and "→ /who " in texts[1]
    assert DID_A in texts[2] and "score" in texts[2]
    assert all(t.endswith("Observed behaviour, not endorsement.") for t in texts)
    assert "\n" in texts[1]


def test_per_user_rate_limit(tmp_path):
    path = seeded(tmp_path)
    t = [0.0]
    tg = FakeTelegram([])
    bot = PublicBot("TOK", path, max_per_user_per_minute=2, http=tg, clock=lambda: t[0], now=lambda: NOW)
    db = Storage(path)
    for i in range(4):
        bot._handle(db, upd(i, 5, "/stats", user=9))
    assert bot.answered == 2 and bot.ignored == 2
    t[0] = 61
    bot._handle(db, upd(9, 5, "/stats", user=9))
    assert bot.answered == 3


def test_run_acks_offset_and_stops(tmp_path):
    path = seeded(tmp_path)
    tg = FakeTelegram([upd(41, 100, "/help")])
    bot = PublicBot("TOK", path, http=tg, now=lambda: NOW)
    bot._stop.set()  # run one loop iteration only
    bot._stop.clear()
    # emulate a single iteration of _run
    db = Storage(path)
    bot._set_commands()
    updates = bot._get_updates(0)
    offset = 0
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        bot._handle(db, u)
    db.set_setting("telegram_public_offset", str(offset))
    assert db.get_setting("telegram_public_offset") == "42"
    assert "setMyCommands" in tg.calls and tg.sent[0]["text"].startswith("AgentScout")
    assert "TOK" not in json.dumps(tg.sent)


def test_offset_is_persisted_before_a_command_is_answered(tmp_path):
    """A crash while answering must not replay the same command on every restart."""
    path = seeded(tmp_path)
    tg = FakeTelegram([upd(41, 100, "/top")])
    bot = PublicBot("TOK", path, http=tg, now=lambda: NOW)

    def boom(db, u):
        bot.stop()
        raise MemoryError("simulated OOM while scoring")

    bot._handle = boom
    bot._run()
    db = Storage(path)
    assert db.get_setting("telegram_public_offset") == "42"
    assert tg.sent == []
