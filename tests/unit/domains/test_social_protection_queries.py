"""SS08, SS09 (#2784, #2789): a social protection indicator for a place as of a release, and a series' history."""

from __future__ import annotations

import pytest

from src.kb.social_protection_identity import SocialProtectionIdentity
from src.kb.social_protection_queries import SocialProtectionQueries
from src.kb.social_protection_records import SocialProtectionError, forbidden_paths
from tests.unit import social_protection_harness as h

DE = {"scheme": "eurostat-geo", "code": "DE"}


@pytest.fixture(scope="module")
def conn():
    connection = h.connection()
    h.load_all(connection, revisions=True)
    h.accept_places(connection)
    return connection


def test_as_of_answers_select_the_vintage_released_by_the_date_side_by_side_by_measure(conn):
    queries = SocialProtectionQueries(conn)
    early = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=DE, as_of="2099-01-31")
    later = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=DE, as_of="2099-06-01")
    assert {r["provider"] for r in early["results"]} == {"eurostat-esspros", "oecd-socx",
                                                         "ilo-social-protection-coverage"}
    assert {c["basis"] for c in early["area_codes"]} == {"published-code", "accepted-match"}
    total = next(r for r in early["results"] if r["native_key"] == "A.TOTALNOREROUTE.MIO_EUR.DE")
    assert total["vintage"]["release_at"] == "2098-09-15T23:00:00Z"
    assert {o["period"]: o["value_text"] for o in total["observations"]}["2096"] == "9120.5"
    revised = next(r for r in later["results"] if r["native_key"] == "A.TOTALNOREROUTE.MIO_EUR.DE")
    assert revised["vintage"]["release_at"] == "2099-03-20T23:00:00Z"
    assert {o["period"]: o["value_text"] for o in revised["observations"]}["2096"] == "9125.0"
    assert total["citation"]["vintage_id"] != revised["citation"]["vintage_id"]
    for row in early["results"]:
        assert row["citation"]["as_of"] and row["citation"]["vintage_id"] and row["definition"]["definition_id"]
    assert set(early["measure_groups"]) == {"expenditure", "beneficiaries", "coverage"}
    before_any = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=DE, as_of="2098-09-01")
    assert before_any["results"] == [] and {u["reason"] for u in before_any["unavailable_by_as_of"]} == {
        "no_release_by_as_of"}


def test_expenditure_and_coverage_are_never_equated_and_nothing_is_combined(conn):
    queries = SocialProtectionQueries(conn)
    answer = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=DE, as_of="2099-06-30")
    assert answer["nothing_combined"] is True and "never combined" in answer["never_combined"]
    coverage_pairs = [p for p in answer["pairs"] if "ilo-social-protection-coverage" in p["providers"]]
    assert coverage_pairs and {p["comparability"] for p in coverage_pairs} == {"different_measure"}
    beneficiaries = next(r for r in answer["results"] if r["measure"]["concept"] == "beneficiaries")
    assert all(p["comparability"] == "different_measure" for p in answer["pairs"]
               if beneficiaries["series_id"] in p["series_ids"])
    esspros_old = next(r for r in answer["results"] if r["native_key"] == "A.OLD.TOTAL.MIO_EUR.DE")
    socx_old = next(r for r in answer["results"] if r["native_key"] == "DEU.A.SOCX.PT_B1GQ.ES10._T.TP11")
    pair = next(p for p in answer["pairs"] if set(p["series_ids"]) == {esspros_old["series_id"],
                                                                       socx_old["series_id"]})
    assert pair["comparability"] == "noted" and {n["relation"] for n in pair["notes"]} == {"scope_difference"}
    # Different sources, units and classifications are recorded as differences, never harmonised.
    kinds = {d["kind"] for d in pair["differences"]}
    assert {"different_source", "different_classification", "different_unit"} <= kinds
    assert forbidden_paths(answer) == []
    with pytest.raises(SocialProtectionError) as cofog:
        queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=DE, function={"scheme": "cofog", "code": "GF10"})
    assert cofog.value.code == "distinct_concept"


