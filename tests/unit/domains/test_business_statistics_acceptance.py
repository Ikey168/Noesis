"""Offline place-to-business-statistics acceptance for the Economics business-statistics feature (IB12, #2738).

The pinned Eurostat STS, Eurostat business demography and Census CBP fixtures (authored in the documented shapes;
every value and every reference period is fictional) replay through the real ``business-statistics`` adapter - the
Eurostat sources through the SDMX connector - and the runtime's projector, with the ``business-statistics`` feature
selected in the composition plan and sockets blocked. The journey takes Germany and California to cited Eurostat and
CBP figures side by side with definitions, statistical units, vintages, flags and comparability notes, never blended,
including a monthly revision, a rebase, provisional deaths revised, a CBP correction and the NAICS vintage change.
Offline evidence only, never live coverage (``docs/development/business-statistics-evidence/``).
"""

from __future__ import annotations

import asyncio
import socket

import duckdb
import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.kb.business_statistics_identity import BusinessIdentity
from src.kb.business_statistics_links import BusinessLinks
from src.kb.business_statistics_monitoring import BusinessMonitor
from src.kb.business_statistics_queries import BusinessQueries
from src.kb.business_statistics_records import (
    feature_enabled,
    forbidden_paths,
    personal_data_paths,
    readiness,
)
from src.kb.business_statistics_store import BusinessStatisticsStore
from src.kb.labour_identity import LabourIdentity
from tests.unit import business_statistics_harness as h
from tests.unit import labour_harness as lh
from tests.unit import trade_harness as th
from tests.unit.composition.test_migration import _migrated

NACE_C = {"scheme": "NACE", "version": "Rev.2", "code": "C"}
NAICS_2017_MANUFACTURING = {"scheme": "NAICS", "version": "2017", "code": "31-33"}


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
    """Every value the store holds, as published: an answer may only show these."""
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT v.series_id, o.period, o.value FROM business_observations o JOIN business_vintages v ON "
        "v.namespace=o.namespace AND v.vintage_id=o.vintage_id").fetchall()}


def review_all(identity, proposed, scopes):
    for assertion in proposed:
        if assertion["state"] == "proposed":
            identity.review(h.NS, assertion["assertion_id"], "accept", "published code or concordance rows",
                            principal_id="reviewer", scopes=scopes)


