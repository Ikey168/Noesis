"""Offline place-to-labour-indicators acceptance for the Economics labour-statistics feature (LB12, #2490).

The pinned ILOSTAT, OECD, Eurostat LFS and BLS fixtures (authored in the documented shapes; every value is
fictional) replay through the real ``labour-statistics`` adapter - the SDMX sources through the SDMX connector - and
the runtime's projector, with the ``labour-statistics`` feature selected in the composition plan and sockets
blocked. The journey takes a place (and a sector and an occupation) to cited labour indicators with definitions
and vintages, including a revised value and a benchmark revision. Offline evidence only, never live coverage
(``docs/development/labour-evidence/``).
"""

from __future__ import annotations

import asyncio
import socket

import duckdb
import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.kb.labour_identity import LabourIdentity
from src.kb.labour_links import LabourLinks
from src.kb.labour_monitoring import LabourMonitor
from src.kb.labour_statistics import (
    LabourComparability,
    LabourQueries,
    LabourStore,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from tests.unit import labour_harness as h
from tests.unit.composition.test_migration import _migrated


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
    """Every value the stores hold, as published: an answer may only show these."""
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT v.series_id, o.period, o.value FROM labour_observations o JOIN labour_vintages v ON "
        "v.namespace=o.namespace AND v.vintage_id=o.vintage_id").fetchall()}


def review_all(identity, proposed):
    for assertion in proposed:
        if assertion["state"] == "proposed":
            identity.review(h.NS, assertion["assertion_id"], "accept", "published code or concordance row",
                            principal_id="reviewer", scopes=h.SCOPES)


