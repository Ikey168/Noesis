"""CI07-CI09 (#2377, #2379, #2382): asset reconciliation, operator matching and citation links."""

from __future__ import annotations

import duckdb
import pytest

from src.kb import infrastructure_assets as ia
from src.kb.infrastructure_identity import InfrastructureIdentity, party_key
from src.kb.ownership_store import OwnershipError
from tests.unit.infrastructure.harness import (
    ENV_SCOPES,
    NS,
    OWNERSHIP_NS,
    SCOPES,
    acquire_all,
    load_environment,
    load_legal,
    load_ownership,
)


def aid(provider, dataset, native_id):
    return ia.asset_id(NS, provider, dataset, native_id)


GPPD_NORD = aid("gppd", "gppd:global_power_plant_database", "DEU9990001")
GPPD_SUED = aid("gppd", "gppd:global_power_plant_database", "DEU9990002")
OSM_NORD = aid("osm", "osm:overpass", "way/9001")
OSM_SUED = aid("osm", "osm:overpass", "way/9002")
UNIT_A = aid("gem", "gem:coal-plants", "G100001")
UNIT_B = aid("gem", "gem:coal-plants", "G100002")
EIA_LNG = aid("eia", "eia:lng-terminals", "lng-terminals:7")
GEM_LNG = aid("gem", "gem:lng-terminals", "T0001")


@pytest.fixture()
def conn():
    connection = duckdb.connect()
    acquire_all(connection)
    yield connection
    connection.close()


def _pair(matches, left, right):
    return next(m for m in matches if {m["left_asset"], m["right_asset"]} == {left, right})


def test_identifier_matches_candidates_and_reversible_reviews(conn):
    identity = InfrastructureIdentity(conn)
    matches = identity.propose_asset_matches(NS, principal_id="analyst", scopes=SCOPES)["matches"]
    exact = _pair(matches, GPPD_SUED, OSM_SUED)  # OSM ref:gppd names the GPPD plant
    assert exact["basis"] == "identifier" and exact["state"] == "accepted" and exact["relation"] == "same_asset"
    assert exact["evidence"]["shared"][0]["scheme"] == "gppd_idnr"
    component = _pair(matches, UNIT_A, GPPD_NORD)  # a GEM unit citing a GPPD plant is a component, not the plant
    assert component["relation"] == "component_of" and component["left_asset"] == UNIT_A
    assert not [m for m in matches if {m["left_asset"], m["right_asset"]} == {UNIT_A, UNIT_B}]
    candidate = _pair(matches, GPPD_NORD, OSM_NORD)
    assert candidate["basis"] == "proximity-name-class" and candidate["state"] == "candidate"
    assert 0 < candidate["distance_m"] < 2000 and candidate["evidence"]["spatial_receipt_id"]
    lng = _pair(matches, EIA_LNG, GEM_LNG)
    assert lng["state"] == "candidate" and lng["evidence"]["name_overlap"] >= 0.5
    assert identity.propose_asset_matches(NS, principal_id="analyst", scopes=SCOPES)["proposed"] == []
    with pytest.raises(ia.InfrastructureError) as caught:
        identity.review_asset_match(NS, candidate["match_id"], "accept", "same plant", principal_id="analyst",
                                    scopes=SCOPES)
    assert caught.value.code == "self_review"
    assert identity.cluster(NS, GPPD_NORD, scopes=SCOPES)["same"] == [GPPD_NORD]
    identity.review_asset_match(NS, candidate["match_id"], "accept", "same outline and name", principal_id="reviewer",
                                scopes=SCOPES)
    cluster = identity.cluster(NS, GPPD_NORD, scopes=SCOPES)
    assert set(cluster["same"]) == {GPPD_NORD, OSM_NORD}
    assert {m["left_asset"] for m in cluster["components"]} == {UNIT_A, UNIT_B}
    reverted = identity.review_asset_match(NS, candidate["match_id"], "revert", "second look", principal_id="reviewer",
                                           scopes=SCOPES)
    assert reverted["state"] == "reverted" and [r["state"] for r in reverted["reviews"]] == ["accepted", "reverted"]
    assert identity.cluster(NS, GPPD_NORD, scopes=SCOPES)["same"] == [GPPD_NORD]
    identity.review_asset_match(NS, exact["match_id"], "reject", "different plant", principal_id="reviewer",
                                scopes=SCOPES)
    assert identity.cluster(NS, GPPD_SUED, scopes=SCOPES)["same"] == [GPPD_SUED]
    assert ia.InfrastructureStore(conn).revisions(NS, OSM_SUED, scopes=SCOPES)  # records stay intact