def test_place_to_cited_eurostat_and_cbp_figures_side_by_side_with_definitions_vintages_and_flags(tmp_path,
                                                                                                    monkeypatch):
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("economics", bundles["economics"]["version"],
                       features=["business-statistics", "labour-statistics", "trade-comext"])
    assert coordinator.activate("business-acceptance")["status"] == "published"
    assert feature_enabled(conn) and "noesis-business-statistics-record-v2" in PROJECTORS

    # The pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    manifest = h.manifest()
    business = [s for s in manifest["sources"] if s["connector"] == "business-statistics"]
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": business})
    assert replay["valid"] and replay["coverage"]["verified"] == 3

    # First releases (retrieved 2024-09-01); a monitor watches Germany; then revisions, a correction and a rebase.
    h.load_all(conn)
    monitor = BusinessMonitor(conn, now=lambda: h.FIRST_RETRIEVAL + 1)
    watch = monitor.create(h.NS, "germany", target={"area": {"scheme": "eurostat-geo", "code": "DE"}},
                           principal_id="alice", scopes=h.SCOPES)
    first_notices = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in first_notices} == {"new_release"} and len(first_notices) == 7
    h.load_all(conn, revisions=True)
    h.apply(conn, "rebase", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    status = readiness(conn)
    assert status["selected"] and {p["live_verification"] for p in status["providers"].values()} == {
        "unverified-live"}

    # Reviewable identity: places by published code; NACE/NAICS candidate links through cited concordances.
    places = h.register_places(conn)
    identity = BusinessIdentity(conn)
    tables = h.concordance_tables()
    labour_identity = LabourIdentity(conn)
    for table in tables[:2]:  # the labour track's LB07 operator imports
        labour_identity.import_concordance(h.NS, table, principal_id="op", scopes=lh.SCOPES)
    identity.import_concordance(h.NS, tables[2], principal_id="op", scopes=h.SCOPES)
    review_all(identity, identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"],
               h.SCOPES)
    links_proposed = identity.propose_classification_links(h.NS, principal_id="analyst", scopes=h.SCOPES)
    review_all(identity, links_proposed["assertions"], h.SCOPES)
    assert {f"{c['scheme']} {c['version']} {c['code']}" for c in links_proposed["unlinked_codes"]} >= {
        "NACE Rev.2 B-D"}

    # Links to Labour (Eurostat LFS by shared code; LAUS through reviewed places) and Trade (Comext by shared code).
    lh.load_all(conn)
    th.load_all(conn)
    review_all(labour_identity, labour_identity.propose_places(lh.NS, principal_id="analyst", scopes=lh.SCOPES,
                                                               geo_namespace="global")["assertions"], lh.SCOPES)
    links = BusinessLinks(conn)
    labour = links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    trade = links.link_trade(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert labour["linked"] and trade["linked"]
    bases = {(link["kind"], link["basis"]) for link in links.links(h.NS, scopes=h.READ_ONLY, state="linked")}
    assert {("labour", "shared-identifier"), ("labour", "accepted-match"), ("trade", "shared-identifier"),
            ("trade", "accepted-match")} <= bases

    # Germany and California side by side as of 2024-09-30: manufacturing per source, never blended.
    queries = BusinessQueries(conn)
    side = queries.compare_places(h.NS, scopes=h.READ_ONLY, places=[places["de"], places["ca"]],
                                  classifications={places["de"]: NACE_C, places["ca"]: NAICS_2017_MANUFACTURING},
                                  as_of="2024-09-30")
    germany, california = (a["answer"] for a in side["places"])
    assert {(r["provider"], r["adjustment"]) for r in germany["results"]} == {("eurostat-sts", "SCA"),
                                                                              ("eurostat-sts", "NSA")}
    assert {r["indicator"]["code"] for r in california["results"]} == {"ESTAB", "EMP", "PAYANN"}
    sts = next(r for r in germany["results"] if r["adjustment"] == "SCA")
    assert sts["statistical_unit"] == "kind-of-activity-unit" and sts["unit"]["base_year"] == "2021"
    assert sts["definition"]["content"]["source_text"].startswith("Volume index of production")
    assert sts["vintage"]["release_at"] == "2024-03-15T23:00:00Z" and sts["vintage"]["release_basis"] == \
        "provider_last_update"
    assert {v["period"]: v["flags"] for v in sts["values"]}["2096-04"] == {"OBS_FLAG": "p"}
    assert any(n["relation"] == "break_in_series" for n in sts["notes"])
    payroll = next(r for r in california["results"] if r["indicator"]["code"] == "PAYANN")
    assert payroll["statistical_unit"] == "establishment" and payroll["classification"]["version"] == "2017"
    assert payroll["values"][0]["status"] == "withheld" and payroll["values"][0]["value"] is None
    assert payroll["citation"]["release_basis"] == "declared_release"
    employment = next(r for r in california["results"] if r["indicator"]["code"] == "EMP")
    assert employment["values"][0]["flags"] == {"EMP_N": "J"} and employment["values"][0]["attributes"][
        "exact"] is False
    assert "self-employed" in employment["definition"]["content"]["scope"]
    assert employment["definition"]["content"]["statistical_unit"] == "establishment"
    # Within Germany the adjustment difference is recorded; no pair is ever combined.
    assert all(p["status"] == "noted" for p in side["comparability"])
    assert any({d["kind"] for d in p["recorded_differences"]} == {"different_adjustment"}
               for p in side["comparability"])
    # Enterprises (Eurostat) beside establishments (CBP): paired only to state that they are different measures.
    units = queries.compare_places(h.NS, scopes=h.READ_ONLY, places=[places["de"], places["ca"]],
                                   as_of="2024-09-30")
    rows = {r["series_id"]: r for a in units["places"] for r in a["answer"]["results"]}
    enterprises_vs_establishments = [
        p for p in units["comparability"]
        if {rows[s]["indicator"]["concept"] for s in p["series"]} == {"active_enterprises", "establishments"}]
    assert enterprises_vs_establishments
    kinds = {d["kind"] for d in enterprises_vs_establishments[0]["recorded_differences"]}
    assert {"different_concept", "different_statistical_unit", "different_source", "different_place",
            "different_classification"} <= kinds
    statement = next(d for d in enterprises_vs_establishments[0]["recorded_differences"]
                     if d["kind"] == "different_concept")["statement"]
    assert "never compared as one" in statement
    # Employment for California through the 2022 vintage and the accepted NACE C candidate link, cited and labelled.
    linked = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["ca"], classification=NACE_C,
                                         concept="employment")
    (row,) = linked["results"]
    assert row["classification"]["version"] == "2022" and "accepted candidate link (partial)" in row["matched_by"][
        "matched_by"]

    # A later as-of date selects the revised vintage; the rebase and the NAICS change show in the history.
    later = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"], classification=NACE_C,
                                        concept="production_index", as_of="2024-10-31")
    revised = next(r for r in later["results"] if r["adjustment"] == "SCA")
    assert {v["period"]: v["value"] for v in revised["values"]}["2096-04"] == "104.0"
    assert revised["vintage"]["revision_of"] == sts["vintage"]["vintage_id"]
    history = queries.series_history(h.NS, sts["series_id"], scopes=h.READ_ONLY)
    kinds = {n["kind"] for pair in history["pairs"] for n in pair["notes"]}
    assert {"provisional_confirmed", "removed_by_source", "base_year_change"} <= kinds
    cbp_history = queries.series_history(h.NS, employment["series_id"], scopes=h.READ_ONLY)
    assert any(link["kind"] == "classification_vintage" for link in cbp_history["classification_links"])
    deaths = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"], concept="enterprise_deaths")
    (deaths_row,) = deaths["results"]
    assert {v["period"]: v["flags"] for v in deaths_row["values"]}["2096"] == {"OBS_FLAG": "p"}

    # The subscription hears the revision, the new period and the rebase once; a restart replays nothing.
    later_monitor = BusinessMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 1)
    kinds = {n["kind"] for n in later_monitor.run(watch["subscription_id"], principal_id="alice",
                                                  scopes=h.SCOPES)["notifications"]}
    assert {"new_period", "revised_value", "removed_by_source", "new_release"} <= kinds
    assert BusinessMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 2).run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []

    # No derived, blended or reconstructed value: every shown value is a stored published value.
    published = stored_values(conn)
    for answer in (germany, california, linked, later, deaths, *(a["answer"] for a in units["places"])):
        assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
        for result in answer["results"]:
            for value in result["values"]:
                assert (result["series_id"], value["period"], value["value"]) in published
                if value["value"] is None:
                    assert value["status"] in {"confidential", "withheld", "not_published"}
    assert "blending Eurostat and Census figures" in side["exclusions"]

    # Re-ingestion adds nothing.
    releases = len(BusinessStatisticsStore(conn).releases(h.NS))
    h.load_all(conn, revisions=True)
    assert len(BusinessStatisticsStore(conn).releases(h.NS)) == releases

    # The MCP tools answer the same journey from a file-backed store with read scopes only.
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "business-acceptance.duckdb")
    file_conn = duckdb.connect(path)
    h.load_all(file_conn, revisions=True)
    file_conn.close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", set(h.READ_ONLY)))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    answer = tools["compare_business_places"].fn(
        namespace="global", places=[{"scheme": "eurostat-geo", "code": "DE"}, {"scheme": "us-fips-state",
                                                                                "code": "06"}],
        classifications={"eurostat-geo:DE": NACE_C, "us-fips-state:06": NAICS_2017_MANUFACTURING},
        as_of="2024-09-30")
    assert answer["side_by_side"] is True and forbidden_paths(answer) == []
    assert {r["provider"] for a in answer["places"] for r in a["answer"]["results"]} == {"eurostat-sts",
                                                                                          "us-census-cbp"}