def test_place_sector_and_occupation_to_cited_labour_indicators_with_definitions_and_vintages(tmp_path, monkeypatch):
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("economics", bundles["economics"]["version"], features=["labour-statistics"])
    assert coordinator.activate("labour-acceptance")["status"] == "published"
    assert feature_enabled(conn) and "noesis-labour-statistics-record-v1" in PROJECTORS

    # The pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    manifest = h.manifest()
    labour = [s for s in manifest["sources"] if s["connector"] == "labour-statistics"]
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": labour})
    assert replay["valid"] and replay["coverage"]["verified"] == 4

    # First releases (2024-12-01), then every source re-published (2025-01-01).
    h.load_all(conn)
    monitor = LabourMonitor(conn, now=lambda: h.FIRST_RETRIEVAL + 1)
    ces_id = h.series_by_key(conn, "bls", "CES3000000001")["series_id"]
    watch = monitor.create(h.NS, "manufacturing", target={"series_id": ces_id}, principal_id="alice",
                           scopes=h.SCOPES)
    assert [n["kind"] for n in monitor.run(watch["subscription_id"], principal_id="alice",
                                           scopes=h.SCOPES)["notifications"]] == ["new_period"]
    h.load_all(conn, revisions=True)
    status = readiness(conn)
    assert status["selected"] and all(p["live_verification"] == "unverified-live" for p in status["providers"].values())

    # Reviewable identity: places by published code, sectors and occupations through cited concordances.
    places = h.register_places(conn)
    h.import_concordances(conn)
    identity = LabourIdentity(conn)
    review_all(identity, identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES,
                                                 geo_namespace="geo")["assertions"])
    review_all(identity, identity.propose_classifications(h.NS, "sector", {"scheme": "ISIC", "version": "Rev.4"},
                                                          principal_id="analyst", scopes=h.SCOPES)["assertions"])
    review_all(identity, identity.propose_classifications(h.NS, "occupation", {"scheme": "ISCO", "version": "08"},
                                                          principal_id="analyst", scopes=h.SCOPES)["assertions"])

    # Comparability notes: source-stated breaks plus reviewed definition differences.
    links = LabourLinks(conn)
    notes = links.propose_definition_notes(h.NS, principal_id="analyst", scopes=h.SCOPES)["proposed"]
    comparability = LabourComparability(conn)
    for note_id in notes:
        comparability.review(h.NS, note_id, "accept", "as each source states", principal_id="reviewer",
                             scopes=h.SCOPES)
    cited = links.link_references(h.NS, principal_id="svc", scopes=h.SCOPES)
    denominators = links.link_denominators(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert cited["unresolved"] and denominators["unresolved"] and not denominators["linked"]

    queries = LabourQueries(conn)
    before = queries.indicators(h.NS, place=places["de"], concept="unemployment_rate", scopes=h.READ_ONLY,
                                as_of_ms=h.day_ms("2024-12-15"))
    after = queries.indicators(h.NS, place=places["de"], concept="unemployment_rate", scopes=h.READ_ONLY,
                               as_of_ms=h.day_ms("2025-02-01"), history=True)
    # Side by side: every definition basis the sources publish for Germany, none merged.
    bases = {(r["provider"], r["definition_basis"], r["estimate_type"], r["seasonal_adjustment"])
             for r in after["results"]}
    assert bases == {("ilostat", "national", "national-reported", "NSA"),
                     ("ilostat", "ilo-harmonised", "ilo-modelled", "NSA"),
                     ("oecd", "oecd-harmonised", "harmonised", "SA"), ("oecd", "oecd-harmonised", "harmonised", "NSA"),
                     ("eurostat-lfs", "eu-lfs", "survey", "NSA")}
    national = {r["estimate_type"]: r for r in before["results"] if r["provider"] == "ilostat"}["national-reported"]
    national_after = {r["estimate_type"]: r for r in after["results"] if r["provider"] == "ilostat"}[
        "national-reported"]
    assert {v["period"]: v["value"] for v in national["values"]}["2098"] == "3.1"
    assert {v["period"]: v["value"] for v in national_after["values"]}["2098"] == "3.0"
    assert national_after["vintage"]["revision_of"] == national["vintage"]["vintage_id"]
    assert national_after["definition"]["content"]["references"][0]["identifier"] == "19th ICLS Resolution I (2013)"
    eurostat = next(r for r in after["results"] if r["provider"] == "eurostat-lfs")
    assert eurostat["vintage"]["release_at"].startswith("2024-04-15")
    assert any(n["relation"] == "break_in_series" and n["periods"] == ["2098"] for n in eurostat["source_notes"])
    assert any(p["notes"] for p in after["comparability"])
    for result in after["results"]:
        for value in result["values"]:
            assert value["citation"]["vintage_id"] and value["citation"]["retrieved_at"]
            assert value["seasonal_adjustment"] == result["seasonal_adjustment"] and "flags" in value

    # Explicit gaps: Berlin's confidential year, a BLS value not published; unmapped codes listed.
    berlin = queries.indicators(h.NS, place=places["be"], scopes=h.READ_ONLY)
    assert berlin["results"][0]["gaps"]["withheld_periods"][0]["status"] == "confidential"
    us = queries.indicators(h.NS, place=places["us"], concept="job_vacancies", scopes=h.READ_ONLY)
    assert us["results"][0]["gaps"]["withheld_periods"][0]["status"] == "not_published"

    # Sector and occupation journeys through the accepted mappings, with the relation shown.
    manufacturing = queries.indicators(h.NS, sector={"scheme": "ISIC", "version": "Rev.4", "code": "C"},
                                       concept="employment", scopes=h.READ_ONLY, history=True)
    relations = {r["provider"]: r["matched_by"]["sector"]["relation"] for r in manufacturing["results"]}
    assert relations == {"ilostat": "exact", "eurostat-lfs": "exact", "bls": "partial"}
    ces = next(r for r in manufacturing["results"] if r["provider"] == "bls")
    assert ces["revision_history"][-1]["changes"]["benchmark_revision"] is True
    assert {v["period"]: v["value"] for v in ces["values"]}["2098-10"] == "12880"
    professionals = queries.indicators(h.NS, occupation={"scheme": "ISCO", "version": "08", "code": "2"},
                                       scopes=h.READ_ONLY)
    assert {r["provider"] for r in professionals["results"]} == {"ilostat", "eurostat-lfs", "bls"}

    # The subscription hears the benchmark revision and the new month once; a restart replays nothing.
    later = LabourMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 1)
    kinds = sorted(n["kind"] for n in later.run(watch["subscription_id"], principal_id="alice",
                                                scopes=h.SCOPES)["notifications"])
    assert kinds == ["benchmark_revision", "new_period"]
    assert LabourMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 2).run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []

    # No derived, blended or forecast value: every shown value is a stored published value, no later period.
    published = stored_values(conn)
    latest = {}
    for series_id, period, _ in published:
        latest[series_id] = max(latest.get(series_id, period), period)
    for answer in (before, after, berlin, us, manufacturing, professionals):
        assert forbidden_keys(answer) == []
        for result in answer["results"]:
            for value in result["values"]:
                assert (result["series_id"], value["period"], value["value"]) in published
                assert value["period"] <= latest[result["series_id"]]
                if value["value"] is None:
                    assert value["status"] in {"confidential", "not_published"}
        assert all("value" not in pair for pair in answer["comparability"])

    # Re-ingestion adds nothing.
    releases = len(LabourStore(conn).releases(h.NS))
    h.load_all(conn, revisions=True)
    assert len(LabourStore(conn).releases(h.NS)) == releases

    # The MCP tools answer the same journey from a file-backed store with read scopes only.
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "labour-acceptance.duckdb")
    file_conn = duckdb.connect(path)
    h.load_all(file_conn, revisions=True)
    file_conn.close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", set(h.READ_ONLY)))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    answer = tools["labour_indicators_for_place"].fn(
        namespace="global", place={"scheme": "iso3166-1-alpha3", "code": "DEU"}, concept="unemployment_rate",
        as_of_ms=h.day_ms("2025-02-01"))
    assert answer["side_by_side"] is True and len(answer["results"]) == 4 and forbidden_keys(answer) == []
