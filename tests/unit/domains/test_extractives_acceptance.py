"""Offline company-to-extractive-payments acceptance for the Economics extractives features (EX12, #2713).

The pinned EITI, USGS and BGS fixtures (authored in the documented shapes; every value, company and project is
fictional) replay through the real ``extractives`` adapter and the runtime's projector with the three extractives
features selected in the composition plan and sockets blocked. The Corporate Ownership fixtures (the Exampla group)
supply the company side. The journey takes a company (and its group) and a country to cited extractive payments
per EITI report revision and to production figures per source and vintage, with revision history, as-of answers,
reviewable identity, cross-pack links and a subject with no records. Offline evidence only, never live coverage
(``docs/development/extractives-evidence/``).
"""

from __future__ import annotations

import asyncio
import json
import socket

import duckdb
import pytest

from src.domains import registry as domain_registry
from src.evidence_bundle.verifier import verify_bundle
from src.ingestion.extractives_sources import EXCLUSIONS, personal_keys
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.kb.extractives_links import ExtractivesLinks
from src.kb.extractives_monitoring import ExtractivesMonitor
from src.kb.extractives_queries import ExtractivesQueries
from src.kb.extractives_records import feature_enabled, forbidden_keys
from src.kb.extractives_store import ExtractivesStore, readiness
from tests.unit import extractives_harness as h
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


def stored_amounts(conn):
    return {r[0] for r in conn.execute("SELECT amount_text FROM ex_payments").fetchall()} | {
        r[0] for r in conn.execute("SELECT discrepancy_text FROM ex_discrepancies").fetchall()} | {
        r[0] for r in conn.execute("SELECT government_amount_text FROM ex_discrepancies").fetchall()} | {
        r[0] for r in conn.execute("SELECT company_amount_text FROM ex_discrepancies").fetchall()}


def stored_values(conn):
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT v.series_id, o.period, o.value FROM ex_observations o JOIN ex_vintages v ON "
        "v.namespace=o.namespace AND v.vintage_id=o.vintage_id").fetchall()}


