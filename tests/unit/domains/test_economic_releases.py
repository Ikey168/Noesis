import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from services.ingest.common.series_model import SeriesRecord
from src.domains.economic.model import ensure_economic_schema, load_fixture, register_series
from src.domains.economic.releases import EconomicReleaseError, EconomicReleaseStore
from src.ingestion.revisions import DocumentRevisionStore
from src.kb.authored_reports import AuthoredReportStore
from src.kb.report_updates import ReportUpdateStore

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/economic/benchmark.json"
AUTH = {"principal_id": "alice", "scopes": {"operator"}}


def fixture_store():
    conn = duckdb.connect()
    load_fixture(conn, json.loads(FIXTURE.read_text()))
    return conn, EconomicReleaseStore(conn, now=lambda: 123456)


def test_existing_economic_vintage_table_gets_clock_basis_columns():
    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE economic_vintages ("
        "domain TEXT NOT NULL,series_id TEXT NOT NULL,as_of BIGINT NOT NULL,"
        "vintage_id TEXT NOT NULL,release_at_ms BIGINT NOT NULL,retrieved_at_ms BIGINT NOT NULL,"
        "revision_of BIGINT,source_url TEXT,source_document_id TEXT,"
        "PRIMARY KEY(domain,series_id,as_of))"
    )
    ensure_economic_schema(conn)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info('economic_vintages')").fetchall()
    }
    assert {
        "release_at_basis",
        "retrieved_at_basis",
        "vintage_basis",
        "release_time_status",
    } <= columns
    conn.close()


def snapshot(
    store,
    key,
    cutoff,
    *,
    acquired=None,
    series_id="fred:GDPC1:US",
    method=None,
    source_revision_id=None,
):
    return store.create_snapshot(
        "economics",
        key,
        key,
        release_cutoff_ms=cutoff,
        acquired_cutoff_ms=acquired if acquired is not None else cutoff,
        series=[
            {
                "series_id": series_id,
                "provider_release_id": key,
                "methodology_id": method,
                "source_revision_id": source_revision_id,
            }
        ],
        **AUTH,
    )


def test_snapshot_replays_exact_retained_vintages_and_reports_late_acquisition():
    conn, store = fixture_store()
    early = snapshot(store, "aug", 1754006400000)
    late = snapshot(store, "sep", 1756684800000)
    assert early["series"][0]["observations"][1]["value"] == 23770.1
    assert late["series"][0]["observations"][1]["value"] == 23800.4
    assert early["series"][0]["provider_release_id_provenance"] == "caller_supplied"
    unavailable = snapshot(
        store,
        "as-published",
        1756684800000,
        acquired=1754006400000,
        series_id="worldbank:NY.GDP.MKTP.CD:DE",
    )
    assert unavailable["coverage"] == "incomplete"
    assert unavailable["series"][0]["unavailable_reason"] == "late_acquired"
    assert unavailable["series"][0]["observations"] == []
    assert (
        store.inspect_snapshot("economics", early["snapshot_id"], **AUTH)["series"][0][
            "observations"
        ][1]["value"]
        == 23770.1
    )
    assert store.create_snapshot(
        "economics",
        "aug",
        "aug",
        release_cutoff_ms=1754006400000,
        acquired_cutoff_ms=1754006400000,
        series=[{"series_id": "fred:GDPC1:US", "provider_release_id": "aug"}],
        **AUTH,
    )["idempotent"]
    schema = json.loads(
        (
            ROOT
            / "contracts/schemas/jsonschema/noesis-economic-release-snapshot-v1.json"
        ).read_text()
    )
    Draft7Validator(schema).validate(early)
    conn.close()


