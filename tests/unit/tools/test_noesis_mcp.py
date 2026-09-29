from __future__ import annotations

import asyncio
from pathlib import Path

from src.argument_mining.models import ClaimPrediction
from src.noesis_cli.config import initialize
from tools.noesis_mcp import server

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "examples/quickstart/moon-mission.md"

EXPECTED_TOOLS = {
    "domains",
    "add",
    "search",
    "ask",
    "brief",
    "documents",
    "claims",
    "inspect_source",
    "coverage",
    "watch",
    "export",
}


def _workspace(tmp_path, monkeypatch):
    config, _changes = initialize(
        config_path=tmp_path / ".noesis/config.json",
    )
    monkeypatch.setenv("NOESIS_CONFIG", str(config.path))
    monkeypatch.setenv("NOESIS_DB_PATH", str(config.warehouse))
    monkeypatch.setenv("NOESIS_DOMAINS_CONFIG", str(config.domains))
    return config


def test_default_gateway_has_only_curated_daily_driver_tools():
    tools = asyncio.run(server.mcp.get_tools())
    assert set(tools) == EXPECTED_TOOLS


def test_gateway_add_ask_inspect_and_export_share_production_state(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)

    class Detector:
        prediction_mode = "test-model"

        def predict(self, document):
            from src.argument_mining.dataset import sentences_from_document

            return [
                ClaimPrediction(sentence, index, True, 0.95)
                for index, sentence in enumerate(sentences_from_document(document))
            ]

    monkeypatch.setattr(
        "src.argument_mining.evidence.get_claim_detector",
        lambda: Detector(),
    )

    added = server.add.fn(str(FIXTURE), domain="local")
    assert added["contract"] == "noesis-gateway-v1"
    assert added["data"]["processing"]["claims_indexed"] == 2
    document_id = added["data"]["documents"][0]

    answer = server.ask.fn("What was the mission result?", domain="local")
    assert answer["data"]["answer_status"] == "answered"
    assert "returned its first rock sample" in answer["data"]["rendered"]

    source = server.inspect_source.fn(document_id, domain="local")
    assert source["data"]["document"]["document_id"] == document_id
    assert source["data"]["integrity"] is not None

    bundle = server.export.fn(
        "answer",
        domain="local",
        question="What was the mission result?",
        include_private=True,
    )
    assert bundle["contract"] == "noesis-evidence-bundle-v1"


def test_gateway_defaults_to_local_domain_and_watch_list(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)

    domains = server.domains.fn()
    assert [item["name"] for item in domains["data"]] == ["local"]

    coverage = server.coverage.fn()
    assert coverage["domain"] == "local"

    watches = server.watch.fn()
    assert watches["domain"] in {None, "local"}
    assert "data" in watches
