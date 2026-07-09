"""M3.1: the live pipeline runner. kg_ingest runs bound connectors for real (the
runner harvests documents into the corpus) and then routes them into the KG
namespace, with no simulation branch. The harvester is injected so the real
persist-and-route path is exercised without a network fetch."""

from datetime import datetime, timezone

from src.provisioning import store
from src.provisioning.pipeline_runner import (
    build_pipeline_runner,
    default_harvester,
    persist_records,
)
from src.provisioning.provisioner import Provisioner


def _clock():
    return datetime(2026, 6, 1, tzinfo=timezone.utc)


def _records(n, source="TestFeed"):
    return [
        {
            "id": f"doc-{i}",
            "title": f"Grid resilience report {i}",
            "url": f"http://feed/{i}",
            "content": "content",
            "publish_date": "2026-05-20",
            "source": source,
        }
        for i in range(n)
    ]


def test_ingest_runs_connector_and_routes_into_namespace(conn):
    store.ensure_schema(conn)
    harvested = _records(3)
    runner = build_pipeline_runner(conn, harvester=lambda ctype, cfg: list(harvested))
    prov = Provisioner(conn, clock=_clock, pipeline_runner=runner)

    assert prov.deploy("energy", "Energy grid", approve=True).get("deployed")
    attached = prov.attach_pipeline(
        "energy",
        connector="grid-feed",
        connector_type="rss",
        config={"url": "http://feed", "source": "TestFeed"},
        approve=True,
    )
    assert attached.get("attached"), attached
    prov.attach_sources("energy", sources=["TestFeed"])

    out = prov.ingest("energy")
    assert out.get("ingested") is True, out
    run = out["pipeline_runs"][0]
    assert run["ok"] is True and run["result"]["written"] == 3

    # The connector really persisted into the shared corpus...
    corpus = conn.execute(
        "SELECT COUNT(*) FROM news_articles WHERE source = 'TestFeed'"
    ).fetchone()[0]
    assert corpus == 3
    # ...and ingest routed those documents into the KG namespace.
    assert out["totals"]["documents"] == 3


def test_persist_records_is_idempotent_by_id(conn):
    assert persist_records(conn, "S", _records(2)) == 2
    # Re-persisting the same ids writes nothing new.
    assert persist_records(conn, "S", _records(2)) == 0
    assert conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 2


def test_ingest_without_runner_degrades_to_routing(conn, seed):
    # No pipeline_runner: pre-existing corpus docs are still routed (R8 path).
    seed.articles([("a1", "t", "http://a/1", None, "2026-05-01", "Alpha Wire", "news")])
    prov = Provisioner(conn, clock=_clock)
    prov.deploy("legacy", "legacy corpus", approve=True)
    prov.attach_sources("legacy", sources=["Alpha Wire"])
    out = prov.ingest("legacy")
    assert out["ingested"] is True
    assert out["pipeline_runs"] == []
    assert out["totals"]["documents"] == 1


def test_default_harvester_unknown_connector_is_empty():
    # Best-effort: an unregistered connector type yields nothing, never raises.
    assert default_harvester("not-a-real-connector-type", {"query": None}) == []


# --------------------------------------------------------------------------- #
# Live harvest_run path: documents sink + news_articles bridge (#907)
# --------------------------------------------------------------------------- #


def _fake_connector(docs):
    from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
    from services.ingest.common.document_model import Document

    class _FakeConnector(Connector):
        source_type = "news"

        def discover(self, query=None):
            return [SourceRef("feed", metadata={"source_id": "feed-1"})]

        def fetch(self, ref):
            return RawDocument(ref=ref, content="raw")

        def parse(self, raw):
            return [
                Document(
                    document_id=d["id"], source_type="news", language="en",
                    ingested_at=1_700_000_000_000, title=d.get("title"),
                    content=d.get("content"), url=d.get("url"),
                )
                for d in docs
            ]

    return _FakeConnector()


def test_live_path_persists_to_documents_and_bridges_to_news_articles(conn):
    docs = [
        {"id": "d1", "title": "One", "content": "Body one.", "url": "https://ex.com/1"},
        {"id": "d2", "title": "Two", "content": "Body two.", "url": "https://ex.com/2"},
    ]
    runner = build_pipeline_runner(conn, connector_resolver=lambda ct: _fake_connector(docs))
    res = runner({"connector": "grid", "connector_type": "news", "config": {"source": "Feed"}})

    assert res["documents"] == 2       # unified documents sink
    assert res["written"] == 2         # bridged into news_articles
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM news_articles WHERE source = 'Feed'"
    ).fetchone()[0] == 2


def test_live_path_is_idempotent(conn):
    docs = [{"id": "d1", "title": "One", "content": "Body.", "url": "https://ex.com/1"}]
    runner = build_pipeline_runner(conn, connector_resolver=lambda ct: _fake_connector(docs))
    spec = {"connector": "grid", "connector_type": "news", "config": {"source": "Feed"}}
    runner(spec)
    second = runner(spec)
    assert second["documents"] == 0    # already ingested -> no new documents
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_live_path_unknown_connector_returns_zeros(conn):
    runner = build_pipeline_runner(conn, connector_resolver=lambda ct: None)
    res = runner({"connector": "x", "connector_type": "nope", "config": {}})
    assert res == {
        "connector": "x", "source": "x", "fetched": 0, "written": 0, "documents": 0,
    }
