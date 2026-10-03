"""Offline place-to-tourism acceptance for the Economics tourism-statistics feature (TO11, #2739).

The pinned Eurostat tourism occupancy and capacity fixtures (authored in the documented shapes; every value and every
reference period is fictional) replay through the real ``tourism-statistics`` adapter - through the SDMX connector -
and the runtime's projector, with the ``tourism-statistics`` feature selected in the composition plan and sockets
blocked. The journey takes Germany and Berlin to cited occupancy and capacity figures with definitions, vintages and
flags: monthly and annual series kept apart, provisional months revised in a later vintage, a confidential cell
returned as its status, as-of answers, history with comparability notes, NUTS identity review across a NUTS version
change, Geospatial boundary and Labour links, a place with no records, and UN Tourism reported not-implemented.
Offline evidence only, never live coverage (``docs/development/tourism-evidence/``).
"""

from __future__ import annotations

import asyncio
import socket

import duckdb
import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.ingestion.tourism_sources import EXCLUSIONS, LIVE_VERIFICATION
from src.kb.tourism_identity import TourismIdentity
from src.kb.tourism_links import TourismLinks
from src.kb.tourism_monitoring import TourismMonitor
from src.kb.tourism_queries import TourismQueries
from src.kb.tourism_records import (
    MINIMISATION,
    feature_enabled,
    forbidden_paths,
    personal_data_paths,
    readiness,
)
from src.kb.tourism_store import TourismStore
from tests.unit import tourism_harness as h
from tests.unit.composition.test_migration import _migrated

GERMANY = {"scheme": "eurostat-geo", "code": "DE", "nuts_version": "2021"}
BERLIN = {"scheme": "eurostat-geo", "code": "DE30", "nuts_version": "2021"}


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
        "SELECT v.series_id, o.period, o.value FROM tourism_observations o JOIN tourism_vintages v ON "
        "v.namespace=o.namespace AND v.vintage_id=o.vintage_id").fetchall()}


def review_all(identity, proposed, scopes):
    for assertion in proposed:
        if assertion["state"] == "proposed":
            identity.review(h.NS, assertion["assertion_id"], "accept", "published code or correspondence row",
                            principal_id="reviewer", scopes=scopes)


