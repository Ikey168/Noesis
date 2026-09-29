"""Demographic monitors: releases, vintage revisions, definition revisions and breaks through subscriptions (#1997)."""

from __future__ import annotations

import pytest

from src.kb.demographics import DemographicError
from src.kb.demographics_monitoring import DemographicMonitor
from src.kb.demographics_places import DemographicPlaces
from src.kb.subscriptions import SubscriptionStore
from tests.unit import demographics_harness as h
from tests.unit.domains.test_demographic_places import LAND, import_boundaries

# Fixed clocks for the monitor: before and after the declared expected release (2100-03-15).
BEFORE_EXPECTED = 4_102_444_800_000  # 2100-01-01
AFTER_EXPECTED = 4_110_307_200_000  # 2100-04-02


def run(conn, subscription, watermark, now=BEFORE_EXPECTED):
    SubscriptionStore(conn).commit_watermark(h.NS, watermark)
    return DemographicMonitor(conn, now=lambda: now).run(
        subscription["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_releases_revisions_and_breaks_are_events_and_replays_emit_nothing():
    conn = h.connection()
    h.apply(conn, "eurostat", 0)
    monitor = DemographicMonitor(conn)
    watch = monitor.create(
        h.NS,
        "pjan",
        series_filter={"provider": "eurostat", "series_code": "demo_pjan"},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    first = run(conn, watch, 1)
    assert kinds(first) == ["new_release"]
    assert run(conn, watch, 2)["notifications"] == []
    assert h.apply(conn, "eurostat", 0)["status"] == "unchanged"
    assert run(conn, watch, 3)["notifications"] == []
    h.apply(conn, "eurostat", 0, h.PJAN_SEPTEMBER)
    revised = run(conn, watch, 4)
    assert kinds(revised) == ["series_break", "vintage_revision"]
    revision = next(
        n for n in revised["notifications"] if n["kind"] == "vintage_revision"
    )
    assert revision["item"]["changed_periods"] == [
        "2097",
        "2098",
    ]  # a new b flag and a revised value
    assert revision["item"]["previous_vintage_id"]
    assert revision["source_revision"]["release_basis"] == "eurostat_dataset_updated"
    brk = next(n for n in revised["notifications"] if n["kind"] == "series_break")
    assert (
        brk["item"]["break_kind"] == "publisher_flag"
        and brk["item"]["period"] == "2097"
    )


def test_a_definition_change_is_a_definition_revision_and_a_break():
    conn = h.connection()
    h.apply(conn, "berlin", 0)
    monitor = DemographicMonitor(conn)
    watch = monitor.create(
        h.NS,
        "mitte",
        series_filter={"provider": "statistik-bb", "geography_code": "001"},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert kinds(run(conn, watch, 1)) == ["new_release", "new_release"]
    h.apply(conn, "berlin", 1)
    result = run(conn, watch, 2)
    assert kinds(result) == [
        "definition_revision",
        "definition_revision",
        "series_break",
        "series_break",
        "vintage_revision",
        "vintage_revision",
    ]
    definition = next(
        n for n in result["notifications"] if n["kind"] == "definition_revision"
    )
    assert "population_base" in definition["item"]["changed_fields"]
    assert (
        definition["item"]["previous_definition_id"]
        != definition["item"]["definition_id"]
    )


def test_overdue_calendar_releases_are_pending_and_pins_turn_stale():
    conn = h.connection()
    h.load_all(conn)
    features = import_boundaries(conn)
    places = DemographicPlaces(conn)
    places.resolve_geographies(
        h.NS, principal_id="op", scopes=h.SCOPES, geo_namespace="geo", collections=LAND
    )
    answer = places.boundary_series(
        h.NS, features["cntr.DE"], scopes=h.READ_ONLY, concept="population_stock"
    )
    places.pin(h.NS, answer["receipt"], principal_id="alice", scopes=h.SCOPES)
    monitor = DemographicMonitor(conn)
    watch = monitor.create(
        h.NS,
        "pjan",
        series_filter={"series_code": "demo_pjan"},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    early = run(conn, watch, 1, now=BEFORE_EXPECTED)
    assert early["release_pending"] == [] and early["stale_pins"] == []
    late = run(conn, watch, 2, now=AFTER_EXPECTED)
    (pending,) = late["release_pending"]
    assert (
        pending["status"] == "release_pending"
        and pending["expected_on"] == "2100-03-15"
    )
    assert (
        late["notifications"] == []
    )  # an overdue release is reported, never an event or a prediction
    h.apply(conn, "eurostat", 0, h.PJAN_SEPTEMBER)
    after = run(conn, watch, 3)
    (stale,) = after["stale_pins"]
    assert stale["status"] == "stale" and stale["newer_vintage_ids"]


def test_monitor_filters_and_scopes():
    conn = h.connection()
    monitor = DemographicMonitor(conn)
    with pytest.raises(DemographicError):
        monitor.create(
            h.NS,
            "x",
            series_filter={"colour": "red"},
            principal_id="a",
            scopes=h.SCOPES,
        )
    with pytest.raises(DemographicError):
        monitor.create(
            h.NS,
            "x",
            series_filter={"provider": "unhcr"},
            principal_id="a",
            scopes={"knowledge:subscriptions:write"},
        )
