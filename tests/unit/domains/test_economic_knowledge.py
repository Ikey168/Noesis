from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from jsonschema import Draft7Validator

from services.ingest.common.series_model import Observation, SeriesRecord
from src.domains import pack_install
from src.domains.economic.model import (
    EconomicModelError,
    assess_comparability,
    load_fixture,
    record_economic_link,
    register_series,
)
from src.domains.economic.queries import EconomicQueryError, economic_research
from src.domains.pack_format import load_manifest, validate_manifest
from src.evidence_bundle import verify_bundle
from src.kb import contract

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/economic/benchmark.json"


class EconomicBacking:
    backing_type = "corpus-view"

    def __init__(self, conn):
        self.conn = conn
        self.definition = SimpleNamespace(name="economics", tags=["economics"])

    def documents(self, limit=50):
        return []

    def claims(self, limit=50):
        return []

    def entities(self):
        return []

    def coverage(self):
        return {
            "domain": "economics",
            "backing": self.backing_type,
            "ready": True,
            "documents": 0,
        }


@pytest.fixture()
def economic_corpus():
    conn = duckdb.connect(":memory:")
    counts = load_fixture(conn, json.loads(FIXTURE.read_text()))
    assert counts == {"series_vintages": 10, "links": 6}
    yield conn, EconomicBacking(conn)
    conn.close()


def test_pack_validates_installs_and_declares_reused_contracts():
    manifest = load_manifest(str(ROOT / "packs/economics"))
    assert validate_manifest(manifest.to_dict()) == []
    assert (
        manifest.ontology_extensions["extends"]
        == "noesis-canonical-entity-relation-ontology"
    )
    assert {
        "indicator",
        "observation",
        "release",
        "vintage",
        "company",
        "policy",
    } <= set(manifest.ontology_extensions["object_types"])
    assert "dataset-series-v1" in manifest.ontology_extensions["reuses"]
    receipt = pack_install.install_manifest(manifest)
    try:
        assert "release-vintage-history" in receipt["capabilities"]
        assert receipt["schema_versions"]["dataset-series"] == "1.0.0"
    finally:
        assert pack_install.uninstall("economics") is True


def test_provider_codes_dimensions_and_shared_dataset_storage(economic_corpus):
    conn, _backing = economic_corpus
    providers = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT provider FROM economic_series_map"
        ).fetchall()
    }
    assert providers == {"fred", "worldbank", "eurostat", "filing", "poll"}
    indicator = conn.execute(
        "SELECT indicator_id, canonical_name, unit, scaling, currency_basis, geography, frequency, price_basis, seasonal_adjustment "
        "FROM economic_indicators WHERE indicator_id='indicator:gdp-real-us-quarterly'"
    ).fetchone()
    assert indicator == (
        "indicator:gdp-real-us-quarterly",
        "US real GDP",
        "usd",
        1000000.0,
        "USD",
        "US",
        "quarterly",
        "chain_linked",
        "adjusted",
    )
    provider_code = conn.execute(
        "SELECT provider_code FROM economic_series_map WHERE series_id='fred:GDPC1:US'"
    ).fetchone()[0]
    assert provider_code == "GDPC1"
    assert conn.execute("SELECT COUNT(*) FROM dataset_observations").fetchone()[0] == 20
    payload = {
        "economic_contract": "noesis-economic-model-v1",
        "indicator_id": indicator[0],
        "canonical_name": indicator[1],
        "concept": "real gross domestic product",
        "definition": "Inflation-adjusted US gross domestic product",
        "unit": indicator[2],
        "scaling": indicator[3],
        "currency_basis": indicator[4],
        "geography": indicator[5],
        "frequency": indicator[6],
        "price_basis": indicator[7],
        "seasonal_adjustment": indicator[8],
        "attributes": {},
    }
    schema = json.loads(
        (
            ROOT / "contracts/schemas/jsonschema/noesis-economic-indicator-v1.json"
        ).read_text()
    )
    assert not list(Draft7Validator(schema).iter_errors(payload))


def test_vintages_replay_initial_and_latest_values_as_observed(economic_corpus):
    _conn, backing = economic_corpus
    early = economic_research(
        backing,
        query_type="trend",
        series_ids=["fred:GDPC1:US"],
        observed_before="2025-08-15",
        period_from="2025-Q2",
        period_to="2025-Q2",
    )
    latest = economic_research(
        backing,
        query_type="trend",
        series_ids=["fred:GDPC1:US"],
        observed_before="2025-10-01",
        period_from="2025-Q2",
        period_to="2025-Q2",
    )
    assert early["results"][0]["observations"][0]["value"] == 23770.1
    assert latest["results"][0]["observations"][0]["value"] == 23800.4
    revision = economic_research(
        backing,
        query_type="vintage_comparison",
        series_ids=["fred:GDPC1:US"],
        observed_before="2025-10-01",
        period_from="2025-Q2",
        include_bundle=True,
    )
    result = revision["results"][0]
    assert result["initial_vintage"]["revision_of"] is None
    assert result["latest_vintage"]["revision_of"] == 1754006400000
    assert result["observations"][0]["revision"] == pytest.approx(30_300_000.0)
    assert revision["temporal"]["matching_observations"] >= 1
    assert verify_bundle(revision["evidence_bundle"]).valid


