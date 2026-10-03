"""TO02 (#2739): tourism series records, appended vintages, removals as vintages, flags verbatim and as-of lookup."""

from __future__ import annotations

import json

import jsonschema
import pytest

from src.kb.tourism_records import (
    CONTRACT,
    TourismError,
    check_item,
    comparability_basis,
    readiness,
    register_schemas,
)
from src.kb.tourism_store import TourismStore
from tests.unit import tourism_harness as h


@pytest.fixture(scope="module")
def loaded():
    conn = h.connection()
    h.load_all(conn, revisions=True, nuts2024=True)
    return conn


def _item(name: str = "occupancy", page: int = 0, index: int = 0) -> dict:
    return json.loads(json.dumps(h.fetch(name)[page][index]["tourism_item"]))


def test_series_are_keyed_by_dataset_indicator_residence_accommodation_unit_frequency_and_nuts_place(loaded):
    nights_total = h.series_by(loaded, dataset="tour_occ_nim", residence="TOTAL")
    nights_for = h.series_by(loaded, dataset="tour_occ_nim", residence="FOR")
    regional = h.series_by(loaded, dataset="tour_occ_nin2")
    assert len({nights_total["series_id"], nights_for["series_id"], regional["series_id"]}) == 3
    assert nights_total["key"] == {
        "provider": "eurostat-tourism-occupancy", "dataset": "tour_occ_nim", "indicator": "NGT_SP",
        "concept": "nights_spent", "residence": "TOTAL", "accommodation": "I551-I553", "unit": "NR",
        "frequency": "monthly", "area": {"scheme": "eurostat-geo", "code": "DE", "nuts_version": "2021"}}
    # Monthly national and annual NUTS 2 series are distinct; the same code under NUTS 2024 is another series.
    assert nights_total["frequency"] == "monthly" and regional["frequency"] == "annual"
    beds_2021 = h.series_by(loaded, indicator="BEDPL", nuts_version="2021")
    beds_2024 = h.series_by(loaded, indicator="BEDPL", nuts_version="2024")
    assert beds_2021["series_id"] != beds_2024["series_id"]
    differences = {d["kind"] for d in comparability_basis(beds_2021, beds_2024)}
    assert differences == {"different_nuts_version"}
    assert {d["kind"] for d in comparability_basis(nights_total, regional)} >= {
        "different_dataset", "different_frequency", "different_place"}
    # Values also live in the Economics series storage (domain economics).
    assert loaded.execute("SELECT count(*) FROM economic_vintages WHERE domain='economics' AND series_id=?",
                          [nights_total["series_id"]]).fetchone()[0] == 2
    store = TourismStore(loaded)
    first = store.vintage_rows(h.NS, nights_total["series_id"])[0]
    numeric = {o["period"]: o["numeric_value"] for o in store.observations(h.NS, first["vintage_id"])}
    assert numeric["2096-01"] == pytest.approx(21004300)


def test_a_revised_provisional_month_is_a_new_vintage_with_both_clocks(loaded):
    store = TourismStore(loaded)
    series = h.series_by(loaded, dataset="tour_occ_nim", residence="TOTAL")
    first, second = store.vintage_rows(h.NS, series["series_id"])
    assert second["revision_of"] == first["vintage_id"]
    assert first["release_basis"] == "provider_last_update" and first["release_at"] == "2024-02-12T11:00:00Z"
    assert first["retrieved_at"] == "2024-04-01T00:00:00Z" and second["release_at"] == "2024-05-12T11:00:00Z"
    assert second["changes"]["new_periods"] == ["2096-05"]
    (revised,) = second["changes"]["revised"]
    assert revised == {"period": "2096-04",
                       "before": {"value_text": "30112600", "value": "30112600", "status": "reported",
                                  "flags": {"OBS_FLAG": "p"}},
                       "after": {"value_text": "30245100", "value": "30245100", "status": "reported", "flags": {}}}
    old = {o["period"]: o for o in store.observations(h.NS, first["vintage_id"])}
    assert old["2096-04"]["value"] == "30112600" and old["2096-04"]["flag_meanings"] == ["provisional"]
    notes = {n["relation"] for n in store.notes(h.NS, series["series_id"])}
    assert "provisional" in notes


def test_confidential_cells_are_a_status_and_definitions_record_the_threshold(loaded):
    store = TourismStore(loaded)
    regional = h.series_by(loaded, dataset="tour_occ_nin2")
    answer = store.values(h.NS, regional["series_id"])
    cells = {o["period"]: o for o in answer["observations"]}
    assert cells["2095"]["status"] == "confidential" and cells["2095"]["value"] is None
    assert cells["2095"]["numeric_value"] is None and cells["2095"]["flags"] == {"OBS_FLAG": "c"}
    content = answer["definition"]["content"]
    assert content["coverage_thresholds"]["DE"].startswith("establishments with 10 or more bed places")
    assert content["nuts_version"] == "2021"
    assert {n["relation"] for n in store.notes(h.NS, regional["series_id"])} >= {"definition_differs"}
    item = _item(page=2)
    item["observations"][1]["value"] = "0"
    with pytest.raises(TourismError) as caught:
        check_item(item)
    assert caught.value.code == "invalid_record"


