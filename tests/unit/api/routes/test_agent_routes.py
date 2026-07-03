"""Route tests for src/api/routes/agent_routes.py (M10 agents, live entry point).

Loads the route module BY PATH, points the warehouse at a seeded in-memory
DuckDB, and drives the analyst / investigator / replay endpoints through HTTP.
"""

import importlib.util
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

pytest.importorskip("fastapi")
duckdb = pytest.importorskip("duckdb")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "agent_routes_under_test", REPO / "src/api/routes/agent_routes.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _seed(conn):
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, content VARCHAR, "
        "publish_date TIMESTAMP, source VARCHAR, category VARCHAR)"
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
        [("e1", "k1", "d2", "news", "supports", 0.88), ("e2", "k1", "d3", "news", "supports", 0.82)],
    )
    conn.execute(
        "CREATE TABLE document_actors (document_id VARCHAR, source_type VARCHAR, actor_name VARCHAR, "
        "entity_id VARCHAR, role VARCHAR, confidence DOUBLE, extracted_at VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO document_actors (document_id, actor_name, entity_id, role) VALUES (?,?,?,?)",
        [("d1", "Jordan Rivera", "person:jr", "speaker"), ("d1", "Casey Morgan", "person:cm", "subject")],
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("NOESIS_AGENT_API", "on")
    monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", "off")
    conn = duckdb.connect(":memory:")
    _seed(conn)
    monkeypatch.setattr(mod, "_conn", lambda: (conn, threading.Lock()))
    app = FastAPI()
    app.include_router(mod.router)
    yield TestClient(app)
    conn.close()


def test_analyst_run_then_replay(client):
    resp = client.post(
        "/api/v1/agent/analyst",
        json={"goal": "flooding in the delta", "sources": ["Alpha Wire"],
              "claim_id": "k1", "source": "Alpha Wire"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"].startswith("analyst-")
    assert body["kg"]["provisioned"] is True
    assert body["findings"] >= 2
    assert body["canvas"]["spec_version"] == "ui-spec-v1"

    # The run is replayable from its audit trail.
    replay = client.get(f"/api/v1/agent/runs/{body['run_id']}")
    assert replay.status_code == 200
    assert replay.json()["count"] == body["steps"]


def test_investigator_run_respects_the_gate(client):
    resp = client.post(
        "/api/v1/agent/investigator",
        json={"title": "delta response", "entities": ["Jordan Rivera"],
              "related_pair": ["Jordan Rivera", "Casey Morgan"], "topic": "delta", "claim_id": "k1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["gated_calls"] == 0  # never touches a gated tool while the gate is off
    assert body["findings"] >= 2
    assert body["canvas"]["spec_version"] == "ui-spec-v1"


def test_status_and_gating(monkeypatch):
    monkeypatch.setenv("NOESIS_AGENT_API", "off")
    app = FastAPI()
    app.include_router(mod.router)
    c = TestClient(app)
    assert c.get("/api/v1/agent").json()["enabled"] is False
    assert c.post("/api/v1/agent/analyst", json={"goal": "x"}).status_code == 404


def test_replay_unknown_run_is_404(client):
    assert client.get("/api/v1/agent/runs/does-not-exist").status_code == 404