def test_snapshot_keeps_provider_vintage_and_local_acquisition_clocks_distinct():
    conn = duckdb.connect()
    register_series(
        conn,
        SeriesRecord(
            series_id="fred:fixture-asof",
            provider="fred",
            title="Fixture unemployment",
            frequency="annual",
            unit="percent",
            geography="US",
            as_of=1000,
            observations=[{"period": "2024", "value": 4.1}],
            metadata={
                "vintage_id": "fred:fixture-asof@2024-01-01",
                "vintage_basis": "explicit_alfred_realtime_period",
                "provider_release_at_ms": None,
                "provider_release_time_status": "official release timestamp unavailable",
                "acquired_at_ms": 2000,
            },
        ),
        semantics={"indicator_id": "indicator:fixture-asof", "scaling": 1},
    )
    store = EconomicReleaseStore(conn, now=lambda: 3000)
    captured = store.create_snapshot(
        "economics",
        "clock-provenance",
        "fixture-release",
        release_cutoff_ms=1000,
        acquired_cutoff_ms=2000,
        series=[{"series_id": "fred:fixture-asof"}],
        **AUTH,
    )
    item = captured["series"][0]
    assert item["provider_vintage_ms"] == 1000
    assert item["release_at_ms"] == 1000
    assert item["release_at_basis"] == "provider_vintage_fallback"
    assert item["release_time_status"] == "official release timestamp unavailable"
    assert item["retrieved_at_ms"] == 2000
    assert item["retrieved_at_basis"] == "connector_acquisition"
    assert item["vintage_basis"] == "explicit_alfred_realtime_period"
    assert any("official release timestamp" in note for note in captured["limitations"])
    Draft7Validator(
        json.loads(
            (
                ROOT
                / "contracts/schemas/jsonschema/noesis-economic-release-snapshot-v1.json"
            ).read_text()
        )
    ).validate(captured)
    conn.close()


def test_revision_new_period_removed_missing_and_conversion_rules():
    conn, store = fixture_store()
    left = snapshot(store, "aug", 1754006400000, method="method-v1")
    right = snapshot(store, "sep", 1756684800000, method="method-v1")
    compared = store.compare(
        "economics",
        "same",
        left["snapshot_id"],
        right["snapshot_id"],
        precision=1,
        **AUTH,
    )
    item = compared["items"][0]
    assert item["status"] == "compared"
    assert (
        item["same_period_revisions"][0]["receipt"]["result"]["delta"] == "30300000.0"
    )
    assert item["unchanged_count"] == 1
    assert not item["new_period_changes"]
    assert item["same_period_revisions"][0]["source_citations"][0]["vintage_id"]
    assert item["same_period_revisions"][0]["source_citations"][0][
        "release_at_basis"
    ] == "provider_vintage_fallback"
    Draft7Validator(
        json.loads(
            (
                ROOT
                / "contracts/schemas/jsonschema/noesis-economic-release-comparison-v1.json"
            ).read_text()
        )
    ).validate(compared)

    broken = snapshot(store, "method-change", 1756684800000, method="method-v2")
    blocked = store.compare(
        "economics", "blocked", left["snapshot_id"], broken["snapshot_id"], **AUTH
    )
    assert blocked["items"][0]["status"] == "blocked"
    assert "methodology_id" in blocked["items"][0]["blockers"]
    converted = store.compare(
        "economics",
        "converted",
        left["snapshot_id"],
        broken["snapshot_id"],
        conversions=[
            {
                "indicator_id": item["indicator_id"],
                "conversion_id": "bridge-v1",
                "method": "documented equivalent methodology",
                "evidence_reference": "doc:bridge",
                "addresses": ["methodology_id"],
                "left_multiplier": "1000000",
                "right_multiplier": "1000000",
            }
        ],
        **AUTH,
    )
    assert converted["items"][0]["status"] == "compared"
    conn.close()


