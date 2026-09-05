import json
from pathlib import Path

import pytest

from src.ingestion.scholarly_api import parameters, records
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.source_packs import validate_source_pack


def test_native_datacite_page_in_runtime():
    payload = json.loads(
        Path("tests/fixtures/integrations/datacite-native.json").read_text()
    )
    pack = validate_source_pack(
        json.loads(Path("config/source_packs/research.json").read_text())
    )
    source = next(s for s in pack["sources"] if s["source_id"] == "datacite-dois")
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return {"content": json.dumps(payload)}

    page = HTTPSPageAdapter(source, transport=transport).fetch_page(
        {"operation": "search", "parameters": {"query": "Berlin"}, "limit": 2},
        cursor=None,
    )
    assert len(page.records) == 2
    assert page.records[0]["doi"] == payload["data"][0]["attributes"]["doi"].lower()
    assert "native_record" in page.records[0]
    assert calls[0]["params"]["page[size]"] == 2 and "limit" not in calls[0]["params"]
    assert page.next_cursor


def test_datacite_rejects_foreign_cursor_and_bad_filters():
    with pytest.raises(ValueError):
        records(
            "datacite",
            {"data": [], "links": {"next": "https://evil.example/dois?page[cursor]=x"}},
            cursor=None,
            limit=2,
        )
    with pytest.raises(ValueError):
        parameters("datacite", {"parameters": {"api_key": "bad"}}, cursor=None, limit=2)


def test_sdmx_native_ecb_retains_dimensions_and_observations():
    pytest.importorskip("sdmx")
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    content = Path("tests/fixtures/integrations/ecb-native.xml").read_bytes()
    records = SDMXConnector().parse(
        RawSeries(SeriesRef("EXR/D.USD.EUR.SP00.A"), content, fetched_at=1000)
    )
    assert len(records) == 1 and len(records[0].observations) == 2
    assert records[0].metadata["dimensions"]["CURRENCY"] == "USD"
    assert records[0].as_of == 1000
    assert records[0].metadata["vintage_semantics"].startswith("retrieval time")


def test_warc_roundtrip_and_expansion_bound(tmp_path):
    pytest.importorskip("warcio")
    from src.integrations.warc import read_warc, write_warc

    path = tmp_path / "capture.warc"
    captures = [
        {
            "url": "https://example.org/berlin",
            "captured_at": "2025-01-01T00:00:00Z",
            "payload": "Über Berlin".encode(),
            "content_type": "text/plain",
            "http_status": "404 Not Found",
            "http_headers": [("Content-Type", "text/plain"), ("ETag", "original")],
        }
    ]
    write_warc(captures, path)
    records = read_warc(path)
    assert records[0]["payload"] == captures[0]["payload"]
    assert records[0]["captured_at"] == captures[0]["captured_at"]
    assert records[0]["http_status"] == "404 Not Found"
    assert ("ETag", "original") in records[0]["http_headers"]
    with pytest.raises(ValueError):
        read_warc(path, max_bytes=2)


def test_mcp_presets_are_opt_in_and_allowlist_only():
    from src.integrations.mcp import federation_adapter

    with pytest.raises(ValueError):
        federation_adapter("github", secret_resolver=lambda _: None)
    with pytest.raises(ValueError):
        federation_adapter("playwright", endpoint="https://example.org/mcp")
    adapter = federation_adapter("context7", secret_resolver=lambda _: None)
    assert adapter.allowed_tools == {"resolve-library-id", "query-docs"}
    assert not adapter.allowed_resources


def test_warc_document_store_retains_capture_and_replays(tmp_path):
    pytest.importorskip("warcio")
    import duckdb

    from src.ingestion.document_store import DocumentStore
    from src.integrations.warc import ingest_warc, write_warc

    path = tmp_path / "evidence.warc"
    write_warc(
        [
            {
                "url": "https://example.org/berlin",
                "captured_at": "2025-01-01T00:00:00Z",
                "payload": b"Berliner Forschung",
                "content_type": "text/plain",
            }
        ],
        path,
    )
    conn = duckdb.connect()
    store = DocumentStore(conn)
    assert ingest_warc(path, store, language="de").invalid == 0
    assert ingest_warc(path, store, language="de").invalid == 0
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
