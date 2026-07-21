"""Contract tests: the same suite runs against both backings, shapes diffed.

This file *is* the noesis-kb-v1 stability guarantee — an internal schema
change that alters an answer shape without a contract bump fails here.
"""

import duckdb
import pytest

from src.kb import contract
from src.kb.contract import KBContractError
from src.kb.membership import run_membership_pass
from src.kb.promotion import promote_to_namespace
from src.kb.registry import load_registry
from tests.unit.kb.test_claim_links import BASE_MS, DUP_A

CONFIG = """
version: 1
domains:
  - name: web3
    backing: corpus-view
    embedding_model: fake-embed
    tags: [web3]
    keywords: [defi, staking]
"""

ENVELOPE_KEYS = {"contract", "domain", "as_of_ms", "data"}
DIFF_SECTIONS = {
    "documents", "new_clusters", "gained_corroboration",
    "new_contradictions", "superseded", "entity_surges", "meta",
}
CLUSTER_KEYS = {"cluster_id", "representative", "citations", "corroboration", "size"}


def _seed(conn, config_path):
    from src.ingestion.document_store import DocumentStore
    from src.kb.claim_links import ensure_claim_link_schema

    ensure_claim_link_schema(conn)
    DocumentStore(conn).upsert(
        [
            {
                "document_id": "d1",
                "source_type": "news",
                "language": "en",
                "ingested_at": BASE_MS,
                "source_id": "wire",
                "url": "https://example.com/d1",
                "title": "Defi staking news",
                "content": "defi staking coverage continues.",
                "metadata": {"tags": ["web3"]},
            }
        ]
    )
    run_membership_pass(conn, load_registry(config_path))
    conn.execute(
        "INSERT INTO argument_claims (claim_id, claim_text, document_id,"
        " source_type, confidence) VALUES ('c1', ?, 'd1', 'news', 0.8)",
        [DUP_A],
    )
    conn.execute(
        "INSERT INTO document_actors (document_id, source_type, actor_name,"
        " role, confidence) VALUES ('d1', 'news', 'Federal Reserve', 'subject', 0.9)"
    )


@pytest.fixture(params=["corpus-view", "namespace"])
def surface(request, tmp_path):
    """One (conn, config_path) per backing — the same domain, twice."""
    conn = duckdb.connect()
    config_path = tmp_path / "domains.yml"
    config_path.write_text(CONFIG)
    _seed(conn, config_path)
    if request.param == "namespace":
        promote_to_namespace(conn, "web3", config_path)
    return conn, config_path, request.param


class TestShapesAcrossBackings:
    def test_envelope_everywhere(self, surface):
        conn, config_path, _ = surface
        for call in (
            lambda: contract.kb_documents("web3", conn=conn, config_path=config_path),
            lambda: contract.kb_search("web3", "staking", conn=conn, config_path=config_path),
            lambda: contract.kb_claims("web3", conn=conn, config_path=config_path),
            lambda: contract.kb_coverage("web3", conn=conn, config_path=config_path),
            lambda: contract.kb_diff("web3", "2020-01-01", conn=conn, config_path=config_path),
            lambda: contract.kb_contradictions("web3", conn=conn, config_path=config_path),
        ):
            payload = call()
            assert set(payload) == ENVELOPE_KEYS
            assert payload["contract"] == "noesis-kb-v1"
            assert payload["domain"] == "web3"

    def test_documents_cited_identically(self, surface):
        conn, config_path, _ = surface
        rows = contract.kb_documents("web3", conn=conn, config_path=config_path)["data"]
        assert [row["document_id"] for row in rows] == ["d1"]
        for key in ("document_id", "title", "url", "source_id"):
            assert key in rows[0]

    def test_search_finds_the_same_document(self, surface):
        conn, config_path, _ = surface
        hits = contract.kb_search("web3", "staking", conn=conn, config_path=config_path)["data"]
        assert [hit["document_id"] for hit in hits] == ["d1"]

    def test_claims_are_clusters(self, surface):
        conn, config_path, _ = surface
        clusters = contract.kb_claims("web3", conn=conn, config_path=config_path)["data"]
        assert len(clusters) == 1
        assert CLUSTER_KEYS <= set(clusters[0])
        assert clusters[0]["representative"]["claim_id"] == "c1"
        assert clusters[0]["citations"]

    def test_diff_sections_identical(self, surface):
        conn, config_path, _ = surface
        diff = contract.kb_diff("web3", "2020-01-01", conn=conn, config_path=config_path)["data"]
        assert DIFF_SECTIONS <= set(diff)
        assert {"new", "total", "sources_delivered"} <= set(diff["documents"])
        assert {"as_of_ms", "since_ms", "consolidation"} <= set(diff["meta"])

    def test_coverage_core_keys(self, surface):
        conn, config_path, backing = surface
        coverage = contract.kb_coverage("web3", conn=conn, config_path=config_path)["data"]
        for key in ("domain", "backing", "embedding_model", "ready", "documents"):
            assert key in coverage
        assert coverage["backing"] == backing
        assert coverage["documents"] == 1


