from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains import pack_install
from src.domains.pack_format import load_manifest, validate_manifest
from src.domains.political.model import (
    PoliticalModelError,
    correct_object,
    load_fixture,
    record_object,
    record_relation,
    resolve_alias,
    reverse_correction,
)
from src.domains.political.queries import PoliticalQueryError, political_research
from src.ingestion.connectors.political_official import PoliticalOfficialConnector
from src.ingestion.document_store import DocumentStore
from src.kb import contract
from src.kb.claim_links import ensure_claim_link_schema
from src.kb.contract import KBContractError
from src.kb.membership import run_membership_pass
from src.kb.registry import load_registry

ROOT = Path(__file__).resolve().parents[3]
BENCHMARK = ROOT / "tests/fixtures/political/benchmark.json"


class EmptyPoliticalBacking:
    backing_type = "corpus-view"

    def __init__(self, conn):
        self.conn = conn
        self.definition = SimpleNamespace(name="political", tags=["political"])

    def documents(self, limit=50):
        return []

    def claims(self, limit=50):
        return []

    def entities(self):
        return []

    def coverage(self):
        return {"domain": "political", "backing": self.backing_type, "ready": True, "documents": 4}


@pytest.fixture()
def political_corpus():
    conn = duckdb.connect(":memory:")
    connector = PoliticalOfficialConnector()
    documents = list(connector.harvest({"offline": True}))
    assert DocumentStore(conn).upsert(documents).inserted == 4
    load_fixture(conn, json.loads(BENCHMARK.read_text()))
    yield conn, EmptyPoliticalBacking(conn)
    conn.close()


def test_distributable_pack_validates_installs_and_round_trips():
    manifest = load_manifest(str(ROOT / "packs/political"))
    assert validate_manifest(manifest.to_dict()) == []
    assert manifest.ontology_extensions["extends"] == "noesis-canonical-entity-relation-ontology"
    assert {"person", "office", "office_term", "proposal", "vote"}.issubset(
        manifest.ontology_extensions["object_types"]
    )
    receipt = pack_install.install_manifest(manifest)
    try:
        assert receipt["schema_versions"] == {"source": "1.0.0", "model": "1.0.0", "research": "1.0.0"}
        assert "political-research-queries" in receipt["capabilities"]
    finally:
        assert pack_install.uninstall("political") is True


def test_scoped_aliases_office_transitions_and_reversible_corrections(political_corpus):
    conn, _backing = political_corpus
    ambiguous = resolve_alias(conn, "Green Party", object_type="party", jurisdiction_id="DE")
    assert ambiguous["status"] == "ambiguous"
    berlin = resolve_alias(
        conn, "Green Party", object_type="party", jurisdiction_id="DE",
        parent_id="jurisdiction:de:berlin",
    )
    assert berlin["object"]["object_id"] == "party:de:green-berlin"

    before = political_research(
        _backing, query_type="officeholder_at_date", jurisdiction="US",
        at="2025-12-31", office_id="office:us:procurement-director",
    )
    boundary = political_research(
        _backing, query_type="officeholder_at_date", jurisdiction="US",
        at="2026-01-01", office_id="office:us:procurement-director",
    )
    assert before["results"][0]["person"]["object_id"] == "person:us:alex-morgan"
    assert boundary["results"][0]["person"]["object_id"] == "person:us:jordan-lee"

    correction = correct_object(
        conn, "person:us:alex-morgan", {"canonical_name": "Alex M. Morgan"},
        source_document_id="political:us-federal-register-executive:EO-TEST-101",
        observed_at_ms=2000,
    )
    assert resolve_alias(conn, "Alex Morgan", object_type="person", jurisdiction_id="US")["object"]["canonical_name"] == "Alex M. Morgan"
    historical = political_research(
        _backing, query_type="officeholder_at_date", jurisdiction="US",
        at="2025-12-31", observed_before=1500,
        office_id="office:us:procurement-director",
    )
    assert historical["results"][0]["person"]["canonical_name"] == "Alex Morgan"
    reverse_correction(conn, correction, reversed_at_ms=3000)
    assert resolve_alias(conn, "Alex Morgan", object_type="person", jurisdiction_id="US")["object"]["canonical_name"] == "Alex Morgan"
    with pytest.raises(PoliticalModelError, match="already reversed"):
        reverse_correction(conn, correction, reversed_at_ms=4000)


