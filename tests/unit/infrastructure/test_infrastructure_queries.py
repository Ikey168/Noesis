"""CI10 (#2386): assets in a place or under an operator as of a date, with status history and evidence bundles."""

from __future__ import annotations

import duckdb
import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb import infrastructure_assets as ia
from src.kb.geospatial import GeospatialStore
from src.kb.infrastructure_identity import InfrastructureIdentity, party_key
from src.kb.infrastructure_queries import NONE_ON_RECORD, InfrastructureQueries
from tests.unit.infrastructure.harness import NS, OWNERSHIP_NS, SCOPES, acquire, acquire_all, load_ownership

LANDKREIS = [[14.3, 51.5], [14.8, 51.5], [14.8, 51.8], [14.3, 51.8], [14.3, 51.5]]


def register_area(conn, name="Fixture Landkreis", ring=None):
    place = GeospatialStore(conn).register_place(
        NS, name, "district", names=[{"value": name, "language": "de", "kind": "canonical"}],
        source_ids={"fixture": name}, parent_ids=[], principal_id="analyst", scopes=SCOPES,
        geometry={"type": "Polygon", "coordinates": [ring or LANDKREIS]})
    return place["place_id"]


@pytest.fixture()
def env():
    conn = duckdb.connect()
    acquire_all(conn)
    acquire(conn, "gem_coal_plants_release_2")
    identity = InfrastructureIdentity(conn)
    identity.propose_asset_matches(NS, principal_id="analyst", scopes=SCOPES)
    yield conn, identity, InfrastructureQueries(conn)
    conn.close()


def _group(answer, native_id):
    return next(g for g in answer["assets"] if any(v["native_id"] == native_id for v in g["sources"]))


def test_place_as_of_status_history_components_and_conflicts(env):
    conn, identity, queries = env
    place_id = register_area(conn)
    answer = queries.assets_in_place(NS, place_id=place_id, as_of="2026-09-01", scopes=SCOPES, principal_id="analyst")
    assert answer["coverage"]["status"] == "covered" and "de-lusatia" in answer["coverage"]["areas"]
    natives = {v["native_id"] for g in answer["assets"] for v in g["sources"]}
    assert {"DEU9990001", "way/9001", "way/9101", "way/9102", "way/9201", "P0001"} <= natives
    assert "DEU9990002" not in natives and "node/9301" not in natives  # outside the district polygon
    assert {n["provider"] for n in answer["not_located"]} == {"entsog", "osm"}  # ENTSOG points, an OSM relation
    assert answer["spatial_receipts"]
    nord = _group(answer, "DEU9990001")
    assert nord["identity"]["pending_candidates"]  # the OSM outline is a candidate, not yet the same asset
    units = {c["native_id"]: c for c in nord["components"]}
    assert units["G100001"]["status"]["normalized"] == "retired"  # retired 2026 (GEM retired year), as of 2026-09
    assert [s["normalized"] for s in units["G100001"]["status_history"]] == ["operating", "retired"]
    early = queries.assets_in_place(NS, place_id=place_id, as_of="2025-06-01", scopes=SCOPES, principal_id="analyst")
    early_units = {c["native_id"]: c for c in _group(early, "DEU9990001")["components"]}
    assert early_units["G100001"]["status"]["normalized"] == "operating"
    assert early_units["G100002"]["capacities"][0]["value"] == "750"
    # what was known by the January release: no retirement yet
    known = queries.assets_in_place(NS, place_id=place_id, as_of="2026-09-01", known_by="2026-02-01", scopes=SCOPES,
                                    principal_id="analyst")
    assert {c["native_id"]: c for c in _group(known, "DEU9990001")["components"]}["G100001"]["status"][
        "normalized"] == "operating"
    # accepting the candidate groups GPPD and OSM; their capacities stay side by side
    candidate = nord["identity"]["pending_candidates"][0]
    identity.review_asset_match(NS, candidate["match_id"], "accept", "same outline", principal_id="reviewer",
                                scopes=SCOPES)
    grouped = _group(queries.assets_in_place(NS, place_id=place_id, as_of="2026-09-01", scopes=SCOPES,
                                             principal_id="analyst"), "DEU9990001")
    assert {v["provider"] for v in grouped["sources"]} == {"gppd", "osm"}
    conflict = grouped["disagreements"]["capacity"][0]
    assert {(v["provider"], v["value"]) for v in conflict["values"]} == {("gppd", "1500.0"), ("osm", "1450")}
    assert grouped["identity"]["used"]


