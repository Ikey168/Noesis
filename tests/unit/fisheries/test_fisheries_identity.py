"""Reviewable vessel identity (FI07, #2323) and citation links to sanctions, areas and other packs (FI08, #2326)."""

from __future__ import annotations

import pytest

from src.kb.fisheries_identity import (
    FisheriesIdentity,
    FisheriesLinks,
    country_entity,
    organisation_name,
    register_citing_provider,
)
from src.kb.fisheries_records import FisheriesError, statement
from tests.unit.fisheries import harness as h

NS = h.NS
GFW_CLEAN = "gfw:vessel:a1b2c3d4-0001-4000-8000-000000000001"
GFW_REFLAGGED = "gfw:vessel:a1b2c3d4-0002-4000-8000-000000000002"


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    item.identity = FisheriesIdentity(item.conn, now=lambda: next(item.clock))
    item.links = FisheriesLinks(item.conn, now=lambda: next(item.clock))
    yield item
    item.conn.close()


def _pair(matches, a, b):
    return next(m for m in matches if {m["left_key"], m["right_key"]} == {a, b})


def test_imo_matches_are_recorded_with_evidence_and_name_only_pairs_need_review(env):
    result = env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    matches = result["matches"]
    imo = _pair(matches, GFW_CLEAN, "iccat:vessel:AT000ESP00001")
    assert imo["basis"] == "imo" and imo["state"] == "accepted" and not imo["review_required"]
    assert imo["evidence"]["imo"] == h.IMO_CLEAN and imo["evidence"]["left_revision"]
    assert imo["decision_id"]
    name_only = _pair(matches, "iccat:vessel:AT000ESP00001", "wcpfc:vessel:WCPFC-KIR-0007")
    assert name_only["basis"] == "name-only" and name_only["state"] == "proposed" and name_only["review_required"]
    ghost = _pair(matches, "combined-iuu:iuu-entry:CIUU-0103", "wcpfc:iuu-entry:WCPFC-IUU-2010-01")
    assert ghost["basis"] == "name-only" and ghost["state"] == "proposed"
    members = env.identity.members(NS, GFW_CLEAN)
    assert set(members) == {GFW_CLEAN, "iccat:vessel:AT000ESP00001", "iotc:vessel:IOTC000101"}
    assert "wcpfc:vessel:WCPFC-KIR-0007" not in members  # never joined without review
    # The malformed IMO is never used for matching.
    assert not [m for m in matches if "iotc:vessel:IOTC000102" in (m["left_key"], m["right_key"])
                and m["basis"] == "imo"]
    replay = env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    assert replay["recorded_or_proposed"] == []


def test_reviews_are_recorded_and_reversible_and_records_are_never_merged(env):
    env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    candidate = _pair(env.identity.matches(NS, scopes=h.READ), "iccat:vessel:AT000ESP00001",
                      "wcpfc:vessel:WCPFC-KIR-0007")
    with pytest.raises(FisheriesError):
        env.identity.review(NS, candidate["match_id"], "accept", "", principal_id="reviewer", scopes=h.REVIEW)
    rejected = env.identity.review(NS, candidate["match_id"], "reject", "different flag and call sign",
                                   principal_id="reviewer", scopes=h.REVIEW)
    assert rejected["state"] == "rejected" and rejected["reviewer"] == "reviewer"
    reverted = env.identity.revert(NS, candidate["match_id"], "reconsider", principal_id="reviewer",
                                   scopes=h.REVIEW)
    assert reverted["state"] == "reverted"
    accepted = env.identity.review(NS, candidate["match_id"], "accept", "owner confirmed same hull",
                                   principal_id="reviewer", scopes=h.REVIEW)
    assert accepted["state"] == "accepted"
    assert "wcpfc:vessel:WCPFC-KIR-0007" in env.identity.members(NS, GFW_CLEAN)
    assert [h_["state"] for h_ in accepted["history"]] == ["proposed", "rejected", "reverted", "accepted"]
    imo = _pair(env.identity.matches(NS, scopes=h.READ), GFW_CLEAN, "iccat:vessel:AT000ESP00001")
    undone = env.identity.revert(NS, imo["match_id"], "wrong hull", principal_id="reviewer", scopes=h.REVIEW)
    assert undone["state"] == "reverted" and undone["history"][-1]["decision_id"] != imo["decision_id"]
    for other in [m for m in env.identity.matches(NS, scopes=h.READ, subject_key=GFW_CLEAN)
                  if m["state"] == "accepted"]:
        env.identity.revert(NS, other["match_id"], "wrong hull", principal_id="reviewer", scopes=h.REVIEW)
    assert env.identity.members(NS, GFW_CLEAN) == [GFW_CLEAN]
    # Both records keep every revision; nothing was rewritten.
    assert env.store.records(NS, provider="wcpfc") and env.store.records(NS, provider="gfw")


