"""Unit tests for the daily brief (thin consumer of the KB contract)."""

import duckdb
import pytest

from src.kb.brief import generate_brief
from src.kb.claim_links import run_claim_linking_pass
from src.kb.clusters import run_clustering_pass
from src.kb.membership import run_membership_pass
from src.kb.registry import load_registry
from tests.unit.kb.test_claim_links import (
    BASE_MS,
    DAY_MS,
    CONTRA,
    DUP_A,
    DUP_B,
    FakeNLI,
    FakeProvider,
    _seed_claims,
)

CONFIG = """
version: 1
domains:
  - name: web3
    backing: corpus-view
    embedding_model: fake-embed
    tags: [web3]
    keywords: [defi, staking]
  - name: papers
    backing: corpus-view
    embedding_model: fake-embed
    tags: [papers, research]
"""

SINCE_DAY2 = "2025-06-16T00:00:00Z"


@pytest.fixture()
def conn():
    return duckdb.connect()


@pytest.fixture()
def config_path(tmp_path):
    path = tmp_path / "domains.yml"
    path.write_text(CONFIG)
    return path


def _seed_world(conn, config_path):
    """Day 1: one web3 story. Day 2: corroboration + contradiction + papers."""
    _seed_claims(
        conn,
        [
            ("c1", DUP_A, "d1", BASE_MS, "web3"),
            ("c2", DUP_B, "d2", BASE_MS + DAY_MS, "web3"),
            ("c3", CONTRA, "d3", BASE_MS + DAY_MS, "web3"),
        ],
    )
    conn.execute("UPDATE documents SET source_id = 'wire-a' WHERE document_id = 'd1'")
    conn.execute("UPDATE documents SET source_id = 'wire-b' WHERE document_id IN ('d2','d3')")
    for index in range(3):
        conn.execute(
            "INSERT INTO documents (document_id, source_type, source_id, url,"
            " title, ingested_at) VALUES (?, 'blog', ?, ?, ?, ?)",
            [
                f"paper-{index}",
                f"arXiv cs.AI",
                f"https://arxiv.org/abs/{index}",
                f"Paper number {index}",
                BASE_MS + DAY_MS,
            ],
        )
        conn.execute(
            "INSERT INTO document_domains VALUES (?, 'papers', 1.0, 'source', 'r0', 0)",
            [f"paper-{index}"],
        )
    # Membership refresh derives source assignments from persisted source tags.
    conn.execute("""UPDATE documents SET metadata=to_json(struct_pack(tags := [dd.domain]))
        FROM document_domains dd WHERE documents.document_id=dd.document_id""")
    run_membership_pass(conn, load_registry(config_path))
    run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
    run_clustering_pass(conn)


class TestBrief:
    def test_research_section_lists_new_publications(self, conn, config_path):
        _seed_world(conn, config_path)
        brief = generate_brief(
            since=SINCE_DAY2, conn=conn, config_path=config_path
        )
        papers = next(s for s in brief["sections"] if s["domain"] == "papers")
        assert papers["research"] is True
        assert len(papers["publications"]) == 3
        assert papers["publications"][0]["source"] == "arXiv cs.AI"
        assert "### New publications (3)" in brief["markdown"]
        assert "Paper number 0" in brief["markdown"]

    def test_items_cited_and_contested_flagged(self, conn, config_path):
        _seed_world(conn, config_path)
        brief = generate_brief(since=SINCE_DAY2, conn=conn, config_path=config_path)
        web3 = next(s for s in brief["sections"] if s["domain"] == "web3")
        assert web3["items"], "expected ranked items for web3"
        for item in web3["items"]:
            assert item["sources"], "every line cited"
        assert "Contested" in brief["markdown"]
        assert "zero-shot:fake-model" in brief["markdown"]

    def test_budget_enforced_with_dropped_count(self, conn, config_path):
        _seed_world(conn, config_path)
        brief = generate_brief(
            since=SINCE_DAY2, budget=1, conn=conn, config_path=config_path
        )
        assert brief["meta"]["kept"] == 1
        assert brief["meta"]["dropped"] >= 1
        assert "below the budget line" in brief["markdown"]

    def test_quiet_day_is_honest_about_why(self, conn, config_path):
        _seed_world(conn, config_path)
        brief = generate_brief(
            since="2030-01-01", conn=conn, config_path=config_path
        )
        assert "nothing new (no arrivals from any feed)" in brief["markdown"]
        assert brief["meta"]["kept"] == 0

    def test_contract_call_wraps_brief_in_envelope(self, conn, config_path):
        from src.kb import contract
        from src.kb.contract import KBContractError

        _seed_world(conn, config_path)
        payload = contract.kb_brief(
            since=SINCE_DAY2, conn=conn, config_path=config_path
        )
        assert payload["contract"] == "noesis-kb-v1"
        assert payload["domain"] is None
        assert "markdown" in payload["data"]
        assert payload["data"]["meta"]["kept"] >= 1

        with pytest.raises(KBContractError) as excinfo:
            contract.kb_brief(
                domains=["finance"], conn=conn, config_path=config_path
            )
        assert excinfo.value.code == "unknown_domain"

    @pytest.mark.asyncio
    async def test_rest_brief_route(self, conn, config_path, tmp_path, monkeypatch):
        import importlib.util
        from pathlib import Path

        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        db_path = tmp_path / "wh.duckdb"
        file_conn = __import__("duckdb").connect(str(db_path))
        _seed_world(file_conn, config_path)
        file_conn.close()

        monkeypatch.setenv("NOESIS_DOMAINS_CONFIG", str(config_path))
        monkeypatch.setenv("NOESIS_DB_PATH", str(db_path))
        import src.database.local_analytics_connector as lac

        monkeypatch.setattr(lac, "_CONNECTION", None, raising=False)

        spec = importlib.util.spec_from_file_location(
            "kb_routes_brief_test",
            Path(__file__).resolve().parents[3] / "src/api/routes/kb_routes.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router)

        # Exercise the ASGI contract directly.  This avoids TestClient's
        # blocking portal and keeps the test valid across Starlette/httpx
        # transport generations.
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/kb/brief",
                params={"domains": "papers", "since": SINCE_DAY2},
            )
        assert response.status_code == 200
        data = response.json()["data"]
        assert [s["domain"] for s in data["sections"]] == ["papers"]
        assert "New publications" in data["markdown"]

    def test_domain_selection(self, conn, config_path):
        _seed_world(conn, config_path)
        brief = generate_brief(
            domains=["papers"], since=SINCE_DAY2, conn=conn, config_path=config_path
        )
        assert [s["domain"] for s in brief["sections"]] == ["papers"]
        assert "## web3" not in brief["markdown"]
