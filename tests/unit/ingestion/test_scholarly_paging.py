"""Systematic-search mode for scholarly connectors: per-source limits, paging,
title/abstract scope and recorded totals (offline)."""
import json
import urllib.parse

import pytest

from src.ingestion.connectors.scholarly.base import HARD_MAX_LIMIT, ScholarlyQuery
from src.ingestion.connectors.scholarly.sources import (
    CoreConnector,
    CrossrefConnector,
    DoajConnector,
    EuropepmcConnector,
    OpenalexConnector,
    PubmedConnector,
    ScopusConnector,
)

WINDOW = {"since": "2020-01-01", "until": "2025-12-31"}
RESOLVER = {"dns_resolver": lambda host: ["93.184.216.34"]}


def _params(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _run(conn, query):
    ref = next(iter(conn.discover(query)))
    return ref, conn.parse(conn.fetch(ref))


class Recorder:
    """Routes each request to ``handler(url) -> dict`` and records the URLs."""

    def __init__(self, handler):
        self.handler, self.urls = handler, []

    def __call__(self, url, headers):
        self.urls.append(url)
        return json.dumps(self.handler(url)).encode()


# -- limits and scope ------------------------------------------------------- #
def test_query_accepts_systematic_limits_and_validates_scope():
    assert ScholarlyQuery.coerce({"topic": "x", "limit": 50_000}).limit == HARD_MAX_LIMIT
    assert ScholarlyQuery.coerce({"topic": "x", "scope": "title_abstract"}).scope == "title_abstract"
    with pytest.raises(ValueError):
        ScholarlyQuery.coerce({"topic": "x", "scope": "fulltext"})


def test_non_paging_source_keeps_one_request_limit_and_rejects_scope():
    conn = CrossrefConnector(http_get=Recorder(lambda u: {}), **RESOLVER)
    ref = next(iter(conn.discover({"topic": "x", "limit": 5000, **WINDOW})))
    assert ref.metadata["query"]["limit"] == 200
    assert _params(ref.locator)["rows"] == "200"
    with pytest.raises(ValueError):
        next(iter(conn.discover({"topic": "x", "scope": "title_abstract"})))


# -- OpenAlex --------------------------------------------------------------- #
def _openalex_handler(total):
    def handler(url):
        p = _params(url)
        start = 0 if p["cursor"] == "*" else int(p["cursor"])
        size = int(p["per-page"])
        page = [{"id": f"https://openalex.org/W{i}", "display_name": f"W{i}", "publication_date": "2024-01-01"}
                for i in range(start, min(start + size, total))]
        nxt = str(start + size) if start + size < total else None
        return {"meta": {"count": total, "next_cursor": nxt}, "results": page}
    return handler


def test_openalex_cursor_paging_beyond_200_with_total():
    rec = Recorder(_openalex_handler(450))
    _, docs = _run(OpenalexConnector(http_get=rec, **RESOLVER), {"topic": "x", "limit": 1000, **WINDOW})
    assert len(docs) == 450 and len(rec.urls) == 3
    assert [_params(u)["cursor"] for u in rec.urls] == ["*", "200", "400"]
    assert all(d.metadata["source_total_results"] == 450 for d in docs)


def test_openalex_stops_at_limit():
    rec = Recorder(_openalex_handler(450))
    _, docs = _run(OpenalexConnector(http_get=rec, **RESOLVER), {"topic": "x", "limit": 250, **WINDOW})
    assert len(docs) == 250 and len(rec.urls) == 2


def test_openalex_title_abstract_scope_uses_filter_not_search():
    rec = Recorder(_openalex_handler(1))
    _, docs = _run(OpenalexConnector(http_get=rec, **RESOLVER),
                   {"topic": "(online OR blended) AND meta-analysis", "scope": "title_abstract", **WINDOW})
    p = _params(rec.urls[0])
    assert "search" not in p
    assert p["filter"].startswith("title_and_abstract.search:(online OR blended) AND meta-analysis,from_publication_date:2020-01-01")
    assert docs[0].metadata["search_scope"] == "title_abstract"
    with pytest.raises(ValueError):
        next(iter(OpenalexConnector(**RESOLVER).discover({"topic": "a, b", "scope": "title_abstract"})))


# -- Europe PMC ------------------------------------------------------------- #
def test_europepmc_cursor_paging_and_title_abstract_scope():
    def handler(url):
        p = _params(url)
        mark = p["cursorMark"]
        start = 0 if mark == "*" else int(mark)
        rows = [{"id": str(i), "title": f"T{i}", "firstPublicationDate": "2023-05-01"} for i in range(start, min(start + 2, 5))]
        nxt = str(start + 2) if start + 2 < 5 else mark  # API repeats the mark on the last page
        return {"hitCount": 5, "nextCursorMark": nxt, "resultList": {"result": rows}}
    rec = Recorder(handler)
    _, docs = _run(EuropepmcConnector(http_get=rec, **RESOLVER),
                   {"topic": "online AND achievement", "scope": "title_abstract", "limit": 2, **WINDOW})
    assert len(docs) == 2  # limit honoured before the next page
    rec = Recorder(handler)
    conn = EuropepmcConnector(http_get=rec, **RESOLVER)
    ref = next(iter(conn.discover({"topic": "online AND achievement", "scope": "title_abstract", "limit": 100, **WINDOW})))
    docs = conn.parse(conn.fetch(ref))
    assert len(docs) == 5 and docs[0].metadata["source_total_results"] == 5
    assert _params(rec.urls[0])["query"].startswith("TITLE_ABS:(online AND achievement) AND (FIRST_PDATE:[2020-01-01 TO 2025-12-31])")


# -- CORE ------------------------------------------------------------------- #
def test_core_offset_paging():
    def handler(url):
        p = _params(url)
        off, size = int(p["offset"]), int(p["limit"])
        rows = [{"id": i + 1, "title": f"C{i}", "yearPublished": 2022} for i in range(off, min(off + size, 230))]
        return {"totalHits": 230, "results": rows}
    rec = Recorder(handler)
    _, docs = _run(CoreConnector(http_get=rec, api_key="k", **RESOLVER), {"topic": "x", "limit": 500, **WINDOW})
    assert len(docs) == 230
    assert [_params(u)["offset"] for u in rec.urls] == ["0", "100", "200"]
    assert docs[0].metadata["source_total_results"] == 230


# -- DOAJ ------------------------------------------------------------------- #
def test_doaj_year_window_in_query_and_page_paging():
    def handler(url):
        page, size = int(_params(url)["page"]), int(_params(url)["pageSize"])
        rows = [{"id": f"d{i}", "bibjson": {"title": f"D{i}", "year": "2021"}} for i in range((page - 1) * size, min(page * size, 150))]
        return {"total": 150, "results": rows}
    rec = Recorder(handler)
    _, docs = _run(DoajConnector(http_get=rec, **RESOLVER), {"topic": "online AND \"meta-analysis\"", "limit": 400, **WINDOW})
    assert len(docs) == 150 and len(rec.urls) == 2
    path = urllib.parse.unquote(urllib.parse.urlsplit(rec.urls[0]).path)
    assert path.endswith('(online AND "meta-analysis") AND bibjson.year:[2020 TO 2025]')


# -- PubMed ----------------------------------------------------------------- #
def test_pubmed_beyond_100_with_batched_summaries():
    ids = [str(1000 + i) for i in range(450)]

    def handler(url):
        if "esearch" in url:
            assert _params(url)["retmax"] == "450"
            return {"esearchresult": {"count": "450", "idlist": ids}}
        batch = _params(url)["id"].split(",")
        assert len(batch) <= 200
        return {"result": {"uids": batch, **{i: {"title": f"P{i}", "sortpubdate": "2024/02/01 00:00"} for i in batch}}}
    rec = Recorder(handler)
    _, docs = _run(PubmedConnector(http_get=rec, **RESOLVER), {"topic": "x[tiab]", "limit": 450, **WINDOW})
    assert len(docs) == 450
    assert sum("esummary" in u for u in rec.urls) == 3
    assert docs[0].metadata["source_total_results"] == 450


# -- Scopus ----------------------------------------------------------------- #
def test_scopus_limit_beyond_200_and_title_abstract_scope():
    def handler(url):
        p = _params(url)
        start, count = int(p["start"]), int(p["count"])
        entries = [{"eid": f"2-s2.0-{i}", "dc:title": f"S{i}", "prism:coverDate": "2024-03-01"}
                   for i in range(start, min(start + count, 600))]
        return {"search-results": {"opensearch:totalResults": "600", "entry": entries}}
    rec = Recorder(handler)
    conn = ScopusConnector(http_get=rec, api_key="k", **RESOLVER)
    _, docs = _run(conn, {"topic": "online", "limit": 5000, **WINDOW})
    assert len(docs) == 600 and len(rec.urls) == 24
    rec = Recorder(handler)
    _run(ScopusConnector(http_get=rec, api_key="k", **RESOLVER), {"topic": "online", "scope": "title_abstract", "limit": 1, **WINDOW})
    assert _params(rec.urls[0])["query"] == "TITLE-ABS(online)"


def test_pubmed_sortpubdate_with_time_is_parsed():
    """Regression: esummary's sortpubdate ("2019/01/01 00:00") parsed to None, so
    every PubMed record was dropped by the publication-date window."""
    from src.ingestion.connectors.scholarly.base import _to_millis
    assert _to_millis("2019/01/01 00:00") == _to_millis("2019-01-01")


def test_transient_5xx_is_retried_then_permanent_errors_raise(monkeypatch):
    import urllib.error
    from src.ingestion.connectors.scholarly import base
    monkeypatch.setattr(base, "_RETRY_BACKOFF_S", 0)
    calls = {"n": 0}

    def flaky(url, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)
        return json.dumps({"total": 1, "results": [{"id": "d1", "bibjson": {"title": "D1", "year": "2021"}}]}).encode()
    _, docs = _run(DoajConnector(http_get=flaky, **RESOLVER), {"topic": "x", **WINDOW})
    assert len(docs) == 1 and calls["n"] == 2

    def bad_request(url, headers):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
    conn = DoajConnector(http_get=bad_request, **RESOLVER)
    ref = next(iter(conn.discover({"topic": "x", **WINDOW})))
    with pytest.raises(urllib.error.HTTPError):
        conn.fetch(ref)


def test_failing_later_page_keeps_partial_results_and_counts_the_shortfall(monkeypatch):
    """Live DOAJ returned 502 for every request at offsets 100-299 of a query
    (2026-10-09). The connector keeps the pages it read and records the gap."""
    import urllib.error
    from src.ingestion.connectors.scholarly import base
    monkeypatch.setattr(base, "_RETRY_BACKOFF_S", 0)

    def handler(url, headers):
        page = int(_params(url)["page"])
        if page == 2:
            raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)
        rows = [{"id": f"d{i}", "bibjson": {"title": f"D{i}", "year": "2021"}} for i in range((page - 1) * 100, page * 100)]
        return json.dumps({"total": 322, "results": rows}).encode()
    _, docs = _run(DoajConnector(http_get=handler, **RESOLVER), {"topic": "x", "limit": 1000, **WINDOW})
    assert len(docs) == 100
    assert docs[0].metadata["source_total_results"] == 322
    assert docs[0].metadata["source_unretrieved_records"] == 222

    def first_page_fails(url, headers):
        raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)
    conn = DoajConnector(http_get=first_page_fails, **RESOLVER)
    with pytest.raises(urllib.error.HTTPError):
        conn.fetch(next(iter(conn.discover({"topic": "x", **WINDOW}))))


