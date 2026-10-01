"""Labour indicators for a place, sector or occupation as of a release vintage (#2480)."""

from __future__ import annotations

import pytest

from src.kb.labour_identity import LabourIdentity
from src.kb.labour_statistics import LabourError, LabourQueries, forbidden_keys, missing_periods
from tests.unit import labour_harness as h


@pytest.fixture()
def reviewed():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    places = h.register_places(conn)
    h.import_concordances(conn)
    identity = LabourIdentity(conn)
    proposed = identity.propose_places(h.NS, principal_id="a", scopes=h.SCOPES, geo_namespace="geo")["assertions"]
    proposed += identity.propose_classifications(h.NS, "sector", {"scheme": "ISIC", "version": "Rev.4"},
                                                 principal_id="a", scopes=h.SCOPES)["assertions"]
    proposed += identity.propose_classifications(h.NS, "occupation", {"scheme": "ISCO", "version": "08"},
                                                 principal_id="a", scopes=h.SCOPES)["assertions"]
    for assertion in proposed:
        if assertion["state"] == "proposed":
            identity.review(h.NS, assertion["assertion_id"], "accept", "published code or table",
                            principal_id="reviewer", scopes=h.SCOPES)
    return conn, places


def test_a_place_returns_every_source_side_by_side_with_definitions_and_vintages(reviewed):
    conn, places = reviewed
    answer = LabourQueries(conn).indicators(h.NS, place=places["de"], concept="unemployment_rate",
                                            scopes=h.READ_ONLY, as_of_ms=h.day_ms("2024-12-15"))
    by = {(r["provider"], r["estimate_type"], r["seasonal_adjustment"]): r for r in answer["results"]}
    assert set(by) == {("ilostat", "national-reported", "NSA"), ("ilostat", "ilo-modelled", "NSA"),
                       ("oecd", "harmonised", "SA"), ("oecd", "harmonised", "NSA"), ("eurostat-lfs", "survey", "NSA")}
    assert answer["side_by_side"] is True and forbidden_keys(answer) == []
    for result in answer["results"]:
        assert result["definition"]["content"]["basis"] == result["definition_basis"]
        for value in result["values"]:
            cite = value["citation"]
            assert cite["provider"] and cite["series_key"] and cite["vintage_id"] and cite["retrieved_at"]
            assert value["seasonal_adjustment"] in {"NSA", "SA"} and "flags" in value
    # The as-of date selects the vintage current then; the later revision is not visible yet.
    ilo = {v["period"]: v["value"] for v in by[("ilostat", "national-reported", "NSA")]["values"]}
    assert ilo["2098"] == "3.1"
    eurostat = by[("eurostat-lfs", "survey", "NSA")]
    assert {v["period"] for v in eurostat["values"]} == {"2097", "2098"}
    assert any(n["relation"] == "break_in_series" for n in eurostat["source_notes"])
    later = LabourQueries(conn).indicators(h.NS, place=places["de"], concept="unemployment_rate",
                                           scopes=h.READ_ONLY, as_of_ms=h.day_ms("2025-02-01"), history=True)
    ilo_later = next(r for r in later["results"] if r["estimate_type"] == "national-reported")
    assert {v["period"]: v["value"] for v in ilo_later["values"]}["2098"] == "3.0"
    assert len(ilo_later["revision_history"]) == 2 and ilo_later["revision_history"][1]["revision_of"]
    # Pairs are compared by their recorded attributes, never blended.
    pair = next(p for p in answer["comparability"]
                if {conn_id for conn_id in p["series"]} == {by[("ilostat", "national-reported", "NSA")]["series_id"],
                                                             by[("ilostat", "ilo-modelled", "NSA")]["series_id"]})
    assert {d["kind"] for d in pair["recorded_differences"]} >= {"different_definition_basis",
                                                                 "different_estimate_type"}
    assert all("value" not in p for p in answer["comparability"])


def test_gaps_withheld_values_and_unmapped_codes_are_explicit(reviewed):
    conn, places = reviewed
    queries = LabourQueries(conn)
    berlin = queries.indicators(h.NS, place=places["be"], scopes=h.READ_ONLY)
    (series,) = berlin["results"]
    assert series["gaps"]["withheld_periods"] == [{"period": "2097", "status": "confidential",
                                                   "flags": {"OBS_FLAG": "c"}}]
    jolts = queries.indicators(h.NS, place={"scheme": "iso3166-1-alpha2", "code": "US"}, concept="job_vacancies",
                               scopes=h.READ_ONLY)
    (openings,) = jolts["results"]
    assert openings["gaps"]["withheld_periods"][0]["status"] == "not_published"
    assert openings["gaps"]["latest_published_period"] == "2099-02"
    assert missing_periods(["2099-01", "2099-04"], "monthly") == ["2099-02", "2099-03"]
    # A code nobody mapped stays queryable natively and a place with pending mappings lists them.
    native = queries.indicators(h.NS, place={"scheme": "bls-laus-area", "code": "ST0600000000000"},
                                scopes=h.READ_ONLY)
    assert native["results"][0]["provider"] == "bls"
    nothing = queries.indicators(h.NS, place={"scheme": "iso3166-1-alpha3", "code": "FRA"}, scopes=h.READ_ONLY)
    assert nothing["status"] == "none_published" and nothing["results"] == []
    with pytest.raises(LabourError):
        queries.indicators(h.NS, scopes=h.READ_ONLY)


def test_sector_and_occupation_queries_follow_accepted_mappings(reviewed):
    conn, places = reviewed
    queries = LabourQueries(conn)
    sector = queries.indicators(h.NS, sector={"scheme": "ISIC", "version": "Rev.4", "code": "C"},
                                concept="employment", scopes=h.READ_ONLY)
    matched = {r["provider"]: r["matched_by"]["sector"]["relation"] for r in sector["results"]}
    assert matched == {"ilostat": "exact", "eurostat-lfs": "exact", "bls": "partial"}
    occupation = queries.indicators(h.NS, occupation={"scheme": "ISCO", "version": "08", "code": "2"},
                                    scopes=h.READ_ONLY)
    relations = {r["provider"]: r["matched_by"]["occupation"]["relation"] for r in occupation["results"]}
    assert relations == {"ilostat": "exact", "eurostat-lfs": "exact", "bls": "narrower"}
    # Without an accepted mapping the Eurostat NACE series is not returned for an ISIC query.
    fresh = h.connection()
    h.load_all(fresh)
    unmapped = LabourQueries(fresh).indicators(h.NS, sector={"scheme": "ISIC", "version": "Rev.4", "code": "C"},
                                               scopes=h.READ_ONLY)
    assert {r["provider"] for r in unmapped["results"]} == {"ilostat"}
    assert places
