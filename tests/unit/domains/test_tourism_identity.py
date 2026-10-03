"""TO05 (#2739): countries and NUTS regions match Geospatial places through reviewed, versioned identity."""

from __future__ import annotations

import pytest

from src.kb.tourism_identity import TourismIdentity
from src.kb.tourism_records import TourismError
from tests.unit import tourism_harness as h


@pytest.fixture()
def conn():
    conn = h.connection()
    h.load_all(conn, revisions=True, nuts2024=True)
    return conn


def _by_key(assertions):
    return {(a["subject"]["nuts_version"], a["subject"]["code"]): a for a in assertions if a["kind"] == "area"}


def test_place_keys_match_by_published_code_and_version_and_nothing_is_auto_merged(conn):
    places = h.register_places(conn, keys=("de", "berlin"))
    identity = TourismIdentity(conn)
    proposed = _by_key(identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"])
    assert set(proposed) == {("2021", "DE"), ("2021", "DE30"), ("2024", "DE30")}
    germany, berlin = proposed[("2021", "DE")], proposed[("2021", "DE30")]
    assert germany["state"] == "proposed" and germany["method"] == "iso-alpha2-equivalent"
    assert berlin["state"] == "proposed" and berlin["method"] == "published-code"
    assert (germany["confidence"], berlin["confidence"]) == ("medium", "medium")
    assert berlin["target"]["place_id"] == places["berlin"]
    assert "states NUTS 2021" in berlin["evidence"]["candidates"][0]["evidence"]["rule"]
    # The NUTS 2024 key is a different place key: the 2021 place does not match it, so it stays visibly unmatched.
    unmatched = proposed[("2024", "DE30")]
    assert unmatched["state"] == "unmatched" and unmatched["target"] is None and unmatched["confidence"] is None
    assert unmatched in identity.unmatched(h.NS, scopes=h.READ_ONLY)
    # Nothing is used until reviewed.
    assert identity.area_keys_for_place(h.NS, places["berlin"]) == []
    with pytest.raises(TourismError):
        identity.review(h.NS, berlin["assertion_id"], "accept", "", principal_id="reviewer", scopes=h.SCOPES)
    accepted = identity.review(h.NS, berlin["assertion_id"], "accept", "published NUTS code DE30",
                               principal_id="reviewer", scopes=h.SCOPES)
    assert accepted["state"] == "accepted" and accepted["history"][-1]["by"] == "reviewer"
    (key,) = identity.area_keys_for_place(h.NS, places["berlin"])
    assert (key["nuts_version"], key["code"]) == ("2021", "DE30")
    reverted = identity.revert(h.NS, berlin["assertion_id"], "wrong version", principal_id="reviewer",
                               scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and identity.area_keys_for_place(h.NS, places["berlin"]) == []
    # Re-proposing after a revert is idempotent and keeps the history.
    again = _by_key(identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"])
    assert again[("2021", "DE30")]["state"] in {"proposed", "reverted"}


def test_codes_come_before_names_and_ambiguity_needs_a_reviewer(conn):
    from src.kb.geospatial import GeospatialStore

    h.register_places(conn, keys=("berlin",))
    # A place named Berlin without the code never matches; a second place with the code makes the key ambiguous.
    geo = GeospatialStore(conn)
    geo.register_place(h.NS, "Berlin", "city", names=[{"value": "Berlin", "language": "en"}], source_ids={},
                       parent_ids=[], principal_id="op", scopes={"knowledge:geospatial:write"}, place_key="name-only")
    twin = geo.register_place(h.NS, "Land Berlin", "region", names=[{"value": "Land Berlin", "language": "de"}],
                              source_ids={"nuts": "DE30"}, parent_ids=[], principal_id="op",
                              scopes={"knowledge:geospatial:write"}, place_key="twin")
    identity = TourismIdentity(conn)
    proposed = _by_key(identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"])
    berlin = proposed[("2021", "DE30")]
    assert berlin["state"] == "ambiguous" and len(berlin["evidence"]["candidates"]) == 2
    assert berlin["confidence"] == "low"
    with pytest.raises(TourismError):
        identity.review(h.NS, berlin["assertion_id"], "accept", "pick", principal_id="reviewer", scopes=h.SCOPES,
                        place_id="not-a-candidate")
    chosen = identity.review(h.NS, berlin["assertion_id"], "accept", "the Land is the NUTS 2 region",
                             principal_id="reviewer", scopes=h.SCOPES, place_id=twin["place_id"])
    assert chosen["target"]["place_id"] == twin["place_id"]


def test_a_nuts_change_is_linked_only_through_the_published_correspondence(conn):
    identity = TourismIdentity(conn)
    assert identity.propose_correspondence_links(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"] == []
    table = h.correspondence_tables()[0]
    with pytest.raises(TourismError):
        identity.import_correspondence(h.NS, {**table, "citation": {}}, principal_id="op", scopes=h.SCOPES)
    imported = identity.import_correspondence(h.NS, table, principal_id="op", scopes=h.SCOPES)
    assert imported["citation"]["publisher"] == "Eurostat (GISCO)"
    proposed = identity.propose_correspondence_links(h.NS, principal_id="analyst", scopes=h.SCOPES)
    links = {tuple(p["code"] for p in a["subject"]["pair"]): a for a in proposed["assertions"]}
    assert set(links) == {("DE", "DE"), ("DE30", "DE30")}
    link = links[("DE30", "DE30")]
    assert link["state"] == "proposed" and link["relation"] == "unchanged" and link["target"]["merge"] is False
    assert link["method"] == "published-nuts-correspondence" and link["confidence"] == "high"
    assert [p["nuts_version"] for p in link["subject"]["pair"]] == ["2021", "2024"]
    old_key = {"scheme": "eurostat-geo", "nuts_version": "2021", "code": "DE30"}
    assert identity.corresponding_keys(h.NS, old_key) == []  # not used until accepted
    identity.review(h.NS, link["assertion_id"], "accept", "Eurostat correspondence row", principal_id="reviewer",
                    scopes=h.SCOPES)
    (other,) = identity.corresponding_keys(h.NS, old_key)
    assert (other["nuts_version"], other["code"], other["relation"]) == ("2024", "DE30", "unchanged")
    assert other["citation"]["url"].startswith("https://ec.europa.eu/")