def test_year_only_dates_are_kept_when_the_year_overlaps_the_window():
    """A DOAJ "2022" record may be from December 2022: keep it for a window that
    starts 2022-11-30 (screening decides), but drop years outside the window."""
    def handler(url, headers):
        rows = [{"id": f"d{y}", "bibjson": {"title": f"D{y}", "year": str(y)}} for y in (2021, 2022, 2024, 2026)]
        return json.dumps({"total": 4, "results": rows}).encode()
    _, docs = _run(DoajConnector(http_get=handler, **RESOLVER), {"topic": "x", "since": "2022-11-30", "until": "2025-12-31"})
    assert sorted(d.title for d in docs) == ["D2022", "D2024"]
    assert all(d.metadata["publication_date_precision"] == "year" for d in docs)


def test_crossref_relevance_order_and_order_validation():
    rec = Recorder(lambda u: {"message": {"items": []}})
    _run(CrossrefConnector(http_get=rec, **RESOLVER), {"topic": "online learning", "order": "relevance", **WINDOW})
    assert _params(rec.urls[0])["sort"] == "score"
    rec = Recorder(lambda u: {"message": {"items": []}})
    _run(CrossrefConnector(http_get=rec, **RESOLVER), {"topic": "online learning", **WINDOW})
    assert _params(rec.urls[0])["sort"] == "published"
    with pytest.raises(ValueError):
        ScholarlyQuery.coerce({"topic": "x", "order": "citations"})
    with pytest.raises(ValueError):
        next(iter(DoajConnector(**RESOLVER).discover({"topic": "x", "order": "relevance"})))


