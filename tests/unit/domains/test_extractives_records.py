"""Extractives records: report revisions, series vintages, as-of lookup, minimisation at write time (#2662)."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from src.kb.extractives_records import CONTRACT, ExtractivesError, check_item
from src.kb.extractives_store import ExtractivesStore, register_schemas
from tests.unit import extractives_harness as h

SCHEMA = json.loads((h.ROOT / f"contracts/schemas/jsonschema/{CONTRACT}.json").read_text())


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_all(conn)
    return conn


def _report_key(store):
    return next(k for k in store.report_keys(h.NS) if store.report_revisions(h.NS, k)[0]["fiscal_period"]["start"]
                == "2023-01-01")


def test_a_revised_eiti_report_is_a_new_revision_and_as_of_selects_by_release(loaded):
    store = ExtractivesStore(loaded)
    key = _report_key(store)
    first, second = store.report_revisions(h.NS, key)
    assert (first["revision"], second["revision"], second["revision_of"]) == (1, 2, first["report_id"])
    assert second["report"]["version"] == "2" and second["changes"]["version"] == {"before": "1", "after": "2"}
    changed = second["changes"]["lines_changed"]
    assert [(c["before"]["amount_text"], c["after"]["amount_text"]) for c in changed] == [("790000.00", "800000.00")]
    assert second["changes"]["discrepancies_changed"] is True
    early, _ = store.report_as_of(h.NS, key, as_of_ms=h.day_ms("2025-01-01"))
    late, _ = store.report_as_of(h.NS, key, as_of_ms=h.day_ms("2025-10-01"))
    none, reason = store.report_as_of(h.NS, key, as_of_ms=h.day_ms("2024-01-01"))
    assert early["revision"] == 1 and late["revision"] == 2 and none is None and reason == "no_revision_by_as_of"
    # The earlier revision keeps its figures: nothing is overwritten.
    gov = [p for p in store.payments(h.NS, first["report_id"]) if p["reported_by"] == "government"
           and p["company"] and p["company"]["name_as_reported"] == "Exampla Intermediate B.V."]
    assert gov[0]["amount_text"] == "790000.00"
    assert store.discrepancies(h.NS, first["report_id"])[0]["discrepancy_text"] == "-10000.00"


def test_each_publication_is_an_appended_vintage_with_new_revised_and_removed_years(loaded):
    store = ExtractivesStore(loaded)
    series = next(s for s in store.find_series(h.NS, provider="usgs-mcs", statistic="production",
                                               country_names=["Chile"]) if s["commodity"]["name"] == "Copper")
    first, second = store.vintage_rows(h.NS, series["series_id"])
    assert second["revision_of"] == first["vintage_id"]
    assert second["changes"]["new_periods"] == ["2024"] and second["changes"]["removed_periods"] == ["2022"]
    assert [r["period"] for r in second["changes"]["revised"]] == ["2023"]
    assert {o["period"]: o["value"] for o in store.observations(h.NS, first["vintage_id"])} == {
        "2022": "5300", "2023": "5200"}
    as_of, _ = store.select_vintage(h.NS, series["series_id"], as_of_ms=h.day_ms("2024-12-31"))
    assert as_of["vintage_id"] == first["vintage_id"]
    # Numbers live in the Economics series storage; a withheld value keeps no number there.
    assert loaded.execute("SELECT count(*) FROM economic_vintages WHERE series_id=?",
                          [series["series_id"]]).fetchone()[0] == 2
    lithium = store.find_series(h.NS, provider="usgs-mcs", statistic="production", country_names=["United States"])
    withheld = next(s for s in lithium if s["commodity"]["name"] == "Lithium")
    obs = store.observations(h.NS, withheld["current_vintage_id"])
    assert {o["status"] for o in obs} == {"withheld"} and all(o["numeric_value"] is None for o in obs)


def test_usgs_and_bgs_are_always_separate_series(loaded):
    store = ExtractivesStore(loaded)
    peru = store.find_series(h.NS, statistic="production", country_names=["Peru"])
    copper = [s for s in peru if "copper" in s["commodity"]["name"].casefold()]
    assert {s["provider"] for s in copper} == {"usgs-mcs", "bgs-wms"} and len({s["series_id"] for s in copper}) == 2


def test_reingesting_adds_nothing_and_a_changed_file_on_the_same_date_is_refused(loaded):
    before = loaded.execute("SELECT count(*) FROM ex_releases").fetchone()[0]
    h.load_all(loaded)
    assert loaded.execute("SELECT count(*) FROM ex_releases").fetchone()[0] == before
    store = ExtractivesStore(loaded)
    page = h.fetch(h.source("usgs", slice(0, 1)), h.pages("usgs"))[0]
    header = dict(page[0]["extractives_release"], file_sha256="f" * 64)
    items = [copy.deepcopy(r["extractives_item"]) for r in page]
    items[0]["observations"][0]["value_text"] = "9999"
    items[0]["observations"][0]["value"] = "9999"
    with pytest.raises(ExtractivesError) as raised:
        store.apply_release(h.NS, header, items, run_id="r", source_id="usgs-mineral-commodity-summaries",
                            retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert raised.value.code == "vintage_conflict"


def test_minimisation_and_exclusions_are_enforced_at_write_time():
    item = h.fetch(h.source("eiti", slice(1, 2)), h.pages("eiti")[1:])[0][0]["extractives_item"]
    check_item(item)
    with pytest.raises(ExtractivesError) as raised:
        check_item({**item, "contact": {"email": "x@example.invalid"}})
    assert raised.value.code == "personal_data_refused"
    person = copy.deepcopy(item)
    line = next(p for p in person["company_payments"] if p["company"]["natural_person"])
    line["company"]["name_as_reported"] = "Someone"
    with pytest.raises(ExtractivesError, match="redacted"):
        check_item(person)
    with pytest.raises(ExtractivesError):
        check_item({**item, "risk_score": 3})
    with pytest.raises(ExtractivesError):
        check_item({**item, "converted_amount": "1"})


def test_records_round_trip_through_the_registered_schema(loaded):
    store = ExtractivesStore(loaded)
    validator = Draft7Validator(SCHEMA)
    records = [store.release(h.NS, r["release_id"]) for r in store.releases(h.NS)]
    for key in store.report_keys(h.NS):
        for report in store.report_revisions(h.NS, key):
            records.append(report)
            records += [{"contract": CONTRACT, **p} for p in store.payments(h.NS, report["report_id"])]
            records += [{"contract": CONTRACT, **d} for d in store.discrepancies(h.NS, report["report_id"])]
    for series in store.find_series(h.NS):
        records.append(series)
        for vintage in store.vintage_rows(h.NS, series["series_id"]):
            records.append(store.vintage(h.NS, vintage["vintage_id"]))
            records += [{"contract": CONTRACT, **o} for o in store.observations(h.NS, vintage["vintage_id"])]
    for record in records:
        errors = sorted(e.message for e in validator.iter_errors(json.loads(json.dumps(record, default=str))))
        assert not errors, (record.get("record_type"), errors)
    registered = register_schemas(loaded, principal_id="svc", scopes={"operator", "knowledge:schema:register",
                                                                      "knowledge:schema:read"})
    assert registered[0]["name"] == "extractives-record"
