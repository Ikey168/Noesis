"""Offline jurisdiction-to-tax-figures and company-to-public-CbCR acceptance for the Economics tax features (#2889).

The pinned OECD Corporate Tax Statistics and EU Tax Observatory fixtures (authored in the documented shapes; every value
and every reference year is fictional) replay through the real ``tax-transparency`` adapter and the runtime's
projector, with every tax feature selected in the composition plan and sockets blocked. The journey takes the
Netherlands and Germany to cited CbCR aggregates, statutory rates and model estimates as of an edition (the 2024
edition before mid-2025, the 2025 edition afterwards), keeps model estimates in their own section with method and
edition, returns a confidential cell as its status, lists the edition history of an index, reports the Tax Justice
Network indices as not-implemented and reaches a company's public CbCR reference only through a reviewed identity
decision. Offline evidence only, never live coverage (``docs/development/tax-transparency-evidence/``).
"""

from __future__ import annotations

import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.ingestion.tax_sources import LIVE_VERIFICATION
from src.kb.tax_identity import TaxIdentity
from src.kb.tax_queries import TaxQueries
from src.kb.tax_records import (
    FEATURES,
    feature_enabled,
    forbidden_paths,
    personal_data_paths,
    readiness,
)
from tests.unit import tax_harness as h
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
    """Every value the store holds, as published: an answer may only show these."""
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT v.series_id, o.period, o.value FROM tax_observations o JOIN tax_vintages v ON "
        "v.namespace=o.namespace AND v.vintage_id=o.vintage_id").fetchall()}


def test_jurisdiction_to_cited_tax_figures_by_edition_and_company_to_public_cbcr_through_review():
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("economics", bundles["economics"]["version"], features=sorted(FEATURES))
    assert coordinator.activate("tax-acceptance")["status"] == "published"
    assert feature_enabled(conn) and "noesis-tax-transparency-record-v2" in PROJECTORS

    # The pinned fixtures replay offline through the real adapter and match their recorded output hashes.
    replay = SourcePackConformance(h.ROOT).offline(h.manifest())
    assert replay["valid"] and replay["coverage"]["verified"] == 4

    # The 2024 editions (retrieved 2024-10-01), then the 2025 editions (retrieved 2025-10-01).
    h.load_all(conn, revisions=True)
    status = readiness(conn)
    assert status["features_selected"] == sorted(FEATURES)
    assert {status["providers"][p]["live_verification"] for p in ("oecd-cts-cbcr", "eutax-offshore-wealth")} == {
        "unverified-live"}
    assert status["providers"]["tjn-cthi"]["access_decision"] == "not-implemented"

    queries = TaxQueries(conn)
    held = stored_values(conn)
    early = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NLD", as_of="2025-03-31",
                                               providers=["oecd-cts-cbcr", "oecd-cts-rates"])
    late = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NLD", as_of="2025-09-30",
                                              providers=["oecd-cts-cbcr", "oecd-cts-rates"])
    for answer in (early, late):
        assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
        for row in answer["published_statistics"] + answer["model_estimates"]:
            assert row["citation"]["edition"] == row["edition"] and row["citation"]["vintage_id"]
            assert row["citation"]["live_verification"] == "unverified-live"
            for value in row["values"]:
                assert value["value"] is None or (row["series_id"], value["period"], value["value"]) in held
    # The CbCR foreign income tax accrued for 2092 is confidential: a status, never a value.
    secret = next(r for r in early["published_statistics"] if r["indicator"]["code"] == "ITA"
                  and r["counterpart"]["code"] == "FOR")
    cell = next(v for v in secret["values"] if v["period"] == "2092")
    assert cell["status"] == "confidential" and cell["value"] is None
    assert secret["definition"]["caveats"]
    # The statutory rate is a published statistic; the effective average rate is a model estimate of each edition.
    statutory = next(r for r in late["published_statistics"] if r["indicator"]["code"] == "CIT_COMB")
    assert statutory["observation"] is True and statutory["vintage"]["economic_vintage_id"]
    eatr_early = next(r for r in early["model_estimates"] if r["indicator"]["code"] == "EATR")
    eatr_late = next(r for r in late["model_estimates"] if r["indicator"]["code"] == "EATR")
    assert eatr_early["edition"].startswith("Corporate Tax Statistics 2024")
    assert eatr_late["edition"].startswith("Corporate Tax Statistics 2025")
    assert eatr_late["observation"] is False and eatr_late["method"]["reference"].startswith("https://")
    assert eatr_late["vintage"]["economic_vintage_id"] is None  # never in the Economics observation storage
    assert {v["period"]: v["value"] for v in eatr_late["values"]}["2092"] == "21.7"

    # The edition history of the offshore-wealth estimate (an index-like model series) lists both editions.
    offshore = h.series_by(conn, provider="eutax-offshore-wealth", jurisdiction="DEU", indicator="OFW_USD")
    history = queries.series_history(h.NS, offshore["series_id"], scopes=h.READ_ONLY)
    assert len(history["editions"]) == 2 and history["editions"][1]["changes"]["method_change"]
    # Billionaire effective tax rates are country estimates; the unpublished Dutch income rate stays a status.
    billionaires = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NLD",
                                                      providers=["eutax-billionaire-etr"])
    assert {r["nature"] for r in billionaires["model_estimates"]} == {"model_estimate"}
    assert billionaires["published_statistics"] == []
    # Haven and secrecy indices are not implemented, never an empty answer.
    cthi = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NLD",
                                              concept="financial_secrecy_index")
    assert cthi["status"] == "not-implemented" and LIVE_VERIFICATION["tjn-fsi"]["status"] == "not-implemented"
    bundle = queries.evidence_bundle(late)
    assert {s["id"] for s in bundle["sections"]} == {"published_statistics", "model_estimates"}

    # Public CbCR: link-only references reach an ownership entity only after a reviewer accepts the match.
    h.ownership(conn)
    h.record_references(conn)
    identity = TaxIdentity(conn)
    proposed = identity.propose_company_matches(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst",
                                                scopes=h.SCOPES)["candidates"]
    assert queries.company_reports_for_entity(h.NS, scopes=h.SCOPES, ownership_key=h.HOLDINGS_LEI)["reports"] == []
    for view in proposed:
        if view["method"] == "exact-identifier":
            identity.review(h.NS, view["candidate_id"], "accept", "the report states the LEI", principal_id="reviewer",
                            scopes=h.REVIEW_SCOPES)
    (report,) = queries.company_reports_for_entity(h.NS, scopes=h.SCOPES, ownership_key=h.HOLDINGS_LEI)["reports"]
    assert report["status"] == "reference-only" and report["report_url"].startswith("https://")
    assert "profit" not in report and "not stored" in report["figures"]
    assert {u["company_name_as_published"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)} == {
        "Exampla Intermediate B.V.", "Northwind Industrie SE (fictional)"}
