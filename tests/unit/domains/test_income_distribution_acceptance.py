"""Offline country-to-poverty-and-inequality acceptance for the Society bundle's ``society.income`` (IP12, #2643).

The pinned World Bank PIP, Eurostat EU-SILC and OECD IDD fixtures (authored in the documented shapes; every value is
fictional) replay through the real ``income-distribution`` adapter - the SDMX sources through the SDMX connector - and
the runtime's projector, with the Society bundle and its link features selected in the composition plan and sockets
blocked. The journey takes a country to cited poverty and inequality figures from each source side by side, with
definitions, release vintages (a PPP revision among them) and comparability notes. Offline evidence only, never live
coverage (``docs/development/income-distribution-evidence/``).
"""

from __future__ import annotations

import asyncio
import socket

import duckdb
import pytest

from src.domains import registry as domain_registry
from src.ingestion.income_distribution_sources import personal_keys
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.kb.income_distribution_identity import IncomeIdentity
from src.kb.income_distribution_links import IncomeLinks
from src.kb.income_distribution_monitoring import IncomeMonitor
from src.kb.income_distribution_queries import IncomeQueries
from src.kb.income_distribution_records import (
    IncomeError,
    feature_enabled,
    forbidden_keys,
    minimised,
)
from src.kb.income_distribution_store import IncomeStore, readiness
from tests.unit import demographics_harness as dh
from tests.unit import income_distribution_harness as h
from tests.unit import labour_harness as lh
from tests.unit.composition.test_migration import _migrated

FEATURES = ["pip", "eu-silc", "oecd-idd", "demographics-links", "labour-links"]


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
        "SELECT v.series_id, o.period, o.value FROM income_observations o JOIN income_vintages v ON "
        "v.namespace=o.namespace AND v.vintage_id=o.vintage_id").fetchall()}


def accept_all(identity, proposed, namespace=h.NS):
    for assertion in proposed:
        if assertion["state"] == "proposed":
            identity.review(namespace, assertion["assertion_id"], "accept", "published code",
                            principal_id="reviewer", scopes=h.SCOPES)


