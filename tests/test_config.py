import pytest

from agentscout.config import ConfigError, Settings, validate_base_url


@pytest.mark.parametrize("bad", ["http://technocore.chat", "https://technocore.chat/r/lobby", "https://user:pw@technocore.chat",
                                 "https://technocore.chat/?x=1", "ftp://technocore.chat", "technocore.chat"])
def test_base_url_rejects_non_bare_https(bad):
    with pytest.raises(ConfigError):
        validate_base_url(bad)


def test_base_url_ok():
    assert validate_base_url("https://technocore.chat/") == "https://technocore.chat"


def test_refuses_unbuilt_milestone_flags(monkeypatch):
    monkeypatch.setenv("SCOUT_LLM_ENABLED", "true")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_publish_requires_dry_run_off(monkeypatch):
    monkeypatch.setenv("SCOUT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("DRY_RUN", "false")
    assert Settings.from_env().will_publish is True
    monkeypatch.setenv("SCOUT_FEED_ROOM", "agentscout-feed")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_defaults_are_read_only(monkeypatch):
    for k in ("DRY_RUN", "SCOUT_PUBLISH_ENABLED", "SCOUT_LLM_ENABLED", "SCOUT_REPLIES_ENABLED", "SCOUT_FREETEXT_QUERIES"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.dry_run and not s.will_publish and not s.llm_enabled and not s.replies_enabled and not s.freetext_queries


def test_invalid_room_name(monkeypatch):
    monkeypatch.setenv("SCOUT_WATCH_ROOMS", "lobby,Bad Room")
    with pytest.raises(ConfigError):
        Settings.from_env()
