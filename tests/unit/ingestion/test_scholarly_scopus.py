"""Scopus scholarly connector: paging, query shaping, date handling, key gate (offline)."""
import json
import urllib.parse

import pytest

from src.ingestion.connectors.base import PermanentFetchError
from src.ingestion.connectors.registry import get_connector
from src.ingestion.connectors.scholarly.sources import ScopusConnector


def _entry(i, cover_date, doi=True):
    rec = {
        "eid": f"2-s2.0-{i:011d}",
        "dc:title": f"Paper {i}",
        "dc:creator": f"Author {i}",
        "prism:coverDate": cover_date,
        "prism:publicationName": "Computers & Education",
        "subtypeDescription": "Article",
        "link": [{"@ref": "scopus", "@href": f"https://www.scopus.com/record/{i}"}],
    }
    if doi:
        rec["prism:doi"] = f"10.1000/X{i}"
    return rec


class FakeScopus:
    """Serves ``entries`` 25 at a time like the STANDARD view, recording requests."""

    def __init__(self, entries):
        self.entries = entries
        self.requests = []

    def __call__(self, url, headers):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.requests.append((params, dict(headers)))
        start, count = int(params["start"]), int(params["count"])
        assert count <= ScopusConnector.PAGE_SIZE
        page = self.entries[start:start + count]
        return json.dumps({"search-results": {
            "opensearch:totalResults": str(len(self.entries)), "entry": page}}).encode()


def _harvest(fake, query, key="test-key"):
    conn = ScopusConnector(http_get=fake, dns_resolver=lambda host: ["93.184.216.34"], api_key=key)
    ref = next(iter(conn.discover(query)))
    return conn.parse(conn.fetch(ref))


def test_registered_under_scopus():
    assert isinstance(get_connector("scopus"), ScopusConnector)


def test_pages_until_limit_and_reports_total():
    fake = FakeScopus([_entry(i, "2024-03-01") for i in range(60)])
    docs = _harvest(fake, {"topic": "ChatGPT", "since": "2023-01-01", "until": "2025-12-31", "limit": 55})
    assert len(docs) == 55
    assert [int(p["start"]) for p, _ in fake.requests] == [0, 25, 50]
    assert [int(p["count"]) for p, _ in fake.requests] == [25, 25, 5]
    assert all(d.metadata["source_total_results"] == 60 for d in docs)


def test_stops_when_results_run_out():
    fake = FakeScopus([_entry(i, "2024-03-01") for i in range(30)])
    docs = _harvest(fake, {"topic": "ChatGPT", "since": "2023-01-01", "until": "2025-12-31", "limit": 200})
    assert len(docs) == 30
    assert len(fake.requests) == 2


def test_query_shaping_and_key_header():
    fake = FakeScopus([])
    _harvest(fake, {"topic": "ChatGPT AND undergraduate*", "since": "2022-11-30", "until": "2026-10-31"})
    params, headers = fake.requests[0]
    assert params["query"] == "TITLE-ABS-KEY(ChatGPT AND undergraduate*)"
    assert params["date"] == "2022-2026"
    assert params["view"] == "STANDARD"
    assert headers["X-ELS-APIKey"] == "test-key"

    fake = FakeScopus([])
    _harvest(fake, {"topic": "TITLE-ABS-KEY(LLM) AND PUBYEAR > 2022"})
    assert fake.requests[0][0]["query"] == "TITLE-ABS-KEY(LLM) AND PUBYEAR > 2022"


def test_date_window_drops_early_and_flags_late_cover_dates():
    fake = FakeScopus([_entry(1, "2022-11-01"), _entry(2, "2023-05-01"), _entry(3, "2027-02-01")])
    docs = _harvest(fake, {"topic": "ChatGPT", "since": "2022-11-30", "until": "2026-10-31"})
    titles = {d.title: d for d in docs}
    assert set(titles) == {"Paper 2", "Paper 3"}
    assert titles["Paper 3"].metadata["cover_date_after_window"] is True
    assert "cover_date_after_window" not in titles["Paper 2"].metadata


def test_document_mapping():
    fake = FakeScopus([_entry(7, "2024-06-15"), _entry(8, "2024-06-15", doi=False)])
    with_doi, without_doi = _harvest(fake, {"topic": "ChatGPT", "since": "2023-01-01", "until": "2025-12-31"})
    assert with_doi.source_id == "scopus" and with_doi.source_type == "paper"
    assert with_doi.content is None
    assert with_doi.authors == ["Author 7"]
    assert with_doi.url == "https://www.scopus.com/record/7"
    assert with_doi.metadata["work_identifier"] == "doi:10.1000/x7"
    assert with_doi.metadata["content_coverage"] == "metadata-only"
    assert with_doi.metadata["authors_coverage"] == "first-author-only"
    assert with_doi.metadata["venue"] == "Computers & Education"
    assert without_doi.metadata["work_identifier"] == "scopus:2-s2.0-00000000008"


def test_same_document_id_as_other_sources_for_a_doi():
    from src.ingestion.connectors.scholarly.base import _document_id
    fake = FakeScopus([_entry(9, "2024-06-15")])
    (doc,) = _harvest(fake, {"topic": "ChatGPT", "since": "2023-01-01", "until": "2025-12-31"})
    assert doc.document_id == _document_id("openalex", "W123", "10.1000/X9")


def test_missing_key_is_a_permanent_skip(monkeypatch):
    monkeypatch.delenv("ELSEVIER_API_KEY", raising=False)
    with pytest.raises(PermanentFetchError):
        _harvest(FakeScopus([]), {"topic": "ChatGPT"}, key=None)


def test_rejects_non_allowlisted_resolution():
    conn = ScopusConnector(http_get=FakeScopus([]), dns_resolver=lambda host: ["10.0.0.5"], api_key="k")
    ref = next(iter(conn.discover({"topic": "ChatGPT"})))
    with pytest.raises(PermanentFetchError):
        conn.fetch(ref)


def test_document_id_is_the_same_for_doi_url_and_bare_doi():
    from src.ingestion.connectors.scholarly.base import _document_id
    assert _document_id("openalex", "W1", "https://doi.org/10.1000/ABC") == _document_id("crossref", "x", "10.1000/abc")
    assert _document_id("pubmed", "1", "doi:10.1000/abc") == _document_id("scopus", "2", "10.1000/ABC")
