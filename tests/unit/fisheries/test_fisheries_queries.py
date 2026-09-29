"""Vessel status as of a date (FI09, #2329) and effort/catch aggregates with revisions (FI10, #2332)."""

from __future__ import annotations

import json

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.fisheries_identity import FisheriesIdentity, FisheriesLinks
from src.kb.fisheries_queries import FisheriesQueries
from src.kb.fisheries_records import FisheriesError
from tests.unit.fisheries import harness as h

NS = h.NS


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    FisheriesIdentity(item.conn, now=lambda: next(item.clock)).propose(NS, principal_id="matcher", scopes=h.WRITE)
    h.seed_sanctions(item.conn)
    FisheriesLinks(item.conn, now=lambda: next(item.clock)).link_sanctions(NS, scopes=h.ALL, principal_id="linker")
    item.queries = FisheriesQueries(item.conn)
    yield item
    item.conn.close()


def test_authorisations_valid_on_a_date_with_snapshot_revision_and_connecting_match(env):
    status = env.queries.vessel_status_as_of(NS, scopes=h.READ, query=h.IMO_CLEAN, as_of="2025-06-01")
    assert status["resolution"]["interpreted_as"] == "imo"
    registers = {a["as_published"]["register"]: a for a in status["authorisations"]}
    assert set(registers) == {"ICCAT Record of Vessels", "IOTC Record of Authorised Vessels"}
    iccat = registers["ICCAT Record of Vessels"]
    assert iccat["state"] == "within the published authorisation period"
    assert iccat["citation"]["snapshot_date"] == "2026-09-01" and iccat["citation"]["retrieved_at_ms"]
    assert iccat["citation"]["revision_id"] and iccat["connected_by"] == []  # it states the queried IMO itself
    via_gfw = env.queries.vessel_status_as_of(NS, scopes=h.READ, query="a1b2c3d4-0001-4000-8000-000000000001",
                                              as_of="2025-06-01")
    connected = {a["as_published"]["register"]: a["connected_by"] for a in via_gfw["authorisations"]}
    assert connected["ICCAT Record of Vessels"][0]["basis"] == "imo"
    assert status["listings"] == [] and "statement" not in status
    assert [s["name"] for s in status["identity_history"]["segments"]] == ["SAMPLE ALBACORA UNO"]
    lists = {c["list_key"] for c in status["coverage"]}
    assert {"iccat:authorised-vessels", "iotc:iuu-vessels", "combined-iuu:iuu-vessels"} <= lists
    assert status["pending_candidates"]  # the WCPFC name-only candidate is shown, not used
    assert not h.forbidden_keys(status)


def test_listings_delistings_identity_history_and_sanctions_citations(env):
    reflagged = env.queries.vessel_status_as_of(NS, scopes=h.READ, query=h.IMO_REFLAGGED, as_of="2026-09-20")
    listed = {x["provider"]: x for x in reflagged["listings"]}
    assert listed["iotc"]["state"] == "listed on the date (as published)"
    assert listed["iotc"]["listing_history"]["stated_reason"].startswith("Fishing in the IOTC Area")
    assert listed["combined-iuu"]["independent_confirmation"] is False
    assert listed["combined-iuu"]["originating_listings"][0]["reference"] == "IOTC-IUU-2025-01"
    (auth,) = reflagged["authorisations"]
    assert auth["state"] == "published authorisation period ended before the date"
    assert [s["change"] for s in reflagged["identity_history"]["segments"]] == [[], ["renamed", "re-flagged"]]
    before = env.queries.vessel_status_as_of(NS, scopes=h.READ, query=h.IMO_REFLAGGED, as_of="2025-01-01")
    assert {x["state"] for x in before["listings"]} == {"not yet listed on the date"}
    assert any(e["event"] == "listed" and e["date"] == "2025-07-01" for e in before["later_events"])
    drifter = env.queries.vessel_status_as_of(NS, scopes=h.READ, query=h.IMO_DELISTED, as_of="2026-01-01")
    assert {x["state"] for x in drifter["listings"]} == {"delisted on 2024-11-18 (as published)"}
    while_listed = env.queries.vessel_status_as_of(NS, scopes=h.READ, query=h.IMO_DELISTED, as_of="2020-01-01")
    assert {x["state"] for x in while_listed["listings"]} == {"listed on the date (as published)"}
    assert drifter["sanctions"] and drifter["sanctions"][0]["matched"] == f"imo:{h.IMO_DELISTED}"


