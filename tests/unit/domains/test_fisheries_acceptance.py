"""Offline acceptance: a vessel and an area to cited fisheries records (#2222, FI13 #2343).

The journey runs the real source-pack runtime over the pinned GFW, FAO
FishStat, ICCAT, WCPFC and IOTC (registers and IUU lists) and Combined IUU
Vessel List fixtures, with sockets blocked. Every vessel is synthetic. It
reproduces: as-of authorisation status, listing and delisting history,
release revisions, identity matches, sanctions and area citations, and the
negative cases - a vessel with no record, a re-flagged vessel, a name-only
candidate, an unpublished cell and an optional pack that is absent. Offline
evidence is recorded here; live evidence (FI14, #2346) is reported separately
in ``docs/development/fisheries-evidence/README.md`` and stays
``unverified-live``.
"""

from __future__ import annotations

import socket

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.ingestion.fisheries_sources import LIVE_VERIFICATION
from src.kb.fisheries_bundle import readiness
from src.kb.fisheries_identity import FisheriesIdentity, FisheriesLinks
from src.kb.fisheries_monitoring import FisheriesMonitor
from src.kb.fisheries_queries import FisheriesQueries
from tests.unit.fisheries import harness as h

NS = h.NS


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_vessel_and_area_to_cited_authorisations_listings_effort_and_catch():
    env = h.Env()
    # 1. Acquire every selected source (GFW, FishStat, three RFMOs' registers and IUU lists, the combined list).
    run = env.run("acceptance-1")
    assert run["status"] == "complete"
    assert {s["source_id"]: s["status"] for s in run["sources"]} == {s: "complete" for s in h.SOURCES}
    assert {r["evidence_origin"] for r in env.store.runs(NS)} == {"fixture"}

    # 2. Identity: IMO matches recorded with evidence; name-only pairs stay review candidates.
    identity = FisheriesIdentity(env.conn, now=lambda: next(env.clock))
    proposed = identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    name_only = [m for m in proposed["matches"] if m["basis"] == "name-only"]
    assert name_only and all(m["state"] == "proposed" for m in name_only)

    # 3. Optional packs absent: sanctions and citing providers degrade to provider_unavailable.
    links = FisheriesLinks(env.conn, now=lambda: next(env.clock))
    assert links.link_sanctions(NS, scopes=h.ALL, principal_id="linker")["status"] == "provider_unavailable"
    assert links.link_cited(NS, "osint.vessel-movements", scopes=h.ALL,
                            principal_id="linker")["status"] == "provider_unavailable"
    h.seed_sanctions(env.conn)
    assert links.link_sanctions(NS, scopes=h.ALL, principal_id="linker")["linked"]
    links.project_areas(NS, scopes=h.ALL, principal_id="linker")
    assert links.link_areas(NS, scopes=h.ALL, principal_id="linker")["unresolved"] == []

    # 4. A re-flagged, renamed vessel on a date: authorisation history, IUU listing, identity history.
    queries = FisheriesQueries(env.conn)
    status = queries.vessel_status_as_of(NS, scopes=h.READ, query=h.IMO_REFLAGGED, as_of="2026-09-20")
    (auth,) = status["authorisations"]
    assert auth["as_published"]["register"] == "ICCAT Record of Vessels"
    assert auth["citation"]["snapshot_date"] == "2026-09-01" and auth["citation"]["retrieved_at_ms"]
    assert {x["provider"]: x["state"] for x in status["listings"]} == {
        "iotc": "listed on the date (as published)", "combined-iuu": "listed on the date (as published)"}
    assert all(x["connected_by"][0]["basis"] == "imo" for x in status["listings"] + status["authorisations"]
               if x["connected_by"])
    segments = status["identity_history"]["segments"]
    assert [(s["name"], s["flag"], s["change"]) for s in segments] == [
        ("SAMPLE STAR", "GHA", []), ("SAMPLE NOVA", "TGO", ["renamed", "re-flagged"])]
    assert {c["list_key"] for c in status["coverage"]} >= {"iccat:authorised-vessels", "wcpfc:authorised-vessels",
                                                          "iotc:authorised-vessels", "combined-iuu:iuu-vessels"}
    assert verify_bundle(queries.export_bundle(NS, status, scopes=h.READ)["bundle"]).valid

    # 5. Listing and delisting history with the stated reason and a sanctions citation by IMO.
    drifter = queries.vessel_status_as_of(NS, scopes=h.READ, query=h.IMO_DELISTED, as_of="2026-01-01")
    iccat = next(x for x in drifter["listings"] if x["provider"] == "iccat")
    assert iccat["listing_history"]["listed_on"] == "2016-11-20" and iccat["state"].startswith("delisted on 2024")
    assert iccat["listing_history"]["stated_reason"].startswith("Fishing activities in the Convention area")
    assert drifter["sanctions"][0]["matched"] == f"imo:{h.IMO_DELISTED}"

    # 6. A vessel with no record, and a name-only candidate that is never used.
    missing = queries.vessel_status_as_of(NS, scopes=h.READ, query="9000041", as_of="2026-09-01")
    assert "not a statement that the vessel is legal" in missing["statement"]
    clean = queries.vessel_status_as_of(NS, scopes=h.READ, query=h.IMO_CLEAN, as_of="2025-06-01")
    assert all(a["as_published"]["register"] != "WCPFC Record of Fishing Vessels" for a in clean["authorisations"])
    assert any(c["basis"] == "name-only" for c in clean["pending_candidates"])

    # 7. An area: effort and catch side by side, with an unpublished cell.
    area = queries.aggregates(NS, scopes=h.READ, area="34", period_from="2021-01-01", period_to="2022-12-31")
    assert len(area["catch"]) == 3 and area["not_published"][0]["flag"] == "GHA"
    effort = queries.aggregates(NS, scopes=h.READ, area="ICCAT", period_from="2024-01-01", period_to="2024-02-29")
    assert len(effort["effort"]) == 3 and effort["catch"] == []
    assert all("not confirmed fishing" in e["as_published"]["method"] for e in effort["effort"])

    # 8. Monitors hear a removal, a new listing and a new release; release revisions are visible.
    monitor = FisheriesMonitor(env.conn, now=lambda: next(env.clock))
    subscription = monitor.create(NS, "acceptance", principal_id="analyst", scopes=h.ALL, vessels=[h.IMO_REFLAGGED],
                                  lists=["iccat:iuu-vessels"], areas=["34"])["subscription_id"]
    assert monitor.run(subscription, principal_id="analyst", scopes=h.ALL)["baseline"]
    assert env.run("acceptance-2", source_ids=["iccat-vessel-lists"], overrides=h.LATER["second"])["status"] \
        == "complete"
    assert env.run("acceptance-3", source_ids=["fao-fishstat-capture"], overrides=h.LATER["release"])["status"] \
        == "complete"
    kinds = {n["kind"] for n in monitor.run(subscription, principal_id="analyst", scopes=h.ALL)["notifications"]}
    assert kinds == {"authorisation_removed", "iuu_listing", "statistics_release"}
    later = queries.vessel_status_as_of(NS, scopes=h.READ, query=h.IMO_REFLAGGED, as_of="2026-12-01")
    assert later["authorisations"][0]["state"].startswith("removed from the register")
    revised = queries.aggregates(NS, scopes=h.READ, area="34", flag="GHA", period_from="2021-01-01",
                                 period_to="2021-12-31")
    assert revised["catch"][0]["changes_between_releases"][0]["to_release"] == "2026.1"
    assert not h.forbidden_keys([status, drifter, missing, area, effort, later])

    # 9. Replaying the acquisition adds nothing.
    generation = env.store.generation(NS)
    assert env.run("acceptance-4", source_ids=["gfw-vessels-effort", "wcpfc-vessel-lists"])["status"] == "complete"
    assert env.store.generation(NS) == generation
    env.conn.close()


def test_live_evidence_is_reported_separately_and_unverified(tmp_path):
    env = h.loaded_env(tmp_path)
    status = readiness(env.conn, NS, scopes=h.READ)
    assert {p["status"] for p in status["providers"].values()} == {"fixture-only"}
    assert {v["status"] for v in LIVE_VERIFICATION.values()} == {"unverified-live"}
    readme = (h.ROOT / "docs/development/fisheries-evidence/README.md").read_text()
    assert "No dated live run exists" in readme
    env.conn.close()
