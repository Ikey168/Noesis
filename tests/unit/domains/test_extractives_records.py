"""Extractives records with revisions and as-of lookup (#2653, EX02 #2662)."""

from __future__ import annotations

import copy

import pytest

from src.kb.extractives_records import (
    ExtractivesError,
    check_minimised,
    day_ms,
    readiness,
)
from src.kb.extractives_store import ExtractivesStore
from tests.unit import extractives_harness as h


def test_a_revised_report_is_a_new_version_with_revisions_and_removals_never_deletions():
    conn = h.connection()
    h.apply(conn, "eiti", retrieved_at_ms=h.FIRST_RETRIEVAL)
    result = h.apply(conn, "eiti", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert result[0]["status"] == "applied"
    # The corrected payment and stream total, the report header, the new version's company-reported figure and the
    # withdrawn Northwind payment are revisions; the rest is unchanged.
    assert result[0]["revisions"] == {"created": 0, "revised": 3, "unchanged": 11, "removed": 1}
    store = ExtractivesStore(conn)
    history = store.history(h.NS, h.CIT_PAYMENT)
    assert [(r["revision"], r["state"], r["report_version"]) for r in history] == [(1, "active", "1"),
                                                                                  (2, "active", "2")]
    assert history[1]["revision_of"] == history[0]["revision_id"]
    assert history[0]["record"]["company_reported"]["value"] == "1250000"  # the first revision is kept intact
    assert history[1]["record"]["discrepancy_as_published"]["explanation"] == "resolved in the revised report"
    removed = store.history(h.NS, h.NW_PAYMENT)
    assert [(r["revision"], r["state"]) for r in removed] == [(1, "active"), (2, "removed")]
    assert removed[1]["record"] == removed[0]["record"]  # a removal keeps the record as last published
    assert store.record(h.NS, h.NW_PAYMENT) is None
    assert store.record(h.NS, h.NW_PAYMENT, include_removed=True)["state"] == "removed"
    rows = conn.execute("SELECT count(*) FROM extractives_records WHERE namespace=?", [h.NS]).fetchone()[0]
    assert rows == 25 + 4  # 25 first revisions, 3 revisions and 1 removal appended; nothing deleted


def test_as_of_lookup_selects_the_revision_in_force_by_the_report_versions_publication_date():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    store = ExtractivesStore(conn)
    before = store.record(h.NS, h.CIT_PAYMENT, as_of_ms=day_ms("2023-06-30"))
    after = store.record(h.NS, h.CIT_PAYMENT, as_of_ms=day_ms("2023-12-31"))
    assert before["report_version"] == "1" and after["report_version"] == "2"
    assert store.record(h.NS, h.CIT_PAYMENT, as_of_ms=day_ms("2023-01-01")) is None  # not yet published
    assert store.record(h.NS, h.NW_PAYMENT, as_of_ms=day_ms("2023-06-30"))["state"] == "active"
    versions = store.report_versions(h.NS, h.NL_REPORT)
    assert [(v["report_version"], v["published_on"]) for v in versions] == [("1", "2023-05-31"), ("2", "2023-11-30")]
    citation = store.cite(h.NS, after)
    assert citation["source"]["provider"] == "eiti" and citation["source"]["release_version"] == "2"
    assert citation["as_of"].startswith("2023-11-30") and citation["observed_at"]
    # Series vintages: as-of selection over annual releases.
    (chile,) = store.find_series(h.NS, provider="usgs-mcs", commodity="copper", statistic="production", country="CL")
    assert store.select_vintage(h.NS, chile["series_id"], day_ms("2024-06-01"))["release_at"].startswith("2024-01-31")
    assert store.select_vintage(h.NS, chile["series_id"], day_ms("2025-06-01"))["release_at"].startswith("2025-01-31")
    assert store.select_vintage(h.NS, chile["series_id"], day_ms("2023-06-01")) is None


def test_replays_add_nothing_and_conflicting_or_stale_versions_are_refused():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    assert {r["status"] for r in h.apply(conn, "eiti", retrieved_at_ms=h.SECOND_RETRIEVAL + 1)} == {"unchanged"}
    assert {r["status"] for r in h.apply(conn, "usgs", revision=True)} == {"unchanged"}
    store = ExtractivesStore(conn)
    records = h.fetch("eiti", revision=True)[0]
    header = copy.deepcopy(records[0]["extractives_release"])
    header["content_sha256"] = "0" * 64
    with pytest.raises(ExtractivesError) as conflict:
        store.apply_release(h.NS, header, [r["extractives_item"] for r in records])
    assert conflict.value.code == "vintage_conflict"
    # Version 1 re-labelled as a new version with an earlier date than the recorded version 2 is stale.
    older = copy.deepcopy(h.fetch("eiti")[0])
    stale = older[0]["extractives_release"]
    stale["release_version"] = "1a"
    with pytest.raises(ExtractivesError) as refused:
        store.apply_release(h.NS, stale, [r["extractives_item"] for r in older])
    assert refused.value.code == "stale_version"


def test_personal_fields_are_refused_at_write_time_under_the_minimisation_decision():
    records = h.fetch("eiti")[0]
    header = copy.deepcopy(records[0]["extractives_release"])
    items = [copy.deepcopy(r["extractives_item"]) for r in records]
    company = next(i for i in items if i["record_type"] == "company" and not i["natural_person"])
    company["contact_person"] = {"email": "someone@example.org"}
    conn = h.connection()
    with pytest.raises(ExtractivesError) as caught:
        ExtractivesStore(conn).apply_release(h.NS, header, items)
    assert caught.value.code == "personal_data_refused"
    assert conn.execute("SELECT count(*) FROM extractives_records").fetchone()[0] == 0  # rolled back
    individual = next(i for i in h.fetch("eiti")[0] if i["extractives_item"].get("natural_person"))
    body = copy.deepcopy(individual["extractives_item"])
    body["name_as_published"] = "A. Fixture-Person"
    with pytest.raises(ExtractivesError):
        check_minimised(body)
    with pytest.raises(ExtractivesError):
        check_minimised({"record_type": "company_payment", "record_key": "x", "converted_amount": "1"})


def test_readiness_reports_features_stores_links_and_minimisation():
    conn = h.connection()
    report = readiness(conn)
    assert report["stores_ready"] is False and report["selected"] == []
    assert report["links"]["ownership"] is False and report["links"]["trade"] is False
    h.apply(conn, "bgs", retrieved_at_ms=h.FIRST_RETRIEVAL)
    report = readiness(conn)
    assert report["stores_ready"] is True and report["providers"]["bgs-wms"]["releases"] == 2
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    assert "contact persons" in " ".join(report["minimisation"]["excluded"])
