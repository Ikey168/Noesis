"""Offline place-and-facility-to-waste acceptance for the Climate and Environment waste features (WC12, #2806).

The pinned Eurostat waste, Eurostat circular-economy, EEA Industrial Reporting waste-transfer and OECD municipal-waste
fixtures (authored in the documented shapes; reference years 2094-2097 and release dates 2098-2099 are fictional and
facility names are marked as fixtures) replay through the real ``waste`` adapter - Eurostat and OECD through the SDMX
connector - and the runtime's projector, with the four waste features selected in the composition plan and sockets
blocked. The journey takes Germany and France to cited waste and circularity figures from each source side by side,
and one Berlin facility known to ``environment.core`` to its cited waste transfers across releases, including a
resubmitted past year, a removal by the source, a truncated EEA page, an unknown INSPIRE id, Chemicals and Products
links and a place with no records. Offline evidence only, never live coverage
(``docs/development/waste-evidence/``).
"""

from __future__ import annotations

import json
import socket

import duckdb
import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.ingestion.waste_sources import EXCLUSIONS, fixture_transport
from src.kb.waste_identity import WasteIdentity
from src.kb.waste_links import WasteLinks
from src.kb.waste_monitoring import WasteMonitor
from src.kb.waste_queries import WasteQueries
from src.kb.waste_records import (
    MINIMISATION,
    feature_enabled,
    forbidden_paths,
    personal_data_paths,
    readiness,
    selected_features,
)
from src.kb.waste_store import WasteStore
from src.mcp_host.introspection import tool_map
from tests.unit import waste_fixture_builder as builder
from tests.unit import waste_harness as h
from tests.unit.composition.test_migration import _migrated

FEATURES = ["waste-eea-transfers", "waste-eurostat", "waste-eurostat-circular-economy", "waste-oecd"]
SCOPES_WITH_ENVIRONMENT = h.READ_ONLY | {"knowledge:environment:read"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def stored_values(conn):
    """Every series value the store holds, as published: an answer may only show these."""
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT v.series_id, o.period, o.value FROM waste_observations o JOIN waste_vintages v ON "
        "v.namespace=o.namespace AND v.vintage_id=o.vintage_id").fetchall()}


def review_all(identity, proposed):
    for assertion in proposed:
        if assertion["state"] == "proposed":
            identity.review(h.NS, assertion["assertion_id"], "accept", "published code checked",
                            principal_id=h.REVIEWER, scopes=h.SCOPES)


