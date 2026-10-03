"""IB02 (#2738): business statistics records, appended vintages, removals as vintages, flags verbatim and as-of lookup."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from src.kb.business_statistics_records import (
    CONTRACT,
    BusinessError,
    check_item,
    comparability_basis,
    readiness,
    register_schemas,
)
from src.kb.business_statistics_store import BusinessStatisticsStore
from tests.unit import business_statistics_harness as h


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_all(conn, revisions=True, rebase=True)
    return conn


def _record(name: str = "cbp", index: int = 0) -> dict:
    return json.loads(json.dumps(h.fetch(name)[0][index]))


def test_series_are_keyed_by_source_dataset_indicator_classification_size_place_adjustment_and_unit(loaded):
    store = BusinessStatisticsStore(loaded)
    sca = h.series_by(loaded, "eurostat-sts", classification="C", adjustment="SCA", unit="I21")
    nsa = h.series_by(loaded, "eurostat-sts", classification="C", adjustment="NSA", unit="I21")
    rebased = h.series_by(loaded, "eurostat-sts", classification="C", adjustment="SCA", unit="I26")
    assert len({sca["series_id"], nsa["series_id"], rebased["series_id"]}) == 3
    assert sca["key"] == {"provider": "eurostat-sts", "dataset": "sts_inpr_m", "indicator": "PROD",
                          "concept": "production_index",
                          "classification": {"scheme": "NACE", "version": "Rev.2", "code": "C"},
                          "size_class": "all", "area": {"scheme": "eurostat-geo", "code": "DE"},
                          "adjustment": "SCA", "unit": {"code": "I21", "base_year": "2021"}, "frequency": "monthly"}
    assert sca["statistical_unit"] == "kind-of-activity-unit"
    births = h.series_by(loaded, "eurostat-business-demography", indicator="V11920")
    assert births["size_class"] == {"code": "TOTAL", "label": "Total (all size classes)"}
    assert births["statistical_unit"] == "enterprise" and births["adjustment"] == "not_applicable"
    emp_2017 = h.series_by(loaded, "us-census-cbp", indicator="EMP", classification="00", version="2017")
    emp_2022 = h.series_by(loaded, "us-census-cbp", indicator="EMP", classification="00", version="2022")
    assert emp_2017["series_id"] != emp_2022["series_id"]  # a NAICS vintage is a different classification key
    assert emp_2017["area"] == {"scheme": "us-fips-state", "code": "06", "label": "California"}
    assert emp_2017["statistical_unit"] == "establishment"
    # Values also live in the Economics series storage (domain economics).
    assert loaded.execute("SELECT count(*) FROM economic_vintages WHERE domain='economics' AND series_id=?",
                          [sca["series_id"]]).fetchone()[0] == 2
    first = store.vintage_rows(h.NS, sca["series_id"])[0]
    numeric = {o["period"]: o["numeric_value"] for o in store.observations(h.NS, first["vintage_id"])}
    assert numeric["2096-01"] == pytest.approx(103.2)
    differences = {d["kind"] for d in comparability_basis(sca, emp_2017)}
    assert {"different_source", "different_statistical_unit", "different_classification",
            "different_place"} <= differences


def test_each_release_is_an_appended_vintage_with_flags_verbatim(loaded):
    store = BusinessStatisticsStore(loaded)
    series = h.series_by(loaded, "eurostat-sts", classification="B-D", adjustment="SCA", unit="I21")
    first, second, removed = store.vintage_rows(h.NS, series["series_id"])
    assert second["revision_of"] == first["vintage_id"] and removed["revision_of"] == second["vintage_id"]
    assert first["release_basis"] == "provider_last_update" and first["release_at"] == "2024-03-15T23:00:00Z"
    assert second["changes"]["new_periods"] == ["2096-05"]
    (revised,) = second["changes"]["revised"]
    assert revised == {"period": "2096-04",
                       "before": {"value_text": "102.6", "value": "102.6", "status": "reported",
                                  "flags": {"OBS_FLAG": "p"}},
                       "after": {"value_text": "102.3", "value": "102.3", "status": "reported", "flags": {}}}
    old = {o["period"]: o for o in store.observations(h.NS, first["vintage_id"])}
    assert old["2096-04"]["value"] == "102.6" and old["2096-04"]["flag_meanings"] == ["provisional"]
    assert old["2096-02"]["flags"] == {"OBS_FLAG": "e"}  # the earlier vintage is untouched
    notes = {n["relation"]: n for n in store.notes(h.NS, series["series_id"])}
    assert notes["provisional"]["origin"] == "source-stated" and notes["provisional"]["periods"] in (
        ["2096-04"], ["2096-05"])


def test_withheld_noise_infused_and_confidential_cells_are_never_filled(loaded):
    store = BusinessStatisticsStore(loaded)
    payroll = h.series_by(loaded, "us-census-cbp", indicator="PAYANN", classification="31-33", version="2017")
    (cell,) = store.values(h.NS, payroll["series_id"])["observations"]
    assert cell["status"] == "withheld" and cell["value"] is None and cell["numeric_value"] is None
    assert cell["value_text"] == "0" and cell["flags"] == {"PAYANN_N": "D"}  # never read as a zero
    employment = h.series_by(loaded, "us-census-cbp", indicator="EMP", classification="31-33", version="2017")
    (noisy,) = store.values(h.NS, employment["series_id"])["observations"]
    assert noisy["flags"] == {"EMP_N": "J"} and noisy["attributes"]["exact"] is False
    assert noisy["flag_meanings"] == ["high noise (5 % or more)"]
    births = h.series_by(loaded, "eurostat-business-demography", indicator="V11920")
    cells = {o["period"]: o for o in store.values(h.NS, births["series_id"])["observations"]}
    assert cells["2095"]["status"] == "confidential" and cells["2095"]["value"] is None
    assert cells["2095"]["flags"] == {"OBS_FLAG": "c"}


def test_provisional_deaths_and_corrections_are_new_vintages(loaded):
    store = BusinessStatisticsStore(loaded)
    deaths = h.series_by(loaded, "eurostat-business-demography", indicator="V11930")
    _first, second = store.vintage_rows(h.NS, deaths["series_id"])
    (revised,) = second["changes"]["revised"]
    assert revised["period"] == "2095" and revised["before"]["flags"] == {"OBS_FLAG": "p"}
    assert revised["after"]["value"] == "249300" and second["changes"]["new_periods"] == ["2096"]
    employment = h.series_by(loaded, "us-census-cbp", indicator="EMP", classification="00", version="2017")
    original, corrected = store.vintage_rows(h.NS, employment["series_id"])
    assert original["release_basis"] == corrected["release_basis"] == "declared_release"
    assert corrected["release_at"] == "2024-10-24T00:00:00Z"
    (change,) = corrected["changes"]["revised"]
    assert change["before"]["flags"] == {"EMP_N": "G"} and change["after"]["flags"] == {"EMP_N": "H"}


def test_a_rebase_removes_the_old_series_as_a_vintage_and_links_the_successor(loaded):
    store = BusinessStatisticsStore(loaded)
    old = h.series_by(loaded, "eurostat-sts", classification="C", adjustment="NSA", unit="I21")
    new = h.series_by(loaded, "eurostat-sts", classification="C", adjustment="NSA", unit="I26")
    assert store.values(h.NS, old["series_id"])["status"] == "removed_by_source"
    earlier = store.values(h.NS, old["series_id"], as_of_ms=h.day_ms("2024-11-01"))
    assert earlier["status"] == "available" and earlier["vintage"]["release_label"] == "LAST UPDATE 2024-10-15T23:00:00"
    (note,) = [n for n in store.notes(h.NS, old["series_id"]) if n["relation"] == "base_year_change"]
    assert note["other_series_id"] == new["series_id"] and "nothing is re-based" in note["statement"]
    # The two base years are never merged: the new series starts its own history.
    assert store.series(h.NS, new["series_id"])["vintage_count"] == 1


def test_as_of_lookup_selects_the_vintage_released_by_the_date_and_cites_it(loaded):
    store = BusinessStatisticsStore(loaded)
    series = h.series_by(loaded, "eurostat-sts", classification="C", adjustment="SCA", unit="I21")
    assert store.values(h.NS, series["series_id"], as_of_ms=h.day_ms("2024-01-01"))["reason"] == "no_release_by_as_of"
    mid = store.values(h.NS, series["series_id"], as_of_ms=h.day_ms("2024-06-30"))
    assert mid["citation"]["as_of"] == "2024-03-15T23:00:00Z" and mid["citation"]["retrieved_at"] == \
        "2024-09-01T00:00:00Z"
    assert mid["citation"]["contract"] == CONTRACT and mid["citation"]["live_verification"] == "unverified-live"
    assert mid["citation"]["evidence_origin"] == "fixture"


def test_reacquisition_is_idempotent_and_an_unchanged_republication_adds_no_vintage(loaded):
    before = loaded.execute("SELECT count(*) FROM business_vintages").fetchone()[0]
    results = h.apply(loaded, "bd", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert {r["status"] for r in results} == {"unchanged"}
    assert loaded.execute("SELECT count(*) FROM business_vintages").fetchone()[0] == before


def test_a_release_dated_after_its_retrieval_or_changing_values_without_a_new_clock_is_refused():
    conn = h.connection()
    with pytest.raises(BusinessError) as caught:
        h.apply(conn, "sts", retrieved_at_ms=h.day_ms("2024-01-01"))
    assert "after its retrieval" in str(caught.value)
    h.apply(conn, "cbp", retrieved_at_ms=h.FIRST_RETRIEVAL)
    record = _record("cbp")
    header, item = record["business_release"], record["business_item"]
    item["observations"][0]["value"] = item["observations"][0]["value_text"] = "1"
    header = {**header, "file_sha256": "f" * 64}
    items = [r["business_item"] for r in h.fetch("cbp")[0]]
    items[0] = item
    with pytest.raises(BusinessError) as conflict:
        BusinessStatisticsStore(conn).apply_release(h.NS, header, items, source_id="x", run_id="r",
                                                    principal_id="svc", scopes=h.SCOPES,
                                                    retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert conflict.value.code == "vintage_conflict"


def test_minimisation_and_exclusions_are_enforced_at_write_time():
    record = _record("cbp")
    item = record["business_item"]
    check_item(item)
    for field, code in (("establishment_id", "personal_data"), ("company_name", "personal_data"),
                        ("reconstructed_value", "derived_value"), ("rebased_value", "derived_value"),
                        ("forecast", "derived_value")):
        leaky = json.loads(json.dumps(item))
        leaky["observations"][0]["attributes"][field] = "x"
        with pytest.raises(BusinessError) as caught:
            check_item(leaky)
        assert caught.value.code == code, field
    filled = json.loads(json.dumps(item))
    filled["observations"][0]["status"], filled["observations"][0]["value"] = "withheld", "0"
    with pytest.raises(BusinessError):
        check_item(filled)
    leaky = json.loads(json.dumps(item))
    leaky["observations"][0]["attributes"]["ein"] = "00-0000000"
    items = [r["business_item"] for r in h.fetch("cbp")[0]]
    items[0] = leaky
    conn = h.connection()
    with pytest.raises(BusinessError) as caught:
        BusinessStatisticsStore(conn).apply_release(h.NS, record["business_release"], items, source_id="x",
                                                    run_id="r", principal_id="svc", scopes=h.SCOPES,
                                                    retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert caught.value.code == "personal_data"
    assert not conn.execute("SELECT count(*) FROM business_vintages").fetchone()[0]


def test_items_validate_against_the_published_record_contract_and_register():
    schema = json.loads((h.ROOT / f"contracts/schemas/jsonschema/{CONTRACT}.json").read_text())
    for name in h.SOURCES:
        for page in h.fetch(name):
            for record in page:
                jsonschema.validate(record["business_item"], schema)
    conn = h.connection()
    registered = register_schemas(conn, principal_id="svc", scopes={"knowledge:schema:register"})
    assert len(registered) == 1
    assert not Path(h.ROOT / "contracts/schemas/jsonschema/noesis-business-statistics-record-v1.json").exists()


def test_failures_leave_vintages_current_and_read_as_stale(loaded):
    store = BusinessStatisticsStore(loaded)
    before = loaded.execute("SELECT count(*) FROM business_vintages").fetchone()[0]
    effect = store.record_failure(h.NS, "us-census-cbp", code="rate_limited", run_id="r2", source_id="us-census-cbp",
                                  scopes=h.SCOPES)
    assert "nothing is marked removed" in effect["effect"]
    assert store.provider_state(h.NS, "us-census-cbp")["stale"] is True
    assert loaded.execute("SELECT count(*) FROM business_vintages").fetchone()[0] == before
    report = readiness(loaded)
    assert report["providers"]["us-census-cbp"]["stale"] is True and report["stores_ready"] is True
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}


def test_writes_and_reads_need_scopes():
    conn = h.connection()
    record = _record("sts")
    items = [r["business_item"] for r in h.fetch("sts")[0]]
    with pytest.raises(BusinessError) as caught:
        BusinessStatisticsStore(conn).apply_release(h.NS, record["business_release"], items, source_id="x",
                                                    run_id="r", principal_id="svc", scopes=h.READ_ONLY)
    assert caught.value.code == "unauthorized"
