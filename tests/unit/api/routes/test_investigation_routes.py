"""Route tests for src/api/routes/investigation_routes.py.

Loads the route module BY PATH (the routes package eagerly imports the heavy
ML stack), points its connection resolver at a seeded in-memory DuckDB, and
drives the case-work endpoints through HTTP.
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
    "investigation_routes_under_test",
    REPO / "src/api/routes/investigation_routes.py",
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


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
        "CREATE TABLE argument_claims (claim_id VARCHAR, claim_text VARCHAR, "
        "document_id VARCHAR, source_type VARCHAR, confidence DOUBLE, "
        "factcheck_verdict VARCHAR)"
    )
    conn.execute(
        "INSERT INTO argument_claims VALUES "
        "('k1', 'Severe flooding struck the delta region.', 'd1', 'news', 0.9, NULL)"
    )
    conn.execute(
        "CREATE TABLE claim_evidence (evidence_id VARCHAR, claim_id VARCHAR, "
        "evidence_text VARCHAR, evidence_document_id VARCHAR, "
        "evidence_source_type VARCHAR, relation VARCHAR, similarity_score DOUBLE, "
        "found_at VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO claim_evidence (evidence_id, claim_id, evidence_document_id, "
        "evidence_source_type, relation, similarity_score) VALUES (?,?,?,?,?,?)",
        [
            ("e1", "k1", "d2", "news", "supports", 0.9),
            ("e2", "k1", "d3", "news", "supports", 0.8),
        ],
    )


@pytest.fixture
def client(monkeypatch):
    conn = duckdb.connect(":memory:")
    _seed(conn)
    monkeypatch.setattr(mod, "_conn", lambda: (conn, threading.Lock()))
    app = FastAPI()
    app.include_router(mod.router)
    yield TestClient(app)
    conn.close()


def test_list_starts_empty(client):
    body = client.get("/api/v1/investigation").json()
    assert body == {"cases": [], "count": 0}


def test_open_then_read_the_case_file(client):
    r = client.post(
        "/api/v1/investigation/open",
        json={"question": "Severe flooding struck the delta region", "topic": "flooding"},
    )
    assert r.status_code == 200
    case_id = r.json()["case"]["case_id"]
    file_ = client.get(f"/api/v1/investigation/{case_id}").json()
    assert {h["kind"] for h in file_["hypotheses"]} == {"affirmative", "null"}
    assert client.get("/api/v1/investigation").json()["count"] == 1


def test_run_advance_conclude_flow(client):
    r = client.post(
        "/api/v1/investigation/run",
        json={"question": "Severe flooding struck the delta region",
              "topic": "flooding", "conclude": False},
    )
    assert r.status_code == 200
    case_id = r.json()["case"]["case_id"]
    adv = client.post(f"/api/v1/investigation/{case_id}/advance")
    assert adv.status_code == 200
    con = client.post(f"/api/v1/investigation/{case_id}/conclude").json()
    assert con["concluded"] is True

    matrix = client.get(f"/api/v1/investigation/{case_id}/matrix").json()
    assert matrix["leader"] == "h1"
    brief = client.get(f"/api/v1/investigation/{case_id}/brief").json()
    assert brief["verdict"]
    md = client.get(
        f"/api/v1/investigation/{case_id}/brief", params={"format": "markdown"}
    ).json()
    assert md["markdown"].startswith("# Case brief:")


def test_unknown_case_is_404(client):
    assert client.get("/api/v1/investigation/case-nope").status_code == 404
    assert client.post("/api/v1/investigation/case-nope/advance").status_code == 404


def test_open_validates_question_length(client):
    r = client.post("/api/v1/investigation/open", json={"question": "x" * 501})
    assert r.status_code == 422
