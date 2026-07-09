"""M10.2: the analyst agent completes goal -> KG -> OSINT on a sample
goal, over the MCP surface (driven here by the in-process backend, which mirrors
the live MCP tool surface)."""

import pytest

from src.agent.analyst import AnalystAgent, kg_name_for
from src.agent.local_backend import build_local_caller
from src.agent.runtime import AgentRuntime, PLANE_OSINT, PLANE_PROVISIONING

duckdb = pytest.importorskip("duckdb")


def _seed(conn):
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO news_articles (id, title, url, source, publish_date) VALUES (?,?,?,?,?)",
        [
            ("d1", "Delta flooding", "http://a/1", "Alpha Wire", "2026-06-01"),
            ("d2", "Delta support", "http://b/1", "Beta Journal", "2026-06-02"),
            ("d3", "Delta support two", "http://c/1", "Gamma Review", "2026-06-03"),
        ],
    )
    conn.execute(
        "CREATE TABLE argument_claims (claim_id VARCHAR, claim_text VARCHAR, document_id VARCHAR, "
        "source_type VARCHAR, confidence DOUBLE, factcheck_verdict VARCHAR)"
    )
    conn.execute(
        "INSERT INTO argument_claims VALUES ('k1', 'Severe flooding struck the delta.', 'd1', 'news', 0.9, NULL)"
    )
    conn.execute(
        "CREATE TABLE claim_evidence (evidence_id VARCHAR, claim_id VARCHAR, evidence_text VARCHAR, "
        "evidence_document_id VARCHAR, evidence_source_type VARCHAR, relation VARCHAR, "
        "similarity_score DOUBLE, found_at VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO claim_evidence (evidence_id, claim_id, evidence_document_id, "
        "evidence_source_type, relation, similarity_score) VALUES (?,?,?,?,?,?)",
        [
            ("e1", "k1", "d2", "news", "supports", 0.88),
            ("e2", "k1", "d3", "news", "supports", 0.82),
        ],
    )
    conn.execute(
        "CREATE TABLE outlet_scores (source VARCHAR, source_type VARCHAR, score_date VARCHAR, "
        "frame_diversity DOUBLE, attribution_rate DOUBLE, stance_neutrality DOUBLE, "
        "composite_score DOUBLE, doc_count INTEGER, claim_count INTEGER, computed_at VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO outlet_scores (source, source_type, score_date, frame_diversity, "
        "attribution_rate, stance_neutrality, composite_score) VALUES (?,?,?,?,?,?,?)",
        [
            ("Alpha Wire", "outlet", "2026-06-01", 0.6, 0.7, 0.5, 0.62),
            ("Beta Journal", "outlet", "2026-06-01", 0.6, 0.8, 0.6, 0.8),
            ("Gamma Review", "outlet", "2026-06-01", 0.5, 0.6, 0.5, 0.6),
        ],
    )


@pytest.fixture
def runtime(tmp_path):
    conn = duckdb.connect(str(tmp_path / "wh.duckdb"))
    _seed(conn)
    rt = AgentRuntime(build_local_caller(conn))
    yield rt
    conn.close()


GOAL = "flooding in the coastal delta"


def test_analyst_completes_goal_to_kg_to_osint(runtime):
    agent = AnalystAgent(runtime)
    result = agent.run(
        GOAL,
        sources=["Alpha Wire", "Beta Journal", "Gamma Review"],
        claim_id="k1",
        source="Alpha Wire",
    )

    # KG: a namespace was provisioned for the goal and is deployed.
    assert result.kg["provisioned"] is True
    assert result.kg["name"] == kg_name_for(GOAL)
    assert result.kg["status"].get("error") is None

    # OSINT: the sweep returned usable findings, including real corroboration.
    assert result.findings >= 2
    corroboration = next(f for f in result.osint if f["tool"] == "corroborate")
    assert corroboration["result"]["independent_support_count"] == 2

    # The run genuinely crossed both planes.
    planes = {c.plane for c in runtime.transcript()}
    assert planes == {PLANE_PROVISIONING, PLANE_OSINT}


def test_analyst_selects_an_existing_kg_instead_of_reprovisioning(runtime):
    name = kg_name_for(GOAL)
    # Pre-provision the KG the goal would map to.
    runtime.call(PLANE_PROVISIONING, "kg_deploy", {"name": name, "description": "seeded", "approve": True})

    agent = AnalystAgent(runtime)
    result = agent.run(GOAL, claim_id="k1")
    # It reused the existing KG rather than deploying a second time.
    assert result.kg["provisioned"] is False
    assert result.kg["name"] == name


def test_analyst_run_stays_within_budget(runtime):
    from src.agent.runtime import Budget

    # A generous but finite budget; the analyst run must fit inside it.
    runtime._budget = Budget(max_steps=24)
    result = AnalystAgent(runtime).run(GOAL, sources=["Alpha Wire"], claim_id="k1", source="Alpha Wire")
    assert result.steps <= 24
    assert runtime.steps_remaining() >= 0
