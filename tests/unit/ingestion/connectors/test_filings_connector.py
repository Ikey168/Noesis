"""Unit tests for the EDGAR filings connector + registry name-keying (#906).

Offline: EDGAR HTTP is a fake injected getter, and harvest_run persists to an
in-memory DuckDB DocumentStore.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from src.ingestion.connectors.base import Connector, PermanentFetchError
from src.ingestion.connectors.edgar import EdgarClient
from src.ingestion.connectors.filings_connector import FilingsConnector
from src.ingestion.connectors.registry import (
    get_connector,
    is_registered,
    register_connector,
    source_types,
)
from src.ingestion.document_store import DocumentStore

# Import the package to trigger registration of the built-in connectors.
import src.ingestion.connectors  # noqa: F401


COMPANY_FACTS = {
    "entityName": "Acme Corp",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {"fy": 2022, "fp": "FY", "form": "10-K", "val": 17.0e9, "filed": "2023-02-01"},
                        {"fy": 2023, "fp": "FY", "form": "10-K", "val": 18.0e9, "filed": "2024-02-01"},
                    ]
                }
            },
        }
    },
}
SUBMISSIONS = {"name": "Acme Corp", "sicDescription": "Widgets"}


def _client(user_agent="noesis test@example.com"):
    def fake_get(url, ua):
        if "companyfacts" in url:
            return json.dumps(COMPANY_FACTS)
        if "submissions" in url:
            return json.dumps(SUBMISSIONS)
        raise AssertionError(f"unexpected url {url}")

    return EdgarClient(user_agent=user_agent, http_get=fake_get)


# --------------------------------------------------------------------------- #
# Registry name-keying
# --------------------------------------------------------------------------- #


def test_filings_and_upload_coexist_under_distinct_keys():
    # Both emit source_type="note"; the registry keys them by name.
    assert is_registered("filings")
    assert is_registered("note")
    assert type(get_connector("filings")).__name__ == "FilingsConnector"
    assert type(get_connector("note")).__name__ == "UploadConnector"
    # The distinct source type still resolves once.
    assert "note" in source_types()


def test_name_defaults_to_source_type():
    from src.ingestion.connectors.registry import _REGISTRY

    class _TmpConnector(Connector):
        source_type = "xtest_type"  # unique, no explicit name

        def discover(self, query=None):
            return []

        def fetch(self, ref):
            raise NotImplementedError

        def parse(self, raw):
            return []

    try:
        register_connector(_TmpConnector)
        assert "xtest_type" in _REGISTRY  # keyed by source_type
        assert isinstance(get_connector("xtest_type"), _TmpConnector)
    finally:
        _REGISTRY.pop("xtest_type", None)


def test_explicit_name_keys_registry_without_clobbering_source_type():
    from src.ingestion.connectors.registry import _REGISTRY

    class _TmpNamed(Connector):
        source_type = "note"      # shared with upload/filings
        name = "xtest_named"      # but keyed by this

        def discover(self, query=None):
            return []

        def fetch(self, ref):
            raise NotImplementedError

        def parse(self, raw):
            return []

    try:
        register_connector(_TmpNamed)
        assert "xtest_named" in _REGISTRY
        # Did not overwrite the "note"-keyed upload connector.
        assert type(get_connector("note")).__name__ == "UploadConnector"
    finally:
        _REGISTRY.pop("xtest_named", None)


# --------------------------------------------------------------------------- #
# discover / fetch / parse
# --------------------------------------------------------------------------- #


def test_discover_from_query_constructor_and_env(monkeypatch):
    conn = FilingsConnector(filers=["from-ctor"])
    assert [r.locator for r in conn.discover()] == ["from-ctor"]
    # query overrides the constructor list.
    assert [r.locator for r in conn.discover(["a", "b"])] == ["a", "b"]

    monkeypatch.setenv("NOESIS_EDGAR_FILERS", "ENV1, ENV2 ,")
    assert [r.locator for r in FilingsConnector().discover()] == ["ENV1", "ENV2"]


def test_fetch_parse_produces_a_note_document():
    conn = FilingsConnector(client=_client(), filers=["320193"])
    docs = list(conn.harvest())
    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_type == "note"
    assert doc.document_id == "filing:edgar-0000320193"
    assert doc.title == "Filing: Acme Corp"
    assert "Widgets" in (doc.content or "")


def test_unconfigured_client_skips_the_source():
    conn = FilingsConnector(
        client=EdgarClient(user_agent="", http_get=lambda u, a: "{}"),
        filers=["320193"],
    )
    with pytest.raises(PermanentFetchError):
        conn.fetch(next(iter(conn.discover())))
    # harvest() swallows it -> no documents.
    assert list(conn.harvest()) == []


def test_unresolvable_filer_skips_the_source():
    def fake_get(url, ua):
        if "company_tickers" in url:
            return json.dumps({})  # ticker table with no match
        raise AssertionError(url)

    conn = FilingsConnector(
        client=EdgarClient(user_agent="noesis test@example.com", http_get=fake_get),
        filers=["NOPE"],
    )
    with pytest.raises(PermanentFetchError):
        conn.fetch(next(iter(conn.discover())))


# --------------------------------------------------------------------------- #
# harvest_run integration
# --------------------------------------------------------------------------- #


def test_harvest_run_persists_filings_to_document_store():
    conn = FilingsConnector(client=_client(), filers=["320193"])
    store = DocumentStore(duckdb.connect(":memory:"))
    summary = conn.harvest_run(store=store)
    assert summary.documents == 1
    assert summary.inserted == 1
    assert store.count() == 1
    stored = store.get("filing:edgar-0000320193")
    assert stored is not None and stored["source_type"] == "note"