def test_place_and_facility_to_cited_waste_figures_side_by_side_and_transfers_with_revisions(tmp_path, monkeypatch):
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("climate-environment", bundles["climate-environment"]["version"], features=FEATURES)
    assert coordinator.activate("waste-acceptance")["status"] == "published"
    assert selected_features(conn) == FEATURES and feature_enabled(conn, "oecd-municipal-waste")
    assert "noesis-waste-record-v2" in PROJECTORS

    # The pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    replay = SourcePackConformance(h.ROOT).offline(h.manifest())
    assert replay["valid"] and replay["coverage"]["verified"] == 4
    for path, text in builder.build(write=False).items():
        assert path.read_text() == text  # authored, regenerable, nothing captured

    # First releases (retrieved 2098-12-01) beside the environment.core Berlin facility records; a monitor watches
    # France and another the facility; then the 2099 releases.
    h.load_all(conn)
    monitor = WasteMonitor(conn, now=lambda: h.FIRST_RETRIEVAL + 1)
    france = monitor.create(h.NS, "france", target={"area": {"scheme": "eurostat-geo", "code": "FR"}},
                            principal_id="alice", scopes=h.SCOPES)["subscription_id"]
    facility = monitor.create(h.NS, "hkw-mitte", target={"inspire_id": h.FACILITY_1}, principal_id="alice",
                              scopes=h.SCOPES)["subscription_id"]
    assert {n["kind"] for n in monitor.run(france, principal_id="alice", scopes=h.SCOPES)["notifications"]} == {
        "new_release"}
    assert len(monitor.run(facility, principal_id="alice", scopes=h.SCOPES)["notifications"]) == 3
    h.load_all(conn, revisions=True, facilities=False)
    status = readiness(conn)
    assert status["selected"] and {p["live_verification"] for p in status["providers"].values()} == {
        "unverified-live"}
    assert all(not p["stale"] for p in status["providers"].values())

    # Reviewable identity: places by published code, facilities by INSPIRE id, Eurostat and OECD related.
    places = h.register_places(conn)
    identity = WasteIdentity(conn)
    review_all(identity, identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"])
    facilities = identity.propose_facilities(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert facilities["unmatched_inspire_ids"] == [h.UNKNOWN_FACILITY]
    review_all(identity, facilities["assertions"])
    review_all(identity, identity.propose_related_indicators(h.NS, principal_id="analyst",
                                                             scopes=h.SCOPES)["assertions"])

    # Chemicals by published CAS number and Products by citation.
    h.seed_chemicals(conn)
    h.seed_products(conn)
    links = WasteLinks(conn)
    assert links.link_chemicals(h.NS, principal_id="svc", scopes=h.SCOPES)["linked"]
    assert links.link_products(h.NS, principal_id="svc", scopes=h.SCOPES)["linked"]
    assert links.link_facilities(h.NS, principal_id="svc", scopes=h.SCOPES)["linked"]
    states = {(link["kind"], link["state"]) for link in links.links(h.NS, scopes=h.READ_ONLY)}
    assert {("chemicals", "linked"), ("products", "linked"), ("products", "target_not_held"),
            ("facility", "linked"), ("facility", "target_not_held")} <= states

    # Germany as of 2098-12-31 and 2099-12-31: each source side by side with definitions and vintages.
    queries = WasteQueries(conn)
    early = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"], as_of="2098-12-31")
    late = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"], as_of="2099-12-31")
    assert set(early["providers"]) == {"eurostat-waste", "eurostat-circular-economy", "oecd-municipal-waste"}
    assert early["side_by_side"] and early["never_blended"] and early["related_pairs"]
    rows = {(r["dataset"], r["hazard"]["code"], r["operation"]["code"]): r for r in early["results"]}
    generated = rows[("env_wasgen", "HAZ_NHAZ", "not_applicable")]
    assert generated["citation"]["as_of"].startswith("2098-03-15") and generated["definition"]["definition_id"]
    assert generated["absent_years"]["biennial_not_collected"] == ["2095"]  # never filled
    assert {o["period"]: o["flags"] for o in generated["observations"]}["2096"] == {"OBS_FLAG": "p"}
    later = {(r["dataset"], r["hazard"]["code"], r["operation"]["code"]): r for r in late["results"]}
    resubmitted = later[("env_wasgen", "HAZ_NHAZ", "not_applicable")]
    assert {o["period"]: o["value"] for o in resubmitted["observations"]}["2094"] == "403900000"
    assert resubmitted["vintage"]["revision_of"] == generated["vintage"]["vintage_id"]
    oecd = next(r for r in early["results"] if r["provider"] == "oecd-municipal-waste")
    assert oecd["citation"]["release_basis"] == "declared_release" and oecd["matched_by"] == "accepted-match"
    recycling = next(r for r in early["results"] if r["dataset"] == "cei_wm011")
    assert recycling["unit"]["code"] == "RT" and recycling["definition"]["source_text"].endswith("never recomputed")
    # France: the landfill series the 2099 release no longer states is removed_by_source, earlier vintages kept.
    fr = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["fr"], concept="waste_treated")
    landfill = next(r for r in fr["results"] if r["operation"]["code"] == "DSP_L")
    assert landfill["status"] == "removed_by_source"
    history = queries.series_history(h.NS, landfill["series_id"], scopes=h.READ_ONLY)
    assert [v["status"] for v in history["vintages"]] == ["published", "removed"]
    # A place with no records.
    italy = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["it"])
    assert italy["status"] == "no_records" and italy["results"] == []

    # A truncated EEA page (a full nrOfHits page of another dataset version) is stored and labelled; it removes nothing.
    rows_500 = [builder.transfer(f"DE.UBA.PRTR/FIXTURE{n:04d}.FACILITY", "NONHW", "R", "DOMESTIC", n + 1, "E")
                for n in range(500)]
    truncated_page = [dict(h.pages("eea-industry-waste-transfers")[0], body=json.dumps(
        {"datasetVersion": "v13.1 (authored fixture)", "datasetPublished": "2099-05-01", "results": rows_500}))]
    (applied,) = h.apply(conn, "eea-industry-waste-transfers", retrieved_at_ms=h.SECOND_RETRIEVAL + 1,
                         transport=fixture_transport(truncated_page))
    assert applied["truncated"] and applied["removed"] == 0

    # One facility's waste transfers: per reporting year, with revisions, method codes and both records cited.
    transfers = queries.facility_transfers(h.NS, scopes=SCOPES_WITH_ENVIRONMENT, facility=h.FACILITY_1)
    assert transfers["facility_record"]["native_id"] == h.FACILITY_1
    assert transfers["facility_identity"]["state"] == "accepted"
    (year,) = transfers["reporting_years"]
    by_key = {(r["hazardous"]["code"], r["treatment"]["code"], r["destination"]["code"]): r for r in year["rows"]}
    corrected = by_key[("NONHW", "R", "DOMESTIC")]
    assert [v["quantity"] for v in corrected["vintages"]] == ["840.25", "851.75"]
    assert corrected["method"] == {"code": "M", "label": "measured"}
    assert by_key[("HW", "R", "TRANSBOUNDARY")]["status"] == "removed_by_source"
    assert by_key[("HW", "R", "TRANSBOUNDARY")]["quantity"] is None  # never a zero
    assert transfers["truncated_acquisitions"] and "never zero" in transfers["threshold_note"]
    assert transfers["totals"] is None and "never summed" in transfers["never_summed"]
    unknown = queries.facility_transfers(h.NS, scopes=SCOPES_WITH_ENVIRONMENT, facility=h.UNKNOWN_FACILITY)
    assert unknown["facility_record"] is None and unknown["facility_identity"]["state"] == "unmatched"
    assert unknown["reporting_years"][0]["rows"]  # visible, never a created facility
    assert conn.execute("SELECT count(*) FROM environment_records WHERE record_type='facility'").fetchone()[0] == 2

    # The subscriptions hear the revisions and removals once; a restart replays nothing.
    later_monitor = WasteMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 10)
    kinds = {n["kind"] for n in later_monitor.run(france, principal_id="alice", scopes=h.SCOPES)["notifications"]}
    assert "removed_by_source" in kinds
    facility_kinds = {n["kind"] for n in later_monitor.run(facility, principal_id="alice",
                                                           scopes=h.SCOPES)["notifications"]}
    assert facility_kinds == {"revised_value", "removed_by_source"}
    assert WasteMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 20).run(
        france, principal_id="alice", scopes=h.SCOPES)["notifications"] == []

    # Exclusions and the WC01 minimisation decision: every shown value is a stored published value; no operator,
    # address, contact or authority field and no derived, filled, blended or summed value anywhere.
    published = stored_values(conn)
    bundle = queries.export_bundle(h.NS, scopes=SCOPES_WITH_ENVIRONMENT, place=places["de"], facility=h.FACILITY_1)
    assert bundle["items"] and all(i["source"] and i["record_revision"] and i["as_of"] for i in bundle["items"])
    for answer in (early, late, fr, italy, transfers, unknown, bundle):
        assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
    for answer in (early, late, fr):
        for result in answer["results"]:
            for value in result["observations"]:
                assert (result["series_id"], value["period"], value["value"]) in published
                if value["value"] is None:
                    assert value["status"] in {"confidential", "not_published"}
    for table in ("waste_series", "waste_releases", "waste_transfer_rows", "environment_records",
                  "environment_record_revisions"):
        columns = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE "
                                              "table_name=?", [table]).fetchall()}
        assert not {c for c in columns if any(w in c for w in ("contact", "email", "phone", "authority"))}
    waste_revisions = [json.loads(r[0]) for r in conn.execute(
        "SELECT v.content_json FROM environment_record_revisions v JOIN waste_transfer_rows w ON "
        "w.environment_record_id=v.record_id").fetchall()]
    assert waste_revisions and all(personal_data_paths(r) == [] for r in waste_revisions)
    assert MINIMISATION["decision"].startswith("published aggregates")
    assert {"blending Eurostat, OECD and EEA figures", "summing facility transfers into national totals"} <= set(
        EXCLUSIONS) and set(early["exclusions"]) == set(EXCLUSIONS)

    # Re-ingestion adds nothing.
    releases = len(WasteStore(conn).releases(h.NS))
    h.load_all(conn, revisions=True, facilities=False)
    assert len(WasteStore(conn).releases(h.NS)) == releases

    # The MCP tools answer the same journey from a file-backed store with read scopes only.
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "waste-acceptance.duckdb")
    file_conn = duckdb.connect(path)
    h.load_all(file_conn, revisions=True)
    file_conn.close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", set(SCOPES_WITH_ENVIRONMENT)))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = tool_map(server.mcp)
    answer = tools["waste_indicator_for_place"].fn(place={"scheme": "eurostat-geo", "code": "FR"},
                                                   as_of="2099-12-31")
    assert answer["side_by_side"] is True and forbidden_paths(answer) == [] and answer["exclusions"]
    transfers = tools["waste_facility_transfers"].fn(facility=h.FACILITY_1)
    assert transfers["status"] == "reported" and personal_data_paths(transfers) == []
