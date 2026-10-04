"""WC02 (#2749): waste series and facility transfer rows with release vintages, as-of lookup and write-time refusals."""

from __future__ import annotations

import json

import pytest

from src.kb.environment_vintages import compare
from src.kb.waste_records import WasteError, check_item, check_transfer
from src.kb.waste_store import WasteStore, environment_scopes
from tests.unit import waste_harness as h


def _transfer_item(**overrides):
    item = {"kind": "transfer", "provider": "eea-industry-waste-transfers", "inspire_id": h.FACILITY_1,
            "reporting_year": 2096, "hazardous": {"code": "HW", "label": "hazardous"},
            "treatment": {"code": "D", "label": "disposal"}, "destination": {"code": "DOMESTIC", "label": "domestic"},
            "quantity": "1.5", "quantity_text": "1.5", "unit": {"code": "t"}, "method": {"code": "M"},
            "facility_ref": f"eea-industry:{h.FACILITY_1}", "definition": {"provider": "eea"}}
    item.update(overrides)
    return item


def _series_item():
    records = [r for page in h.fetch("eurostat-waste") for r in page]
    return json.loads(json.dumps(records[0]["waste_item"]))


def test_series_are_keyed_by_category_hazard_activity_operation_and_unit_as_published():
    conn = h.connection()
    h.load_all(conn)
    store = WasteStore(conn)
    wasgen = store.find_series(h.NS, provider="eurostat-waste", concept="waste_generated")
    assert {(s["area"]["code"], s["hazard"]["code"]) for s in wasgen} == {("DE", "HAZ_NHAZ"), ("DE", "HAZ"),
                                                                          ("FR", "HAZ_NHAZ"), ("FR", "HAZ")}
    one = h.series_by(conn, "eurostat-waste", area="DE", dataset="env_wasgen", hazard="HAZ_NHAZ")
    assert one["key"] == {"provider": "eurostat-waste", "dataset": "env_wasgen", "indicator": "env_wasgen",
                          "concept": "waste_generated", "waste_category": "TOTAL", "hazard": "HAZ_NHAZ",
                          "activity": "TOTAL_HH", "operation": "not_applicable", "unit": "T",
                          "area": {"scheme": "eurostat-geo", "code": "DE"}, "periodicity": "biennial"}
    treated = store.find_series(h.NS, provider="eurostat-waste", concept="waste_treated")
    assert {s["operation"]["code"] for s in treated} == {"TRT", "RCV_R", "DSP_L"}
    # Values also live in the Economics series storage.
    assert conn.execute("SELECT count(*) FROM economic_vintages WHERE series_id=?", [one["series_id"]]).fetchone()[0] == 1


def test_biennial_gaps_stay_absent_and_confidential_cells_carry_no_value():
    conn = h.connection()
    h.load_all(conn)
    store = WasteStore(conn)
    one = h.series_by(conn, "eurostat-waste", area="DE", dataset="env_wasgen", hazard="HAZ_NHAZ")
    values = store.values(h.NS, one["series_id"])
    assert [o["period"] for o in values["observations"]] == ["2094", "2096"]  # 2095 never stored
    hidden = h.series_by(conn, "eurostat-waste", area="FR", dataset="env_wasgen", hazard="HAZ")
    cell = {o["period"]: o for o in store.values(h.NS, hidden["series_id"])["observations"]}["2096"]
    assert cell["status"] == "confidential" and cell["value"] is None and cell["flags"] == {"OBS_FLAG": "c"}


def test_facility_transfer_rows_live_in_the_environment_store_keyed_by_inspire_id():
    conn = h.connection()
    h.load_all(conn)
    store = WasteStore(conn)
    rows = store.transfer_rows(h.NS, inspire_id=h.FACILITY_1)
    assert {(r["reporting_year"], r["hazardous"], r["treatment"], r["destination"]) for r in rows} == {
        (2096, "HW", "D", "DOMESTIC"), (2096, "NONHW", "R", "DOMESTIC"), (2096, "HW", "R", "TRANSBOUNDARY")}
    row = next(r for r in rows if r["hazardous"] == "NONHW")
    (vintage,) = store.transfer_vintages(h.NS, row["row_id"])
    assert vintage["quantity"] == "840.25" and vintage["unit"] == "t" and vintage["method"] == "C"
    assert vintage["release_at"].startswith("2098-05-12") and vintage["release_at_basis"] == "provider_reported"
    env = store.environment()
    record = env.record(h.NS, row["environment_record_id"], scopes=environment_scopes(h.NS))
    assert record["record_type"] == "observation_series" and record["content"]["location"] == {
        "kind": "facility", "ref": f"eea-industry:{h.FACILITY_1}"}
    # No second facility register: only the two environment.core facility records exist.
    assert conn.execute("SELECT count(*) FROM environment_records WHERE record_type='facility'").fetchone()[0] == 2
    # The waste rows never mark the environment.core eea-industry provider as freshly acquired.
    state = env.provider_state(h.NS, "eea-industry")
    assert state["last_run_id"] == "run:environment-core:fixture"


