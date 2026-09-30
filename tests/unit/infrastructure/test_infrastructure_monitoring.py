"""CI11 (#2390): status, capacity and ownership changes through subscriptions, refreshed within bounds."""

from __future__ import annotations

import duckdb
import pytest

from src.ingestion import infrastructure_sources as src
from src.kb.infrastructure_assets import InfrastructureError
from src.kb.infrastructure_monitoring import InfrastructureMonitor
from tests.unit.infrastructure import fixture_builder as fb
from tests.unit.infrastructure.harness import NS, SCOPES


def _refresh(monitor, name):
    provider, selection = fb.selection(name)
    fixture = fb.load(name)
    return monitor.refresh(NS, provider, selection, fetch=src.fixture_transport(fixture["native_pages"]),
                           principal_id="analyst", scopes=SCOPES)


def _kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_status_capacity_and_ownership_changes_cite_both_revisions():
    conn = duckdb.connect()
    monitor = InfrastructureMonitor(conn, now=lambda: 1_790_488_800_000)
    created = monitor.create(NS, "lusatia", subject={"kind": "place", "bbox": [14.4, 51.6, 14.5, 51.7]},
                             principal_id="analyst", scopes=SCOPES)
    with pytest.raises(InfrastructureError):
        monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    refreshed = _refresh(monitor, "gem-coal-plants-de")
    assert refreshed["ok"] and refreshed["receipts"] and refreshed["watermark"] == 1
    first = monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    assert _kinds(first) == ["new_asset", "new_asset"]
    replay = monitor.run(created["subscription_id"], 1, principal_id="analyst", scopes=SCOPES)
    assert replay["notifications"] == []
    _refresh(monitor, "gem_coal_plants_release_2")
    later = monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    kinds = _kinds(later)
    assert kinds == ["capacity_change", "ownership_change", "ownership_change", "status_change"]
    status = next(n for n in later["notifications"] if n["kind"] == "status_change")
    assert (status["old"]["normalized"], status["new"]["normalized"]) == ("operating", "retired")
    assert status["cites"]["old_revision"]["revision_id"] != status["cites"]["new_revision"]["revision_id"]
    assert status["cites"]["new_revision"]["release"]["label"] == "GCPT July 2026"
    capacity = next(n for n in later["notifications"] if n["kind"] == "capacity_change")
    assert capacity["changes"][0]["old"]["value"] == "750" and capacity["changes"][0]["new"]["value"] == "760"
    ownership = next(n for n in later["notifications"] if n["kind"] == "ownership_change")
    assert {o["share"] for o in ownership["new"] if o["name"] == "Fixture Holding SE"} == {"75"}
    polled = monitor.poll(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    assert len(polled["events"]) >= 4


def test_new_assets_in_a_later_extract_and_a_failed_refresh():
    conn = duckdb.connect()
    monitor = InfrastructureMonitor(conn, now=lambda: 1_790_488_800_000)
    created = monitor.create(NS, "osm", subject={"kind": "place", "bbox": [14.2, 51.3, 14.8, 51.8]},
                             principal_id="analyst", scopes=SCOPES)
    _refresh(monitor, "osm-lusatia")
    monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    _refresh(monitor, "osm_lusatia_later")
    later = monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    assert _kinds(later) == ["new_asset", "ownership_change"]  # a new generator; way/9002 has a new operator
    failed = monitor.refresh(NS, "osm", fb.SELECTIONS["osm-lusatia"][1],
                             fetch=lambda **_: {"status": 503, "content": b""}, principal_id="analyst", scopes=SCOPES)
    assert not failed["ok"] and failed["failures"]
    degraded = monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    assert "stale_source" in _kinds(degraded) and degraded["coverage"]["stale_providers"] == ["osm"]
    with pytest.raises(InfrastructureError):
        monitor.create(NS, "bad", subject={"kind": "place"}, principal_id="analyst", scopes=SCOPES)
    with pytest.raises(InfrastructureError):
        monitor.run(created["subscription_id"], principal_id="someone-else", scopes=SCOPES)
