from __future__ import annotations

import json
from pathlib import Path

import duckdb
from jsonschema import Draft7Validator

from src.domains.economic.dashboard import EconomicDashboardStore
from src.domains.economic.model import load_fixture
from src.domains.market.entitlements import MarketEntitlementStore

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/economic/benchmark.json"


def test_dashboard_composes_vintages_calendar_and_explicitly_unavailable_consensus():
    conn = duckdb.connect(":memory:")
    load_fixture(conn, json.loads(FIXTURE.read_text()))
    dashboard = EconomicDashboardStore(conn, now=lambda: 1)
    result = dashboard.build(
        "economics",
        release_id="fixture-release",
        request_key="dashboard",
        series=["fred:GDPC1:US"],
        release_cutoff_ms=1756684800000,
        acquired_cutoff_ms=1756684800000,
        initial_release_cutoff_ms=1754006400000,
        principal_id="alice",
        scopes={"operator"},
    )
    assert result["snapshots"]["initial"]["series"][0]["observations"][1]["value"] == 23770.1
    assert result["snapshots"]["latest"]["series"][0]["observations"][1]["value"] == 23800.4
    assert result["snapshots"]["comparison"] is not None
    assert result["release_calendar"][0]["release_at_basis"]
    assert result["consensus_surprises"][0]["status"] == "unavailable"
    assert result["breadth"]["status"] == "not_requested"
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-economic-market-dashboard-v1.json").read_text())
    Draft7Validator(schema).validate(result)
    conn.close()


def test_same_cutoff_dashboard_reuses_one_snapshot():
    conn = duckdb.connect(":memory:")
    load_fixture(conn, json.loads(FIXTURE.read_text()))
    result = EconomicDashboardStore(conn).build(
        "economics",
        release_id="fixture-release",
        request_key="same-cutoff",
        series=["fred:GDPC1:US"],
        release_cutoff_ms=1756684800000,
        acquired_cutoff_ms=1756684800000,
        principal_id="alice",
        scopes={"operator"},
    )
    assert result["snapshots"]["initial"]["snapshot_id"] == result["snapshots"]["latest"]["snapshot_id"]
    assert result["snapshots"]["comparison"] is None
    conn.close()


def test_consensus_surprise_requires_current_entitled_timestamped_source():
    conn = duckdb.connect(":memory:")
    load_fixture(conn, json.loads(FIXTURE.read_text()))
    MarketEntitlementStore(conn, now=lambda: 1756684800000).put_entitlement(
        "economics",
        "consensus-entitlement",
        provider="fixture-consensus",
        license_id="fixture-license",
        capabilities=["read", "display", "retain"],
        evidence_ref="evidence:consensus",
        decision_ref="decision:consensus",
        principal_id="reviewer",
        scopes={"operator"},
        effective_at_ms=0,
    )
    result = EconomicDashboardStore(conn).build(
        "economics",
        release_id="fixture-release",
        request_key="consensus",
        series=["fred:GDPC1:US"],
        release_cutoff_ms=1756684800000,
        acquired_cutoff_ms=1756684800000,
        consensus=[
            {
                "series_id": "fred:GDPC1:US",
                "value": "23750",
                "public_at_ms": 1754000000000,
                "retrieved_at_ms": 1754000000000,
                "source_refs": [
                    {
                        "source_ref_id": "consensus-1",
                        "entitlement_id": "consensus-entitlement",
                        "provider": "fixture-consensus",
                        "license_id": "fixture-license",
                        "retrieved_at_ms": 1754000000000,
                    }
                ],
            }
        ],
        principal_id="alice",
        scopes={"operator"},
    )
    surprise = result["consensus_surprises"][0]
    assert surprise["status"] == "available"
    assert surprise["surprise"]
    conn.close()
