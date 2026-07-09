"""Unit tests for the structured filings connector (#784)."""

from __future__ import annotations

import pytest

from services.ingest.common.document_model import Document
from services.ingest.common.series_model import SeriesRecord
from src.ingestion.connectors.filings import (
    Filing,
    FilingFact,
    filing_entities,
    filing_to_document,
    filing_to_series,
    ingest_filing,
)


def _filing():
    return Filing(
        filer="Acme Corp",
        filing_id="acme-2023-10K",
        cik="0001234",
        facts=[
            FilingFact("Revenue", 17.2e9, "2022", "usd"),
            FilingFact("Revenue", 18.5e9, "2023", "usd"),
            FilingFact("NetIncome", 2.1e9, "2023", "usd"),
        ],
        narrative="Acme Corp reported strong growth.",
        officers=["Jane Doe", "John Roe"],
        filed_at=1000,
        source_url="http://sec/x",
    )


def test_filing_to_series_one_per_concept():
    series = filing_to_series(_filing())
    ids = {s.series_id for s in series}
    assert ids == {"filing:acme-corp:revenue", "filing:acme-corp:netincome"}
    rev = next(s for s in series if s.series_id == "filing:acme-corp:revenue")
    assert isinstance(rev, SeriesRecord)
    assert rev.provider == "filing"
    assert [(o.period, o.value) for o in rev.observations] == [("2022", 17.2e9), ("2023", 18.5e9)]
    assert rev.unit == "usd"


def test_filing_to_document():
    doc = filing_to_document(_filing(), ingested_at=1)
    assert isinstance(doc, Document)
    assert doc.document_id == "filing:acme-2023-10K"
    assert doc.metadata["filer"] == "Acme Corp"
    assert "Jane Doe" in doc.authors


def test_filing_entities():
    rels = filing_entities(_filing())
    assert {(r.subject, r.predicate, r.object) for r in rels} == {
        ("Jane Doe", "OFFICER_OF", "Acme Corp"),
        ("John Roe", "OFFICER_OF", "Acme Corp"),
    }


def test_ingest_filing_three_products():
    out = ingest_filing(_filing(), ingested_at=1)
    assert isinstance(out["document"], Document)
    assert len(out["series"]) == 2
    assert len(out["entities"]) == 2


def test_reported_figure_is_checkable_evidence():
    duckdb = pytest.importorskip("duckdb")
    from src.analytics.claim_check import check_assertion
    from src.argument_mining.quantities import QuantityExtractor
    from src.ingestion.connectors.dataset.store import ObservationStore

    conn = duckdb.connect(":memory:")
    store = ObservationStore(conn)
    for s in filing_to_series(_filing(), as_of=1):
        store.upsert(s)
    a = QuantityExtractor().extract("Acme Corp Revenue rose in 2023.")[0]
    env = check_assertion(conn, a)
    assert env["verdict"] == "supported"
    assert env["series_id"] == "filing:acme-corp:revenue"


def test_quarterly_frequency_inferred():
    f = Filing(filer="X", filing_id="x", facts=[
        FilingFact("Revenue", 1.0, "2023-Q1", "usd"),
        FilingFact("Revenue", 1.1, "2023-Q2", "usd"),
    ])
    series = filing_to_series(f)
    assert series[0].frequency == "quarterly"