def test_renames_and_re_flagging_are_a_time_bounded_identity_history(env):
    env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    vessel = env.identity.vessel(NS, GFW_REFLAGGED, scopes=h.READ)
    assert set(vessel["members"]) == {GFW_REFLAGGED, "iccat:vessel:AT000GHA00002",
                                      "iotc:iuu-entry:IOTC-IUU-2025-01", "combined-iuu:iuu-entry:CIUU-0101"}
    assert {m["basis"] for m in vessel["matches"]} == {"imo"}
    segments = vessel["history"]["segments"]
    assert [(s["name"], s["flag"]) for s in segments] == [("SAMPLE STAR", "GHA"), ("SAMPLE NOVA", "TGO")]
    assert segments[1]["change"] == ["renamed", "re-flagged"] and segments[1]["from"] == "2025-01-15"
    assert segments[0]["flag_entity"]["entity_id"] == "ent-country-gh"
    assert all(c["revision_id"] for s in segments for c in s["citations"])
    assert vessel["history"]["previous_identities"][0]["names"] == ["SAMPLE STAR"]
    before = env.identity.identity_history(NS, vessel["members"], as_of="2024-06-30")
    assert [s["name"] for s in before["segments"]] == ["SAMPLE STAR"]


def test_conflicting_imo_numbers_are_never_proposed(env):
    src = {"url": "https://iotc.org/vessels/export/rav.csv", "locator": "/row/9", "attribution": "Source: IOTC",
           "list": "authorised-vessels", "snapshot_date": "2026-09-02", "evidence_origin": "fixture"}
    twin = statement("authorisation", "iotc", {"key": "iotc:vessel:IOTC000999", "kind": "vessel",
                                               "name": "SAMPLE STAR"}, "authorisation:IOTC000999",
                     {"register": "IOTC Record of Authorised Vessels", "register_number": "IOTC000999",
                      "vessel_name": "SAMPLE STAR", "flag": "GHA", "call_sign": "9GZZ9", "imo": h.IMO_LATE,
                      "gear": "PS", "valid_from": "2025-01-01", "valid_to": "2026-12-31"}, source=src,
                     event="authorised", effective_from="2025-01-01")
    env.store.apply(NS, twin)
    result = env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    conflict = [c for c in result["conflicts"] if "iotc:vessel:IOTC000999" in c["subjects"]
                and "iccat:vessel:AT000GHA00002" in c["subjects"]]
    assert conflict and conflict[0]["left_imo"] != conflict[0]["right_imo"]
    assert not [m for m in result["matches"] if {m["left_key"], m["right_key"]} ==
                {"iotc:vessel:IOTC000999", "iccat:vessel:AT000GHA00002"}]


def test_flags_resolve_from_iso_codes_and_natural_persons_are_not_resolved(env):
    assert country_entity("ESP")["entity_id"] == "ent-country-es"
    assert country_entity("Unknown")["status"] == "unresolved"
    assert organisation_name("Sample Pesquerias S.L.") and not organisation_name("Jean Sample")
    parties = env.identity.resolve_parties(NS, ["iotc:vessel:IOTC000102", "iccat:vessel:AT000ESP00001"])
    owners = {o["published"]: o for o in parties["owners"]}
    assert owners["Jean Sample"]["status"] == "not_resolved" and "natural person" in owners["Jean Sample"]["reason"]
    assert owners["Sample Pesquerias S.L."]["status"] == "no_canonical_entity"
    assert {f["iso2"] for f in parties["flags"] if f["status"] == "resolved"} == {"ES", "SC"}
    assert env.conn.execute("SELECT entity_type FROM canonical_entities WHERE canonical_id='ent-country-es'"
                            ).fetchone()[0] == "country"


