"""Unit tests for tables-as-evidence extraction (#781)."""

from __future__ import annotations

import pytest

from services.ingest.common.series_model import SeriesRecord
from src.ingestion.connectors.dataset.tables import (
    document_series,
    extract_tables,
    table_to_series,
)

MD_TABLE = """
Some prose before.

| Year | Unemployment (%) |
|------|------------------|
| 2022 | 3.1 |
| 2023 | 3.0 |
| 2024 | 3.4 |

Some prose after.
"""


def test_extract_pipe_table():
    tables = extract_tables(MD_TABLE)
    assert len(tables) == 1
    t = tables[0]
    assert [(o.period, o.value) for o in t.observations] == [("2022", 3.1), ("2023", 3.0), ("2024", 3.4)]
    assert t.unit == "percent"
    # Label prefers the value-column header, not "Year".
    assert "Unemployment" in t.label


def test_extract_whitespace_table():
    text = "GDP by year\n2020 100.0\n2021 105.5\n2022 110.2\n"
    tables = extract_tables(text)
    assert tables
    obs = tables[-1].observations
    assert [(o.period, o.value) for o in obs] == [("2020", 100.0), ("2021", 105.5), ("2022", 110.2)]


def test_quarterly_period_normalization():
    text = "| Q | V |\n|---|---|\n| 2023Q1 | 1.0 |\n| 2023 Q2 | 2.0 |\n"
    t = extract_tables(text)[0]
    assert [o.period for o in t.observations] == ["2023-Q1", "2023-Q2"]


def test_single_row_table_ignored():
    text = "| Year | Val |\n|---|---|\n| 2022 | 3.1 |\n"
    assert extract_tables(text) == []


def test_non_table_text_yields_nothing():
    assert extract_tables("Just a paragraph with the year 2024 mentioned once.") == []


def test_table_to_series_provenance():
    t = extract_tables(MD_TABLE)[0]
    s = table_to_series(t, "paper:1", subject="Unemployment DE", geography="DE", as_of=5, source_url="http://x")
    assert isinstance(s, SeriesRecord)
    assert s.provider == "document"
    assert s.series_id.startswith("document:paper:1:")
    assert s.metadata["parent_document_id"] == "paper:1"
    assert s.geography == "DE"
    assert s.frequency == "annual"


def test_document_series_integrates_with_check(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from src.analytics.claim_check import check_assertion
    from src.argument_mining.quantities import QuantityExtractor
    from src.ingestion.connectors.dataset.store import ObservationStore

    conn = duckdb.connect(":memory:")
    store = ObservationStore(conn)
    for s in document_series("paper:1", MD_TABLE, subject="Unemployment in Germany", geography="DE", as_of=1):
        store.upsert(s)
    assert store.list_series()
    a = QuantityExtractor().extract("Unemployment in Germany rose in 2024.")[0]
    env = check_assertion(conn, a)
    # The claim is checked against the document's own table.
    assert env["verdict"] == "supported"
    assert env["series_id"].startswith("document:paper:1:")
