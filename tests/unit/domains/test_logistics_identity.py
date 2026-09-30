"""Source port codes to UN/LOCODE and places through reviewable identity (#2229, SL07 #2542)."""

from __future__ import annotations

import pytest

from src.kb.geospatial import GeospatialStore
from src.kb.logistics_ports import LogisticsPorts
from src.kb.logistics_records import LogisticsError
from src.kb.logistics_series import LogisticsStore
from tests.unit import logistics_harness as h


@pytest.fixture()
def loaded():
    conn = h.connection()
    for name in ("unlocode", "unctad", "eurostat"):
        h.apply(conn, name, retrieved_at_ms=h.FIRST_RETRIEVAL)
    ports = LogisticsPorts(conn, now=lambda: h.FIRST_RETRIEVAL)
    ports.import_crosswalk(h.NS, h.CROSSWALK, principal_id="operator-1", scopes=h.SCOPES)
    return conn, ports


def _by_source(ports):
    return {(m["source"]["scheme"], m["source"]["code"]): m for m in ports.matches(h.NS)}


def test_published_crosswalks_and_embedded_unlocodes_are_exact_and_names_are_candidates(loaded):
    conn, ports = loaded
    result = ports.propose_matches(h.NS, principal_id="svc", scopes=h.SCOPES)
    matches = _by_source(ports)
    hamburg_unctad = matches[("unctad-port", "1101")]
    assert (hamburg_unctad["unlocode"], hamburg_unctad["basis"], hamburg_unctad["exact"], hamburg_unctad["state"]) == (
        "DEHAM", "embedded-unlocode", True, "accepted")
    hamburg_eurostat = matches[("eurostat-port", "DE001")]
    assert hamburg_eurostat["basis"] == "published-crosswalk" and hamburg_eurostat["exact"] is True
    assert hamburg_eurostat["evidence"]["citation"]["url"].startswith("https://ec.europa.eu/")
    for source in (("unctad-port", "1102"), ("eurostat-port", "DE003"), ("unctad-port", "1103"),
                   ("eurostat-port", "NL002")):
        assert matches[source]["basis"] == "name" and matches[source]["exact"] is False
        assert matches[source]["state"] == "proposed" and matches[source]["used_in_answers"] is False
    assert [u["code"] for u in result["unmatched"]] == ["DE999"]
    # A second run adds nothing.
    assert ports.propose_matches(h.NS, principal_id="svc", scopes=h.SCOPES)["created"] == 0
    # Only accepted matches resolve; a pending candidate does not.
    assert ports.resolve_source(h.NS, "eurostat-port", "DE003")["status"] == "unmatched"
    assert ports.resolve_source(h.NS, "eurostat-port", "DE001") == {
        "status": "matched", "unlocode": "DEHAM", "basis": "published-crosswalk", "exact": True,
        "match_id": hamburg_eurostat["match_id"]}


def test_review_accepts_or_rejects_candidates_and_rejected_or_unmatched_codes_stay_on_their_own_code(loaded):
    conn, ports = loaded
    ports.propose_matches(h.NS, principal_id="svc", scopes=h.SCOPES)
    matches = _by_source(ports)
    with pytest.raises(LogisticsError) as denied:
        ports.review(h.NS, matches[("eurostat-port", "DE003")]["match_id"], "accept", "same port",
                     principal_id="bob", scopes=h.READ_ONLY)
    assert denied.value.code == "unauthorized"
    accepted = ports.review(h.NS, matches[("eurostat-port", "DE003")]["match_id"], "accept",
                            "Eurostat labels DE003 Bremerhaven; UN/LOCODE DEBRV is Bremerhaven",
                            principal_id="reviewer", scopes=h.SCOPES)
    assert accepted["state"] == "accepted" and accepted["decided_by"] == "reviewer"
    rejected = ports.review(h.NS, matches[("eurostat-port", "NL002")]["match_id"], "reject",
                            "partner-port code list not yet verified", principal_id="reviewer", scopes=h.SCOPES)
    assert rejected["state"] == "rejected" and rejected["used_in_answers"] is False
    assert ports.resolve_source(h.NS, "eurostat-port", "NL002")["status"] == "unmatched"
    with pytest.raises(LogisticsError):
        ports.review(h.NS, rejected["match_id"], "accept", "changed my mind", principal_id="reviewer",
                     scopes=h.SCOPES)
    assert {c["code"] for c in ports.source_codes(h.NS, "DEBRV")} == {"DE003"}
    reverted = ports.revert(h.NS, accepted["match_id"], "wrong terminal", principal_id="reviewer", scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and ports.source_codes(h.NS, "DEBRV") == []
    # The unmatched source code is still queryable by its own code.
    store = LogisticsStore(conn)
    (other,) = store.find_series(h.NS, codes=[("eurostat-port", "DE999")])
    assert other["geography"]["label"] == "Other ports (DE)"


def test_unlocode_changes_and_removals_record_a_rematch_and_never_repoint(loaded):
    conn, ports = loaded
    ports.propose_matches(h.NS, principal_id="svc", scopes=h.SCOPES)
    matches = _by_source(ports)
    ports.review(h.NS, matches[("unctad-port", "1103")]["match_id"], "accept", "UNCTAD label equals the name",
                 principal_id="reviewer", scopes=h.SCOPES)
    h.apply(conn, "unlocode", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    after = _by_source(ports)
    hamburg = after[("unctad-port", "1101")]
    assert hamburg["state"] == "accepted" and hamburg["port_revision"] == 2
    assert hamburg["rematches"][0]["trigger"] == "changed" and "kept" in hamburg["rematches"][0]["outcome"]
    wilhelmshaven = after[("unctad-port", "1103")]
    assert wilhelmshaven["state"] == "target-removed" and wilhelmshaven["rematches"][0]["trigger"] == "removed"
    assert ports.resolve_source(h.NS, "unctad-port", "1103")["status"] == "unmatched"
    # Rejected or untouched matches get no rematch record.
    assert after[("unctad-port", "1102")]["rematches"] == []


def test_ports_are_projected_as_places_with_points_only_when_coordinates_are_published(loaded):
    conn, ports = loaded
    with pytest.raises(LogisticsError):
        ports.project_places(h.NS, principal_id="svc", scopes={"knowledge:logistics:write", "namespace:global:write"})
    result = ports.project_places(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert set(result["places"]) == {"DEBRV", "DEEME", "DEHAM", "DEWVN", "NLRTM"}
    assert result["without_published_coordinates"] == ["DEEME"]
    geo = GeospatialStore(conn)
    hamburg = geo.place("global", result["places"]["DEHAM"], scopes={"knowledge:geospatial:read"})
    assert hamburg["source_ids"] == {"unlocode": "DEHAM"} and hamburg["place_type"] == "port"
    points = dict(conn.execute("SELECT place_id, coordinates_json FROM geospatial_geometries").fetchall())
    ours = {code: points.get(place) for code, place in result["places"].items()}
    assert ours["DEEME"] is None and sum(1 for v in ours.values() if v) == 4
    assert ours["DEHAM"].replace(" ", "") == "[9.966667,53.55]"
    assert ports.place_id("DEHAM") == result["places"]["DEHAM"]
    # Exact matches proposed after the projection carry the place.
    ports.propose_matches(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert _by_source(ports)[("unctad-port", "2201")]["place_id"] == result["places"]["NLRTM"]