def test_pubmed_pauses_between_summary_batches(monkeypatch):
    from src.ingestion.connectors.scholarly import sources
    pauses = []
    monkeypatch.setattr(sources.time, "sleep", lambda s: pauses.append(s))
    ids = [str(i) for i in range(1, 451)]

    def handler(url):
        if "esearch" in url:
            return {"esearchresult": {"count": "450", "idlist": ids}}
        batch = _params(url)["id"].split(",")
        return {"result": {"uids": batch, **{i: {"title": f"P{i}", "sortpubdate": "2024/02/01 00:00"} for i in batch}}}
    _run(PubmedConnector(http_get=Recorder(handler), **RESOLVER), {"topic": "x[tiab]", "limit": 450, **WINDOW})
    assert pauses == [0.4, 0.4]  # 3 batches, keyless pacing between them


def test_crossref_records_its_total_without_paging():
    item = {"DOI": "10.1/x", "title": ["T"], "published": {"date-parts": [[2024, 1, 2]]}}
    rec = Recorder(lambda u: {"message": {"total-results": 1234567, "items": [item]}})
    _, docs = _run(CrossrefConnector(http_get=rec, **RESOLVER), {"topic": "x", "order": "relevance", "limit": 5000, **WINDOW})
    assert docs[0].metadata["source_total_results"] == 1234567
    assert docs[0].metadata["result_order"] == "relevance"
    assert _params(rec.urls[0])["rows"] == "200"  # one request: truncation is visible from the total
