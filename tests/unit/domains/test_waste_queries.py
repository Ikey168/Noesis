"""WC08 (#2788) and WC09 (#2792): an indicator for a place as of a release, and a facility's waste transfers."""

from __future__ import annotations

import shutil

import duckdb
import pytest

from src.kb.waste_identity import WasteIdentity
from src.kb.waste_queries import WasteQueries, absent_years
from src.kb.waste_records import WasteError, forbidden_paths, personal_data_paths
from tests.unit import waste_harness as h


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("waste") / "queries.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    places = h.register_places(conn)
    identity = WasteIdentity(conn)
    identity.propose_places(h.NS, principal_id="svc", scopes=h.SCOPES)
    identity.propose_facilities(h.NS, principal_id="svc", scopes=h.SCOPES)
    for a in identity.assertions(h.NS, scopes=h.SCOPES, state="proposed"):
        identity.review(h.NS, a["assertion_id"], "accept", "checked", principal_id=h.REVIEWER, scopes=h.SCOPES)
    identity.propose_related_indicators(h.NS, principal_id="svc", scopes=h.SCOPES)
    for a in identity.assertions(h.NS, scopes=h.SCOPES, kind="indicator", state="proposed"):
        identity.review(h.NS, a["assertion_id"], "accept", "joint questionnaire", principal_id=h.REVIEWER,
                        scopes=h.SCOPES)
    conn.close()
    return path, places


@pytest.fixture()
def env(loaded, tmp_path):
    path, places = loaded
    copy = str(tmp_path / "copy.duckdb")
    shutil.copy(path, copy)
    return duckdb.connect(copy), places


def test_as_of_answers_select_the_vintage_released_by_the_date_side_by_side_and_cited(env):
    conn, places = env
    queries = WasteQueries(conn)
    early = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"], as_of="2098-12-31")
    late = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"], as_of="2099-12-31")
    assert early["side_by_side"] is True and early["never_blended"] is True and early["status"] == "reported"
    assert set(early["providers"]) == {"eurostat-waste", "eurostat-circular-economy", "oecd-municipal-waste"}

    def value(answer, dataset, period, hazard="HAZ_NHAZ"):
        row = next(r for r in answer["results"] if r["dataset"] == dataset and r["hazard"]["code"] == hazard)
        return {o["period"]: o["value"] for o in row["observations"]}[period], row

    before, row = value(early, "env_wasgen", "2094")
    after, later = value(late, "env_wasgen", "2094")
    assert (before, after) == ("401200000", "403900000")  # a resubmitted past year is a later vintage
    assert row["citation"]["vintage_id"] != later["citation"]["vintage_id"]
    assert row["citation"]["as_of"].startswith("2098-03-15") and later["citation"]["as_of"].startswith("2099-02-20")
    assert row["definition"]["definition_id"] and row["definition"]["source_text"]
    assert row["matched_by"] == "accepted-match"
    # The OECD figure stands beside Eurostat's with its own definition; the relation is shown, never reconciled.
    oecd = next(r for r in early["results"] if r["provider"] == "oecd-municipal-waste")
    assert oecd["area"] == {"scheme": "iso3166-1-alpha3", "code": "DEU", "label": "Germany"}
    assert early["related_pairs"] and all(p["reconciled"] is False for p in early["related_pairs"])
    assert forbidden_paths(early) == [] and personal_data_paths(early) == []


def test_biennial_odd_years_are_reported_absent_never_filled(env):
    conn, _ = env
    answer = WasteQueries(conn).indicator_for_place(h.NS, scopes=h.READ_ONLY, place={"scheme": "eurostat-geo",
                                                                                     "code": "FR"},
                                                     concept="waste_generated")
    for row in answer["results"]:
        assert [o["period"] for o in row["observations"]] == ["2094", "2096"]
        assert row["absent_years"]["biennial_not_collected"] == ["2095"] and "never filled" in row["absence_note"]
    assert absent_years(["2094", "2095", "2097"], "annual") == {"biennial_not_collected": [], "not_published": ["2096"]}
    hidden = next(r for r in answer["results"] if r["hazard"]["code"] == "HAZ")
    assert {o["period"]: o["status"] for o in hidden["observations"]}["2096"] == "confidential"


