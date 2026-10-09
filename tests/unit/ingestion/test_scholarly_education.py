"""ERIC and EdArXiv scholarly connectors (offline): parsing, date windows, authors, URLs."""
import json
from datetime import datetime, timezone

from src.ingestion.connectors.registry import get_connector
from src.ingestion.connectors.scholarly.sources import (
    EdarxivConnector,
    EricConnector,
    SCHOLARLY_SOURCES,
)


def _ms(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def _harvest(conn_cls, payload, query):
    captured = {}

    def fake_http_get(url, headers):
        captured["url"] = url
        return json.dumps(payload).encode()

    conn = conn_cls(http_get=fake_http_get, dns_resolver=lambda host: ["93.184.216.34"])
    ref = next(iter(conn.discover(query)))
    return conn.parse(conn.fetch(ref)), captured["url"]


def test_registered_and_advertised():
    assert isinstance(get_connector("eric"), EricConnector)
    assert isinstance(get_connector("edarxiv"), EdarxivConnector)
    assert {"eric", "edarxiv"} <= set(SCHOLARLY_SOURCES)


def test_eric_year_authors_url_and_window():
    payload = {"response": {"numFound": 2, "docs": [
        {"id": "EJ100", "title": "AI &amp; Teachers",
         "author": ["Ada One", "Bo Two"], "description": "An &quot;abstract&quot;.",
         "publicationdateyear": 2024, "source": "J. Ed. Tech",
         "subject": ["Artificial Intelligence"]},
        {"id": "ED200", "title": "Older Work", "author": ["Cy Three"],
         "publicationdateyear": 2000, "source": "Report"},
    ]}}
    docs, url = _harvest(EricConnector, payload,
                         {"topic": "ai education", "since": "2023-01-01",
                          "until": "2026-12-31", "limit": 50})
    assert "api.ies.ed.gov/eric/" in url and "publicationdateyear" in url
    # the year-2000 record falls outside the window and is dropped
    assert [d.metadata["external_id"] for d in docs] == ["EJ100"]
    doc = docs[0]
    assert doc.source_id == "eric"
    assert doc.title == "AI & Teachers"                 # HTML entities unescaped
    assert doc.content == 'An "abstract".'
    assert doc.authors == ["Ada One", "Bo Two"]
    assert doc.url == "https://eric.ed.gov/?id=EJ100"   # synthesized from id
    assert doc.metadata["venue"] == "J. Ed. Tech"
    assert doc.created_at == _ms(2024, 1, 1)            # integer year parsed correctly
    assert doc.metadata["work_identifier"] == "eric:EJ100"


def test_eric_keeps_explicit_url_field():
    payload = {"response": {"docs": [
        {"id": "ED200", "title": "W", "author": ["C"], "publicationdateyear": 2024,
         "url": "https://files.eric.ed.gov/fulltext/ED200.pdf", "source": "R"}]}}
    docs, _ = _harvest(EricConnector, payload,
                       {"topic": "x", "since": "2023-01-01", "until": "2026-12-31"})
    assert docs[0].url == "https://files.eric.ed.gov/fulltext/ED200.pdf"


def test_edarxiv_flattens_contributors_and_derives_doi():
    payload = {"data": [
        {"id": "abc12",
         "attributes": {"title": "Learning at Scale", "description": "Abstract here.",
                        "date_published": "2025-06-15T10:00:00.449559", "doi": None},
         "links": {"html": "https://osf.io/preprints/edarxiv/abc12/",
                   "preprint_doi": "https://doi.org/10.35542/osf.io/abc12"},
         "embeds": {"contributors": {"data": [
             {"embeds": {"users": {"data": {"attributes": {"full_name": "Jane Roe"}}}}},
             {"embeds": {"users": {"data": {"attributes": {"full_name": "John Doe"}}}}},
         ]}}},
    ]}
    docs, url = _harvest(EdarxivConnector, payload,
                         {"topic": "learning", "since": "2023-01-01",
                          "until": "2026-12-31", "limit": 10})
    assert "api.osf.io/v2/preprints" in url and "edarxiv" in url
    assert "filter%5Btitle%5D=learning" in url
    doc = docs[0]
    assert doc.source_id == "edarxiv"
    assert doc.title == "Learning at Scale"
    assert doc.authors == ["Jane Roe", "John Doe"]
    assert doc.content == "Abstract here."
    assert doc.url == "https://osf.io/preprints/edarxiv/abc12/"
    assert doc.metadata["doi"] == "10.35542/osf.io/abc12"   # lifted from preprint_doi
    assert doc.metadata["venue"] == "EdArXiv"
    assert doc.created_at == _ms(2025, 6, 15, 10, 0, 0)


def test_edarxiv_handles_missing_contributors_and_doi():
    payload = {"data": [
        {"id": "zz9", "attributes": {"title": "No Authors", "date_published": "2024-02-02T00:00:00"},
         "links": {}, "embeds": {}},
    ]}
    docs, _ = _harvest(EdarxivConnector, payload,
                       {"topic": "x", "since": "2023-01-01", "until": "2026-12-31"})
    doc = docs[0]
    assert doc.authors == []
    assert doc.url == "https://osf.io/preprints/edarxiv/zz9/"   # synthesized fallback
    assert doc.metadata["work_identifier"] == "edarxiv:zz9"