@pytest.mark.parametrize(
    "query_type,jurisdiction,selector,expected_key",
    [
        ("proposal_lifecycle", "US", {"proposal_id": "proposal:us:clean-procurement"}, "proposal"),
        ("vote_records", "DE", {"proposal_id": "proposal:de:bt-test-42"}, "vote"),
        ("institutional_positions", "EU", {"institution_id": "institution:eu:commission"}, "relation_id"),
        ("policy_changes", "EU", {}, "relation_id"),
    ],
)
def test_benchmark_queries_are_cited_and_compose_core_contracts(
    political_corpus, query_type, jurisdiction, selector, expected_key
):
    _conn, backing = political_corpus
    result = political_research(
        backing, query_type=query_type, jurisdiction=jurisdiction,
        at="2026-04-12", **selector,
    )
    assert result["results"] and expected_key in result["results"][0]
    assert result["results"][0]["evidence"]["locator_available"] is True
    assert result["as_of"]["valid_at_ms"]
    assert result["coverage"]["official_sources"]["jurisdiction"] == jurisdiction
    assert result["uncertainty"]["status"] in {"supported", "partial"}
    assert "noesis-temporal-v1" in result["composed_contracts"]
    assert result["evidence_independence"]["publication_count"] >= 1


def test_unsupported_jurisdiction_is_honest_and_response_validates(political_corpus):
    _conn, backing = political_corpus
    result = political_research(
        backing, query_type="policy_changes", jurisdiction="ZZ", at="2026-01-01"
    )
    assert result["results"] == []
    assert result["uncertainty"]["status"] == "unsupported"
    assert len(result["uncertainty"]["reasons"]) == 2
    schema = json.loads(
        (ROOT / "contracts/schemas/jsonschema/noesis-political-research-v1.json").read_text()
    )
    assert not list(Draft7Validator(schema).iter_errors(result))
    with pytest.raises(PoliticalQueryError):
        political_research(backing, query_type="unknown", jurisdiction="US")


def test_object_schema_and_type_identity_are_enforced():
    conn = duckdb.connect(":memory:")
    obj = record_object(
        conn, object_id="person:test", object_type="person", canonical_name="Test Person",
        jurisdiction_id="TEST", observed_at_ms=1,
    )
    schema = json.loads(
        (ROOT / "contracts/schemas/jsonschema/noesis-political-object-v1.json").read_text()
    )
    assert not list(Draft7Validator(schema).iter_errors(obj))
    with pytest.raises(PoliticalModelError, match="cannot change"):
        record_object(
            conn, object_id="person:test", object_type="office", canonical_name="Wrong",
            jurisdiction_id="TEST", observed_at_ms=2,
        )


def test_contract_resolves_domain_scope_and_does_not_mix_private_rows(
    political_corpus, tmp_path
):
    conn, _backing = political_corpus
    record_object(
        conn, object_id="proposal:private:one", object_type="proposal",
        canonical_name="Private proposal", jurisdiction_id="US", observed_at_ms=1,
        domain="local", visibility="private",
    )
    record_object(
        conn, object_id="instrument:private:one", object_type="instrument",
        canonical_name="Private instrument", jurisdiction_id="US", observed_at_ms=1,
        domain="local", visibility="private",
    )
    record_relation(
        conn, relation_type="adopted_as", subject_id="proposal:private:one",
        object_id="instrument:private:one", observed_at_ms=1, domain="local",
        visibility="private",
    )
    config_path = tmp_path / "domains.yml"
    config_path.write_text(
        """version: 1
domains:
  - name: political
    backing: corpus-view
    embedding_model: test
    tags: [political]
    keywords: [proposal]
  - name: local
    backing: corpus-view
    embedding_model: test
    tags: [local, private]
    keywords: [private]
"""
    )
    ensure_claim_link_schema(conn)
    run_membership_pass(conn, load_registry(config_path))
    response = contract.kb_political(
        "political", "policy_changes", "US", conn=conn, config_path=config_path
    )
    serialized = json.dumps(response)
    assert response["data"]["n"] == 1
    assert "proposal:private:one" not in serialized
    with pytest.raises(KBContractError) as excinfo:
        contract.kb_political(
            "local", "policy_changes", "US", conn=conn, config_path=config_path
        )
    assert excinfo.value.code == "unauthorized"


def test_rest_and_mcp_political_surfaces_share_contract(monkeypatch):
    sentinel = {"contract": "noesis-kb-v1", "domain": "political", "as_of_ms": 1, "data": {}}
    calls = []

    def fake_political(*args):
        calls.append(args)
        return sentinel

    monkeypatch.setattr(contract, "kb_political", fake_political)
    from src.api.routes import kb_routes

    request = kb_routes.PoliticalQueryRequest(
        domain="political", query_type="policy_changes", jurisdiction="EU"
    )
    rest = kb_routes.political_query(request)
    spec = importlib.util.spec_from_file_location(
        "political_kb_mcp", ROOT / "tools/kb_mcp/server.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.get_tools())
    mcp = tools["kb_political"].fn(
        domain="political", query_type="policy_changes", jurisdiction="EU"
    )
    assert rest == mcp == sentinel
    assert len(calls) == 2
