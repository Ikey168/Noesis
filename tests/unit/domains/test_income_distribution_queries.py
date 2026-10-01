"""An income indicator for a place as of a release, and a series' history with comparability (#2583, IP08-IP09)."""

from __future__ import annotations

import pytest

from src.kb.income_distribution_identity import IncomeIdentity
from src.kb.income_distribution_queries import IncomeQueries
from src.kb.income_distribution_records import IncomeError, forbidden_keys
from tests.unit import income_distribution_harness as h

DEU = {"scheme": "iso3166-1-alpha3", "code": "DEU"}


@pytest.fixture(scope="module")
def env():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    places = h.register_places(conn)
    identity = IncomeIdentity(conn)
    for a in identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace="geo")["assertions"]:
        if a["state"] == "proposed":
            identity.review(h.NS, a["assertion_id"], "accept", "code", principal_id="reviewer", scopes=h.SCOPES)
    return conn, places, IncomeQueries(conn)


def test_as_of_answers_select_the_vintage_released_by_the_date(env):
    _conn, places, queries = env
    before = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"],
                                         concept="poverty_headcount_ratio", as_of_ms=h.day_ms("2026-07-01"))
    middle = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"],
                                         concept="poverty_headcount_ratio", as_of_ms=h.day_ms("2099-10-01"))
    after = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"],
                                        concept="poverty_headcount_ratio", as_of_ms=h.day_ms("2100-02-01"))
    silc_before = next(r for r in before["results"] if r["provider"] == "eu-silc")
    silc_after = next(r for r in after["results"] if r["provider"] == "eu-silc")
    assert {v["period"]: v["value"] for v in silc_before["values"]}["2098"] == "14.4"
    assert {v["period"]: v["value"] for v in silc_after["values"]}["2098"] == "14.3"
    assert silc_after["vintage"]["revision_of"] == silc_before["vintage"]["vintage_id"]
    # PIP released its first estimates on 2026-08-01; the OECD response was only retrieved on 2099-12-01.
    assert {r["provider"] for r in before["results"]} == {"eu-silc"}
    assert {r["provider"] for r in middle["results"]} == {"pip", "eu-silc"}
    assert before["unavailable_by_as_of"] and before["unavailable_by_as_of"][0]["reason"] == "no_release_by_as_of"
    assert {r["provider"] for r in after["results"]} == {"pip", "eu-silc", "oecd-idd"}


def test_lines_ppp_rounds_and_welfare_concepts_are_never_combined(env):
    _conn, places, queries = env
    answer = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"],
                                         concept="poverty_headcount_ratio")
    assert answer["side_by_side"] and len(answer["groups"]) == 3
    lines = {g["definition"]["poverty_line"]["kind"] + ":" + (g["definition"]["poverty_line"].get("share") or
             g["definition"]["poverty_line"].get("value")) for g in answer["groups"]}
    assert lines == {"absolute:2.15", "relative:60", "relative:50"}
    assert all(not p["same_group"] and p["combined"] is False and p["status"] == "noted"
               for p in answer["comparability"])
    for result in answer["results"]:
        for value in result["values"]:
            assert value["citation"]["vintage_id"] == result["vintage"]["vintage_id"]
            assert value["citation"]["definition_id"] == result["definition"]["definition_id"]
            assert value["attributes"]["welfare_type"] == result["welfare_concept"]
    assert forbidden_keys(answer) == []


def test_a_subject_with_no_records_and_withheld_cells_are_explicit(env):
    _conn, places, queries = env
    none = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place={"scheme": "iso3166-1-alpha3", "code": "FRA"})
    assert none["status"] == "none_published" and none["results"] == []
    berlin = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["be"])
    assert berlin["results"][0]["withheld_periods"][0]["status"] == "confidential"
    austria = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["at"], concept="gini_index")
    assert austria["status"] == "none_published"
    assert austria["unavailable_by_as_of"][0]["reason"] == "withdrawn_by_source"
    with pytest.raises(IncomeError):
        queries.indicator_for_place(h.NS, scopes=set(), place=DEU)


def test_history_lists_revisions_ppp_revisions_breaks_and_unknown_comparability(env):
    conn, _places, queries = env
    median = h.series_where(conn, "pip", concept="median_welfare")
    history = queries.history(h.NS, median["series_id"], scopes=h.READ_ONLY)
    _first, second = history["vintages"]
    assert second["changed_periods"] == {"new": ["2099"], "revised": ["2096", "2097", "2098"], "removed": []}
    assert second["citation"]["release_version"] == "20991120_2017_02_02_PROD"
    (pair,) = history["comparability"]
    assert pair["status"] == "noted" and {n["relation"] for n in pair["notes"]} >= {"ppp_revision", "break_in_series"}
    threshold = h.series_where(conn, "eu-silc", concept="poverty_threshold")
    (plain,) = queries.history(h.NS, threshold["series_id"], scopes=h.READ_ONLY)["comparability"]
    assert plain["status"] == "comparability_unknown" and plain["notes"] == []


def test_evidence_bundle_cites_every_value_with_source_revision_and_as_of(env):
    _conn, places, queries = env
    answer = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"], concept="gini_index",
                                         as_of_ms=h.day_ms("2100-02-01"))
    bundle = queries.export_bundle(answer, created_at_ms=h.SECOND_RETRIEVAL)
    evidence = [o for o in bundle["objects"] if o["payload"].get("kind") == "income-value"]
    assert len(evidence) == sum(len(r["values"]) for r in answer["results"])
    for item in evidence:
        payload = item["payload"]
        assert payload["source"]["provider"] and payload["record_revision"]["vintage_id"]
        assert payload["as_of"] == answer["as_of"] and payload["retrieved_at"]