def test_operator_identifier_candidate_and_rejected_matches(conn):
    load_ownership(conn)
    identity = InfrastructureIdentity(conn)
    result = identity.propose_operator_matches(NS, ownership_namespace=OWNERSHIP_NS, principal_id="analyst",
                                               scopes=SCOPES)
    by_party = {}
    for item in result["offered"]:
        by_party.setdefault(item["party_key"], []).append(item)
    energie = party_key("Fixture Energie AG")
    assert {i["basis"] for i in by_party[energie]} == {"cross-referenced-identifier"}  # OSM operator:wikidata
    assert by_party[party_key("Fixture Gastransport GmbH")][0]["basis"] == "name-jurisdiction"
    assert by_party[party_key("Fixture Holding SE")][0]["basis"] == "similar-name"  # LU entity, German assets
    assert party_key("Stadtwerke Fixture") in result["unmatched"]
    candidates = identity.operator_candidates(NS, scopes=SCOPES)
    energie_candidate = next(c for c in candidates if energie in (c["left_key"], c["right_key"]))
    accepted = identity.review_operator_match(NS, energie_candidate["candidate_id"], "accept", "QID on both",
                                              principal_id="reviewer", scopes=SCOPES)
    assert accepted["state"] == "accepted" and accepted["decision_id"]
    parties = identity.operator_parties(NS, "lei:529900FIXTURE0000001", scopes=SCOPES)
    assert [p["party_key"] for p in parties] == [energie]
    holding = next(c for c in candidates if party_key("Fixture Holding SE") in (c["left_key"], c["right_key"]))
    with pytest.raises(OwnershipError):
        identity.review_operator_match(NS, holding["candidate_id"], "accept", "same name", principal_id="reviewer",
                                       scopes=SCOPES)
    rejected = identity.review_operator_match(NS, holding["candidate_id"], "reject", "different company",
                                              principal_id="reviewer", scopes=SCOPES)
    assert rejected["state"] == "rejected"
    party = next(p for p in identity.parties(NS, scopes=SCOPES) if p["party_key"] == party_key("Fixture Holding SE"))
    assert {(o["name"], o["share"]) for o in party["occurrences"]} == {("Fixture Holding SE", "60")}
    from src.kb.ownership_identity import OwnershipIdentityService

    assert not [k for k in OwnershipIdentityService(conn).clusters(NS) if k.startswith("infrastructure:")]


def test_operator_matching_skips_cleanly_without_corporate_ownership(conn):
    result = InfrastructureIdentity(conn).propose_operator_matches(NS, ownership_namespace=OWNERSHIP_NS,
                                                                   principal_id="analyst", scopes=SCOPES)
    assert "not installed" in result["skipped"] and result["offered"] == []


def test_citation_links_optional_packs_and_candidates(conn):
    identity = InfrastructureIdentity(conn)
    for result in (identity.link_energy(NS, energy_namespace="energy", principal_id="a", scopes=SCOPES),
                   identity.link_legal(NS, legal_namespace="legal", principal_id="a", scopes=SCOPES),
                   identity.link_environment(NS, environment_namespace="environment", principal_id="a",
                                             scopes=SCOPES)):
        assert "not installed" in result["skipped"]
    from tests.unit.energy.harness import acquire as energy_acquire

    energy_acquire(conn, "energy-eia-capacity")
    assert identity.link_energy(NS, energy_namespace="energy", principal_id="a", scopes=SCOPES)["linked"]
    links = identity.links(NS, aid("eia", "eia:power-plants", "plant:99901"), scopes=SCOPES)["links"]
    assert links and all(link["state"] == "linked" and link["target"]["owner"] == "energy" for link in links)
    assert links[0]["citation"]["citing_revision_id"] and "eia_plant_code=99901" in links[0]["citation"]["locator"]
    load_legal(conn)
    legal = identity.link_legal(NS, legal_namespace="legal", principal_id="a", scopes=SCOPES)
    assert len(legal["linked"]) == 1 and legal["unresolved"] == []
    load_environment(conn)
    environment = identity.link_environment(NS, environment_namespace="environment", principal_id="a",
                                            scopes=SCOPES | ENV_SCOPES)
    assert environment["linked"] and environment["candidates"]
    candidate = next(link for link in identity.links(NS, scopes=SCOPES)["links"]
                     if link["link_id"] in environment["candidates"])
    assert candidate["state"] == "candidate" and "not a citation" in candidate["citation"]["basis"]
    reviewed = identity.review_link(NS, candidate["link_id"], "reject", "different site", principal_id="reviewer",
                                    scopes=SCOPES)
    assert reviewed["state"] == "rejected"
    with pytest.raises(ia.InfrastructureError) as caught:
        identity.link_cited(NS, GPPD_NORD, {"owner": "legal", "kind": "legal-work", "id": "x"},
                            citation={"source": "s", "locator": "l", "basis": "proximity"}, principal_id="a",
                            scopes=SCOPES)
    assert caught.value.code == "inferred_link_refused"