def test_mixed_new_period_and_missing_values_are_distinct():
    conn = duckdb.connect()
    for as_of, observations in [
        (
            1000,
            [
                {"period": "2024-Q4", "value": None},
                {"period": "2025-Q1", "value": 1.005},
                {"period": "2025-Q2", "value": 2.0},
                {"period": "2025-Q3", "value": 3.0},
            ],
        ),
        (
            2000,
            [
                {"period": "2024-Q4", "value": None},
                {"period": "2025-Q1", "value": 1.015},
                {"period": "2025-Q3", "value": 3.0},
                {"period": "2025-Q4", "value": 4.0},
            ],
        ),
    ]:
        register_series(
            conn,
            SeriesRecord(
                series_id="fixture:metric",
                provider="fixture",
                title="Metric",
                frequency="quarterly",
                unit="count",
                geography="US",
                as_of=as_of,
                observations=observations,
                metadata={"release_at": as_of, "retrieved_at": as_of},
            ),
            semantics={
                "indicator_id": "metric:one",
                "scaling": 1,
                "seasonal_adjustment": "not_adjusted",
            },
        )
    store = EconomicReleaseStore(conn)
    left = snapshot(store, "one", 1000, series_id="fixture:metric")
    right = snapshot(store, "two", 2000, series_id="fixture:metric")
    result = store.compare(
        "economics",
        "mixed",
        left["snapshot_id"],
        right["snapshot_id"],
        precision=2,
        **AUTH,
    )
    item = result["items"][0]
    assert item["same_period_revisions"][0]["receipt"]["result"]["delta"] == "0.01"
    assert len(item["new_period_changes"]) == 1
    assert item["new_period_changes"][0]["receipt"]["result"]["delta"] == "1.00"
    assert len(item["added_observations"]) == len(item["removed_observations"]) == 1
    assert item["missing_value_count"] == 1
    conn.close()


def test_authored_report_links_source_revisions_and_preserves_export():
    conn = duckdb.connect()
    revisions = DocumentRevisionStore(conn)
    source_revisions = {}
    for as_of, value, doc in [(1000, 1, "doc-old"), (2000, 2, "doc-new")]:
        source_revisions[doc] = revisions.observe(
            {"document_id": doc, "content": f"Metric {value}", "ingested_at": as_of}
        )["revision_id"]
        register_series(
            conn,
            SeriesRecord(
                series_id="fixture:report",
                provider="fixture",
                title="Metric",
                frequency="annual",
                unit="count",
                geography="US",
                as_of=as_of,
                observations=[{"period": "2025", "value": value}],
                metadata={"release_at": as_of, "retrieved_at": as_of},
            ),
            semantics={
                "indicator_id": "metric:report",
                "scaling": 1,
                "source_document_id": doc,
            },
        )
    store = EconomicReleaseStore(conn)
    old = snapshot(
        store,
        "old",
        1000,
        series_id="fixture:report",
        source_revision_id=source_revisions["doc-old"],
    )
    new = snapshot(
        store,
        "new",
        2000,
        series_id="fixture:report",
        source_revision_id=source_revisions["doc-new"],
    )
    comparison = store.compare(
        "economics",
        "cmp",
        old["snapshot_id"],
        new["snapshot_id"],
        assumptions=["Same units"],
        **AUTH,
    )
    report = store.create_report(
        "economics", "report", comparison["comparison_id"], **AUTH
    )["report"]
    assert (
        report["content"]["sections"][0]["assertions"][0]["dependencies"][0]["kind"]
        == "source"
    )
    prior = AuthoredReportStore(conn).export("economics", report["report_id"], **AUTH)
    assert "economic-calculation:" in prior["markdown"]
    assessed = ReportUpdateStore(conn).assess("economics", report["report_id"], **AUTH)
    assert assessed["sections"][0]["status"] in {"current", "uncertain"}
    revisions.observe(
        {"document_id": "doc-new", "content": "Metric revised", "ingested_at": 3000}
    )
    affected = ReportUpdateStore(conn).assess("economics", report["report_id"], **AUTH)
    assert affected["sections"][0]["status"] == "affected"
    assert (
        AuthoredReportStore(conn).export(
            "economics", report["report_id"], revision=1, **AUTH
        )
        == prior
    )
    conn.close()


def test_owner_and_source_revocation_block_inspection():
    conn, store = fixture_store()
    captured = snapshot(store, "old", 1754006400000)
    scopes = {"knowledge:economic:read", "namespace:economics:read"}
    assert store.inspect_snapshot(
        "economics", captured["snapshot_id"], principal_id="alice", scopes=scopes
    )
    with pytest.raises(EconomicReleaseError) as error:
        store.inspect_snapshot(
            "economics", captured["snapshot_id"], principal_id="bob", scopes=scopes
        )
    assert error.value.code == "unauthorized"
    with pytest.raises(EconomicReleaseError) as error:
        store.inspect_snapshot(
            "economics", captured["snapshot_id"], principal_id="alice", scopes=set()
        )
    assert error.value.code == "unauthorized"
    conn.close()
