import logging

from agentscout.notify import TelegramLogHandler, TelegramNotifier, load_token


class Poster:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, url, body, timeout):
        self.calls.append((url, body))
        return self.status


def test_disabled_without_token_or_chat():
    assert TelegramNotifier(None, "1").send("x") is False
    assert TelegramNotifier("t", None).send("x") is False


def test_send_posts_to_bot_url_and_truncates():
    p = Poster()
    n = TelegramNotifier("SECRET", "42", poster=p)
    assert n.send("y" * 5000)
    url, body = p.calls[0]
    assert url == "https://api.telegram.org/botSECRET/sendMessage"
    assert b'"chat_id": "42"' in body and len(body) < 4200


def test_hourly_cap_and_no_token_in_logs(caplog):
    t = [0.0]
    p = Poster()
    n = TelegramNotifier("SECRET", "42", max_per_hour=2, poster=p, clock=lambda: t[0])
    with caplog.at_level(logging.DEBUG):
        assert n.send("a") and n.send("b") and not n.send("c")
        t[0] = 3601
        assert n.send("d")
        p.status = 401
        assert not n.send("e")
    assert "SECRET" not in caplog.text
    assert n.dropped == 1


def test_log_handler_forwards_warnings_only():
    p = Poster()
    n = TelegramNotifier("t", "1", poster=p)
    h = TelegramLogHandler(n)
    lg = logging.getLogger("agentscout.test")
    lg.addHandler(h)
    lg.error("boom")
    lg.warning("plain warning from a non-publisher logger is not forwarded")
    lg.info("quiet")
    logging.getLogger("agentscout.notify").error("never forwarded")
    lg.removeHandler(h)
    assert len(p.calls) == 1 and b"boom" in p.calls[0][1]


def test_load_token_prefers_file(tmp_path):
    f = tmp_path / "tok"
    f.write_text("abc\n")
    assert load_token(str(f), "env") == "abc"
    assert load_token(str(tmp_path / "missing"), "env") == "env"
    assert load_token(str(tmp_path / "missing"), "") is None


def test_log_handler_filters_transient_noise_and_counts():
    from agentscout.notify import OpsCounter
    p = Poster()
    n = TelegramNotifier("t", "1", poster=p)
    counter = OpsCounter()
    h = TelegramLogHandler(n, counter)
    ing = logging.getLogger("agentscout.ingest.test")
    pub = logging.getLogger("agentscout.publisher.test")
    ing.addHandler(h); pub.addHandler(h)
    ing.warning("room x: GET /r/x: HTTP 500")
    ing.warning("sequence gap in lobby: expected 1, first available 9")
    ing.warning("write /kv/did/abc: HTTP 500 Internal Server Error")
    ing.warning("write /kv/agentscout/new failed: POST /kv/agentscout/new: TimeoutError")
    ing.warning("TECHNOCORE VERSION CHANGED 0.7.0 -> 0.8.0")
    pub.warning("NOTE_TAMPERED /kv/agentscout/top")
    pub.warning("post to d-x rejected (400)")
    ing.error("cycle failed")
    ing.removeHandler(h); pub.removeHandler(h)
    sent = [c[1].decode() for c in p.calls]
    assert len(sent) == 4
    assert any("VERSION CHANGED" in s for s in sent) and any("NOTE_TAMPERED" in s for s in sent) and any("cycle failed" in s for s in sent)
    assert counter.counts == {"transient_http_errors": 3, "ring_gaps": 1}
    assert counter.summary_and_reset() == "1 ring gaps, 3 transient http errors"
    assert counter.counts == {}