def test_revision_chain_appends_vintages_and_removed_rows_are_removed_by_source():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    store = WasteStore(conn)
    one = h.series_by(conn, "eurostat-waste", area="DE", dataset="env_wasgen", hazard="HAZ_NHAZ")
    vintages = store.vintage_rows(h.NS, one["series_id"])
    assert len(vintages) == 2 and vintages[1]["revision_of"] == vintages[0]["vintage_id"]
    revised = vintages[1]["changes"]["revised"]
    assert {r["period"] for r in revised} == {"2094", "2096"}  # resubmitted past year and confirmed provisional
    landfill = h.series_by(conn, "eurostat-waste", area="FR", dataset="env_wastrt", operation="DSP_L")
    assert [v["status"] for v in store.vintage_rows(h.NS, landfill["series_id"])] == ["published", "removed"]
    assert store.values(h.NS, landfill["series_id"], as_of_ms=h.day_ms("2098-12-31"))["status"] == "available"
    assert store.values(h.NS, landfill["series_id"])["status"] == "removed_by_source"
    # Facility rows: a corrected past-year row is a new vintage; an absent row is removed_by_source, never deleted.
    rows = {(r["inspire_id"], r["hazardous"], r["treatment"], r["destination"]): r
            for r in store.transfer_rows(h.NS)}
    corrected = store.transfer_vintages(h.NS, rows[(h.FACILITY_1, "NONHW", "R", "DOMESTIC")]["row_id"])
    assert [(v["quantity"], v["method"]) for v in corrected] == [("840.25", "C"), ("851.75", "M")]
    assert corrected[1]["revision_of"] == corrected[0]["vintage_id"]
    removed = store.transfer_vintages(h.NS, rows[(h.FACILITY_1, "HW", "R", "TRANSBOUNDARY")]["row_id"])
    assert [v["status"] for v in removed] == ["published", "removed_by_source"] and removed[1]["quantity"] is None
    assert rows[(h.FACILITY_2, "HW", "D", "DOMESTIC")]  # a new row
    comparison = compare(conn, h.NS, rows[(h.FACILITY_1, "NONHW", "R", "DOMESTIC")]["environment_record_id"],
                         scopes=environment_scopes(h.NS))
    assert comparison["changes"][0]["change"] == "value_revised" and comparison["changes"][0]["delta"] == "11.50"
    unchanged = store.transfer_vintages(h.NS, rows[(h.FACILITY_1, "HW", "D", "DOMESTIC")]["row_id"])
    assert len(unchanged) == 1  # an unchanged row adds no vintage


def test_as_of_lookup_selects_the_vintage_released_by_the_date():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    store = WasteStore(conn)
    one = h.series_by(conn, "eurostat-waste", area="DE", dataset="env_wasgen", hazard="HAZ_NHAZ")
    before = {o["period"]: o["value"] for o in store.values(h.NS, one["series_id"],
                                                             as_of_ms=h.day_ms("2098-12-31"))["observations"]}
    after = {o["period"]: o["value"] for o in store.values(h.NS, one["series_id"],
                                                            as_of_ms=h.day_ms("2099-12-31"))["observations"]}
    assert before["2094"] == "401200000" and after["2094"] == "403900000"
    assert store.values(h.NS, one["series_id"], as_of_ms=h.day_ms("2098-01-01"))["status"] == "unavailable"
    oecd = h.series_by(conn, "oecd-municipal-waste", area="FRA")
    vintages = store.vintage_rows(h.NS, oecd["series_id"])
    assert [v["release_basis"] for v in vintages] == ["declared_release", "declared_release"]
    assert [v["release_at"][:10] for v in vintages] == ["2098-11-20", "2099-03-31"]


def test_replays_are_idempotent_and_a_release_after_its_retrieval_is_refused():
    conn = h.connection()
    h.load_all(conn)
    again = h.apply(conn, "eurostat-waste", retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert {r["status"] for r in again} == {"unchanged"}
    early = h.connection()
    with pytest.raises(WasteError) as caught:
        h.apply(early, "eurostat-waste", retrieved_at_ms=h.day_ms("2098-01-01"))
    assert caught.value.code == "invalid_release"


@pytest.mark.parametrize("field", ["operator", "parentCompanyName", "address", "contact_person", "competent_authority",
                                   "facilityName", "email"])
def test_operator_address_contact_and_authority_fields_are_refused_at_write_time(field):
    with pytest.raises(WasteError) as caught:
        check_item(_transfer_item(**{field: "x"}))
    assert caught.value.code == "personal_data"
    item = _series_item()
    item["definition"][field] = "x"
    with pytest.raises(WasteError):
        check_item(item)


@pytest.mark.parametrize("field", ["forecast", "gap_filled", "blended_value", "derived_rate", "per_capita",
                                   "national_total", "imputed"])
def test_derived_filled_blended_and_forecast_values_are_refused_at_write_time(field):
    item = _series_item()
    item["observations"][0][field] = "1"
    with pytest.raises(WasteError) as caught:
        check_item(item)
    assert caught.value.code == "derived_value"
    with pytest.raises(WasteError):
        check_item(_transfer_item(**{field: "1"}))


def test_a_missing_facility_row_is_never_a_zero_and_a_whole_release_is_refused_on_one_bad_item():
    with pytest.raises(WasteError):
        check_transfer(_transfer_item(quantity=None))
    conn = h.connection()
    header = h.fetch("eea-industry-waste-transfers")[0][0]["waste_release"]
    items = [r["waste_item"] for r in h.fetch("eea-industry-waste-transfers")[0]]
    items[-1] = {**items[-1], "operator": {"name": "x"}}
    with pytest.raises(WasteError):
        WasteStore(conn).apply_release(h.NS, header, items, source_id="x", run_id="r", principal_id="p",
                                       scopes=h.SCOPES, retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert conn.execute("SELECT count(*) FROM waste_releases").fetchone()[0] == 0
