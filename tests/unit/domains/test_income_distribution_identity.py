"""IP06 (#2613): places and related indicators through reviewable identity; nothing auto-merged."""

from __future__ import annotations

import pytest

from src.kb.income_distribution_identity import IncomeIdentity
from src.kb.income_distribution_records import IncomeError
from tests.unit import income_distribution_harness as h


@pytest.fixture()
def world():
    conn = h.connection()
    h.load_all(conn)
    places = h.register_places(conn, keys=("de",))
    return conn, places


def test_places_are_proposed_by_published_identifier_and_unmatched_stay_visible(world):
    conn, _ = world
    identity = IncomeIdentity(conn)
    result = identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)
    by_subject = {(a["subject"]["scheme"], a["subject"]["code"]): a for a in result["proposed"]}
    assert set(by_subject) == {("iso3166-1-alpha3", "DEU"), ("eurostat-geo", "DE")}
    assert by_subject[("eurostat-geo", "DE")]["method"] == "iso-alpha2-equivalent"
    assert by_subject[("iso3166-1-alpha3", "DEU")]["evidence"]["identifier"] == {"key": "iso3166-1-alpha3",
                                                                                 "value": "DEU"}
    assert all(a["state"] == "proposed" and a["confidence"] == "high" for a in result["proposed"])
    assert [u["code"] for u in result["unmatched"]] == ["ECA"]  # no place carries the region code
    # Nothing is accepted without review, so every area is still unmatched for queries.
    assert {u["code"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)} == {"DE", "DEU", "ECA"}
    assert identity.place_for_area(h.NS, "iso3166-1-alpha3", "DEU") is None


def test_review_accepts_by_another_principal_records_a_decision_and_reverts(world):
    conn, places = world
    identity = IncomeIdentity(conn)
    proposed = identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)["proposed"]
    assertion = next(a for a in proposed if a["subject"]["code"] == "DEU")
    with pytest.raises(IncomeError) as caught:
        identity.review(h.NS, assertion["assertion_id"], "accept", "own", principal_id="proposer", scopes=h.SCOPES)
    assert caught.value.code == "self_review"
    accepted = identity.review(h.NS, assertion["assertion_id"], "accept", "ISO alpha-3 identifier",
                               principal_id="reviewer", scopes=h.SCOPES)
    assert accepted["state"] == "accepted" and accepted["decision_id"]
    assert conn.execute("SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
                        [accepted["decision_id"]]).fetchone()[0] == "match"
    assert identity.place_for_area(h.NS, "iso3166-1-alpha3", "DEU")["place_id"] == places["de"]
    reverted = identity.revert(h.NS, assertion["assertion_id"], "wrong place", principal_id="reviewer",
                               scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]] == [
        "proposed", "accepted", "reverted"]
    assert identity.place_for_area(h.NS, "iso3166-1-alpha3", "DEU") is None


def test_ambiguous_codes_need_a_chosen_candidate():
    conn = h.connection()
    h.apply(conn, "oecd", retrieved_at_ms=h.FIRST_RETRIEVAL)
    first = h.register_places(conn, keys=("de",))
    from src.kb.geospatial import GeospatialStore

    second = GeospatialStore(conn).register_place(
        "geo", "Germany (duplicate gazetteer entry)", "country", names=[{"value": "Germany", "language": "en"}],
        source_ids={"iso3166-1-alpha3": "DEU"}, parent_ids=[], principal_id="op",
        scopes={"knowledge:geospatial:write"}, place_key="fixture:de-2")
    identity = IncomeIdentity(conn)
    (assertion,) = identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)["ambiguous"]
    assert assertion["state"] == "ambiguous" and len(assertion["candidates"]) == 2
    with pytest.raises(IncomeError):
        identity.review(h.NS, assertion["assertion_id"], "accept", "pick", principal_id="reviewer", scopes=h.SCOPES)
    accepted = identity.review(h.NS, assertion["assertion_id"], "accept", "the national gazetteer entry",
                               principal_id="reviewer", scopes=h.SCOPES, place_id=first["de"])
    assert accepted["target"]["place_id"] == first["de"] != second["place_id"]


def test_the_same_indicator_across_sources_is_related_never_merged(world):
    conn, _ = world
    identity = IncomeIdentity(conn)
    for assertion in identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)["proposed"]:
        identity.review(h.NS, assertion["assertion_id"], "accept", "identifier", principal_id="reviewer",
                        scopes=h.SCOPES)
    related = identity.propose_related(h.NS, principal_id="proposer", scopes=h.SCOPES)["proposed"]
    pairs = {tuple(sorted(conn.execute("SELECT provider FROM income_series WHERE series_id IN (?, ?)",
                                       a["subject"]["series_ids"]).fetchall())) for a in related}
    assert (("eurostat-silc",), ("oecd-idd",)) in pairs and (("eurostat-silc",), ("pip",)) in pairs
    for gini in (a for a in related if a["subject"]["concept"] == "gini"):
        assert gini["evidence"]["merge"] is False and gini["confidence"] == "related-not-equal"
        assert {d["field"] for d in gini["evidence"]["differences"]} >= {"provider", "equivalence_scale"}
    # Each series keeps its own vintages: nothing was combined.
    assert conn.execute("SELECT count(DISTINCT series_id) FROM income_vintages").fetchone()[0] == \
        conn.execute("SELECT count(*) FROM income_series").fetchone()[0]
