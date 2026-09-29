"""Offline country-pair-to-flows acceptance for the Economics trade features (TF11, #2555).

The pinned UN Comtrade, Eurostat Comext and WITS concordance fixtures (authored in the documented shapes; every
value is fictional) replay through the real ``trade-flows`` adapter - Comext through the extended Eurostat
connector - and the runtime's projector, with the ``trade-comtrade`` and ``trade-comext`` features selected in the
composition plan. The journey takes a country pair and a product to cited flows with release vintages and
reporter-versus-mirror asymmetries visible. This is offline evidence only, never live coverage
(``docs/development/trade-evidence/``).
"""

from __future__ import annotations

import json
import socket
from decimal import Decimal

import pytest

from src.domains import registry as domain_registry
from src.evidence_bundle.verifier import verify_bundle
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.kb.geospatial import GeospatialStore
from src.kb.sanctions_trade import SanctionsTrade
from src.kb.trade_flows import TradeFlowStore, feature_enabled, forbidden_keys, readiness
from src.kb.trade_identity import TradeIdentity
from src.kb.trade_links import TradeLinks
from src.kb.trade_monitoring import TradeMonitor
from src.kb.trade_queries import TradeQueries
from tests.unit import trade_harness as h
from tests.unit.composition.test_migration import _migrated

HS2022 = {"scheme": "HS", "vintage": "HS2022"}
LEGAL = h.READ_ONLY | {"knowledge:legal:read"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    """``_migrated`` cuts every bundle over; restore the legacy registry for later tests."""
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def rows(answer, provider, direction):
    result = next(r for r in answer["results"] if r["provider"] == provider and r["direction"] == direction)
    return {row["period"]: row for group in result["groups"] for row in group["rows"]}


def stored_values(conn):
    """Every value the store holds, as published: an answer may only show these."""
    return {
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT v.series_id, o.period, o.value FROM trade_observations o JOIN trade_vintages v ON "
            "v.namespace=o.namespace AND v.vintage_id=o.vintage_id"
        ).fetchall()
    }


def figures(answer):
    return [f for r in answer["results"] for g in r["groups"] for row in g["rows"]
            for f in row["reporter_figures"] + row["mirror_figures"]]