def test_a_vessel_with_no_record_is_none_on_record_never_legal(env):
    missing = env.queries.vessel_status_as_of(NS, scopes=h.READ, query="9000041", as_of="2026-09-01")
    assert missing["members"] == [] and "not a statement that the vessel is legal" in missing["statement"]
    assert "iccat:authorised-vessels" in missing["statement"]
    coral = env.queries.vessel_status_as_of(NS, scopes=h.READ, subject_key="iotc:vessel:IOTC000102",
                                            as_of="2026-09-01")
    assert coral["authorisations"] and "statement" not in coral
    with pytest.raises(FisheriesError) as caught:
        env.queries.vessel_status_as_of(NS, scopes=h.READ, query="Sample Albacora Uno", as_of="2026-09-01")
    assert caught.value.code == "ambiguous"  # a name reaches records no accepted match joins
    with pytest.raises(FisheriesError):
        env.queries.vessel_status_as_of(NS, scopes={"knowledge:read"}, query=h.IMO_CLEAN)


def test_status_answers_export_as_verifiable_evidence_bundles(env):
    status = env.queries.vessel_status_as_of(NS, scopes=h.READ, query=h.IMO_DELISTED, as_of="2026-01-01")
    exported = env.queries.export_bundle(NS, status, scopes=h.READ)
    bundle = exported["bundle"]
    assert verify_bundle(bundle).valid, verify_bundle(bundle).errors
    kinds = {o["payload"].get("kind") for o in bundle["objects"]}
    assert {"fisheries-record", "sanctions-citation", "fisheries-vessel-status"} <= kinds
    none = env.queries.vessel_status_as_of(NS, scopes=h.READ, query="9000041", as_of="2026-01-01")
    partial = env.queries.export_bundle(NS, none, scopes=h.READ)["bundle"]
    assert partial["completeness"]["status"] == "partial"


def test_effort_and_catch_side_by_side_with_releases_units_and_unpublished_cells(env):
    catch = env.queries.aggregates(NS, scopes=h.READ, area="34", species="SKJ", period_from="2021-01-01",
                                   period_to="2022-12-31")
    assert {c["record_key"] for c in catch["catch"]} == {"capture:ESP:SKJ:2021:Q_tlw", "capture:ESP:SKJ:2022:Q_tlw",
                                                         "capture:GHA:SKJ:2021:Q_tlw"}
    gha = next(c for c in catch["catch"] if c["record_key"].startswith("capture:GHA"))
    assert gha["as_published"]["status_flags"] == ["E"] and gha["citation"]["release"] == "2025.1"
    assert gha["as_published"]["unit"] == "t (tonnes live weight)"
    assert catch["not_published"] == [{"area": "34", "flag": "GHA", "species": "SKJ", "year": 2022,
                                       "release": "2025.1", "status": catch["not_published"][0]["status"]}]
    assert "not zero" in catch["not_published"][0]["status"]
    by_flag = env.queries.aggregates(NS, scopes=h.READ, flag="ESP", period_from="2024-01-01", period_to="2024-12-31")
    assert len(by_flag["effort"]) == 2 and by_flag["catch"] == []
    cell = by_flag["effort"][0]
    assert "not confirmed fishing" in cell["as_published"]["method"]
    assert cell["citation"]["dataset_version"] == "public-global-fishing-effort:v3.0"
    assert "never combined" in by_flag["presentation"]
    assert not {"indicator", "catch_per_effort", "cpue"} & set(json.dumps(by_flag).split('"'))
    with pytest.raises(FisheriesError):
        env.queries.aggregates(NS, scopes=h.READ)


def test_release_to_release_changes_are_visible_and_bundles_export(env):
    assert env.run("release-2", source_ids=["fao-fishstat-capture"], overrides=h.LATER["release"])["status"] \
        == "complete"
    answer = env.queries.aggregates(NS, scopes=h.READ, area="34", flag="GHA", period_from="2021-01-01",
                                    period_to="2021-12-31")
    (gha,) = answer["catch"]
    assert [r["release"] for r in gha["releases"]] == ["2025.1", "2026.1"]
    assert gha["changes_between_releases"] == [{"from_release": "2025.1", "to_release": "2026.1",
                                                "from_value": "50000", "to_value": "51200",
                                                "from_status_flags": ["E"], "to_status_flags": []}]
    assert gha["citation"]["release"] == "2026.1"
    bundle = env.queries.export_bundle(NS, answer, scopes=h.READ)["bundle"]
    assert verify_bundle(bundle).valid