def test_status_conflicts_bbox_queries_coverage_and_empty_answers(env):
    conn, identity, queries = env
    lng = next(m for m in identity.asset_matches(NS, scopes=SCOPES) if m["left_asset"].startswith("infra-asset")
               and {m["left_asset"], m["right_asset"]} == {ia.asset_id(NS, "eia", "eia:lng-terminals", "lng-terminals:7"),
                                                            ia.asset_id(NS, "gem", "gem:lng-terminals", "T0001")})
    identity.review_asset_match(NS, lng["match_id"], "accept", "same terminal", principal_id="reviewer", scopes=SCOPES)
    gulf = queries.assets_in_place(NS, bbox=[-94.0, 29.5, -93.5, 30.0], scopes=SCOPES, principal_id="analyst")
    terminal = _group(gulf, "T0001")
    assert {(s["provider"], s["published"]) for s in terminal["disagreements"]["status"]} == {
        ("eia", "Operating"), ("gem", "construction")}
    outside = queries.assets_in_place(NS, bbox=[100.0, 10.0, 101.0, 11.0], scopes=SCOPES, principal_id="analyst")
    assert outside["status"] == "not_covered" and outside["assets"] == []
    empty = queries.assets_in_place(NS, bbox=[14.21, 51.31, 14.22, 51.32], scopes=SCOPES, principal_id="analyst")
    assert empty["coverage"]["status"] == "covered" and empty["status"] == NONE_ON_RECORD
    with pytest.raises(ia.InfrastructureError) as caught:
        queries.assets_in_place(NS, place_name="germany", scopes=SCOPES, principal_id="analyst")
    assert caught.value.code in {"place_has_no_area", "place_not_found", "place_ambiguous"}


def test_operator_answers_use_accepted_matches_and_export_evidence_bundles(env):
    conn, identity, queries = env
    load_ownership(conn)
    identity.propose_operator_matches(NS, ownership_namespace=OWNERSHIP_NS, principal_id="analyst", scopes=SCOPES)
    before = queries.assets_of_operator(NS, "lei:529900FIXTURE0000001", scopes=SCOPES)
    assert before["assets"] == [] and before["status"].startswith("unmatched operator")
    candidate = next(c for c in identity.operator_candidates(NS, scopes=SCOPES)
                     if party_key("Fixture Energie AG") in (c["left_key"], c["right_key"]))
    identity.review_operator_match(NS, candidate["candidate_id"], "accept", "QID on both", principal_id="reviewer",
                                   scopes=SCOPES)
    answer = queries.assets_of_operator(NS, "lei:529900FIXTURE0000001", as_of="2026-09-01", scopes=SCOPES)
    natives = {v["native_id"] for g in answer["assets"] for v in g["sources"] + g["components"]}
    assert {"DEU9990001", "way/9001", "G100001", "G100002"} <= natives
    assert answer["operator_matches"][0]["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert {r["name"] for r in answer["roles"]} == {"Fixture Energie AG"}  # as published, never rewritten
    by_name = queries.assets_of_operator(NS, "Stadtwerke Fixture", scopes=SCOPES)
    assert by_name["operator_matches"][0]["basis"].startswith("published name")
    bundle = queries.export_bundle(answer)
    assert verify_bundle(bundle).status in {"valid", "valid_with_external_references"}
    assert len([o for o in bundle["objects"] if o["type"] == "evidence"]) >= 4
    none = queries.export_bundle(queries.assets_of_operator(NS, "lei:529900FIXTURE0000003", scopes=SCOPES))
    checked = verify_bundle(none)
    assert checked.status == "incomplete" and checked.errors == [] and none["completeness"]["omissions"]  # an explicit gap
    history = queries.asset_history(NS, ia.asset_id(NS, "gem", "gem:coal-plants", "G100001"), scopes=SCOPES)
    assert [r["release"]["label"] for r in history["revisions"]] == ["GCPT January 2026", "GCPT July 2026"]