def test_a_series_a_later_complete_release_no_longer_states_is_removed_by_source(loaded):
    store = TourismStore(loaded)
    foreign = h.series_by(loaded, dataset="tour_occ_arm", residence="FOR")
    first, removed = store.vintage_rows(h.NS, foreign["series_id"])
    assert removed["status"] == "removed" and removed["revision_of"] == first["vintage_id"]
    assert "no longer states" in removed["changes"]["removed_by_source"]["statement"]
    assert store.values(h.NS, foreign["series_id"])["status"] == "removed_by_source"
    earlier = store.values(h.NS, foreign["series_id"], as_of_ms=h.day_ms("2024-03-01"))
    assert earlier["status"] == "available" and earlier["vintage"]["vintage_id"] == first["vintage_id"]
    # The NUTS 2024 re-declaration removes the NUTS 2021 capacity series and states new ones; nothing is overwritten.
    beds_2021 = h.series_by(loaded, indicator="BEDPL", nuts_version="2021")
    assert [v["status"] for v in store.vintage_rows(h.NS, beds_2021["series_id"])] == [
        "published", "published", "removed"]


def test_as_of_lookup_selects_the_vintage_released_by_the_date(loaded):
    store = TourismStore(loaded)
    series = h.series_by(loaded, dataset="tour_occ_nim", residence="TOTAL")
    before, reason = store.select_vintage(h.NS, series["series_id"], as_of_ms=h.day_ms("2024-01-01"))
    assert before is None and reason == "no_release_by_as_of"
    early = store.values(h.NS, series["series_id"], as_of_ms=h.day_ms("2024-04-30"))
    late = store.values(h.NS, series["series_id"], as_of_ms=h.day_ms("2024-05-31"))
    assert {o["period"]: o["value"] for o in early["observations"]}["2096-04"] == "30112600"
    assert {o["period"]: o["value"] for o in late["observations"]}["2096-04"] == "30245100"
    assert late["citation"]["vintage_id"] == late["vintage"]["vintage_id"]
    assert late["citation"]["as_of"] == "2024-05-12T11:00:00Z" and late["citation"]["live_verification"] == \
        "unverified-live"


def test_derived_filled_blended_forecast_and_personal_fields_are_refused_at_write_time():
    for key, value in (("occupancy_rate", "61.2"), ("average_length_of_stay", "2.4"), ("per_bed", "3"),
                       ("forecast", "1"), ("nowcast", "1"), ("filled_month", "2096-06"), ("blended_value", "1"),
                       ("annual_total_from_months", "1")):
        item = _item()
        item["observations"][0][key] = value
        with pytest.raises(TourismError) as caught:
            check_item(item)
        assert caught.value.code == "derived_value", key
    item = _item()
    item["guest_name"] = "x"
    with pytest.raises(TourismError) as caught:
        check_item(item)
    assert caught.value.code == "personal_data"
    # An annual period in a monthly series (an annual total computed from months) is refused.
    item = _item()
    item["observations"].append({**item["observations"][0], "period": "2096"})
    with pytest.raises(TourismError):
        check_item(item)
    excluded = _item()
    excluded["dataset"] = "tour_ce_oan"
    with pytest.raises(TourismError) as caught:
        check_item(excluded)
    assert caught.value.code == "excluded_dataset"
    # The whole release is refused; nothing is written.
    conn = h.connection()
    store = TourismStore(conn)
    (record,) = h.fetch("capacity")[0][:1]
    header = dict(record["tourism_release"], item_count=1)
    bad = dict(record["tourism_item"], per_capita="1")
    with pytest.raises(TourismError):
        store.apply_release(h.NS, header, [bad], source_id="x", run_id="r", principal_id="svc", scopes=h.SCOPES,
                            retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert conn.execute("SELECT count(*) FROM tourism_vintages").fetchone()[0] == 0


def test_releases_are_idempotent_and_never_dated_after_retrieval():
    conn = h.connection()
    first = h.apply(conn, "capacity", retrieved_at_ms=h.FIRST_RETRIEVAL)
    again = h.apply(conn, "capacity", retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert [r["status"] for r in first] == ["applied"] and [r["status"] for r in again] == ["unchanged"]
    with pytest.raises(TourismError) as caught:
        h.apply(h.connection(), "capacity", retrieved_at_ms=h.day_ms("2024-01-01"))
    assert caught.value.code == "invalid_release"


def test_the_record_schema_validates_every_item_and_registers():
    schema = json.loads((h.ROOT / f"contracts/schemas/jsonschema/{CONTRACT}.json").read_text())
    for name in h.SOURCES:
        for page in h.fetch(name):
            for record in page:
                jsonschema.validate(record["tourism_item"], schema)
    conn = h.connection()
    results = register_schemas(conn, principal_id="svc", scopes={"knowledge:schema:register"})
    assert results and readiness(conn)["stores_ready"] is False