class TestCorpusOnlyDepth:
    """Sections with corpus-only depth still answer on both backings —
    these assert the corpus side's extra data, not a shape difference."""

    def test_entities_folded(self, tmp_path):
        conn = duckdb.connect()
        config_path = tmp_path / "domains.yml"
        config_path.write_text(CONFIG)
        _seed(conn, config_path)
        from src.kb.entities import run_entity_canonicalization_pass

        run_entity_canonicalization_pass(conn)
        entities = contract.kb_entities("web3", conn=conn, config_path=config_path)["data"]
        assert entities[0]["name"] == "Federal Reserve"
        assert entities[0]["mentions"] == 1


class TestErrors:
    def test_unknown_domain(self, tmp_path):
        config_path = tmp_path / "domains.yml"
        config_path.write_text(CONFIG)
        with pytest.raises(KBContractError) as excinfo:
            contract.kb_coverage("finance", conn=duckdb.connect(), config_path=config_path)
        assert excinfo.value.code == "unknown_domain"

    def test_empty_query_rejected(self, tmp_path):
        config_path = tmp_path / "domains.yml"
        config_path.write_text(CONFIG)
        with pytest.raises(KBContractError) as excinfo:
            contract.kb_search("web3", "  ", conn=duckdb.connect(), config_path=config_path)
        assert excinfo.value.code == "bad_request"

    def test_bad_since_rejected(self, tmp_path):
        conn = duckdb.connect()
        config_path = tmp_path / "domains.yml"
        config_path.write_text(CONFIG)
        _seed(conn, config_path)
        with pytest.raises(KBContractError) as excinfo:
            contract.kb_diff("web3", "not-a-date", conn=conn, config_path=config_path)
        assert excinfo.value.code == "bad_since"

    def test_domains_listing(self, tmp_path):
        config_path = tmp_path / "domains.yml"
        config_path.write_text(CONFIG)
        listing = contract.kb_domains(config_path=config_path)["data"]
        assert listing == [
            {
                "name": "web3",
                "backing": "corpus-view",
                "description": "",
                "embedding_model": "fake-embed",
            }
        ]


class TestRESTMirror:
    def test_routes_serve_the_contract(self, tmp_path, monkeypatch):
        import importlib.util
        from pathlib import Path

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        conn = duckdb.connect(str(tmp_path / "wh.duckdb"))
        config_path = tmp_path / "domains.yml"
        config_path.write_text(CONFIG)
        _seed(conn, config_path)
        conn.close()

        monkeypatch.setenv("NOESIS_DOMAINS_CONFIG", str(config_path))
        monkeypatch.setenv("NOESIS_DB_PATH", str(tmp_path / "wh.duckdb"))
        # Fresh shared connection for the patched path.
        import src.database.local_analytics_connector as lac

        monkeypatch.setattr(lac, "_CONNECTION", None, raising=False)

        spec = importlib.util.spec_from_file_location(
            "kb_routes_test",
            Path(__file__).resolve().parents[3] / "src/api/routes/kb_routes.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        app = FastAPI()
        app.include_router(module.router)
        client = TestClient(app)

        payload = client.get("/api/v1/kb/web3/documents").json()
        assert payload["contract"] == "noesis-kb-v1"
        assert [row["document_id"] for row in payload["data"]] == ["d1"]

        missing = client.get("/api/v1/kb/finance/coverage")
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "unknown_domain"