def test_a_place_with_no_records_and_a_date_before_any_release(env):
    conn, places = env
    queries = WasteQueries(conn)
    italy = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["it"])
    assert italy["status"] == "no_records" and italy["results"] == [] and "nothing is estimated" in italy[
        "no_records_note"]
    early = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"], as_of="2097-01-01")
    assert early["status"] == "not_released_by_as_of"
    assert {r["status"] for r in early["results"]} == {"unavailable"}
    with pytest.raises(WasteError):
        queries.indicator_for_place(h.NS, scopes={"knowledge:waste:read"}, place=places["de"])


def test_facility_transfers_per_reporting_year_with_revisions_threshold_note_and_citations(env):
    conn, _ = env
    scopes = h.READ_ONLY | {"knowledge:environment:read"}
    answer = WasteQueries(conn).facility_transfers(h.NS, scopes=scopes, facility=h.FACILITY_1)
    assert answer["status"] == "reported" and answer["facility_record"]["native_id"] == h.FACILITY_1
    assert answer["facility_record"]["revision_id"] and answer["facility_identity"]["state"] == "accepted"
    assert "operator" not in answer["facility_record"] and personal_data_paths(answer) == []
    (year,) = answer["reporting_years"]
    assert year["reporting_year"] == 2096
    rows = {(r["hazardous"]["code"], r["treatment"]["code"], r["destination"]["code"]): r for r in year["rows"]}
    corrected = rows[("NONHW", "R", "DOMESTIC")]
    assert corrected["quantity"] == "851.75" and corrected["method"] == {"code": "M", "label": "measured"}
    assert [v["quantity"] for v in corrected["vintages"]] == ["840.25", "851.75"]
    assert all(v["citation"]["vintage_id"] and v["citation"]["release_id"] for v in corrected["vintages"])
    assert corrected["vintages"][1]["citation"]["dataset_version"]["stated"] == "v13.0 (authored fixture)"
    removed = rows[("HW", "R", "TRANSBOUNDARY")]
    assert removed["status"] == "removed_by_source" and removed["quantity"] is None
    assert "never zero" in answer["threshold_note"] and "never summed" in answer["never_summed"]
    assert answer["totals"] is None and forbidden_paths(answer) == []
    as_of = WasteQueries(conn).facility_transfers(h.NS, scopes=scopes, facility=h.FACILITY_1, as_of="2098-12-31")
    rows = {(r["hazardous"]["code"], r["treatment"]["code"]): r for r in as_of["reporting_years"][0]["rows"]}
    assert rows[("NONHW", "R")]["quantity"] == "840.25" and rows[("HW", "R")]["status"] == "published"


def test_an_unknown_inspire_id_is_visible_as_unmatched_and_a_facility_without_rows_says_so(env):
    conn, _ = env
    scopes = h.READ_ONLY | {"knowledge:environment:read"}
    queries = WasteQueries(conn)
    unknown = queries.facility_transfers(h.NS, scopes=scopes, facility=h.UNKNOWN_FACILITY)
    assert unknown["facility_record"] is None and unknown["facility_identity"]["state"] == "unmatched"
    assert unknown["reporting_years"][0]["rows"][0]["quantity"] == "55"
    with pytest.raises(WasteError):
        queries.facility_transfers(h.NS, scopes=h.READ_ONLY, facility=h.FACILITY_1)  # environment read declared


def test_the_evidence_bundle_cites_every_item_with_source_record_revision_and_as_of(env):
    conn, places = env
    scopes = h.READ_ONLY | {"knowledge:environment:read"}
    bundle = WasteQueries(conn).export_bundle(h.NS, scopes=scopes, place=places["fr"], facility=h.FACILITY_2)
    assert {i["kind"] for i in bundle["items"]} == {"series", "transfer_row"}
    for item in bundle["items"]:
        assert item["source"]["provider"] and item["record_revision"] and item["as_of"] and item["citation"]
    assert personal_data_paths(bundle) == [] and forbidden_paths(bundle) == []
