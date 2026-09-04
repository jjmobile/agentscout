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
    s = Settings(watch_rooms=["lobby"], db_path=str(tmp_path / "t.db"), inference_provider="flop")
    p = make_provider(s, storage, None)
    storage.set_doc_snapshot("agent.json", json.dumps({"endpoints": {"inference": "/inference/run"}}), "2026-09-04T00:00Z")
    assert p.endpoint() == "/inference/run"
    with pytest.raises(InferenceUnavailable, match="client not implemented yet"):
        p.messages.parse(model="x", max_tokens=10, messages=[])     # endpoint known, POST still to build


def test_radar_watches_for_inference_keywords():
    from agentscout.ingest import Ingestor
    assert all(k in Ingestor.DOC_KEYWORDS for k in ("inference", "compute", "gpu", "miner"))
