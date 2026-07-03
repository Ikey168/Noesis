"""R12 #619 equivalence: the articles_data data-mode tool returns payloads
equivalent to the /api/v1/news/articles REST route for the articles family.

Loads the pipeline server by path, calls articles_data over an in-memory
FastMCP client on a temp warehouse, and asserts each row carries exactly the
REST route's fields. Skips where fastmcp/duckdb are unavailable.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")
duckdb = pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[3]

# The exact field set the /api/v1/news/articles route selects (src/api/routes/
# news_routes.py get_articles): id, title, url, publish_date, source, category,
# sentiment_score, sentiment_label.
REST_FIELDS = {
    "id", "title", "url", "publish_date", "source", "category",
    "sentiment_score", "sentiment_label",
}


def _load_pipeline():
    path = REPO / "tools/pipeline_mcp/server.py"
    spec = importlib.util.spec_from_file_location("pl_equiv_srv", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    con.executemany(
        "INSERT INTO news_articles (id, title, url, publish_date, source, category, "
        "sentiment_score, sentiment_label) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("a1", "Grid policy shifts", "http://x/1", "2026-06-02", "Alpha Wire", "energy", 0.4, "positive"),
            ("a2", "Market reaction muted", "http://x/2", "2026-06-01", "Beta Journal", "markets", -0.2, "negative"),
        ],
    )
    con.close()
    monkeypatch.setenv("NEURONEWS_DB_PATH", str(db))
    return db


def _call_articles_data(pipeline):
    from fastmcp.client import Client

    async def run():
        async with Client(pipeline.mcp) as c:
            return (await c.call_tool("articles_data", {"limit": 50})).structured_content

    return asyncio.run(run())


def test_articles_data_matches_rest_fields(warehouse):
    pipeline = _load_pipeline()
    out = _call_articles_data(pipeline)
    assert out["count"] == 2
    for article in out["articles"]:
        assert set(article) == REST_FIELDS, f"field drift vs REST route: {set(article)}"
    # Full-payload (not a summary): url and sentiment_score are present, which
    # the compact latest_articles summary omits.
    first = out["articles"][0]
    assert first["url"] and first["sentiment_score"] is not None


def test_articles_data_filters_like_the_route(warehouse):
    pipeline = _load_pipeline()
    from fastmcp.client import Client

    async def run():
        async with Client(pipeline.mcp) as c:
            return (
                await c.call_tool("articles_data", {"source": "Alpha Wire"})
            ).structured_content

    out = asyncio.run(run())
    assert out["count"] == 1 and out["articles"][0]["source"] == "Alpha Wire"
