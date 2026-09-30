"""Offline company-to-extractive-payments acceptance for the Economics extractives features (EX12, #2713).

The pinned EITI, USGS Mineral Commodity Summaries and BGS World Mineral Statistics fixtures (authored in the
documented shapes as known to the author; every company, project and figure is fictional) replay through the real
``extractives`` adapter and the runtime's projector beside the Corporate Ownership fixtures, with the extractives
features selected in the composition plan and sockets blocked. The journey takes a company (and its group) and a
country to cited extractive payments per report version and to production and reserves side by side, with
reviewable identity, cross-pack links and a subject with no records. Offline evidence only, never live coverage
(``docs/development/extractives-evidence/source-audit.md``).
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.extractives_sources import EXCLUSIONS, personal_keys
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.kb.extractives_identity import ExtractivesIdentity
from src.kb.extractives_links import ExtractivesLinks
from src.kb.extractives_monitoring import ExtractivesMonitor
from src.kb.extractives_queries import ExtractivesQueries
from src.kb.extractives_records import feature_enabled, forbidden_keys, readiness
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


def _clean(answer) -> None:
    """The exclusions and the minimisation decision hold for every answer."""
    assert forbidden_keys(answer) == [] and personal_keys(answer) == []
    text = json.dumps(answer)
    for needle in ("Fixture-Person", "msg@example.org", "secretariat@example.org", "Fixture Owner"):
        assert needle not in text


def test_company_and_country_to_cited_extractive_payments_and_production_with_versions_identity_and_links():
    conn, coordinator, bundles, _ = _migrated()
    coordinator.select("economics", bundles["economics"]["version"],
                       features=["extractives-eiti", "extractives-usgs", "extractives-bgs", "trade-comtrade",
                                 "trade-comext"])
    assert coordinator.activate("economics-extractives-acceptance")["status"] == "published"
    assert feature_enabled(conn) and feature_enabled(conn, "extractives-eiti")
    assert "noesis-extractives-record-v1" in PROJECTORS
    assert SourcePackConformance(h.ROOT).offline(json.loads(h.PACK_PATH.read_text()))["valid"]

    # Acquire: ownership, the first report versions and releases, then the revised versions; review identity.
    state = h.reviewed(conn, revisions=True)
    identity: ExtractivesIdentity = state["identity"]
    report = readiness(conn)
    assert report["stores_ready"] and set(report["selected"]) == {"extractives-eiti", "extractives-usgs",
                                                                  "extractives-bgs"}
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    queries = ExtractivesQueries(conn)

    # Reviewable identity: exact identifiers accepted by a reviewer; name-only and Northwind stay unmatched.
    accepted = [c for c in identity.company_candidates(h.NS, scopes=h.SCOPES) if c["state"] == "accepted"]
    assert accepted and {c["method"] for c in accepted} == {"exact-identifier"}
    assert {u["key"] for u in identity.unmatched_companies(h.NS, scopes=h.SCOPES)} == {h.NORTHWIND, h.UK_COMPANY}

    # Company to cited payments: the group reaches both reports, each payment cites its report revision.
    group = queries.payments_for_company(h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                         group=True, include_unknowns=True, all_versions=True)
    _clean(group)
    reports = {r["report_key"]: r for r in group["reports"]}
    assert set(reports) == {h.NL_REPORT, h.DE_REPORT}
    for block in reports.values():
        for payment in block["payments"]:
            citation = payment["citation"]
            assert citation["revision_id"] and citation["as_of"] and citation["report_version"]
            assert citation["source"]["provider"] == "eiti" and citation["source"]["evidence_origin"] == "fixture"
            assert payment["government_reported"] and payment["company_reported"]
    nl_payment = next(p for p in reports[h.NL_REPORT]["payments"] if p["record_key"] == h.CIT_PAYMENT)
    assert nl_payment["citation"]["report_version"] == "2"
    assert [u["key"] for u in group["unknowns"]] == [h.UK_COMPANY]

    # Revision history and as-of answers: version 1 as of mid-2023, version 2 afterwards.
    history = queries.record_history(h.NS, h.CIT_PAYMENT, scopes=h.SCOPES)
    assert [r["report_version"] for r in history["revisions"]] == ["1", "2"]
    early = queries.payments_for_company(h.NS, h.INT_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                         as_of="2023-06-30")
    (early_report,) = early["reports"]
    early_payment = next(p for p in early_report["payments"] if p["record_key"] == h.CIT_PAYMENT)
    assert early_payment["company_reported"]["value"] == "1250000"
    assert early_payment["discrepancy_as_published"]["value"] == "-50000"

    # Country to cited payments, companies as reported with their match status, the individual withheld.
    country = queries.payments_for_country(h.NS, "NL", scopes=h.SCOPES)
    _clean(country)
    assert country["reports"][0]["version_used"]["report_version"] == "2"

    # Production and reserves side by side: USGS and BGS separately, markers kept, vintages cited.
    production = queries.production_and_reserves(h.NS, "copper", "CL", scopes=h.SCOPES, all_vintages=True)
    _clean(production)
    assert [s["provider"] for s in production["sources"]] == ["bgs-wms", "usgs-mcs"] and not production["blended"]
    withheld = queries.production_and_reserves(h.NS, "lithium", "US", scopes=h.SCOPES, statistic="production")
    assert {v["status"] for v in withheld["sources"][0]["series"][0]["values"]} == {"withheld"}

    # Cross-pack links: trade through an accepted HS match, Energy absent reported, infrastructure by identifier.
    links = ExtractivesLinks(conn)
    assert links.link_energy(h.NS, principal_id="analyst", scopes=h.SCOPES)["status"] == "provider_absent"
    h.seed_trade(conn)
    identity.import_concordance(h.NS, h.CONCORDANCE, principal_id="operator", scopes=h.SCOPES)
    for match in identity.propose_commodities(h.NS, principal_id="analyst", scopes=h.SCOPES)["matches"]:
        identity.review(h.NS, match["match_id"], "accept", "concordance row checked", principal_id="reviewer",
                        scopes=h.SCOPES)
    trade = links.link_trade_flows(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert trade["status"] == "linked" and all(link_id.startswith("extractives-link:")
                                               for item in trade["linked"] for link_id in item["links"])
    h.seed_infrastructure(conn)
    for match in identity.propose_projects(h.NS, infrastructure_namespace="infra", principal_id="analyst",
                                           scopes=h.SCOPES)["matches"]:
        identity.review(h.NS, match["match_id"], "accept", "shared GEM id", principal_id="reviewer",
                        scopes=h.SCOPES)
    assert links.link_infrastructure(h.NS, principal_id="analyst", scopes=h.SCOPES)["status"] == "linked"
    for link in links.links(h.NS, status="linked"):
        assert link["basis"] in {"accepted-match", "shared-identifier", "explicit-citation"}
        assert link["subject"]["revision_id"] and link["target"]["revision_id"]

    # A subject with no records: no clean bill, no guessed series.
    trading = queries.payments_for_company(h.NS, "gleif:lei:213800EXAMPLATRADE88", ownership_namespace=h.OWN_NS,
                                           scopes=h.SCOPES)
    assert trading["status"] == "no_payment_on_record" and "not a clean bill" in trading["message"]
    assert queries.production_and_reserves(h.NS, "cobalt", "CD", scopes=h.SCOPES)["status"] == "no_series_on_record"

    # Evidence bundle: every item cites source, record revision and as-of time.
    bundle = queries.evidence_bundle(group)
    assert bundle["exclusions"] == list(EXCLUSIONS)
    for assertion in bundle["sections"][0]["assertions"]:
        assert assertion["dependencies"][0]["revision"] and assertion["dependencies"][0]["as_of"]
        assert assertion["citations"][0] in {b["id"] for b in bundle["bibliography"]}

    # Re-ingestion adds nothing; a restarted monitor replays without duplicates.
    monitor = ExtractivesMonitor(conn, now=lambda: h.SECOND_RETRIEVAL)
    sub = monitor.create(h.NS, "exampla", watch={"companies": [h.INT_ENTITY], "countries": ["NL"]},
                         principal_id="alice", scopes=h.SCOPES)
    assert monitor.run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    h.load_all(conn, revisions=True)
    restarted = ExtractivesMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 1)
    assert restarted.run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