def test_place_to_cited_occupancy_and_capacity_with_definitions_vintages_and_flags(tmp_path, monkeypatch):
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("economics", bundles["economics"]["version"],
                       features=["tourism-statistics", "labour-statistics"])
    assert coordinator.activate("tourism-acceptance")["status"] == "published"
    assert feature_enabled(conn) and "noesis-tourism-statistics-record-v2" in PROJECTORS

    # The pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    manifest = h.manifest()
    tourism = [s for s in manifest["sources"] if s.get("connector") == "tourism-statistics"]
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": tourism})
    assert replay["valid"] and replay["coverage"]["verified"] == 2

    # First releases (retrieved 2024-04-01) from each source; a monitor watches Germany's monthly series.
    h.load_all(conn)
    monitor = TourismMonitor(conn, now=lambda: h.FIRST_RETRIEVAL + 1)
    watch = monitor.create(h.NS, "germany", target={"area": GERMANY, "frequency": "monthly"}, principal_id="alice",
                           scopes=h.SCOPES)
    first_notices = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in first_notices} == {"new_release"} and len(first_notices) == 6
    # Revisions (provisional months revised, a new month, a series removed) and the NUTS 2024 re-declaration.
    h.load_all(conn, revisions=True, nuts2024=True)
    status = readiness(conn)
    assert status["selected"] and status["providers"]["eurostat-tourism-occupancy"]["live_verification"] == \
        "unverified-live"
    assert status["providers"]["un-tourism"]["access_decision"] == "not-implemented"

    # As of 2024-04-30, Germany's monthly nights spent per residence, provisional April flagged; cited per vintage.
    queries = TourismQueries(conn)
    april = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, concept="nights_spent",
                                        period="2096-04", as_of="2024-04-30")
    assert {r["residence"]["code"] for r in april["results"]} == {"TOTAL", "DOM", "FOR"}
    total = next(r for r in april["results"] if r["residence"]["code"] == "TOTAL")
    (cell,) = total["values"]
    assert (cell["value"], cell["flags"]) == ("30112600", {"OBS_FLAG": "p"})
    assert total["vintage"]["release_at"] == "2024-02-12T11:00:00Z" and total["citation"]["release_basis"] == \
        "provider_last_update"
    assert total["definition"]["content"]["source_text"].startswith("Nights spent")
    assert total["coverage_threshold"].startswith("establishments with 10 or more bed places")
    # A later date selects the revised vintage; the provisional month is restated without the flag.
    later = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, concept="nights_spent",
                                        residence="TOTAL", period="2096-04", as_of="2024-05-31")
    (revised,) = later["results"]
    assert revised["values"][0]["value"] == "30245100" and revised["values"][0]["flags"] == {}
    assert revised["vintage"]["revision_of"] == total["vintage"]["vintage_id"]
    # Monthly and annual are never combined: an annual period for Germany is not computed from months.
    annual = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, concept="nights_spent",
                                         period="2096")
    assert annual["results"] == [] and annual["other_frequency"]

    # Reviewable identity: place keys by published code; the NUTS 2024 key only through the correspondence.
    places = h.register_places(conn, keys=("de", "berlin", "berlin-2024"))
    identity = TourismIdentity(conn)
    proposed = identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"]
    review_all(identity, proposed, h.SCOPES)
    keys = {(a["subject"]["nuts_version"], a["subject"]["code"]): a for a in identity.assertions(
        h.NS, scopes=h.READ_ONLY, kind="area")}
    assert keys[("2021", "DE30")]["target"]["place_id"] == places["berlin"]
    assert keys[("2024", "DE30")]["target"]["place_id"] == places["berlin-2024"]
    assert keys[("2021", "DE30")]["state"] == keys[("2024", "DE30")]["state"] == "accepted"
    identity.import_correspondence(h.NS, h.correspondence_tables()[0], principal_id="op", scopes=h.SCOPES)
    review_all(identity, identity.propose_correspondence_links(h.NS, principal_id="analyst",
                                                               scopes=h.SCOPES)["assertions"], h.SCOPES)

    # Berlin through its place id: the annual NUTS 2 nights with a confidential cell, and capacity per value.
    berlin = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["berlin"], as_of="2024-07-31")
    assert set(berlin["by_frequency"]) == {"annual"}
    nights = next(r for r in berlin["results"] if r["dataset"] == "tour_occ_nin2")
    cells = {v["period"]: v for v in nights["values"]}
    assert cells["2095"]["status"] == "confidential" and cells["2095"]["value"] is None
    assert cells["2094"]["flags"] == {"OBS_FLAG": "d"}
    assert any(n["relation"] == "definition_differs" for n in nights["notes"])
    beds = next(r for r in berlin["results"] if r["indicator"]["code"] == "BEDPL")
    assert all("31 July" in v["attributes"]["capacity_reference"]["stated"] for v in beds["values"])
    assert {v["period"]: v["value"] for v in beds["values"]}["2097"] == "155300"
    # The NUTS 2024 Berlin reaches the 2021 rows only through the accepted correspondence, labelled as such.
    berlin_2024 = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place={**BERLIN, "nuts_version": "2024"},
                                              concept="bed_places")
    bases = {r["area"]["nuts_version"]: r["area_basis"] for r in berlin_2024["results"]}
    assert bases == {"2024": "published-code", "2021": "accepted NUTS correspondence (unchanged)"}

    # History with comparability notes: provisional revised, the NUTS change and removal, comparability_unknown.
    beds_2021 = h.series_by(conn, indicator="BEDPL", nuts_version="2021")
    history = queries.series_history(h.NS, beds_2021["series_id"], scopes=h.READ_ONLY)
    assert [v["status"] for v in history["vintages"]] == ["published", "published", "removed"]
    assert all(v["citation"]["vintage_id"] == v["vintage_id"] for v in history["vintages"])
    revision_pair, removal_pair = history["pairs"]
    assert "provisional_revised" in {n["kind"] for n in revision_pair["notes"]}
    assert {"removed_by_source", "nuts_version_change"} <= {n["kind"] for n in removal_pair["notes"]}
    arrivals_dom = h.series_by(conn, dataset="tour_occ_arm", residence="DOM")
    arrivals_history = queries.series_history(h.NS, arrivals_dom["series_id"], scopes=h.READ_ONLY)
    assert arrivals_history["pairs"][0]["comparability"] == "noted"

    # Links: Geospatial boundaries by shared NUTS code and Labour section I series by shared code, both pinned.
    h.register_boundaries(conn, version="2021", codes=("DE", "DE30"))
    h.register_boundaries(conn, version="2024", codes=("DE30",))
    h.load_labour_accommodation(conn)
    links = TourismLinks(conn)
    links.link_boundaries(h.NS, principal_id="svc", scopes=h.SCOPES, geo_namespace=h.GEO_NS)
    links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    linked = links.links(h.NS, scopes=h.READ_ONLY, state="linked")
    assert {(link["kind"], link["basis"]) for link in linked} == {("boundary", "shared-identifier"),
                                                                 ("labour", "shared-identifier")}
    boundary_2024 = next(link for link in linked if link["kind"] == "boundary"
                         and link["reference"]["nuts_version"] == "2024")
    assert boundary_2024["target"]["collection"] == "gisco:nuts:2024" and boundary_2024["target"]["revision_id"]
    labour = next(link for link in linked if link["kind"] == "labour" and link["reference"]["code"] == "DE30")
    assert labour["target"]["sector"]["code"] == "I" and labour["evidence"]["labour_citation"]["vintage_id"]

    # A place with no records: reported, never an empty success; UN Tourism is not-implemented, never empty.
    paris = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place={"scheme": "eurostat-geo", "code": "FR10",
                                                                          "nuts_version": "2021"})
    assert paris["status"] == "none_published" and paris["results"] == []
    assert {n["provider"] for n in paris["none_on_record"]} == {"eurostat-tourism-occupancy",
                                                                 "eurostat-tourism-capacity"}
    untourism = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=GERMANY, providers=["un-tourism"])
    assert untourism["status"] == "not-implemented" and untourism["not_implemented"]
    assert LIVE_VERIFICATION["un-tourism"]["status"] == "not-implemented"

    # Evidence bundle: every item cites source, record revision and as-of time.
    bundle = queries.evidence_bundle(berlin)
    assert all(a["dependencies"][0]["revision"] and a["dependencies"][0]["as_of"] and a["citations"]
               for a in bundle["sections"][0]["assertions"])

    # The subscription hears the revisions, the new month and the removal once; a restart replays nothing.
    later_monitor = TourismMonitor(conn, now=lambda: h.THIRD_RETRIEVAL + 1)
    kinds = {n["kind"] for n in later_monitor.run(watch["subscription_id"], principal_id="alice",
                                                  scopes=h.SCOPES)["notifications"]}
    assert {"new_period", "revised_value", "removed_by_source"} <= kinds
    assert TourismMonitor(conn, now=lambda: h.THIRD_RETRIEVAL + 2).run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []

    # Exclusions and the TO01 minimisation decision: every shown value is a stored published value.
    published = stored_values(conn)
    for answer in (april, later, berlin, berlin_2024, paris):
        assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
        assert answer["exclusions"] == list(EXCLUSIONS)
        assert answer["minimisation"] == MINIMISATION["decision"]
        for result in answer["results"]:
            for value in result["values"]:
                assert (result["series_id"], value["period"], value["value"]) in published
                if value["value"] is None:
                    assert value["status"] in {"confidential", "not_published"}
    assert {"blending Eurostat and UN Tourism figures", "annual totals computed from months"} <= set(EXCLUSIONS)
    datasets = {r[0] for r in conn.execute("SELECT DISTINCT dataset FROM tourism_series").fetchall()}
    assert not any(d.startswith(("tour_dem_", "tour_ce_oa")) for d in datasets)

    # Re-ingestion adds nothing.
    releases = len(TourismStore(conn).releases(h.NS))
    h.load_all(conn, revisions=True, nuts2024=True)
    assert len(TourismStore(conn).releases(h.NS)) == releases

    # The MCP tools answer the same journey from a file-backed store with read scopes only.
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "tourism-acceptance.duckdb")
    file_conn = duckdb.connect(path)
    h.load_all(file_conn, revisions=True)
    file_conn.close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", set(h.READ_ONLY)))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    answer = tools["tourism_indicator_for_place"].fn(namespace="global", place=GERMANY, concept="arrivals",
                                                     as_of="2024-05-31")
    assert answer["status"] == "reported" and forbidden_paths(answer) == []
    statuses = {r["residence"]["code"]: (r["status"], len(r["values"])) for r in answer["results"]}
    # The foreign-residence series the source no longer states is shown as removed, never silently dropped.
    assert statuses["FOR"] == ("removed_by_source", 0)
    assert statuses["TOTAL"][0] == statuses["DOM"][0] == "available"
