"""Reviewable place and related-indicator identity for income series (#2583, IP06)."""

from __future__ import annotations

import pytest

from src.kb.income_distribution_identity import IncomeIdentity
from src.kb.income_distribution_records import IncomeError
from tests.unit import income_distribution_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn)
    places = h.register_places(conn, keys=("de", "at", "id", "be", "ssf"))
    return conn, places, IncomeIdentity(conn)


def by_code(assertions):
    return {a["subject"]["code"]: a for a in assertions}


def test_places_match_by_published_codes_and_unmatched_codes_stay_visible(env):
    _conn, places, identity = env
    proposed = by_code(identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES,
                                               geo_namespace="geo")["assertions"])
    assert proposed["DEU"]["method"] == "published-code" and proposed["DEU"]["target"]["place_id"] == places["de"]
    assert proposed["DE"]["method"] == "iso-alpha2-equivalent" and proposed["DE"]["target"]["place_id"] == places["de"]
    assert proposed["DE30"]["method"] == "published-code" and proposed["DE30"]["target"]["place_id"] == places["be"]
    assert proposed["SSF"]["target"]["place_id"] == places["ssf"]
    assert proposed["USA"]["state"] == "unmatched" and proposed["USA"]["target"] is None
    assert {u["code"] for u in identity.unmatched(h.NS)} >= {"USA"}
    # Nothing is used before review.
    assert identity.area_codes_for_place(h.NS, places["de"]) == []
    assert all(a["evidence"]["basis"] == "published identifiers before names" for a in proposed.values())


def test_review_requires_another_reviewer_and_can_be_reverted(env):
    _conn, places, identity = env
    deu = by_code(identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES,
                                          geo_namespace="geo")["assertions"])["DEU"]
    with pytest.raises(IncomeError) as own:
        identity.review(h.NS, deu["assertion_id"], "accept", "code", principal_id="analyst", scopes=h.SCOPES)
    assert own.value.code == "self_review"
    with pytest.raises(IncomeError):
        identity.review(h.NS, deu["assertion_id"], "accept", "code", principal_id="reviewer", scopes=h.READ_ONLY)
    accepted = identity.review(h.NS, deu["assertion_id"], "accept", "ISO code", principal_id="reviewer",
                               scopes=h.SCOPES)
    assert accepted["state"] == "accepted" and accepted["history"][-1]["by"] == "reviewer"
    assert [c["code"] for c in identity.area_codes_for_place(h.NS, places["de"])] == ["DEU"]
    reverted = identity.revert(h.NS, deu["assertion_id"], "wrong place", principal_id="reviewer", scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and identity.area_codes_for_place(h.NS, places["de"]) == []


def test_ambiguous_codes_are_resolved_by_choosing_a_cited_candidate():
    conn = h.connection()
    h.load_all(conn)
    first = h.register_places(conn, keys=("de",))
    from src.kb.geospatial import GeospatialStore

    twin = GeospatialStore(conn).register_place(
        "geo", "Germany (duplicate)", "country", names=[{"value": "Germany", "language": "en"}],
        source_ids={"iso3166-1-alpha3": "DEU"}, parent_ids=[], principal_id="op",
        scopes={"knowledge:geospatial:write"}, place_key="fixture:de-twin")
    identity = IncomeIdentity(conn)
    deu = by_code(identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES,
                                          geo_namespace="geo")["assertions"])["DEU"]
    assert deu["state"] == "ambiguous" and len(deu["evidence"]["candidates"]) == 2
    with pytest.raises(IncomeError):
        identity.review(h.NS, deu["assertion_id"], "accept", "pick", principal_id="reviewer", scopes=h.SCOPES,
                        place_id="place:not-a-candidate")
    chosen = identity.review(h.NS, deu["assertion_id"], "accept", "pick", principal_id="reviewer", scopes=h.SCOPES,
                             place_id=first["de"])
    assert chosen["target"]["place_id"] == first["de"] != twin["place_id"]


def test_the_same_indicator_across_sources_is_related_never_merged(env):
    conn, places, identity = env
    for a in identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace="geo")["assertions"]:
        if a["state"] == "proposed":
            identity.review(h.NS, a["assertion_id"], "accept", "code", principal_id="reviewer", scopes=h.SCOPES)
    related = identity.propose_related(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"]
    gini = [a for a in related if a["target"]["concept"] == "gini_index" and a["target"]["place"] == "place:" +
            places["de"]]
    providers = {frozenset(s["provider"] for s in a["target"]["series"]) for a in gini}
    assert frozenset({"pip", "eu-silc"}) in providers and frozenset({"eu-silc", "oecd-idd"}) in providers
    pair = next(a for a in gini if {s["provider"] for s in a["target"]["series"]} == {"pip", "eu-silc"})
    kinds = {d["kind"] for d in pair["evidence"]["recorded_differences"]}
    assert {"different_source", "different_equivalence_scale", "same_underlying_survey"} <= kinds
    identity.review(h.NS, pair["assertion_id"], "accept", "same concept", principal_id="reviewer", scopes=h.SCOPES)
    pip_series = next(s["series_id"] for s in pair["target"]["series"] if s["provider"] == "pip")
    assert identity.related_series(h.NS, pip_series)[0]["provider"] == "eu-silc"
    assert conn.execute("SELECT count(*) FROM income_series").fetchone()[0] == 17  # nothing merged
