"""Unit tests for the SEC EDGAR fetch layer (#821) — offline, fixture-driven."""

from __future__ import annotations

import json

import pytest

from src.ingestion.connectors.edgar import (
    EdgarClient,
    facts_to_filing_facts,
    harvest_filing,
    normalize_cik,
)

# Shape-accurate trims of the three EDGAR payloads.
COMPANY_FACTS = {
    "cik": 320193,
    "entityName": "Acme Corp",
    "facts": {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {
                    "USD": [
                        {"fy": 2022, "fp": "FY", "form": "10-K", "val": 17.2e9, "filed": "2023-02-01"},
                        {"fy": 2023, "fp": "FY", "form": "10-K", "val": 18.5e9, "filed": "2024-02-01"},
                        # An amendment refiling FY2023 later — must win.
                        {"fy": 2023, "fp": "FY", "form": "10-K", "val": 18.6e9, "filed": "2024-06-01"},
                        {"fy": 2024, "fp": "Q1", "form": "10-Q", "val": 4.4e9, "filed": "2024-05-01"},
                        # An 8-K row that must be ignored (form filter).
                        {"fy": 2024, "fp": "Q2", "form": "8-K", "val": 9.9e9, "filed": "2024-08-01"},
                    ]
                }
            },
            "NetIncomeLoss": {
                "units": {
                    "USD": [
                        {"fy": 2023, "fp": "FY", "form": "10-K", "val": 2.1e9, "filed": "2024-02-01"},
                    ]
                }
            },
            # A share-count unit that must be ignored (unit filter).
            "Assets": {
                "units": {
                    "shares": [
                        {"fy": 2023, "fp": "FY", "form": "10-K", "val": 123, "filed": "2024-02-01"},
                    ]
                }
            },
        }
    },
}
SUBMISSIONS = {"cik": "320193", "name": "Acme Corp", "sicDescription": "Widgets"}
TICKERS = {"0": {"cik_str": 320193, "ticker": "ACME", "title": "Acme Corp"}}


def _client(user_agent="test tester@example.com"):
    def fake_get(url, ua):
        assert ua == user_agent  # the UA header reaches every request
        if "companyfacts" in url:
            return json.dumps(COMPANY_FACTS)
        if "submissions" in url:
            return json.dumps(SUBMISSIONS)
        if "company_tickers" in url:
            return json.dumps(TICKERS)
        raise AssertionError(f"unexpected url {url}")

    return EdgarClient(user_agent=user_agent, http_get=fake_get)


def test_normalize_cik():
    assert normalize_cik("320193") == "0000320193"
    assert normalize_cik(320193) == "0000320193"
    assert normalize_cik("CIK0000320193") == "0000320193"
    with pytest.raises(ValueError):
        normalize_cik("no-digits")


def test_facts_mapping_filters_and_amendments():
    facts = facts_to_filing_facts(COMPANY_FACTS)
    by_key = {(f.concept, f.period): f for f in facts}
    # FY2023 revenue takes the later-filed amendment value.
    assert by_key[("Revenue", "2023")].value == 18.6e9
    assert by_key[("Revenue", "2022")].value == 17.2e9
    # The quarterly row maps to the contract quarter form.
    assert by_key[("Revenue", "2024-Q1")].value == 4.4e9
    assert by_key[("Revenue", "2024-Q1")].unit == "usd"
    # The 8-K row and the shares-unit Assets row are excluded.
    assert ("Revenue", "2024-Q2") not in by_key
    assert not any(f.concept == "Assets" for f in facts)
    assert by_key[("NetIncome", "2023")].value == 2.1e9


def test_harvest_by_ticker_end_to_end():
    filing = harvest_filing("ACME", client=_client())
    assert filing is not None
    assert filing.cik == "0000320193"
    assert filing.filer == "Acme Corp"
    assert filing.filing_id == "edgar-0000320193"
    assert any(f.concept == "Revenue" for f in filing.facts)
    assert "Widgets" in filing.narrative


def test_harvest_by_cik():
    filing = harvest_filing("320193", client=_client())
    assert filing is not None and filing.cik == "0000320193"


def test_no_user_agent_skips(monkeypatch):
    monkeypatch.delenv("NOESIS_EDGAR_USER_AGENT", raising=False)
    client = EdgarClient(user_agent="", http_get=lambda u, a: "{}")
    assert client.configured is False
    assert harvest_filing("ACME", client=client) is None


def test_unresolvable_ticker_returns_none():
    filing = harvest_filing("NOPE", client=_client())
    assert filing is None


def test_feeds_the_filings_mapper_and_a4_check():
    duckdb = pytest.importorskip("duckdb")
    from src.analytics.claim_check import check_assertion
    from src.argument_mining.quantities import QuantityExtractor
    from src.ingestion.connectors.dataset.store import ObservationStore
    from src.ingestion.connectors.filings import filing_to_series

    filing = harvest_filing("ACME", client=_client())
    conn = duckdb.connect(":memory:")
    store = ObservationStore(conn)
    for series in filing_to_series(filing, as_of=1):
        store.upsert(series)
    assertion = QuantityExtractor().extract("Acme Corp Revenue rose in 2023.")[0]
    env = check_assertion(conn, assertion)
    # 17.2e9 (2022) -> 18.6e9 (2023): the harvested filing supports the claim.
    assert env["verdict"] == "supported"
    assert env["series_id"] == "filing:acme-corp:revenue"