def test_country_pair_and_product_to_cited_flows_with_release_vintages_and_mirror_asymmetries():
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("economics", bundles["economics"]["version"], features=["trade-comtrade", "trade-comext"])
    assert coordinator.activate("trade-acceptance")["status"] == "published"
    assert feature_enabled(conn, "trade-comtrade") and feature_enabled(conn, "trade-comext")
    assert "noesis-trade-flow-record-v1" in PROJECTORS

    # The pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    manifest = h.manifest()
    trade_sources = [s for s in manifest["sources"] if s["connector"] == "trade-flows"]
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": trade_sources})
    assert replay["valid"] and replay["coverage"]["verified"] == 3

    # First releases, then Germany's Comtrade re-release (2099-09-01) and Comext update (2099-04-15).
    h.load_all(conn, revisions=True)
    status = readiness(conn)
    assert status["selected"] and status["providers"]["un-comtrade"]["releases"] == 3
    assert status["providers"]["un-comtrade"]["live_verification"] == "unverified-live"

    # Reviewable identity: Germany's M49 code and Comext GEO code resolve to one place once a reviewer accepts.
    places = GeospatialStore(conn)
    for key, name, ids in (("de", "Germany", {"m49": "276", "eurostat-geo": "DE"}), ("cn", "China", {"m49": "156"})):
        places.register_place("geo", name, "country", names=[{"value": name, "language": "en"}], source_ids=ids,
                              parent_ids=[], principal_id="op", scopes={"knowledge:geospatial:write"},
                              place_key=f"fixture:{key}")
    identity = TradeIdentity(conn)
    proposed = {a["subject"]["code"]: a for a in identity.propose_areas(
        h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace="geo")["assertions"]}
    for code in ("276", "DE", "156"):
        identity.review(h.NS, proposed[code]["assertion_id"], "accept", "published code", principal_id="reviewer",
                        scopes=h.SCOPES)
    # France only meets the built-in gazetteer by name: a weak proposal, visible and unused until reviewed.
    assert proposed["FR"]["state"] == "proposed" and proposed["FR"]["method"] == "gazetteer-name"
    assert proposed["FR"]["evidence"]["strength"] == "weak"

    queries = TradeQueries(conn)
    product = {"code": "854143", **HS2022}
    # As of July 2099: Germany's first 2098 release and China's mirror release, each cited.
    july = queries.flows(h.NS, reporter="DE", partner="156", product=product, scopes=h.READ_ONLY,
                         as_of_ms=h.day_ms("2099-07-01"))
    assert july["identity"]["reporter"]["codes"] == ["276", "DE"]
    exports = rows(july, "un-comtrade", "export")
    row = exports["2098"]
    (reporter,) = row["reporter_figures"]
    (mirror,) = row["mirror_figures"]
    assert (reporter["value"], reporter["valuation"]["basis"]) == ("5200000", "FOB")
    assert (mirror["value"], mirror["valuation"]["basis"], mirror["role"]) == ("5600000", "CIF", "mirror")
    assert reporter["release"]["release_at"].startswith("2099-05-15")
    assert mirror["release"]["release_at"].startswith("2099-06-20")
    assert row["asymmetry"]["status"] == "displayed" and row["asymmetry"]["reporter_minus_mirror"] == "-400000"
    # As of October: the revised German release is selected and cited; the earlier vintage stays queryable.
    october = rows(queries.flows(h.NS, reporter="DE", partner="156", product=product, scopes=h.READ_ONLY,
                                 as_of_ms=h.day_ms("2099-10-01")), "un-comtrade", "export")["2098"]
    assert october["reporter_figures"][0]["value"] == "5250000"
    assert october["reporter_figures"][0]["release"]["revision_of"] == reporter["release"]["vintage_id"]
    assert october["asymmetry"]["reporter_minus_mirror"] == "-350000"

    # Cross-vintage product filter: China reported 2097 in HS2017 (854140); the WITS row is n:1, flagged non-exact.
    old = exports["2097"]
    assert old["mirror_figures"][0]["classification"]["vintage"] == "HS2017"
    assert old["mirror_figures"][0]["product_match"]["exact"] is False and old["non_exact_mapping"] is True
    assert old["mirror_figures"][0]["product_match"]["concordance"]["revision"] == 1

    # Comext: the intra-EU pair, a confidential cell kept confidential, the cube update as release vintage.
    comext = queries.flows(h.NS, reporter="DE", partner="FR", product={"code": "29309098", "scheme": "CN",
                                                                       "vintage": "CN2099"}, scopes=h.READ_ONLY)
    feb = rows(comext, "eurostat-comext", "import")["2099-02"]
    assert feb["reporter_figures"][0]["status"] == "confidential" and feb["reporter_figures"][0]["value"] is None
    assert feb["asymmetry"]["status"] == "not_shown"

    # A pair with no reported flow is none reported, not zero.
    nothing = queries.flows(h.NS, reporter="156", partner="FR", scopes=h.READ_ONLY)
    assert nothing["status"] == "none_reported"
    assert all(r["status"] == "none_reported" and "not zero" in r["note"] for r in nothing["results"])

    # Sanctions-linked products: flows in products the cited measure covers; uncovered cited products none reported.
    table = json.loads((h.ROOT / "tests/fixtures/sanctions/dual_use_cn_correlation.json").read_text())
    table["rows"].append({"control_code": "1C350", "product_code": "300215", "product_scheme": "HS6"})
    SanctionsTrade(conn).record_correlations(h.NS, table, principal_id="op", scopes=h.SCOPES)
    linked = TradeLinks(conn).link_sanctions(h.NS, principal_id="op", scopes=h.SCOPES)
    assert linked["status"] == "linked" and [m["product_code"] for m in linked["unmatched_measures"]] == ["300215"]
    sanctioned = queries.sanctioned_flows(h.NS, reporter="DE", partner="156", control_code="1C350", scopes=LEGAL)
    covered = {f["product"]["code"] for f in figures(sanctioned)}
    assert covered == {"293090"} and [n["product_code"] for n in sanctioned["none_reported"]] == ["300215"]
    assert "lookup aid" in sanctioned["notice"]

    # Exclusions: every shown value is a stored published value; nothing imputed, reconciled or nowcast.
    published = stored_values(conn)
    for answer in (july, comext, sanctioned):
        assert forbidden_keys(answer) == []
        for figure in figures(answer):
            assert (figure["series_id"], figure["period"], figure["value"]) in published
            if figure["value"] is None:
                assert figure["status"] in {"confidential", "not_published"}
        for result in answer["results"]:
            for group in result["groups"]:
                for row in group["rows"]:
                    assert "value" not in row  # no single reconciled value per period
                    if row["asymmetry"]["status"] == "displayed":
                        (left,), (right,) = row["reporter_figures"], row["mirror_figures"]
                        assert Decimal(row["asymmetry"]["reporter_minus_mirror"]) == (
                            Decimal(left["value"]) - Decimal(right["value"]))
    assert max(p for (_, p, _) in published) <= "2099-02"  # no period beyond the published ones (no nowcast)

    # The evidence bundle cites every figure with source, classification vintage, release vintage and as-of time.
    bundle = queries.export_bundle(july, created_at_ms=1)
    verified = verify_bundle(bundle)
    assert verified.errors == []
    cited = [o["payload"] for o in bundle["objects"] if o["type"] == "evidence"]
    assert len(cited) == len(figures(july))
    assert all(c["source"]["file_sha256"] and c["classification_vintage"]["vintage"] and
               c["release_vintage"]["vintage_id"] and c["as_of"].startswith("2099-07-01") for c in cited)

    # Re-ingestion adds nothing, and a restarted monitor on the pair replays without duplicate notices.
    before = TradeFlowStore(conn).releases(h.NS)
    assert {r["status"] for r in h.apply(conn, "comtrade", retrieved_at_ms=h.SECOND_RETRIEVAL)} == {"unchanged"}
    assert len(TradeFlowStore(conn).releases(h.NS)) == len(before)
    monitor = TradeMonitor(conn, now=lambda: h.SECOND_RETRIEVAL)
    watch = monitor.create(h.NS, "de-cn", flow_filter={"reporter": "DE", "partner": "156", "products": ["854143"]},
                           principal_id="alice", scopes=h.SCOPES)
    first = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert {n["kind"] for n in first["notifications"]} == {"new_release", "revision"}
    restarted = TradeMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 1).run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert restarted["notifications"] == []