def test_comparison_rejects_incompatible_dimensions_and_qualifies_scaling(
    economic_corpus,
):
    conn, backing = economic_corpus
    incompatible = economic_research(
        backing,
        query_type="series_comparison",
        series_ids=["fred:GDPC1:US", "worldbank:NY.GDP.MKTP.CD:DE"],
        observed_before="2025-10-01",
    )
    assert incompatible["results"] == []
    assert incompatible["comparison"]["comparable"] is False
    assert {item["dimension"] for item in incompatible["comparison"]["blockers"]} >= {
        "concept",
        "frequency",
        "price_basis",
    }

    peer = SeriesRecord(
        series_id="provider-b:GDPC1:US",
        provider="provider-b",
        title="US real GDP",
        frequency="quarterly",
        unit="usd",
        geography="US",
        as_of=1756684800000,
        source_url="https://example.invalid/gdp",
        observations=[Observation("2025-Q2", 23.8004)],
        metadata={"release_at": "2025-09-01", "retrieved_at": "2025-09-01"},
    )
    register_series(
        conn,
        peer,
        semantics={
            "indicator_id": "indicator:gdp-real-us-quarterly-b",
            "canonical_name": "US real GDP",
            "concept": "real gross domestic product",
            "definition": "Inflation-adjusted US gross domestic product",
            "scaling": 1000000000,
            "currency_basis": "USD",
            "price_basis": "chain_linked",
            "seasonal_adjustment": "adjusted",
        },
    )
    assessment = assess_comparability(conn, "fred:GDPC1:US", "provider-b:GDPC1:US")
    assert assessment["comparable"] is True
    assert assessment["qualifications"][0]["dimension"] == "scaling"
    compared = economic_research(
        backing,
        query_type="series_comparison",
        series_ids=["fred:GDPC1:US", "provider-b:GDPC1:US"],
        observed_before="2025-10-01",
    )
    assert compared["results"][0]["delta"] == pytest.approx(0.0)


def test_claim_links_preserve_ambiguity_and_never_promote_proximity_to_causation(
    economic_corpus,
):
    conn, backing = economic_corpus
    response = economic_research(
        backing, query_type="claim_evidence", claim_id="claim:inflation"
    )
    item = response["results"][0]
    assert item["match_method"] == "temporal_proximity"
    assert item["ambiguity"]["note"] == "index level is not an inflation rate"
    assert item["causal_interpretation_allowed"] is False
    assert response["causal_safety"]["default"] == "association_only"
    assert response["citations"][0]["provider"] == "eurostat"

    with pytest.raises(EconomicModelError, match="confidence"):
        record_economic_link(
            conn,
            claim_id="claim:bad",
            target_kind="policy",
            target_id="policy:x",
            relation="associated_with",
            match_method="correlation",
            confidence=1.1,
        )
    with pytest.raises(EconomicQueryError):
        economic_research(backing, query_type="unknown")


def test_research_response_schema(economic_corpus):
    _conn, backing = economic_corpus
    response = economic_research(
        backing,
        query_type="trend",
        indicator_id="indicator:hicp-de-monthly",
        observed_before="2025-10-01",
    )
    schema = json.loads(
        (
            ROOT / "contracts/schemas/jsonschema/noesis-economic-research-v1.json"
        ).read_text()
    )
    assert not list(Draft7Validator(schema).iter_errors(response))


def test_kb_contract_resolves_the_economic_domain(economic_corpus, tmp_path):
    conn, _backing = economic_corpus
    config_path = tmp_path / "domains.yml"
    config_path.write_text(
        """version: 1
domains:
  - name: economics
    backing: corpus-view
    embedding_model: test
    tags: [economics]
    keywords: [inflation, gdp]
"""
    )
    response = contract.kb_economic(
        "economics",
        "trend",
        ["fred:GDPC1:US"],
        observed_before="2025-10-01",
        conn=conn,
        config_path=config_path,
    )
    assert response["contract"] == "noesis-kb-v1"
    assert response["domain"] == "economics"
    assert response["data"]["results"][0]["series"]["provider_code"] == "GDPC1"


def test_rest_and_mcp_economic_surfaces_share_contract(monkeypatch):
    sentinel = {
        "contract": "noesis-kb-v1",
        "domain": "economics",
        "as_of_ms": 1,
        "data": {},
    }
    calls = []

    def fake_economic(*args):
        calls.append(args)
        return sentinel

    monkeypatch.setattr(contract, "kb_economic", fake_economic)
    from src.api.routes import kb_routes

    request = kb_routes.EconomicQueryRequest(
        domain="economics", query_type="trend", series_ids=["fred:GDPC1:US"]
    )
    rest = kb_routes.economic_query(request)
    spec = importlib.util.spec_from_file_location(
        "economic_kb_mcp", ROOT / "tools/kb_mcp/server.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.get_tools())
    mcp = tools["kb_economic"].fn(
        domain="economics", query_type="trend", series_ids=["fred:GDPC1:US"]
    )
    assert rest == mcp == sentinel
    assert len(calls) == 2