def test_country_to_cited_poverty_and_inequality_figures_from_each_source_side_by_side(tmp_path, monkeypatch):
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("society", bundles["society"]["version"], features=FEATURES)
    assert coordinator.activate("income-acceptance")["status"] == "published"
    assert all(feature_enabled(conn, f) for f in FEATURES)
    assert "noesis-income-distribution-record-v1" in PROJECTORS

    # The pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    replay = SourcePackConformance(h.ROOT).offline(h.manifest())
    assert replay["valid"] and replay["coverage"]["verified"] == 3

    # First releases (retrieved 2099-12-01), a monitor on Germany, then every source re-published (2100-01-01).
    h.load_all(conn)
    monitor = IncomeMonitor(conn, now=lambda: h.FIRST_RETRIEVAL + 1)
    watch = monitor.create(h.NS, "germany", target={"place": {"scheme": "iso3166-1-alpha3", "code": "DEU"}},
                           principal_id="alice", scopes=h.SCOPES)
    assert {n["kind"] for n in monitor.run(watch["subscription_id"], principal_id="alice",
                                           scopes=h.SCOPES)["notifications"]} == {"new_period"}
    h.load_all(conn, revisions=True)
    status = readiness(conn)
    assert all(p["live_verification"] == "unverified-live" and p["live_releases"] == 0
               for p in status["providers"].values())

    # Cross-pack stores the links point into: Labour and Demographics fixtures.
    lh.load_all(conn)
    dh.load_all(conn)

    # Reviewable identity: places by published ISO, Eurostat GEO and World Bank region codes; related indicators.
    places = h.register_places(conn)
    identity = IncomeIdentity(conn)
    proposed = identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace="geo")
    assert {a["subject"]["code"]: a["target"]["place_id"] for a in proposed["assertions"]
            if a["subject"]["code"] in {"DEU", "DE"}} == {"DEU": places["de"], "DE": places["de"]}
    accept_all(identity, proposed["assertions"])
    from src.kb.labour_identity import LabourIdentity

    labour_identity = LabourIdentity(conn)
    for a in labour_identity.propose_places(lh.NS, principal_id="analyst", scopes=lh.SCOPES,
                                            geo_namespace="geo")["assertions"]:
        if a["state"] == "proposed":
            labour_identity.review(lh.NS, a["assertion_id"], "accept", "code", principal_id="reviewer",
                                   scopes=lh.SCOPES)
    accept_all(identity, identity.propose_related(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"])

    # Cross-pack links by citation, shared identifier and accepted match, pointing at record revisions.
    links = IncomeLinks(conn)
    demographics = links.link_demographics(h.NS, principal_id="svc", scopes=h.SCOPES)
    labour = links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert demographics["linked"] and labour["linked"] and labour["missing"]
    made = [links.link(h.NS, i) for i in demographics["linked"] + labour["linked"]]
    assert {m["basis"] for m in made} == {"shared_identifier", "accepted_match"}
    assert all(m["income_vintage_id"] and m["target"]["vintage_id"] for m in made)

    queries = IncomeQueries(conn)
    before = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"],
                                         as_of_ms=h.day_ms("2026-07-01"))
    after = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["de"],
                                        as_of_ms=h.day_ms("2100-02-01"), history=True)
    # Each source side by side for Germany, never one series.
    assert {r["provider"] for r in after["results"]} == {"pip", "eu-silc", "oecd-idd"}
    gini = {(r["provider"], r["methodology_version"] or "") for r in after["results"]
            if r["indicator"]["concept"] == "gini_index"}
    assert len(gini) == 4  # PIP, EU-SILC and both OECD methodologies
    assert all(p["combined"] is False for p in after["comparability"])
    silc = lambda answer: next(r for r in answer["results"] if r["provider"] == "eu-silc"
                               and r["indicator"]["concept"] == "poverty_headcount_ratio")
    assert {v["period"]: v["value"] for v in silc(before)["values"]}["2098"] == "14.4"
    assert {v["period"]: v["value"] for v in silc(after)["values"]}["2098"] == "14.3"
    assert silc(after)["vintage"]["revision_of"] == silc(before)["vintage"]["vintage_id"]
    assert silc(after)["definition"]["content"]["equivalence_scale"]["code"] == "modified-oecd"
    pip = next(r for r in after["results"] if r["provider"] == "pip" and r["indicator"]["code"] == "headcount")
    assert pip["poverty_line"]["ppp_base_year"] == "2017" and pip["welfare_concept"] == "income"
    assert pip["revision_history"][-1]["changes"]["ppp_revision"] is True
    assert pip["related_series"] and any(n["relation"] == "break_in_series" for n in pip["source_notes"])
    for result in after["results"]:
        for value in result["values"]:
            citation = value["citation"]
            assert citation["vintage_id"] and citation["definition_id"] and citation["retrieved_at"]
            assert citation["as_of"] == after["as_of"]

    # History across releases and the PPP revision, with comparability stated or unknown.
    history = queries.history(h.NS, pip["series_id"], scopes=h.READ_ONLY)
    assert history["comparability"][0]["status"] == "noted"
    threshold = h.series_where(conn, "eu-silc", concept="poverty_threshold")
    assert queries.history(h.NS, threshold["series_id"], scopes=h.READ_ONLY)["comparability"][0]["status"] == (
        "comparability_unknown")

    # A subject with no records, a withdrawn series and a confidential cell stay explicit.
    france = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place={"scheme": "iso3166-1-alpha3",
                                                                          "code": "FRA"})
    assert france["status"] == "none_published" and france["results"] == []
    austria = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["at"], concept="gini_index")
    assert austria["unavailable_by_as_of"][0]["reason"] == "withdrawn_by_source"
    berlin = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place=places["be"])
    assert berlin["results"][0]["withheld_periods"][0]["status"] == "confidential"

    # The evidence bundle cites every value with source, record revision and as-of time.
    bundle = queries.export_bundle(after, created_at_ms=h.SECOND_RETRIEVAL)
    cited = [o["payload"] for o in bundle["objects"] if o["payload"].get("kind") == "income-value"]
    assert len(cited) == sum(len(r["values"]) for r in after["results"])
    assert all(c["source"]["file_sha256"] and c["record_revision"]["vintage_id"] and c["as_of"] for c in cited)

    # The subscription hears the PPP revision, revisions and new years once; a restart replays nothing.
    later = IncomeMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 1)
    kinds = {n["kind"] for n in later.run(watch["subscription_id"], principal_id="alice",
                                          scopes=h.SCOPES)["notifications"]}
    assert {"ppp_revision", "revised_value", "new_period", "definition_change"} <= kinds
    assert IncomeMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 2).run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []

    # Exclusions and minimisation: only stored published values, no later year, no derived key, no person data.
    published = stored_values(conn)
    latest = {}
    for series_id, period, _ in published:
        latest[series_id] = max(latest.get(series_id, period), period)
    for answer in (before, after, france, austria, berlin, history):
        assert forbidden_keys(answer) == [] and personal_keys(answer) == []
        for result in answer.get("results") or []:
            for value in result["values"]:
                assert (result["series_id"], value["period"], value["value"]) in published
                assert value["period"] <= latest[result["series_id"]]
    assert "setting or applying poverty lines no source published" in after["exclusions"]
    with pytest.raises(IncomeError):
        minimised({"results": [{"household_id": "H-1"}]})

    # Re-ingestion adds nothing.
    releases = len(IncomeStore(conn).releases(h.NS))
    h.load_all(conn, revisions=True)
    assert len(IncomeStore(conn).releases(h.NS)) == releases

    # The MCP tools answer the same journey from a file-backed store with read scopes only.
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "income-acceptance.duckdb")
    file_conn = duckdb.connect(path)
    h.load_all(file_conn, revisions=True)
    file_conn.close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", set(h.READ_ONLY)))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    answer = tools["income_indicator_for_place"].fn(
        namespace="global", place={"scheme": "iso3166-1-alpha3", "code": "DEU"}, concept="gini_index",
        as_of_ms=h.day_ms("2100-02-01"))
    assert answer["side_by_side"] is True and {r["provider"] for r in answer["results"]} == {"pip", "oecd-idd"}
    assert forbidden_keys(answer) == [] and answer["minimisation"]
