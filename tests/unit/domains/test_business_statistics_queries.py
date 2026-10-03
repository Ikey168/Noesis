"""IB08/IB09 (#2738): an indicator for a place as of a release, places side by side, and a series' history."""

from __future__ import annotations

import pytest

from src.kb.business_statistics_identity import BusinessIdentity
from src.kb.business_statistics_queries import BusinessQueries, missing_periods
from src.kb.business_statistics_records import BusinessError, forbidden_paths
from src.kb.labour_identity import LabourIdentity
from tests.unit import business_statistics_harness as h

GERMANY = {"scheme": "eurostat-geo", "code": "DE"}
CALIFORNIA = {"scheme": "us-fips-state", "code": "06"}
NACE_C = {"scheme": "NACE", "version": "Rev.2", "code": "C"}


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_all(conn, revisions=True, rebase=True)
    return conn


def _accept_everything(conn):
    places = h.register_places(conn)
    identity = BusinessIdentity(conn)
    labour = LabourIdentity(conn)
    tables = h.concordance_tables()
    for table in tables[:2]:
        labour.import_concordance(h.NS, table, principal_id="op", scopes={"knowledge:labour:write",
                                                                          "namespace:global:write"})
    identity.import_concordance(h.NS, tables[2], principal_id="op", scopes=h.SCOPES)
    proposals = identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)["assertions"]
    proposals += identity.propose_classification_links(h.NS, principal_id="proposer", scopes=h.SCOPES)["assertions"]
    for assertion in proposals:
        identity.review(h.NS, assertion["assertion_id"], "accept", "published codes and concordances",
                        principal_id="reviewer", scopes=h.SCOPES)
    return places


def test_an_indicator_for_a_place_as_of_a_release_with_definitions_flags_and_citations(loaded):
    queries = BusinessQueries(loaded)
    early = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, concept="production_index",
                                        as_of="2024-06-30")
    assert early["status"] == "reported" and early["side_by_side"] is True
    rows = {(r["classification"]["code"], r["adjustment"]): r for r in early["results"]}
    assert set(rows) == {("B-D", "SCA"), ("B-D", "NSA"), ("C", "SCA"), ("C", "NSA")}
    c_sca = rows[("C", "SCA")]
    assert c_sca["unit"] == {"code": "I21", "base_year": "2021", "label": "Index, 2021=100"}
    assert c_sca["statistical_unit"] == "kind-of-activity-unit" and c_sca["definition"]["content"]["references"]
    values = {v["period"]: v for v in c_sca["values"]}
    assert values["2096-04"]["value"] == "104.4" and values["2096-04"]["flags"] == {"OBS_FLAG": "p"}
    assert c_sca["citation"]["as_of"] == "2024-03-15T23:00:00Z" and c_sca["citation"]["release_basis"] == \
        "provider_last_update"
    assert {n["relation"] for n in c_sca["notes"]} >= {"break_in_series", "provisional"}
    # Adjusted and unadjusted are different series side by side, with the difference recorded.
    pair = next(p for p in early["comparability"] if set(p["series"]) == {rows[("C", "SCA")]["series_id"],
                                                                          rows[("C", "NSA")]["series_id"]})
    assert {d["kind"] for d in pair["recorded_differences"]} == {"different_adjustment"}
    later = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, concept="production_index",
                                        as_of="2024-10-31")
    revised = next(r for r in later["results"] if (r["classification"]["code"], r["adjustment"]) == ("C", "SCA"))
    assert {v["period"]: v["value"] for v in revised["values"]}["2096-04"] == "104.0"
    latest = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, concept="production_index")
    statuses = {(r["unit"]["code"], r["status"]) for r in latest["results"]}
    # The removed base-year series are reported as removed by the source, beside their successors.
    assert statuses == {("I26", "available"), ("I21", "removed_by_source")}
    before = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, as_of="2024-01-01")
    assert before["status"] == "none_published" and len(before["unavailable_by_as_of"]) >= 7
    assert forbidden_paths(early) == []


def test_withheld_cells_gaps_and_none_on_record_are_explicit(loaded):
    queries = BusinessQueries(loaded)
    answer = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=CALIFORNIA, concept="annual_payroll",
                                         classification={"scheme": "NAICS", "version": "2017", "code": "31-33"})
    (row,) = answer["results"]
    assert row["gaps"]["withheld_or_confidential"] == [{"period": "2096", "status": "withheld",
                                                        "flags": {"PAYANN_N": "D"}}]
    assert row["values"][0]["value"] is None and row["values"][0]["numeric_value"] is None
    assert {p["provider"] for p in answer["none_on_record"]} == {"eurostat-sts", "eurostat-business-demography"}
    assert missing_periods(["2096-01", "2096-04"], "monthly") == ["2096-02", "2096-03"]
    nowhere = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place="geo:place:none")
    assert nowhere["unmatched_place"] is True and nowhere["results"] == []
    with pytest.raises(BusinessError):
        queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, concept="gdp")
    with pytest.raises(BusinessError):
        queries.indicator_for_place(h.NS, scopes=set(), place=GERMANY)


