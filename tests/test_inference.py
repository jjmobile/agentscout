import json

import pytest

from agentscout import inference
from agentscout.config import Settings
from agentscout.inference import FlopProvider, InferenceUnavailable, make_provider


def test_default_provider_is_anthropic_and_needs_a_key(storage, tmp_path):
    s = Settings(watch_rooms=["lobby"], db_path=str(tmp_path / "t.db"))
    assert s.inference_provider == "anthropic"
    assert make_provider(s, storage, None) is None                 # no key → None (LLM disabled, as before)


def test_flop_provider_reports_unavailable_until_an_endpoint_is_advertised(storage, tmp_path):
    s = Settings(watch_rooms=["lobby"], db_path=str(tmp_path / "t.db"), inference_provider="flop")
    p = make_provider(s, storage, api_key="unused-when-flop")
    assert isinstance(p, FlopProvider) and p.name == "flop"
    assert p.endpoint() is None                                     # no agent.json snapshot yet
    with pytest.raises(InferenceUnavailable):
        p.messages.parse(model="x", max_tokens=10, messages=[])     # smoke would fail → LLM disabled cleanly


def test_flop_provider_discovers_the_endpoint_from_agent_json(storage, tmp_path):
    s = Settings(watch_rooms=["lobby"], db_path=str(tmp_path / "t.db"), inference_provider="flop",
                 flop_live_enabled=True, flop_allow_provisional_transport=True)
    p = make_provider(s, storage, None)
    storage.set_doc_snapshot("agent.json", json.dumps({"endpoints": {"inference": "/inference/run"}}), "2026-09-04T00:00Z")
    assert p.endpoint() == "/inference/run"
    from agentscout.identity import Identity
    p.bind_identity(Identity.load_or_create(str(tmp_path / "id.key"))[0])
    with pytest.raises(InferenceUnavailable, match="no Flop RPC configured"):
        p.messages.parse(model="x", max_tokens=10, messages=[])     # endpoint known, no testnet RPC yet → honest stop


def test_radar_watches_for_inference_keywords():
    from agentscout.ingest import Ingestor
    assert all(k in Ingestor.DOC_KEYWORDS for k in ("inference", "compute", "gpu", "miner"))


def test_flop_live_and_provisional_gates_precede_network_or_keys(storage, tmp_path, monkeypatch):
    from agentscout import flopchain, flopsession
    s = Settings(inference_provider="flop", flop_rpc_url="wss://unused.invalid", flop_miner_url="https://unused.invalid")
    p = make_provider(s, storage, None)

    def forbidden(*args, **kwargs):
        pytest.fail("blocked provider must not connect or load keys")

    monkeypatch.setattr(flopchain, "make_chain", forbidden)
    with pytest.raises(InferenceUnavailable, match="spending is disabled"):
        p.session()
    s.flop_live_enabled = True
    with pytest.raises(InferenceUnavailable, match="provisional FLOP transport is blocked"):
        p.session()
    with pytest.raises(flopsession.SessionError, match="provisional miner transport is disabled"):
        flopsession.HttpMinerTransport("https://unused.invalid")