def test_another_publishers_function_is_reached_only_through_an_accepted_relation(conn):
    queries = SocialProtectionQueries(conn)
    old = {"scheme": "esspros-spfunc", "code": "OLD"}
    native = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=DE, function=old, as_of="2099-06-30")
    assert {r["provider"] for r in native["results"]} == {"eurostat-esspros"}
    assert {n["provider"] for n in native["none_on_record"]} == {"oecd-socx", "ilo-social-protection-coverage"}
    identity = SocialProtectionIdentity(conn)
    proposed = identity.propose_function_relations(h.NS, principal_id="proposer", scopes=h.SCOPES)["proposed"]
    identity.review(h.NS, proposed[0]["assertion_id"], "accept", "published reconciliation (verify)",
                    principal_id="reviewer", scopes=h.SCOPES)
    related = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=DE, function=old, as_of="2099-06-30")
    socx = next(r for r in related["results"] if r["provider"] == "oecd-socx")
    assert socx["function"]["scheme"] == "socx-branch" and socx["function_basis"]["basis"] == \
        "accepted-related-function"
    identity.revert(h.NS, proposed[0]["assertion_id"], "re-check", principal_id="reviewer", scopes=h.SCOPES)


def test_a_place_with_no_records_and_disabled_features_are_reported(conn):
    queries = SocialProtectionQueries(conn)
    answer = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area={"scheme": "eurostat-geo", "code": "PT"})
    assert answer["results"] == [] and len(answer["none_on_record"]) == 3
    limited = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=DE,
                                          enabled_providers={"eurostat-esspros"})
    assert {r["provider"] for r in limited["results"]} == {"eurostat-esspros"}
    assert {d["provider"] for d in limited["features_disabled"]} == {"oecd-socx", "ilo-social-protection-coverage"}
    with pytest.raises(SocialProtectionError):
        queries.indicator_for_place(h.NS, scopes={"namespace:global:read"}, area=DE)


def test_history_lists_revisions_estimates_replaced_restatements_and_comparability_notes(conn):
    queries = SocialProtectionQueries(conn)
    esspros = queries.series_history(h.NS, h.series(conn, "eurostat-esspros", "A.TOTALNOREROUTE.MIO_EUR.DE")[
        "series_id"], scopes=h.READ_ONLY)
    _first, second = esspros["vintages"]
    assert second["changed_periods"] == ["2096"] and second["new_periods"] == ["2097"]
    assert second["revisions"][0]["before"]["value_text"] == "9120.5"
    (pair,) = esspros["pairs"]
    kinds = {n["kind"] for n in pair["notes"]}
    assert "manual_edition_change" in kinds and pair["comparability"] == "noted"
    assert all(v["citation"]["vintage_id"] and v["citation"]["as_of"] for v in esspros["vintages"])
    socx = queries.series_history(h.NS, h.series(conn, "oecd-socx", "FRA.A.SOCX.PT_B1GQ.ES10._T.TP11")["series_id"],
                                  scopes=h.READ_ONLY)
    assert socx["vintages"][1]["estimates_replaced"] == ["2097"]
    assert {n["kind"] for n in socx["pairs"][0]["notes"]} >= {"estimate_replaced"}
    assert socx["scope_notes"] and socx["scope_notes"][0]["origin"] == "source-stated"
    ilo = queries.series_history(h.NS, h.series(conn, "ilo-social-protection-coverage",
                                                "DEU.A.SDG_0131_RT.SEX_T.SOC_CONTIG_TOTAL")["series_id"],
                                 scopes=h.READ_ONLY)
    restated = next(n for n in ilo["pairs"][0]["notes"] if n["kind"] == "edition_restatement")
    assert restated["periods"] == ["2094"] and "fixture edition 2" in restated["statement"]
    # A pair with no stated or recorded note is comparability_unknown.
    unknown = queries.series_history(h.NS, h.series(conn, "ilo-social-protection-coverage",
                                                    "FRA.A.SDG_0131_RT.SEX_T.SOC_CONTIG_TOTAL")["series_id"],
                                     scopes=h.READ_ONLY)
    assert unknown["pairs"][0]["comparability"] == "comparability_unknown"
    removed = queries.series_history(h.NS, h.series(conn, "eurostat-esspros", "A.TOTAL.NR.FR")["series_id"],
                                     scopes=h.READ_ONLY)
    assert removed["vintages"][-1]["status"] == "removed" and removed["pairs"][0]["notes"][0]["kind"] == \
        "removed_by_source"