def test_sanctions_links_need_a_stated_imo_and_name_only_is_a_candidate(env):
    missing = env.links.link_sanctions(NS, scopes=h.ALL, principal_id="linker")
    assert missing["status"] == "provider_unavailable" and missing["linked"] == []
    h.seed_sanctions(env.conn)
    linked = env.links.link_sanctions(NS, scopes=h.ALL, principal_id="linker")
    assert {link["subject_key"] for link in linked["linked"]} == {"iccat:iuu-entry:20160001",
                                                                  "combined-iuu:iuu-entry:CIUU-0102"}
    assert {link["matched"] for link in linked["linked"]} == {f"imo:{h.IMO_DELISTED}"}
    stored = env.links.links(NS, ["iccat:iuu-entry:20160001"], scopes=h.READ, owner="sanctions")
    assert stored[0]["target_revision"] and stored[0]["locator"]["list_id"] == "ofac"
    names = {c["subject_key"] for c in linked["name_only"]}
    assert "iccat:vessel:AT000ESP00001" in names  # SAMPLE ALBACORA UNO shares a designation's name only
    assert not env.links.links(NS, ["iccat:vessel:AT000ESP00001"], scopes=h.READ, owner="sanctions")
    with pytest.raises(FisheriesError):
        env.links.link_sanctions(NS, scopes=h.WRITE, principal_id="linker")  # sanctions read scope required


def test_area_links_use_published_codes_and_grid_cells_only(env):
    before = env.links.link_areas(NS, scopes=h.ALL, principal_id="linker")
    assert before["linked"] == [] and before["status"] == "provider_unavailable"  # no geospatial store yet
    from src.kb.geospatial import GeospatialStore

    GeospatialStore(env.conn)
    unprojected = env.links.link_areas(NS, scopes=h.ALL, principal_id="linker")
    assert unprojected["linked"] == [] and unprojected["unresolved"]  # codes without a registered place
    projected = env.links.project_areas(NS, scopes=h.ALL, principal_id="linker")
    codes = {(p["scheme"], p["code"]) for p in projected["places"]}
    assert {("fao-major-area", "34"), ("rfmo-convention-area", "ICCAT"), ("gfw-grid", "LOW:35.1,-20.3")} <= codes
    linked = env.links.link_areas(NS, scopes=h.ALL, principal_id="linker")
    assert linked["unresolved"] == []
    area = env.links.links(NS, ["fao-fishstat:area:34"], scopes=h.READ, owner="geospatial")
    assert area and {x["basis"] for x in area} == {"published-area-code"}
    grid = [x for x in env.links.links(NS, ["gfw:region:public-rfmo/ICCAT"], scopes=h.READ, owner="geospatial")
            if x["basis"] == "published-grid-cell"]
    assert grid
    from src.kb.geospatial import GeospatialStore

    place = GeospatialStore(env.conn, initialize=False).place(NS, area[0]["target_id"],
                                                             scopes={"knowledge:geospatial:read"})
    assert place["source_ids"] == {"fao-major-area": "34"}


def test_citing_interface_is_unavailable_when_absent_and_links_explicit_identifiers(env):
    absent = env.links.link_cited(NS, "agrifood.food-systems", scopes=h.WRITE, principal_id="linker")
    assert absent["status"] == "provider_unavailable" and absent["linked"] == []
    records = [
        {"record_id": "agrifood:trade:1", "revision_id": "r1", "kind": "trade-flow",
         "title": "Skipjack imports", "identifiers": {"asfis": ["SKJ"], "fao_area": ["34"]}},
        {"record_id": "agrifood:note:2", "title": "SAMPLE ALBACORA UNO landings", "identifiers": {}},
    ]
    register_citing_provider("agrifood.food-systems", lambda: records)
    try:
        linked = env.links.link_cited(NS, "agrifood.food-systems", scopes=h.WRITE, principal_id="linker")
    finally:
        register_citing_provider("agrifood.food-systems", None)
    assert linked["status"] == "linked" and linked["records_read"] == 2
    assert {link["target_id"] for link in linked["linked"]} == {"agrifood:trade:1"}  # a name is never a link
    movements = [{"record_id": "osint:vessel:1", "kind": "observed-vessel", "identifiers": {"imo": [h.IMO_CLEAN]}}]
    observed = env.links.link_cited(NS, "osint.vessel-movements", scopes=h.WRITE, principal_id="linker",
                                    reader=lambda: movements)
    assert {link["subject_key"] for link in observed["linked"]} == {GFW_CLEAN, "iccat:vessel:AT000ESP00001",
                                                                    "iotc:vessel:IOTC000101"}
