"""
Comprehensive line-coverage tests for src/api/routes/argument_routes.py.

The router imports ``get_shared_connection`` (and every argument-mining helper)
*lazily inside each endpoint* from ``src.database.local_analytics_connector`` and
``src.argument_mining.*``.  We therefore patch those symbols at the module where
they are defined (that is where the lazy ``from ... import ...`` looks them up)
and drive the endpoints with a scriptable fake DuckDB connection.

No live DB / network required.  All assertions are real assertions on status
codes and response bodies.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Make `import src...` and bare `import ...` both resolve.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
SRC = os.path.join(ROOT, "src")
for p in (ROOT, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.api.routes.argument_routes as mod  # noqa: E402


# ---------------------------------------------------------------------------
# Fake DuckDB connection
# ---------------------------------------------------------------------------

class _FakeCursor:
    """Result object returned by FakeConn.execute — supports fetchall/fetchone."""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """
    A scriptable DuckDB-like connection.

    ``router`` is a list of ``(predicate, rows)`` pairs.  On each ``execute`` we
    return the rows of the first predicate that matches the (lower-cased) SQL.
    A predicate is either a substring or a callable ``sql -> bool``.  When a
    matched entry's rows is an ``Exception`` (class or instance) we raise it,
    which exercises the endpoints' ``try/except`` fallbacks.
    """

    def __init__(self, routes=None, default=None):
        self.routes = list(routes or [])
        self.default = default if default is not None else []
        self.calls = []
        # Some endpoints read `conn._lock`; leaving it absent forces the
        # `threading.Lock()` fallback branch, so we deliberately omit it here
        # and add it only in tests that need to cover the present-lock branch.

    def add(self, predicate, rows):
        self.routes.append((predicate, rows))
        return self

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))
        low = " ".join(sql.lower().split())
        for predicate, rows in self.routes:
            if callable(predicate):
                matched = predicate(low)
            else:
                matched = predicate.lower() in low
            if matched:
                if isinstance(rows, type) and issubclass(rows, BaseException):
                    raise rows("boom")
                if isinstance(rows, BaseException):
                    raise rows
                return _FakeCursor(rows)
        return _FakeCursor(self.default)


def make_client(conn):
    """Fresh FastAPI app with only the argument router mounted."""
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app, raise_server_exceptions=False)


def patch_conn(conn):
    """Patch get_shared_connection where the endpoints look it up."""
    return patch(
        "src.database.local_analytics_connector.get_shared_connection",
        return_value=conn,
    )


# ===========================================================================
# Module-level helpers
# ===========================================================================

def test_parse_date_cutoff_none_and_empty():
    assert mod._parse_date_cutoff(None) is None
    assert mod._parse_date_cutoff("") is None


def test_parse_date_cutoff_valid_days():
    out = mod._parse_date_cutoff("7d")
    assert isinstance(out, str)
    # ISO date string YYYY-MM-DD
    assert len(out) == 10 and out[4] == "-" and out[7] == "-"
    assert mod._parse_date_cutoff("  30D  ") is not None


def test_parse_date_cutoff_bad_number_and_no_suffix():
    # "xd" -> int("x") raises ValueError -> falls through to None
    assert mod._parse_date_cutoff("xd") is None
    # no trailing 'd' -> None
    assert mod._parse_date_cutoff("7") is None
    assert mod._parse_date_cutoff("week") is None


def test_classify_stance_all_branches():
    assert mod._classify_stance(5, 0, 0.1) == "ambiguous"      # low confidence
    assert mod._classify_stance(1, 3, 0.9) == "critical"       # contradicts wins
    assert mod._classify_stance(4, 0, 0.9) == "supportive"     # supports present
    assert mod._classify_stance(0, 0, 0.9) == "neutral"        # nothing


# ===========================================================================
# GET /frames
# ===========================================================================

def test_frames_aggregate_precomputed():
    conn = FakeConn(routes=[
        ("group by frame", [("economic", 0.5123, 3), ("security", 0.20, 3)]),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/frames")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "precomputed"
    assert body["dominant"] == "economic"
    assert body["distribution"]["economic"] == 0.5123
    assert body["total_documents"] == 3


def test_frames_aggregate_with_filters():
    conn = FakeConn(routes=[
        ("group by frame", [("legal", 0.9, 2)]),
    ])
    with patch_conn(conn):
        r = make_client(conn).get(
            "/api/v1/arguments/frames",
            params={"source_type": "news", "date_range": "30d", "limit": 5},
        )
    assert r.status_code == 200
    assert r.json()["source_type_filter"] == "news"


def test_frames_aggregate_live_empty_no_articles():
    # No precomputed rows AND no news_articles -> FRAME_LABELS zeros branch
    conn = FakeConn(default=[])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/frames")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "live"
    assert body["total_documents"] == 0
    assert body["dominant"] == "other"
    assert set(body["distribution"]) >= {"economic", "security", "other"}


def test_frames_aggregate_live_with_classifier():
    # No precomputed rows, but news_articles rows exist -> classify via fake fc
    conn = FakeConn(routes=[
        ("group by frame", []),
        ("from news_articles limit 20", [("d1", "T", "content"), ("d2", "T2", "c2")]),
    ])
    fc = MagicMock()
    from src.argument_mining.dataset import FRAME_LABELS
    pred = MagicMock()
    pred.frames = {f: 0.3 for f in FRAME_LABELS}
    pred.frames["economic"] = 0.9
    fc.predict.return_value = pred
    with patch_conn(conn), \
         patch("src.argument_mining.frames.get_frame_classifier", return_value=fc):
        r = make_client(conn).get("/api/v1/arguments/frames")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "live"
    assert body["total_documents"] == 2
    assert body["dominant"] == "economic"


def test_frames_aggregate_live_classifier_error():
    conn = FakeConn(routes=[
        ("group by frame", []),
        ("from news_articles limit 20", [("d1", "T", "content")]),
    ])
    with patch_conn(conn), \
         patch("src.argument_mining.frames.get_frame_classifier",
               side_effect=RuntimeError("no model")):
        r = make_client(conn).get("/api/v1/arguments/frames")
    assert r.status_code == 500
    assert "Live frame classification failed" in r.json()["detail"]


def test_frames_for_document_precomputed():
    conn = FakeConn(routes=[
        ("from document_frames where document_id",
         [("economic", 0.7, "news"), ("legal", 0.2, "news")]),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/frames", params={"document_id": "doc1"})
    assert r.status_code == 200
    body = r.json()
    assert body["document_id"] == "doc1"
    assert body["source"] == "precomputed"
    assert body["dominant"] == "economic"
    assert body["frames"]["economic"] == 0.7


def test_frames_for_document_not_found():
    conn = FakeConn(routes=[
        ("from document_frames where document_id", []),
        ("from news_articles where id", []),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/frames", params={"document_id": "nope"})
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_frames_for_document_live_classify():
    conn = FakeConn(routes=[
        ("from document_frames where document_id", []),
        ("from news_articles where id", [("doc9", "Title", "Body text", "src")]),
    ])
    fc = MagicMock()
    pred = MagicMock()
    pred.source_type = "news"
    pred.frames = {"economic": 0.88, "legal": 0.1}
    pred.dominant = "economic"
    fc.predict.return_value = pred
    with patch_conn(conn), \
         patch("src.argument_mining.frames.get_frame_classifier", return_value=fc):
        r = make_client(conn).get("/api/v1/arguments/frames", params={"document_id": "doc9"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "live"
    assert body["dominant"] == "economic"
    assert body["frames"]["economic"] == 0.88


def test_frames_for_document_live_error():
    conn = FakeConn(routes=[
        ("from document_frames where document_id", []),
        ("from news_articles where id", [("doc9", "Title", "Body", "src")]),
    ])
    with patch_conn(conn), \
         patch("src.argument_mining.frames.get_frame_classifier",
               side_effect=RuntimeError("bad")):
        r = make_client(conn).get("/api/v1/arguments/frames", params={"document_id": "doc9"})
    assert r.status_code == 500
    assert "Frame classification failed" in r.json()["detail"]


def test_frames_validation_limit_too_big():
    conn = FakeConn()
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/frames", params={"limit": 9999})
    assert r.status_code == 422


# ===========================================================================
# GET /claims
# ===========================================================================

_CLAIM_ROW = (
    "c1", "The sky is blue", "d1", "news", 0.912345,
    "2026-01-01T00:00:00Z", "verified", "http://x", "PolitiFact", True, "per NASA",
)


def test_get_claims_basic():
    conn = FakeConn(routes=[("from argument_claims", [_CLAIM_ROW])])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    c = body["claims"][0]
    assert c["claim_id"] == "c1"
    assert c["confidence"] == 0.9123
    assert c["factcheck_verdict"] == "verified"
    assert c["attributed"] is True


def test_get_claims_all_filters():
    conn = FakeConn(routes=[("from argument_claims", [])])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims", params={
            "document_id": "d1", "source_type": "news",
            "topic": "climate", "unsourced_only": "true", "limit": 10,
        })
    assert r.status_code == 200
    assert r.json() == {"claims": [], "count": 0}
    # verify all filter clauses got compiled into the SQL
    sql = conn.calls[-1][0].lower()
    assert "document_id = ?" in sql
    assert "claim_text ilike ?" in sql
    assert "attributed is null" in sql


def test_get_claims_null_confidence():
    row = list(_CLAIM_ROW)
    row[4] = None
    conn = FakeConn(routes=[("from argument_claims", [tuple(row)])])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims")
    assert r.status_code == 200
    assert r.json()["claims"][0]["confidence"] is None


def test_get_claims_validation():
    conn = FakeConn()
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims", params={"limit": 0})
    assert r.status_code == 422


# ===========================================================================
# POST /claims/classify-attribution
# ===========================================================================

def test_classify_attribution_batch():
    conn = FakeConn()
    with patch_conn(conn), \
         patch("src.argument_mining.attribution.run_attribution_batch",
               return_value={"classified": 3, "sourced": 1}) as rb:
        r = make_client(conn).post("/api/v1/arguments/claims/classify-attribution",
                                   params={"limit": 42})
    assert r.status_code == 200
    assert r.json() == {"classified": 3, "sourced": 1}
    assert rb.call_args.kwargs["limit"] == 42


def test_classify_attribution_validation():
    conn = FakeConn()
    with patch_conn(conn):
        r = make_client(conn).post("/api/v1/arguments/claims/classify-attribution",
                                   params={"limit": 99999})
    assert r.status_code == 422


# ===========================================================================
# POST /claims/extract
# ===========================================================================

def test_extract_claims_success():
    conn = FakeConn(routes=[("from news_articles where id", [("d1", "Title", "Body content")])])
    claim = MagicMock()
    claim.claim_id, claim.claim_text, claim.confidence = "c1", "a claim", 0.777777
    with patch_conn(conn), \
         patch("src.argument_mining.evidence.run_pipeline",
               return_value=([claim], ["ev1", "ev2"])):
        r = make_client(conn).post("/api/v1/arguments/claims/extract",
                                   params={"document_id": "d1"})
    assert r.status_code == 200
    body = r.json()
    assert body["claims_extracted"] == 1
    assert body["evidence_found"] == 2
    assert body["claims"][0]["confidence"] == 0.7778


def test_extract_claims_from_paper_document_uses_stored_source_type():
    conn = FakeConn(routes=[
        ("from documents where document_id", [("paper:1", "Setun", "A ternary computer was developed.", "paper")]),
    ])
    with patch_conn(conn), \
         patch("src.argument_mining.evidence.run_pipeline", return_value=([], [])) as pipeline:
        r = make_client(conn).post(
            "/api/v1/arguments/claims/extract",
            params={"document_id": "paper:1"},
        )
    assert r.status_code == 200
    assert r.json()["source_type"] == "paper"
    assert pipeline.call_args.args[0].source_type == "paper"
    assert not any("from news_articles" in sql.lower() for sql, _ in conn.calls)


def test_extract_claims_not_found():
    conn = FakeConn(routes=[("from news_articles where id", [])])
    with patch_conn(conn):
        r = make_client(conn).post("/api/v1/arguments/claims/extract",
                                   params={"document_id": "missing"})
    assert r.status_code == 404


def test_extract_claims_no_content():
    conn = FakeConn(routes=[("from news_articles where id", [("d1", "Title", "")])])
    with patch_conn(conn):
        r = make_client(conn).post("/api/v1/arguments/claims/extract",
                                   params={"document_id": "d1"})
    assert r.status_code == 422
    assert "no content" in r.json()["detail"]


def test_extract_claims_pipeline_error():
    conn = FakeConn(routes=[("from news_articles where id", [("d1", "T", "Body")])])
    with patch_conn(conn), \
         patch("src.argument_mining.evidence.run_pipeline",
               side_effect=RuntimeError("pipeline died")):
        r = make_client(conn).post("/api/v1/arguments/claims/extract",
                                   params={"document_id": "d1"})
    assert r.status_code == 500
    assert "Pipeline failed" in r.json()["detail"]


def test_extract_claims_missing_required_param():
    conn = FakeConn()
    with patch_conn(conn):
        r = make_client(conn).post("/api/v1/arguments/claims/extract")
    assert r.status_code == 422


# ===========================================================================
# GET /claims/{id}/evidence
# ===========================================================================

_EV_ROW = ("e1", "supporting text", "d2", "news", "supports", 0.87654321, "2026-01-01")


def test_get_evidence_basic():
    conn = FakeConn(routes=[("from claim_evidence", [_EV_ROW])])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims/c1/evidence")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["evidence"][0]["similarity_score"] == 0.876543
    assert body["evidence"][0]["relation"] == "supports"


def test_get_evidence_relation_filter():
    conn = FakeConn(routes=[("from claim_evidence", [_EV_ROW])])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims/c1/evidence",
                                  params={"relation": "contradicts"})
    assert r.status_code == 200
    assert "relation = ?" in conn.calls[-1][0].lower()


def test_get_evidence_bad_relation():
    conn = FakeConn()
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims/c1/evidence",
                                  params={"relation": "invalid"})
    assert r.status_code == 422
    assert "supports" in r.json()["detail"]


def test_get_evidence_claim_not_found():
    # No evidence rows AND claim does not exist -> 404
    conn = FakeConn(routes=[
        ("from claim_evidence", []),
        ("from argument_claims where claim_id", []),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims/ghost/evidence")
    assert r.status_code == 404


def test_get_evidence_claim_exists_but_no_evidence():
    conn = FakeConn(routes=[
        ("from claim_evidence", []),
        ("from argument_claims where claim_id", [(1,)]),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims/c1/evidence")
    assert r.status_code == 200
    assert r.json() == {"claim_id": "c1", "evidence": [], "count": 0}


# ===========================================================================
# GET /claims/{id}
# ===========================================================================

def test_get_claim_success():
    claim_row = ("c1", "text", "d1", "news", 0.5, "2026", "verified", "u", "pub")
    conn = FakeConn(routes=[
        ("from argument_claims where claim_id", [claim_row]),
        ("from claim_evidence where claim_id", [_EV_ROW]),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims/c1")
    assert r.status_code == 200
    body = r.json()
    assert body["claim_id"] == "c1"
    assert body["evidence_count"] == 1
    assert body["evidence"][0]["evidence_id"] == "e1"


def test_get_claim_not_found():
    conn = FakeConn(routes=[("from argument_claims where claim_id", [])])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims/none")
    assert r.status_code == 404


# ===========================================================================
# POST /claims/{id}/factcheck
# ===========================================================================

def test_factcheck_no_api_key():
    conn = FakeConn()
    with patch_conn(conn), patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GOOGLE_FACTCHECK_API_KEY", None)
        r = make_client(conn).post("/api/v1/arguments/claims/c1/factcheck")
    assert r.status_code == 503
    assert "GOOGLE_FACTCHECK_API_KEY" in r.json()["detail"]


def test_factcheck_claim_not_found():
    conn = FakeConn(routes=[("from argument_claims where claim_id", [])])
    with patch_conn(conn), \
         patch.dict(os.environ, {"GOOGLE_FACTCHECK_API_KEY": "k"}):
        r = make_client(conn).post("/api/v1/arguments/claims/x/factcheck")
    assert r.status_code == 404


def test_factcheck_no_result():
    conn = FakeConn(routes=[("from argument_claims where claim_id", [("some claim text",)])])
    with patch_conn(conn), \
         patch.dict(os.environ, {"GOOGLE_FACTCHECK_API_KEY": "k"}), \
         patch("src.argument_mining.factcheck.lookup_claim", return_value=None):
        r = make_client(conn).post("/api/v1/arguments/claims/c1/factcheck")
    assert r.status_code == 502


def test_factcheck_success():
    conn = FakeConn(routes=[("from argument_claims where claim_id", [("some claim text",)])])
    result = MagicMock()
    result.verdict, result.url, result.publisher = "verified", "http://u", "Snopes"
    result.textual_rating, result.checked_at = "True", "2026-01-01T00:00:00Z"
    with patch_conn(conn), \
         patch.dict(os.environ, {"GOOGLE_FACTCHECK_API_KEY": "k"}), \
         patch("src.argument_mining.factcheck.lookup_claim", return_value=result), \
         patch("src.argument_mining.factcheck.store_result") as store:
        r = make_client(conn).post("/api/v1/arguments/claims/c1/factcheck")
    assert r.status_code == 200
    body = r.json()
    assert body["factcheck_verdict"] == "verified"
    assert body["factcheck_publisher"] == "Snopes"
    assert store.called


# ===========================================================================
# GET /stance/sources
# ===========================================================================

def test_stance_sources_from_table():
    row = ("BBC", "news", "climate", 5, 2, 3, 1, 11, 0.812345,
           "2026-01-01", "2026-01-08")
    conn = FakeConn(routes=[("from source_stances", [row])])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/stance/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    s = body["sources"][0]
    assert s["source"] == "BBC"
    assert s["supportive"] == 5
    assert s["total"] == 11
    assert s["confidence"] == 0.8123


def test_stance_sources_filters_and_null_confidence():
    row = ("X", "blog", "econ", 1, 0, 0, 0, 1, None, None, None)
    conn = FakeConn(routes=[("from source_stances", [row])])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/stance/sources", params={
            "topic": "econ", "source": "X", "source_type": "blog",
            "date_range": "7d", "limit": 5,
        })
    assert r.status_code == 200
    assert r.json()["sources"][0]["confidence"] is None
    sql = conn.calls[0][0].lower()
    assert "topic ilike ?" in sql and "source ilike ?" in sql and "window_start >= ?" in sql


def test_stance_sources_fallback_from_claims():
    # source_stances empty -> fallback query on argument_claims
    fb_rows = [
        ("BBC", "news", "climate", 0.9, 3, 0),   # supportive
        ("CNN", "news", "climate", 0.2, 0, 0),   # ambiguous (low conf)
        ("BBC", "news", "climate", 0.9, 0, 5),   # critical
    ]
    conn = FakeConn(routes=[
        ("from source_stances", []),
        ("from argument_claims c", fb_rows),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/stance/sources",
                                  params={"topic": "climate", "source_type": "news"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    bbc = [s for s in body["sources"] if s["source"] == "BBC"][0]
    assert bbc["supportive"] == 1 and bbc["critical"] == 1


# ===========================================================================
# GET /stance/drift
# ===========================================================================

def test_stance_drift_with_events_and_series():
    drift_events = [
        ("BBC", "news", "climate", "neutral", "supportive", 0.3, "2026-01-05", "w1-w2"),
    ]
    # claim stance rows: (topic, source_type, confidence, extracted_at, sup, con)
    claim_rows = [
        ("climate", "news", 0.9, "2026-01-01T00:00:00Z", 3, 0),
        ("climate", "news", 0.9, "2026-01-10T00:00:00Z", 0, 2),
        ("climate", "news", 0.2, "2026-01-15T00:00:00Z", 0, 0),
    ]
    conn = FakeConn(routes=[
        ("from stance_drift_events", drift_events),
        ("from argument_claims c", claim_rows),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/stance/drift",
                                  params={"source": "BBC", "topic": "climate",
                                          "source_type": "news"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["events"][0]["from_stance"] == "neutral"
    assert len(body["drift"]) == 7
    assert len(body["periods"]) == 7


def test_stance_drift_events_table_missing():
    # stance_drift_events raises -> _load_drift_events returns []
    claim_rows = []
    conn = FakeConn(routes=[
        ("from stance_drift_events", RuntimeError),
        ("from argument_claims c", claim_rows),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/stance/drift")
    assert r.status_code == 200
    body = r.json()
    assert body["events"] == []
    assert body["drift"] == []
    assert body["periods"] == []


def test_stance_drift_unparseable_timestamps():
    # rows with timestamps that fail iso parsing -> valid list empty, drift stays []
    claim_rows = [("climate", "news", 0.9, "not-a-date", 3, 0)]
    conn = FakeConn(routes=[
        ("from stance_drift_events", []),
        ("from argument_claims c", claim_rows),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/stance/drift")
    assert r.status_code == 200
    assert r.json()["drift"] == []


# ===========================================================================
# GET /stance
# ===========================================================================

def test_stance_distribution():
    claim_rows = [
        ("climate", "news", 0.9, "2026-01-01T00:00:00Z", 3, 0),  # supportive
        ("climate", "blog", 0.9, "2026-01-02T00:00:00Z", 0, 4),  # critical
        ("econ", "news", 0.2, "2026-01-03T00:00:00Z", 0, 0),     # ambiguous
    ]
    conn = FakeConn(routes=[("from argument_claims c", claim_rows)])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/stance",
                                  params={"date_range": "90d", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    climate = [s for s in body["stances"] if s["topic"] == "climate"][0]
    assert climate["supportive"] == 1 and climate["critical"] == 1
    assert climate["total"] == 2
    assert len(climate["drift"]) == 7
    assert "news" in climate["by_source"]


def test_stance_empty():
    conn = FakeConn(routes=[("from argument_claims c", [])])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/stance")
    assert r.status_code == 200
    assert r.json() == {"stances": [], "count": 0}


# ===========================================================================
# GET /frames/source
# ===========================================================================

def test_frames_source_concentrated_flag():
    news_rows = [
        ("BBC", "news", "economic", 0.75, 10),   # concentrated (> 0.60)
        ("BBC", "news", "legal", 0.10, 10),
    ]
    other_rows = [
        ("blog", "blog", "security", 0.30, 4),
        ("blog", "blog", "legal", 0.20, 4),
    ]
    conn = FakeConn(routes=[
        ("join news_articles n on df.document_id = n.id", news_rows),
        ("from document_frames df where", other_rows),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/frames/source")
    assert r.status_code == 200
    body = r.json()
    bbc = [s for s in body["sources"] if s["source"] == "BBC"][0]
    assert bbc["concentrated"] is True
    assert bbc["concentrated_frame"] == "economic"
    assert bbc["dominant"] == "economic"
    blog = [s for s in body["sources"] if s["source"] == "blog"][0]
    assert blog["concentrated"] is False
    assert blog["concentrated_frame"] is None


def test_frames_source_filter_news_only():
    news_rows = [("BBC", "news", "economic", 0.5, 3)]
    conn = FakeConn(routes=[
        ("join news_articles n on df.document_id = n.id", news_rows),
        ("from document_frames df where", []),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/frames/source", params={
            "source_type": "news", "source": "BBC", "topic": "x",
            "date_range": "30d",
        })
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_frames_source_filter_non_news():
    other_rows = [("blog", "blog", "security", 0.5, 3)]
    conn = FakeConn(routes=[
        ("join news_articles n on df.document_id = n.id", []),
        ("from document_frames df where", other_rows),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/frames/source",
                                  params={"source_type": "blog"})
    assert r.status_code == 200
    assert r.json()["sources"][0]["source_type"] == "blog"


def test_frames_source_name_filter_post_agg():
    news_rows = [("BBC", "news", "economic", 0.5, 3)]
    conn = FakeConn(routes=[
        ("join news_articles n on df.document_id = n.id", news_rows),
        ("from document_frames df where", []),
    ])
    with patch_conn(conn):
        # 'zzz' won't match 'BBC' after aggregation
        r = make_client(conn).get("/api/v1/arguments/frames/source", params={"source": "zzz"})
    assert r.status_code == 200
    assert r.json()["count"] == 0


# ===========================================================================
# GET /positions
# ===========================================================================

def test_positions_from_table_with_updates():
    pos_rows = [
        ("p1", "PM", "climate", "will cut emissions", "news", "d1", "2026-01-01", 0.9),
    ]
    update_rows = [
        ("u1", "p1", "a1", "reaffirmed", "said again", 0.8, "2026-02-01"),
    ]
    conn = FakeConn(routes=[
        ("from policy_positions", pos_rows),
        ("from position_updates", update_rows),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/positions", params={
            "actor": "PM", "topic": "climate", "source_type": "news",
            "date_range": "90d",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    p = body["positions"][0]
    assert p["actor"] == "PM"
    assert p["updates"][0]["update_type"] == "reaffirmed"


def test_positions_table_query_error_then_fallback():
    # policy_positions raises -> rows=[] -> claims fallback
    fb_rows = [
        ("c1", "BBC", "climate", "claim text", "news", "d1", "2026-01-01", 0.7),
    ]
    conn = FakeConn(routes=[
        ("from policy_positions", RuntimeError),
        ("from argument_claims c", fb_rows),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/positions")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["positions"][0]["_source"] == "claims_fallback"
    assert "_note" in body


def test_positions_fallback_with_all_filters():
    # policy_positions empty + all filters set -> exercises fallback WHERE branches
    fb_rows = [
        ("c1", "BBC", "climate", "claim text", "news", "d1", "2026-01-01", 0.7),
    ]
    conn = FakeConn(routes=[
        ("from policy_positions", []),
        ("from argument_claims c", fb_rows),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/positions", params={
            "actor": "BBC", "topic": "climate", "source_type": "news",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["positions"][0]["_source"] == "claims_fallback"
    # confirm all three fallback filter clauses were compiled in
    fb_sql = conn.calls[-1][0].lower()
    assert "c.source_type = ?" in fb_sql
    assert "n.source ilike ?" in fb_sql
    assert "n.category ilike ?" in fb_sql


def test_positions_fallback_query_error():
    # both policy_positions and fallback raise -> empty
    conn = FakeConn(routes=[
        ("from policy_positions", RuntimeError),
        ("from argument_claims c", RuntimeError),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/positions")
    assert r.status_code == 200
    assert r.json() == {"positions": [], "count": 0}


def test_positions_updates_query_error():
    pos_rows = [("p1", "PM", "climate", "text", "news", "d1", "2026-01-01", 0.9)]
    conn = FakeConn(routes=[
        ("from policy_positions", pos_rows),
        ("from position_updates", RuntimeError),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/positions")
    assert r.status_code == 200
    assert r.json()["positions"][0]["updates"] == []


# ===========================================================================
# POST /positions/extract
# ===========================================================================

def test_positions_extract_success():
    import datetime as _dt
    pub = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    conn = FakeConn(routes=[
        ("from news_articles where id",
         [("d1", "Title", "Body content", pub, "BBC", "climate")]),
    ])
    rec = MagicMock()
    rec.position_id, rec.actor, rec.topic = "p1", "PM", "climate"
    rec.position_text, rec.position_date, rec.confidence = "will act", "2026-01-01", 0.9
    with patch_conn(conn), \
         patch("src.argument_mining.positions.run_position_pipeline", return_value=[rec]):
        r = make_client(conn).post("/api/v1/arguments/positions/extract",
                                   params={"document_id": "d1"})
    assert r.status_code == 200
    body = r.json()
    assert body["positions_extracted"] == 1
    assert body["positions"][0]["actor"] == "PM"


def test_positions_extract_bad_publish_date():
    # publish_date present but .timestamp() raises -> created_at_ms stays None
    class BadDate:
        def timestamp(self):
            raise ValueError("bad")
    conn = FakeConn(routes=[
        ("from news_articles where id",
         [("d1", "T", "Body", BadDate(), "BBC", None)]),
    ])
    with patch_conn(conn), \
         patch("src.argument_mining.positions.run_position_pipeline", return_value=[]):
        r = make_client(conn).post("/api/v1/arguments/positions/extract",
                                   params={"document_id": "d1"})
    assert r.status_code == 200
    assert r.json()["positions_extracted"] == 0


def test_positions_extract_not_found():
    conn = FakeConn(routes=[("from news_articles where id", [])])
    with patch_conn(conn):
        r = make_client(conn).post("/api/v1/arguments/positions/extract",
                                   params={"document_id": "x"})
    assert r.status_code == 404


def test_positions_extract_no_content():
    conn = FakeConn(routes=[
        ("from news_articles where id", [("d1", "T", None, None, "BBC", None)]),
    ])
    with patch_conn(conn):
        r = make_client(conn).post("/api/v1/arguments/positions/extract",
                                   params={"document_id": "d1"})
    assert r.status_code == 422


def test_positions_extract_pipeline_error():
    conn = FakeConn(routes=[
        ("from news_articles where id", [("d1", "T", "Body", None, "BBC", None)]),
    ])
    with patch_conn(conn), \
         patch("src.argument_mining.positions.run_position_pipeline",
               side_effect=RuntimeError("nope")):
        r = make_client(conn).post("/api/v1/arguments/positions/extract",
                                   params={"document_id": "d1"})
    assert r.status_code == 500
    assert "Extraction failed" in r.json()["detail"]


# ===========================================================================
# POST /positions/{id}/check
# ===========================================================================

def test_position_check_success():
    conn = FakeConn(routes=[
        ("from policy_positions", [("PM", "climate", "2026-01-01T00:00:00Z")]),
        ("from news_articles where cast", [("a1", "content", "2026-02-01")]),
    ])
    rec = MagicMock()
    rec.update_id, rec.article_id, rec.update_type = "u1", "a1", "reaffirmed"
    rec.evidence_text, rec.confidence, rec.detected_at = "text", 0.9, "2026-02-01"
    with patch_conn(conn), \
         patch("src.argument_mining.position_tracker.check_position_followthrough",
               return_value=[rec]), \
         patch("src.argument_mining.position_tracker.store_followthrough") as store:
        r = make_client(conn).post("/api/v1/arguments/positions/p1/check")
    assert r.status_code == 200
    body = r.json()
    assert body["updates_found"] == 1
    assert body["summary"]["reaffirmed"] == 1
    assert store.called


def test_position_check_not_found():
    conn = FakeConn(routes=[("from policy_positions", [])])
    with patch_conn(conn):
        r = make_client(conn).post("/api/v1/arguments/positions/nope/check")
    assert r.status_code == 404


def test_position_check_null_extracted_at_and_no_records():
    conn = FakeConn(routes=[
        ("from policy_positions", [("PM", "climate", None)]),
        ("from news_articles where cast", []),
    ])
    with patch_conn(conn), \
         patch("src.argument_mining.position_tracker.check_position_followthrough",
               return_value=[]):
        r = make_client(conn).post("/api/v1/arguments/positions/p1/check")
    assert r.status_code == 200
    assert r.json()["updates_found"] == 0


def test_position_check_error():
    conn = FakeConn(routes=[
        ("from policy_positions", [("PM", "climate", "2026-01-01T00:00:00Z")]),
        ("from news_articles where cast", [("a1", "c", "2026-02-01")]),
    ])
    with patch_conn(conn), \
         patch("src.argument_mining.position_tracker.check_position_followthrough",
               side_effect=RuntimeError("boom")):
        r = make_client(conn).post("/api/v1/arguments/positions/p1/check")
    assert r.status_code == 500
    assert "Follow-through check failed" in r.json()["detail"]


# ===========================================================================
# GET /controversy
# ===========================================================================

def test_controversy_from_conflicts_table():
    cf_rows = [
        ("BBC", "CNN", "climate", 0.9, "direct", "news", "news"),
        ("BBC", "Fox", "climate", 0.45, "implied", "news", "news"),
    ]
    conn = FakeConn(routes=[("from claim_conflicts cf", cf_rows)])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/controversy", params={
            "topic": "climate", "source_type": "news", "date_range": "30d",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["_source"] == "claim_conflicts"
    assert body["conflicts"][0]["intensity"] == 1.0   # normalised to max
    assert body["conflicts"][1]["intensity"] == 0.5


def test_controversy_fallback_from_evidence():
    conn = FakeConn(routes=[
        ("from claim_conflicts cf", RuntimeError),   # table query fails
        ("from claim_evidence e", [
            ("BBC", "CNN", "climate", 8, 2),
            ("BBC", "Fox", "climate", 4, 1),
        ]),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/controversy")
    assert r.status_code == 200
    body = r.json()
    assert body["_source"] == "claim_evidence_fallback"
    assert body["conflicts"][0]["intensity"] == 1.0
    assert body["conflicts"][0]["source_count"] == 2


def test_controversy_fallback_with_filters():
    # claim_conflicts empty + filters set -> exercises fallback WHERE branches
    conn = FakeConn(routes=[
        ("from claim_conflicts cf", []),
        ("from claim_evidence e", [("BBC", "CNN", "climate", 6, 2)]),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/controversy", params={
            "topic": "climate", "source_type": "news", "date_range": "30d",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["_source"] == "claim_evidence_fallback"
    fb_sql = conn.calls[-1][0].lower()
    assert "c.source_type = ?" in fb_sql
    assert "n1.category ilike ?" in fb_sql
    assert "n1.publish_date >= ?" in fb_sql


def test_controversy_empty():
    conn = FakeConn(routes=[
        ("from claim_conflicts cf", []),
        ("from claim_evidence e", []),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/controversy")
    assert r.status_code == 200
    assert r.json() == {"conflicts": [], "count": 0}


# ===========================================================================
# GET /controversy/graph
# ===========================================================================

def test_controversy_graph_from_builder():
    graph = {"nodes": [{"id": "c1"}], "edges": [], "node_count": 1, "edge_count": 0}
    conn = FakeConn()
    with patch_conn(conn), \
         patch("src.argument_mining.conflict_graph.build_conflict_graph", return_value=graph):
        r = make_client(conn).get("/api/v1/arguments/controversy/graph")
    assert r.status_code == 200
    assert r.json()["node_count"] == 1


def test_controversy_graph_fallback():
    graph = {"nodes": [], "edges": [], "node_count": 0, "edge_count": 0}
    fb_row = (
        "c1", "claim a", "news", 0.9, "2026-01-01", "BBC", "climate", "d1",
        "c2", "claim b", "news", 0.8, "2026-01-02", "CNN", "climate", "d2",
        0.77,
    )
    conn = FakeConn(routes=[("from claim_evidence e", [fb_row])])
    with patch_conn(conn), \
         patch("src.argument_mining.conflict_graph.build_conflict_graph", return_value=graph):
        r = make_client(conn).get("/api/v1/arguments/controversy/graph", params={
            "topic": "climate", "source_type": "news", "date_range": "7d",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["_source"] == "claim_evidence_fallback"
    assert body["node_count"] == 2
    assert body["edge_count"] == 1
    assert body["edges"][0]["severity"] == 0.77


# ===========================================================================
# POST /controversy/compute
# ===========================================================================

def test_controversy_compute_success():
    conn = FakeConn()
    with patch_conn(conn), \
         patch("src.argument_mining.conflict_graph.compute_claim_conflicts",
               return_value={"pairs_scanned": 10, "conflicts_found": 3}):
        r = make_client(conn).post("/api/v1/arguments/controversy/compute",
                                   params={"limit": 100, "date_range": "30d"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["conflicts_found"] == 3


def test_controversy_compute_error():
    conn = FakeConn()
    with patch_conn(conn), \
         patch("src.argument_mining.conflict_graph.compute_claim_conflicts",
               side_effect=RuntimeError("crunch")):
        r = make_client(conn).post("/api/v1/arguments/controversy/compute")
    assert r.status_code == 500
    assert "Conflict computation failed" in r.json()["detail"]


# ===========================================================================
# GET /sources/ranking
# ===========================================================================

def test_sources_ranking():
    rows = [
        ("BBC", "news", 42, 0.85, 100),
        ("blog", "blog", 5, None, 0),
    ]
    conn = FakeConn(routes=[("from argument_claims c", rows)])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/sources/ranking",
                                  params={"source_type": "news"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["sources"][0]["claim_count"] == 42
    assert body["sources"][0]["avg_confidence"] == 0.85
    assert body["sources"][1]["avg_confidence"] == 0.0


# ===========================================================================
# GET /actors
# ===========================================================================

def test_actors_list():
    rows = [("d1", "news", "PM", "eid1", "speaker", 0.912345, "2026-01-01")]
    conn = FakeConn(routes=[("from document_actors", rows)])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/actors", params={
            "document_id": "d1", "source_type": "news",
            "role": "speaker", "actor_name": "PM",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["actors"][0]["actor_name"] == "PM"
    assert body["actors"][0]["confidence"] == 0.9123
    sql = conn.calls[-1][0].lower()
    assert "role = ?" in sql and "actor_name ilike ?" in sql


def test_actors_null_confidence():
    rows = [("d1", "news", "PM", "eid1", "speaker", None, "2026-01-01")]
    conn = FakeConn(routes=[("from document_actors", rows)])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/actors")
    assert r.status_code == 200
    assert r.json()["actors"][0]["confidence"] is None


# ===========================================================================
# POST /actors/extract
# ===========================================================================

def test_actors_extract_success():
    conn = FakeConn(routes=[("from news_articles where id", [("d1", "T", "Body", "BBC")])])
    rec = MagicMock()
    rec.actor_name, rec.role, rec.entity_id, rec.confidence = "PM", "speaker", "eid", 0.912345
    with patch_conn(conn), \
         patch("src.argument_mining.metadata.extract_actors", return_value=[rec]), \
         patch("src.argument_mining.metadata.store_actors") as store:
        r = make_client(conn).post("/api/v1/arguments/actors/extract",
                                   params={"document_id": "d1"})
    assert r.status_code == 200
    body = r.json()
    assert body["actors_extracted"] == 1
    assert body["actors"][0]["confidence"] == 0.9123
    assert store.called


def test_actors_extract_no_records():
    conn = FakeConn(routes=[("from news_articles where id", [("d1", "T", None, "BBC")])])
    with patch_conn(conn), \
         patch("src.argument_mining.metadata.extract_actors", return_value=[]), \
         patch("src.argument_mining.metadata.store_actors") as store:
        r = make_client(conn).post("/api/v1/arguments/actors/extract",
                                   params={"document_id": "d1"})
    assert r.status_code == 200
    assert r.json()["actors_extracted"] == 0
    assert not store.called


def test_actors_extract_not_found():
    conn = FakeConn(routes=[("from news_articles where id", [])])
    with patch_conn(conn):
        r = make_client(conn).post("/api/v1/arguments/actors/extract",
                                   params={"document_id": "x"})
    assert r.status_code == 404


# ===========================================================================
# POST /actors/batch
# ===========================================================================

def test_actors_batch():
    conn = FakeConn()
    with patch_conn(conn), \
         patch("src.argument_mining.metadata.run_actor_batch",
               return_value={"processed": 5, "actors": 12}) as rb:
        r = make_client(conn).post("/api/v1/arguments/actors/batch", params={"limit": 50})
    assert r.status_code == 200
    assert r.json() == {"processed": 5, "actors": 12}
    assert rb.call_args.kwargs["limit"] == 50


# ===========================================================================
# GET /actors/summary
# ===========================================================================

def test_actors_summary():
    rows = [
        ("PM", "eid1", "speaker", 20, 0.9),
        ("Sen", "eid2", "subject", 5, None),
    ]
    conn = FakeConn(routes=[("from document_actors", rows)])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/actors/summary",
                                  params={"source_type": "news", "role": "speaker"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["actors"][0]["doc_count"] == 20
    assert body["actors"][1]["avg_confidence"] == 0.0


# ===========================================================================
# GET /outlets/clusters
# ===========================================================================

def test_outlet_clusters():
    rows = [
        ("BBC", "news", 1, "Economic-leaning", 0.512345, -0.212345, "economic", 30, "2026-01-01"),
        ("blog", "blog", 2, "Mixed", None, None, "other", 5, "2026-01-01"),
    ]
    conn = FakeConn(routes=[("from outlet_clusters", rows)])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/outlets/clusters",
                                  params={"source_type": "news", "cluster_id": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["outlets"][0]["pca_x"] == 0.5123
    assert body["outlets"][1]["pca_x"] == 0.0
    sql = conn.calls[-1][0].lower()
    assert "cluster_id = ?" in sql


# ===========================================================================
# POST /outlets/cluster
# ===========================================================================

def test_outlet_cluster_trigger():
    conn = FakeConn()
    with patch_conn(conn), \
         patch("src.argument_mining.outlet_clustering.run_cluster_pipeline",
               return_value={"n_outlets": 12, "k": 3, "method": "kmeans"}) as rc:
        r = make_client(conn).post("/api/v1/arguments/outlets/cluster",
                                   params={"date_range": "30d", "k_min": 2, "k_max": 6})
    assert r.status_code == 200
    assert r.json()["k"] == 3
    assert rc.call_args.kwargs["date_range"] == "30d"


def test_outlet_cluster_validation():
    conn = FakeConn()
    with patch_conn(conn):
        r = make_client(conn).post("/api/v1/arguments/outlets/cluster", params={"k_min": 99})
    assert r.status_code == 422


# ===========================================================================
# GET /outlets/ranking
# ===========================================================================

def test_outlets_ranking_with_trend():
    latest = [
        ("BBC", "news", "2026-01-08", 0.7, 0.8, 0.6, 0.712345, 30, 42, "2026-01-08"),
    ]
    history = [
        ("BBC", "news", "2026-01-01", 0.65),
        ("BBC", "news", "2026-01-08", 0.71),
    ]
    conn = FakeConn(routes=[
        (lambda s: "from outlet_scores os" in s, latest),
        (lambda s: "select source, source_type, score_date, composite_score" in s, history),
    ])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/outlets/ranking",
                                  params={"source_type": "news", "sort_by": "frame_diversity"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    o = body["outlets"][0]
    assert o["rank"] == 1
    assert o["composite_score"] == 0.7123
    assert o["trend"] == [0.65, 0.71]


def test_outlets_ranking_bad_sort_defaults():
    latest = [("BBC", "news", "2026-01-08", None, None, None, None, 30, 42, "2026-01-08")]
    conn = FakeConn(routes=[
        (lambda s: "from outlet_scores os" in s, latest),
        (lambda s: "select source, source_type, score_date, composite_score" in s, []),
    ])
    with patch_conn(conn):
        # invalid sort_by falls back to composite_score
        r = make_client(conn).get("/api/v1/arguments/outlets/ranking",
                                  params={"sort_by": "nonsense"})
    assert r.status_code == 200
    o = r.json()["outlets"][0]
    assert o["frame_diversity"] is None
    # trend falls back to the (0.0) default since history empty and composite None
    assert o["trend"] == [0.0]


def test_outlets_ranking_empty():
    conn = FakeConn(routes=[(lambda s: "from outlet_scores os" in s, [])])
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/outlets/ranking")
    assert r.status_code == 200
    assert r.json() == {"outlets": [], "count": 0}


# ===========================================================================
# POST /outlets/score
# ===========================================================================

def test_outlets_score_trigger():
    conn = FakeConn()
    with patch_conn(conn), \
         patch("src.argument_mining.outlet_scorer.run_scorer_batch",
               return_value={"outlets_scored": 8, "top_outlet": "BBC"}) as rs:
        r = make_client(conn).post("/api/v1/arguments/outlets/score",
                                   params={"date_range": "180d"})
    assert r.status_code == 200
    assert r.json()["outlets_scored"] == 8
    assert rs.call_args.kwargs["date_range"] == "180d"


# ===========================================================================
# Lock-present branch (covers getattr(conn, "_lock", ...) truthy path)
# ===========================================================================

def test_endpoint_uses_conn_lock_when_present():
    import threading as _t
    conn = FakeConn(routes=[("from argument_claims", [_CLAIM_ROW])])
    conn._lock = _t.Lock()  # exercise the `getattr(conn, "_lock", None)` truthy branch
    with patch_conn(conn):
        r = make_client(conn).get("/api/v1/arguments/claims")
    assert r.status_code == 200
    assert r.json()["count"] == 1
