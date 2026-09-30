"""Income series records: keys, revision chains, PPP revisions, withdrawals and as-of lookup (#2583, IP02)."""

from __future__ import annotations

import copy

import pytest

from src.kb.income_distribution_records import IncomeError, series_key
from src.kb.income_distribution_store import IncomeStore, readiness
from tests.unit import income_distribution_harness as h


@pytest.fixture(scope="module")
def conn():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    return conn


def test_series_are_keyed_by_source_welfare_scale_line_ppp_basis_survey_and_coverage(conn):
    store = IncomeStore(conn)
    series = store.find_series(h.NS)
    assert len({s["series_id"] for s in series}) == len(series) == 17
    item = h.fetch("pip")[0][0]["income_item"]
    other = copy.deepcopy(item)
    for field, value in (("welfare_concept", "consumption"), ("ppp_base_year", "2021"), ("survey", "HBS"),
                         ("coverage", "urban"), ("reference_year_basis", "lineup-year")):
        changed = {**copy.deepcopy(item), field: value}
        assert series_key(changed) != series_key(item), field
    other["poverty_line"] = {**other["poverty_line"], "value": "3.00"} if other["poverty_line"] else {
        "kind": "absolute", "value": "3.00", "unit": "PPP$", "ppp_base_year": "2021"}
    assert series_key(other) != series_key(item)
    # Values live in the Economics series storage.
    assert conn.execute("SELECT count(*) FROM economic_vintages WHERE series_id LIKE 'inc-series:%'").fetchone()[0]


def test_a_ppp_revision_is_a_new_vintage_and_the_earlier_one_stays(conn):
    store = IncomeStore(conn)
    median = h.series_where(conn, "pip", concept="median_welfare")
    first, second = store.vintage_rows(h.NS, median["series_id"])
    assert second["revision_of"] == first["vintage_id"]
    assert second["changes"]["ppp_revision"] is True and second["changes"]["restated_periods"] == [
        "2096", "2097", "2098"]
    assert second["release_version"] == "20991120_2017_02_02_PROD"
    before = {o["period"]: o["value"] for o in store.observations(h.NS, first["vintage_id"])}
    after = {o["period"]: o["value"] for o in store.observations(h.NS, second["vintage_id"])}
    assert before["2096"] == "48.12" and after["2096"] == "47.8" and "2099" in after
    assert conn.execute("SELECT count(*) FROM dataset_observations WHERE series_id=?",
                        [median["series_id"]]).fetchone()[0] == 7


def test_as_of_lookup_selects_the_vintage_released_by_the_date(conn):
    store = IncomeStore(conn)
    arop = h.series_where(conn, "eu-silc", native_key="A.LI_R_MD60.PC.T.TOTAL.DE")
    early, reason = store.select_vintage(h.NS, arop["series_id"], as_of_ms=h.day_ms("2026-01-01"))
    assert early is None and reason == "no_release_by_as_of"
    mid, _ = store.select_vintage(h.NS, arop["series_id"], as_of_ms=h.day_ms("2026-07-01"))
    late, _ = store.select_vintage(h.NS, arop["series_id"], as_of_ms=h.day_ms("2100-02-01"))
    assert mid["release_at"].startswith("2026-06-15") and late["release_at"].startswith("2026-09-20")
    assert late["changes"]["revised"][0]["period"] == "2098" and late["changes"]["new_periods"] == ["2099"]


def test_removals_by_the_source_are_revisions_never_deletions(conn):
    store = IncomeStore(conn)
    austria = h.series_where(conn, "eu-silc", native_key="A.GINI_HND.TOTAL.AT")
    published, withdrawn = store.vintage_rows(h.NS, austria["series_id"])
    assert withdrawn["status"] == "withdrawn" and withdrawn["changes"]["withdrawn"] is True
    assert withdrawn["changes"]["removed_periods"] == ["2096", "2097"]
    assert [o["period"] for o in store.observations(h.NS, published["vintage_id"])] == ["2096", "2097"]


def test_reacquisition_and_unchanged_republication_add_nothing(conn):
    releases = len(IncomeStore(conn).releases(h.NS))
    vintages = conn.execute("SELECT count(*) FROM income_vintages").fetchone()[0]
    h.load_all(conn, revisions=True)
    assert len(IncomeStore(conn).releases(h.NS)) == releases
    assert conn.execute("SELECT count(*) FROM income_vintages").fetchone()[0] == vintages
    austria = h.series_where(conn, "eu-silc", native_key="A.LI_R_MD60.PC.T.TOTAL.AT")
    assert austria["vintage_count"] == 1  # the later LAST UPDATE restated nothing for Austria


def test_minimisation_and_no_derived_values_are_enforced_at_write_time():
    conn = h.connection()
    store = IncomeStore(conn)
    header = dict(h.fetch("eusilc")[0][0]["income_release"])
    item = copy.deepcopy(h.fetch("eusilc")[0][0]["income_item"])
    header["item_count"] = 1
    personal = copy.deepcopy(item)
    personal["observations"][0]["attributes"]["respondent_id"] = "R-1"
    with pytest.raises(IncomeError) as refused:
        store.apply_release(h.NS, header, [personal], run_id="r", source_id="s", retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert refused.value.code == "personal_data_refused"
    filled = copy.deepcopy(item)
    filled["observations"][0]["gap_filled"] = True
    with pytest.raises(IncomeError):
        store.apply_release(h.NS, header, [filled], run_id="r", source_id="s", retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert store.find_series(h.NS) == []


def test_changed_values_without_a_new_release_clock_are_refused():
    conn = h.connection()
    store = IncomeStore(conn)
    record = h.fetch("eusilc")[0][0]
    header, item = dict(record["income_release"]), copy.deepcopy(record["income_item"])
    header["item_count"] = 1
    store.apply_release(h.NS, header, [item], run_id="r", source_id="s", retrieved_at_ms=h.FIRST_RETRIEVAL)
    item["observations"][0]["value"] = item["observations"][0]["value_text"] = "99.9"
    header["file_sha256"] = "f" * 64
    with pytest.raises(IncomeError) as conflict:
        store.apply_release(h.NS, header, [item], run_id="r2", source_id="s", retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert conflict.value.code == "vintage_conflict"
    live = {**header, "evidence_origin": "live", "file_sha256": "e" * 64, "published_on": "2100-06-01",
            "published_at": None}
    with pytest.raises(IncomeError) as future:
        store.apply_release(h.NS, live, [item], run_id="r3", source_id="s", retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert "after its retrieval" in str(future.value)


def test_readiness_reports_features_live_state_and_minimisation(conn):
    report = readiness(conn)
    assert report["stores_ready"] and report["providers"]["pip"]["releases"] == 6
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    assert {p["live_releases"] for p in report["providers"].values()} == {0}
    assert report["providers"]["eu-silc"]["feature_state"] == "unmanaged"
    assert report["minimisation"]["who_may_query"]
