import json
from pathlib import Path

import duckdb

from src.ingestion.document_store import DocumentStore
from src.ingestion.guardian_api import parameters, records
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.source_packs import validate_source_pack


def test_native_page_dates_identity_and_partial_coverage():
    params = parameters(
        {"query": "science", "section": "science"},
        cursor="2",
        limit=3,
        secret="private",
        from_ms=0,
        to_ms=86400000,
    )
    assert (
        params["page"] == 2
        and params["from-date"] == "1970-01-01"
        and params["to-date"] == "1970-01-02"
    )
    raw = {
        "response": {
            "currentPage": 1,
            "pages": 2,
            "results": [
                {
                    "id": "world/2026/sep/01/a",
                    "webUrl": "https://www.theguardian.com/world/2026/sep/01/a",
                    "webTitle": "A",
                    "fields": {"body": "<p>Body.</p>"},
                }
            ],
        }
    }
    mapped, cursor = records(raw, limit=1)
    assert (
        cursor == "2"
        and mapped[0]["content"] == "Body."
        and mapped[0]["content_representation"] == "full-text-html"
    )
    source = validate_source_pack(
        json.loads(Path("config/source_packs/guardian.json").read_text())
    )["sources"][0]
    page = HTTPSPageAdapter(
        source, secret="private", transport=lambda **kw: {"content": json.dumps(raw)}
    ).fetch_page(
        {"operation": "search", "parameters": {"query": "science"}, "limit": 1},
        cursor=None,
    )
    assert "private" not in json.dumps(page.receipt)


def test_three_acquisitions_keep_revisions_and_one_origin():
    store = DocumentStore(duckdb.connect())
    url = "https://www.theguardian.com/world/2026/sep/01/a"
    docs = [
        {
            "document_id": kind,
            "source_type": "web",
            "language": "en",
            "ingested_at": 1,
            "url": url + suffix,
            "title": "Title",
            "content": kind,
            "metadata": {},
        }
        for kind, suffix in [("api", ""), ("rss", "?CMP=feed"), ("html", "#top")]
    ]
    assert store.upsert(docs).inserted == 3
    origins = {
        store.get(d["document_id"])["metadata"]["reporting_origin"] for d in docs
    }
    assert origins == {"guardian:world/2026/sep/01/a"}
    from src.osint.independence import (
        origin_summary,
        record_document_signals,
        run_origin_inference,
    )

    for doc in docs:
        record_document_signals(store.conn, doc["document_id"])
    run_origin_inference(store.conn)
    result = origin_summary(store.conn, [doc["document_id"] for doc in docs])
    assert result["publication_count"] == 3
    assert result["probable_origin_count"] == 1


def test_explicit_preference_retains_partial_receipts_and_rejects_wrong_article():
    from src.ingestion.guardian_api import collect_with_preference

    conn = duckdb.connect()
    store = DocumentStore(conn)
    url = "https://www.theguardian.com/world/2026/sep/01/a"

    def doc(route, coverage):
        return {
            "document_id": route,
            "source_type": "web",
            "language": "en",
            "ingested_at": 1,
            "url": url,
            "title": "Article",
            "content": route,
            "metadata": {"content_coverage": coverage},
        }

    try:
        result = collect_with_preference(
            url,
            {
                "api": lambda _: {**doc("wrong", "full-text"), "url": url + "-other"},
                "rss": lambda _: doc("rss", "summary"),
                "html": lambda _: doc("html", "full-text"),
            },
            store=store,
        )
        assert result["selected"]["document_id"] == "html"
        assert [attempt["outcome"] for attempt in result["attempts"]] == [
            "failed",
            "partial",
            "full-text",
        ]
        assert store.get("wrong") is None
        assert store.get("rss")["metadata"]["content_coverage"] == "partial"
        assert conn.execute("SELECT count(*) FROM documents").fetchone() == (2,)
    finally:
        conn.close()


def test_recorded_native_fixture_maps_contributors_dates_and_cursor():
    fixture = json.loads(Path("tests/fixtures/source_packs/guardian.json").read_text())
    mapped, cursor = records(fixture["native_response"], limit=1)
    assert cursor == "2"
    assert mapped[0]["id"] == "world/2026/sep/01/example"
    assert mapped[0]["authors"] == ["Fixture Author"]
    assert mapped[0]["published_at"] == "2026-09-01T08:00:00Z"
    assert mapped[0]["updated_at"] == "2026-09-01T09:00:00Z"
    assert mapped[0]["body_html"] == "<p>Fixture content only.</p>"


def test_guardian_source_pack_fixture_conformance_and_live_disabled():
    from src.ingestion.source_packs import SourcePackConformance

    manifest = json.loads(Path("config/source_packs/guardian.json").read_text())
    conformance = SourcePackConformance(Path("."))
    offline = conformance.offline(manifest)
    assert offline["valid"] and offline["coverage"] == {"configured": 1, "verified": 1}
    live = conformance.live(manifest, lambda source: {}, enabled=False)
    assert live["status"] == "disabled" and live["requests"] == 0
