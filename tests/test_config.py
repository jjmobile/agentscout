import pytest

from agentscout.config import ConfigError, Settings, validate_base_url


@pytest.mark.parametrize("bad", ["http://technocore.chat", "https://technocore.chat/r/lobby", "https://user:pw@technocore.chat",
                                 "https://technocore.chat/?x=1", "ftp://technocore.chat", "technocore.chat"])
def test_base_url_rejects_non_bare_https(bad):
    with pytest.raises(ConfigError):
        validate_base_url(bad)


def test_base_url_ok():
    assert validate_base_url("https://technocore.chat/") == "https://technocore.chat"


def test_milestone_a_refuses_write_or_llm_modes(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("SCOUT_LLM_ENABLED", "true")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_defaults_are_read_only(monkeypatch):
    for k in ("DRY_RUN", "SCOUT_LLM_ENABLED", "SCOUT_REPLIES_ENABLED", "SCOUT_FREETEXT_QUERIES"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.dry_run and not s.llm_enabled and not s.replies_enabled and not s.freetext_queries


def test_invalid_room_name(monkeypatch):
    monkeypatch.setenv("SCOUT_WATCH_ROOMS", "lobby,Bad Room")
    with pytest.raises(ConfigError):
        Settings.from_env()
