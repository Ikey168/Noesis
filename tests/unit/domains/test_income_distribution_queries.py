"""IP08 and IP09 (#2623, #2628): an indicator for a place as of a release, and a series' history with comparability."""

from __future__ import annotations

import pytest

from src.kb.income_distribution_identity import IncomeIdentity
from src.kb.income_distribution_queries import IncomeQueries
from src.kb.income_distribution_records import IncomeError
from tests.unit import income_distribution_harness as h


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    places = h.register_places(conn, keys=("de",))
    identity = IncomeIdentity(conn)
    for assertion in identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)["proposed"]:
        identity.review(h.NS, assertion["assertion_id"], "accept", "identifier", principal_id="reviewer",
                        scopes=h.SCOPES)
    identity.propose_related(h.NS, principal_id="proposer", scopes=h.SCOPES)
    return conn, places


def test_each_source_side_by_side_as_released_by_the_date_never_combined(world):
    conn, places = world
    answer = IncomeQueries(conn).indicator_for_place(h.NS, scopes=h.READ_ONLY, concept="poverty_headcount",
                                                     place_id=places["de"], as_of="2099-01-31")
    providers = {r["provider"] for r in answer["results"]}
    assert providers == {"pip", "eurostat-silc", "oecd-idd"}
    assert answer["combined_value"] is None and "never combined" in answer["never_combined"]
    # Different lines, PPP rounds and welfare concepts are different rows and groups.
    pip_rows = [r for r in answer["results"] if r["provider"] == "pip"]
    assert {r["ppp_base_year"] for r in pip_rows} == {2017, 2021}
    assert len({r["comparability_group"] for r in answer["results"]}) == len(answer["results"])
    silc = next(r for r in answer["results"] if r["provider"] == "eurostat-silc")
    assert silc["vintage"]["release_at"] == "2098-06-10T11:00:00Z"  # the 2099-05 release is after the date
    assert silc["definition"]["content"]["threshold"].startswith("60 %")
    assert silc["citation"]["vintage_id"] == silc["vintage"]["vintage_id"] and silc["citation"]["as_of"]
    pip_2017 = next(r for r in pip_rows if r["ppp_base_year"] == 2017)
    assert pip_2017["vintage"]["release_label"] == "20980915_2017_01_02_PROD"
    assert {o["estimation_type"] for o in pip_2017["observations"]} == {"survey", "interpolation"}
    assert pip_2017["related_series"]


def test_as_of_selects_later_releases_and_reports_what_was_not_yet_released(world):
    conn, places = world
    queries = IncomeQueries(conn)
    later = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, concept="poverty_headcount",
                                        place_id=places["de"], as_of="2099-06-30", reference_year="2094")
    pip_2017 = next(r for r in later["results"] if r["provider"] == "pip" and r["ppp_base_year"] == 2017)
    assert pip_2017["vintage"]["release_label"] == "20990320_2017_02_02_PROD"
    assert [(o["period"], o["value"]) for o in pip_2017["observations"]] == [("2094", "0.0022")]
    oecd = next(r for r in later["results"] if r["provider"] == "oecd-idd")
    assert oecd["status"] == "removed_by_source" and oecd["observations"] == []
    early = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, concept="gini",
                                        area={"scheme": "eurostat-geo", "code": "DE"}, as_of="2098-08-01")
    assert {r["provider"] for r in early["results"]} == {"eurostat-silc"}
    assert {u["provider"] for u in early["unavailable_by_as_of"]} == {"pip", "oecd-idd"}
    assert {r["area_basis"] for r in early["results"]} == {"published-code"}


def test_a_place_with_no_records_and_unmatched_places(world):
    conn, places = world
    queries = IncomeQueries(conn)
    nothing = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, concept="gini",
                                          area={"scheme": "iso3166-1-alpha3", "code": "ZZX"})
    assert nothing["results"] == [] and {n["provider"] for n in nothing["none_on_record"]} == {
        "pip", "eurostat-silc", "oecd-idd"}
    region = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, concept="poverty_headcount",
                                         area={"scheme": "wb-region", "code": "ECA"})
    assert region["results"][0]["welfare_concept"] == "mixed"
    disabled = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, concept="gini", place_id=places["de"],
                                           enabled_providers={"pip"})
    assert {r["provider"] for r in disabled["results"]} == {"pip"}
    assert {f["provider"] for f in disabled["features_disabled"]} == {"eurostat-silc", "oecd-idd"}
    with pytest.raises(IncomeError):
        queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, concept="happiness", place_id=places["de"])
    with pytest.raises(IncomeError):
        queries.indicator_for_place(h.NS, scopes={"knowledge:income:read"}, concept="gini", place_id=places["de"])


def test_series_history_lists_revisions_ppp_revisions_breaks_and_unknown_comparability(world):
    conn, _ = world
    queries = IncomeQueries(conn)
    head = h.series(conn, "pip", "pip:DEU:national:income:headcount", ppp=2017)
    history = queries.series_history(h.NS, head["series_id"], scopes=h.READ_ONLY)
    first, second = history["vintages"]
    assert first["release_at"] == "2098-09-15T00:00:00Z" and second["release_at"] == "2099-03-20T00:00:00Z"
    assert second["changed_periods"] == ["2094"] and second["new_periods"] == ["2097"]
    assert second["revisions"][0]["before"]["value"] == "0.0021" and second["revisions"][0]["after"]["value"] == "0.0022"
    (pair,) = history["pairs"]
    assert pair["comparability"] == "noted"
    assert {n["kind"] for n in pair["notes"]} >= {"ppp_revision"}
    assert all(v["citation"]["vintage_id"] == v["vintage_id"] for v in history["vintages"])

    arop = h.series(conn, "eurostat-silc", "LI_R_MD60")
    (silc_pair,) = queries.series_history(h.NS, arop["series_id"], scopes=h.READ_ONLY)["pairs"]
    assert silc_pair["changed_periods"] == ["2096"] and silc_pair["comparability"] == "comparability_unknown"

    rate = h.series(conn, "oecd-idd", "PR_INC_DISP")
    (removal,) = queries.series_history(h.NS, rate["series_id"], scopes=h.READ_ONLY)["pairs"]
    assert removal["notes"][0]["kind"] == "removed_by_source"