def test_places_side_by_side_through_accepted_matches_and_candidate_links_never_blended(loaded):
    places = _accept_everything(loaded)
    queries = BusinessQueries(loaded)
    answer = queries.compare_places(h.NS, scopes=h.READ_ONLY, places=[places["de"], places["ca"]],
                                    classification=NACE_C, as_of="2024-09-30")
    germany, california = (a["answer"] for a in answer["places"])
    assert {r["provider"] for r in germany["results"]} == {"eurostat-sts"}
    # California answers NAICS 2022 31-33 only through the accepted candidate link, and says so.
    assert {(r["classification"]["version"], r["classification"]["code"]) for r in california["results"]} == {
        ("2022", "31-33")}
    assert all(r["matched_by"]["matched_by"].startswith("accepted candidate link") for r in california["results"])
    assert forbidden_paths(answer) == [] and "never combined" in answer["never_combined"]
    employment = queries.compare_places(h.NS, scopes=h.READ_ONLY, places=[places["de"], places["ca"]],
                                        concept="active_enterprises")
    assert {r["provider"] for a in employment["places"] for r in a["answer"]["results"]} == {
        "eurostat-business-demography"}
    side = queries.compare_places(h.NS, scopes=h.READ_ONLY, places=[GERMANY, CALIFORNIA], as_of="2024-09-30",
                                  classifications={"eurostat-geo:DE": NACE_C, "us-fips-state:06": {
                                      "scheme": "NAICS", "version": "2017", "code": "31-33"}})
    providers = {r["provider"] for a in side["places"] for r in a["answer"]["results"]}
    assert providers == {"eurostat-sts", "us-census-cbp"}


def test_a_series_history_across_releases_with_comparability_notes(loaded):
    queries = BusinessQueries(loaded)
    old = h.series_by(loaded, "eurostat-sts", classification="C", adjustment="SCA", unit="I21")
    history = queries.series_history(h.NS, old["series_id"], scopes=h.READ_ONLY)
    _first, second, removed = history["vintages"]
    assert second["changed_periods"] == ["2096-04"] and second["new_periods"] == ["2096-05"]
    assert removed["status"] == "removed" and removed["removed_by_source"]
    revision_pair, removal_pair = history["pairs"]
    kinds = {n["kind"] for n in revision_pair["notes"]}
    assert "provisional_confirmed" in kinds
    assert {n["kind"] for n in removal_pair["notes"]} >= {"removed_by_source", "base_year_change"}
    successor = next(n for n in removal_pair["notes"] if n["kind"] == "base_year_change")
    new = h.series_by(loaded, "eurostat-sts", classification="C", adjustment="SCA", unit="I26")
    assert successor["successor_series_id"] == new["series_id"]
    assert history["related_series"][0]["relation"] == "base_year_change"
    assert all(v["citation"]["vintage_id"] == v["vintage_id"] for v in history["vintages"])
    # A break flagged by the source is a comparability note on the series (classification or method break).
    assert any(n["relation"] == "break_in_series" for n in history["series_notes"])


def test_naics_vintages_show_in_history_as_separate_classification_keys(loaded):
    _accept_everything(loaded)
    queries = BusinessQueries(loaded)
    series = h.series_by(loaded, "us-census-cbp", indicator="EMP", classification="31-33", version="2017")
    history = queries.series_history(h.NS, series["series_id"], scopes=h.READ_ONLY)
    vintage_links = [link for link in history["classification_links"] if link["kind"] == "classification_vintage"]
    assert vintage_links and vintage_links[0]["version"] == "2022" and vintage_links[0]["state"] == "accepted"
    assert "stay separate" in vintage_links[0]["statement"]
    corrected = h.series_by(loaded, "us-census-cbp", indicator="EMP", classification="00", version="2017")
    entries = queries.series_history(h.NS, corrected["series_id"], scopes=h.READ_ONLY)["vintages"]
    assert [v["release_basis"] for v in entries] == ["declared_release", "declared_release"]
    assert entries[1]["revisions"][0]["after"]["flags"] == {"EMP_N": "H"}