def test_company_and_country_to_cited_extractive_payments_and_production_with_revisions(tmp_path, monkeypatch):
    conn, coordinator, bundles, _ = _migrated(h.connection())
    features = ["extractives-bgs", "extractives-eiti", "extractives-usgs"]
    coordinator.select("economics", bundles["economics"]["version"], features=features)
    assert coordinator.activate("extractives-acceptance")["status"] == "published"
    assert feature_enabled(conn) and all(feature_enabled(conn, f) for f in features)
    assert "noesis-extractives-record-v1" in PROJECTORS

    # The pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    manifest = h.manifest()
    extractives = [s for s in manifest["sources"] if s["connector"] == "extractives"]
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": extractives})
    assert replay["valid"] and replay["coverage"]["verified"] == 3

    # First publications, a monitor on the company group, then the revised EITI report and the next releases.
    h.load_first(conn)
    status = readiness(conn)
    assert status["selected"] and {p["live_verification"] for p in status["providers"].values()} == {
        "unverified-live"}
    reviewed = h.reviewed(conn)  # loads the later publications, the ownership fixtures, reviews identity
    identity = reviewed["identity"]
    monitor = ExtractivesMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 1)
    watch = monitor.create(h.NS, "exampla", target={"company": h.HOLD_ENTITY, "ownership_namespace": h.OWN_NS,
                                                    "group": True}, principal_id="alice", scopes=h.SCOPES)
    heard = sorted(n["kind"] for n in monitor.run(watch["subscription_id"], principal_id="alice",
                                                  scopes=h.SCOPES)["notifications"])
    assert heard == ["new_report", "new_report", "report_revision"]
    assert ExtractivesMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 2).run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []

    # Reviewable identity: identifier matches accepted, name-only candidates rejected, the rest unmatched.
    candidates = identity.company_candidates(h.NS, scopes=h.SCOPES)
    assert {c["state"] for c in candidates if c["method"] == "exact-identifier"} == {"accepted"}
    assert {c["state"] for c in candidates if c["low_evidence"]} <= {"rejected"}
    unmatched = {u["name_as_reported"] for u in identity.unmatched_companies(h.NS, scopes=h.SCOPES)}
    assert unmatched == {"Andes Cobre S.A. (fixture)", "[natural person - redacted]"}

    # Cross-pack links by shared identifier or accepted match, missing targets reported.
    links = ExtractivesLinks(conn)
    h.seed_trade(conn)
    h.seed_public_finance(conn)
    links.link_trade(h.NS, principal_id="svc", scopes=h.SCOPES)
    links.link_public_finance(h.NS, principal_id="svc", scopes=h.SCOPES)
    links.link_infrastructure(h.NS, principal_id="svc", scopes=h.SCOPES)
    energy = links.link_energy(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert energy["unresolved"] and not energy["linked"]  # the Energy store is not held: reported, not dropped
    owners = {link["target_owner"] for link in links.links(h.NS, scopes=h.READ_ONLY, state="linked")}
    assert owners == {"economics.trade", "economics.public-finance", "geospatial.infrastructure"}

    queries = ExtractivesQueries(conn)
    before = queries.payments_for_company(h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, group=True,
                                          scopes=h.SCOPES, as_of_ms=h.day_ms("2100-06-30"))
    after = queries.payments_for_company(h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, group=True,
                                         scopes=h.SCOPES, as_of_ms=h.day_ms("2100-12-31"), history=True)
    fy98 = {a: next(r for r in ans["reports"] if r["citation"]["fiscal_period"]["start"] == "2098-01-01")
            for a, ans in (("before", before), ("after", after))}
    assert (fy98["before"]["citation"]["revision"], fy98["after"]["citation"]["revision"]) == (1, 2)
    assert fy98["after"]["citation"]["revision_of"] == fy98["before"]["citation"]["report_id"]

    def royalties(report):
        stream = next(s for s in report["streams"] if s["company"]["name_as_reported"] == "Exampla Intermediate B.V.")
        return ([f["amount_text"] for f in stream["government_reported"]],
                [f["amount_text"] for f in stream["company_reported"]],
                [d["discrepancy_text"] for d in stream["discrepancies"]])

    assert royalties(fy98["before"]) == (["790000.00"], ["800000.00"], ["-10000.00"])
    assert royalties(fy98["after"]) == (["800000.00"], ["800000.00"], ["0.00"])
    currencies = {c for r in after["reports"] for s in r["streams"] for c in s["currencies"]}
    assert currencies == {"PEN", "USD"}  # kept apart, never converted or summed

    production = queries.production(h.NS, commodity={"hs_code": "2603"}, country="PER", statistic="production",
                                    scopes=h.READ_ONLY, history=True)
    assert set(production["by_source"]) == {"usgs-mcs", "bgs-wms"} and production["never_blended"]
    for result in production["results"]:
        assert result["vintage"]["source_revision"]["licence"]["terms"]
        assert all(v["citation"]["vintage_id"] for v in result["values"])
    lithium = queries.production(h.NS, commodity="Lithium", country="United States", scopes=h.READ_ONLY,
                                 statistic="production")
    assert lithium["results"][0]["marked"]["withheld"] == ["2098", "2099"]

    # A subject with no records answers explicitly, never zero.
    trading = queries.payments_for_company(h.NS, "gleif:lei:213800EXAMPLATRADE88", ownership_namespace=h.OWN_NS,
                                           scopes=h.SCOPES)
    assert trading["status"] == "no_payment_on_record"
    assert queries.production(h.NS, commodity="Lithium", country="PER", scopes=h.READ_ONLY)["status"] == \
        "none_published"

    # Evidence bundles cite every item with source, record revision and as-of time.
    bundle = queries.export_bundle(after, created_at_ms=1)
    assert verify_bundle(bundle).errors == []
    assert all(o["payload"]["report_revision"]["report_id"] and o["payload"]["as_of"]
               for o in bundle["objects"] if o["type"] == "evidence")

    # Exclusions and the minimisation decision: no derived value, no personal field, only stored figures.
    amounts, values = stored_amounts(conn), stored_values(conn)
    for answer in (before, after, trading):
        assert forbidden_keys(answer) == [] and personal_keys(answer) == []
        assert answer["exclusions"] == list(EXCLUSIONS)
        for report in answer["reports"]:
            for stream in report["streams"]:
                for side in ("government_reported", "company_reported"):
                    assert all(f["amount_text"] in amounts for f in stream[side])
    for result in production["results"]:
        for value in result["values"]:
            assert (result["series_id"], value["period"], value["value"]) in values
    text = json.dumps([before, after, production], default=str)
    assert "contact@example.invalid" not in text and "Juan" not in text
    assert conn.execute("SELECT count(*) FROM ex_payments WHERE company_json LIKE '%Juan%'").fetchone()[0] == 0

    # Re-ingestion adds nothing.
    releases = len(ExtractivesStore(conn).releases(h.NS))
    h.load_all(conn)
    assert len(ExtractivesStore(conn).releases(h.NS)) == releases

    # The MCP tools answer from a file-backed store with read scopes only.
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "extractives-acceptance.duckdb")
    file_conn = duckdb.connect(path)
    h.load_all(file_conn)
    file_conn.close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", set(h.READ_ONLY)))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    answer = tools["extractive_payments_for_country"].fn(namespace="global", country="PER")
    assert answer["status"] == "reported" and len(answer["reports"]) == 2 and personal_keys(answer) == []
