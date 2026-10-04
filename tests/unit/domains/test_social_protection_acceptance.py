"""Offline country-to-social-protection acceptance for the Society ``society.social-protection`` provider (SS12, #2803).

The pinned ESSPROS, SOCX and ILOSTAT fixtures (authored in the publishers' documented SDMX-CSV shapes; every value is
synthetic, reference years 2094-2098, releases in 2098-2099) run through the source-pack runtime and the real
``social-protection`` adapter - each through the SDMX connector - with the three Society features selected in the
composition plan and sockets blocked. The journey takes Germany to cited ESSPROS, SOCX and ILO figures side by side
with definitions, vintages and comparability notes, never blended, including an ESSPROS revision with a new manual
edition, an OECD estimate year replaced, a restated ILO report edition, function identity review, Demographics and
Public finance links and a country with no records. Offline evidence only, never live coverage
(``docs/development/social-protection-evidence/``).
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.social_protection_sources import (
    EXCLUSIONS,
    LIVE_VERIFICATION,
    fixture_transport,
)
from src.ingestion.source_pack_runtime import PROJECTORS, SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.social_protection_identity import SocialProtectionIdentity
from src.kb.social_protection_links import SocialProtectionLinks
from src.kb.social_protection_monitoring import SocialProtectionMonitor
from src.kb.social_protection_queries import (
    SocialProtectionQueries,
    export_profile,
    place_profile,
)
from src.kb.social_protection_records import (
    MINIMISATION,
    enabled_providers,
    forbidden_paths,
    personal_data_paths,
    readiness,
)
from src.kb.social_protection_store import SocialProtectionStore
from tests.unit import demographics_harness as dh
from tests.unit import social_protection_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "society-social-protection"
FEATURES = ["pip", "eu-silc", "oecd-idd", "esspros", "socx", "ilo-coverage"]
PUBLIC_DNS = lambda _host: ["8.8.8.8"]


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
        "SELECT v.series_id, o.period, o.value_text FROM social_protection_observations o JOIN "
        "social_protection_vintages v ON v.namespace=o.namespace AND v.vintage_id=o.vintage_id").fetchall()}


def test_country_to_cited_esspros_socx_and_ilo_figures_side_by_side():
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("society", bundles["society"]["version"], features=FEATURES)
    assert coordinator.activate("social-protection-acceptance")["status"] == "published"
    assert enabled_providers(conn) == set(h.SOURCES.values())
    assert "noesis-social-protection-record-v2" in PROJECTORS

    # First releases through the source-pack runtime and the real adapter (pinned fixtures, no sockets).
    clock = [4_102_444_800_000]  # 2100-01-01: the runtime retrieves after every fixture release

    def now():
        clock[0] += 1
        return clock[0]

    manifest = validate_source_pack(json.loads(h.PACK.read_text()))
    SourcePackStore(conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
    runtime = SourcePackRuntime(conn, now=now, sleep=lambda _d: None)
    for item in manifest["sources"]:
        runtime.accept_license(PACK_ID, item["source_id"], principal_id="operator")
    fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
    run = runtime.run({"pack_id": PACK_ID, "run_key": "first", "operation": "release",
                       "source_ids": sorted(fixtures), "max_results": 100, "max_bytes": 10_000_000,
                       "timeout_ms": 60_000}, principal_id="operator", adapters=fixtures, dns_resolver=PUBLIC_DNS)
    assert run["status"] == "complete" and {s["status"] for s in run["sources"]} == {"complete"}
    status = readiness(conn)
    assert {status["providers"][p]["status"] for p in h.SOURCES.values()} == {"fixture-only"}
    assert {p: LIVE_VERIFICATION[p]["status"] for p in status["providers"]} == {
        **{p: "unverified-live" for p in h.SOURCES.values()},
        "ilo-world-social-protection-dashboards": "not-implemented"}

    # A failed run (schema drift) fails that source with its code and a receipt; vintages stay current.
    store = SocialProtectionStore(conn)
    before = {s["series_id"]: s["current_vintage_id"] for s in store.find_series(h.NS)}
    broken = runtime.factory.compile(next(s for s in manifest["sources"] if s["source_id"] == "oecd-socx"),
                                     transport=fixture_transport([dict(p, body="DATAFLOW,REF_AREA\nX,DEU\n")
                                                                  for p in h.pages("socx")]))
    failed = runtime.run({"pack_id": PACK_ID, "run_key": "drift", "operation": "release",
                          "source_ids": ["oecd-socx"], "max_results": 100, "max_bytes": 10_000_000,
                          "timeout_ms": 60_000}, principal_id="operator", adapters={"oecd-socx": broken},
                         dns_resolver=PUBLIC_DNS)
    assert failed["sources"][0]["status"] == "failed"
    assert {s["series_id"]: s["current_vintage_id"] for s in store.find_series(h.NS)} == before
    receipts = [r for r in store.receipts(h.NS, scopes=h.READ_ONLY) if r["outcome"] == "failed"]
    assert receipts and receipts[-1]["detail"]["failure_code"] == "schema_drift"
    assert readiness(conn)["providers"]["oecd-socx"]["status"] == "stale"

    # A monitor watches Germany's old-age series; then revisions, a restating ILO edition and a removal.
    monitor = SocialProtectionMonitor(conn, now=now)
    watch = monitor.create(h.NS, "de-socx", target={"area": {"scheme": "iso3166-1-alpha3", "code": "DEU"},
                                                    "provider": "oecd-socx"}, principal_id="alice", scopes=h.SCOPES)
    first_notices = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in first_notices} == {"new_release"} and len(first_notices) == 2
    for name in h.SOURCES:
        h.apply(conn, name, revised=True)
    notices = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in notices} == {"new_period", "revised_value"}
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []

    # Places through reviewed ISO codes; functions related only by review.
    places = h.accept_places(conn, keys=("de", "fr", "pt"))
    identity = SocialProtectionIdentity(conn)
    proposed = identity.propose_function_relations(h.NS, principal_id="analyst", scopes=h.SCOPES)["proposed"]
    identity.review(h.NS, proposed[0]["assertion_id"], "accept", "published reconciliation (verify)",
                    principal_id="reviewer", scopes=h.SCOPES)
    assert identity.related_functions(h.NS, "socx-branch", "TP11")[0]["code"] == "OLD"

    # Demographics denominator and COFOG (Public finance) links, pinned, nothing computed.
    dh.apply(conn, "eurostat", 0)
    h.load_public_finance(conn)
    links = SocialProtectionLinks(conn)
    links.link_demographics(h.NS, principal_id="svc", scopes=h.SCOPES)
    links.link_public_finance(h.NS, principal_id="svc", scopes=h.SCOPES)
    linked = {(k["kind"], k["basis"]) for k in links.links(h.NS, scopes=h.READ_ONLY) if k["state"] == "linked"}
    assert {("denominator", "shared-identifier"), ("cofog", "shared-identifier"),
            ("cofog", "accepted-match")} <= linked

    # Germany as of two dates: each source's vintage released by the date, side by side by measure.
    queries = SocialProtectionQueries(conn)
    early = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place_id=places["de"], as_of="2099-01-31")
    later = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place_id=places["de"], as_of="2099-06-30")
    assert {r["provider"] for r in later["results"]} == set(h.SOURCES.values())
    assert set(later["measure_groups"]) == {"expenditure", "beneficiaries", "coverage"}

    def cell(answer, key, period):
        row = next(r for r in answer["results"] if r["native_key"] == key)
        return row, {o["period"]: o for o in row["observations"]}[period]

    row, estimate = cell(early, "DEU.A.SOCX.PT_B1GQ.ES10._T.TP_ALL", "2097")
    assert estimate["publication_status"] == "estimated" and row["vintage"]["release_label"].startswith(
        "SOCX update 2098-10")
    _, final = cell(later, "DEU.A.SOCX.PT_B1GQ.ES10._T.TP_ALL", "2097")
    assert final["publication_status"] == "normal" and final["value_text"] != estimate["value_text"]
    _, restated_before = cell(early, "DEU.A.SDG_0131_RT.SEX_T.SOC_CONTIG_TOTAL", "2094")
    row, restated_after = cell(later, "DEU.A.SDG_0131_RT.SEX_T.SOC_CONTIG_TOTAL", "2094")
    assert restated_before["value_text"] != restated_after["value_text"]
    assert row["vintage"]["edition"].endswith("fixture edition 2 (synthetic label)")
    assert row["population"]["label"] == "total population"
    for answer in (early, later):
        for result in answer["results"]:
            assert result["citation"]["vintage_id"] and result["citation"]["as_of"] and result["definition"]
            assert result["citation"]["live_verification"] == "unverified-live"
        # Coverage is never paired with expenditure as the same thing; nothing is combined.
        assert all(p["comparability"] == "different_measure" for p in answer["pairs"]
                   if "ilo-social-protection-coverage" in p["providers"])
        assert answer["nothing_combined"] is True and answer["cofog_links"]
        shown = {(r["series_id"], o["period"], o["value_text"]) for r in answer["results"] for o in r["observations"]
                 if o["value_text"] is not None}
        assert shown <= stored_values(conn)  # only published values; nothing derived, filled or blended
        assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
    assert set(EXCLUSIONS) == set(later["exclusions"]) and later["minimisation"] == MINIMISATION["decision"]

    # The history: revisions, the manual edition change, the estimate replaced and the restated edition.
    def history(provider, key):
        return queries.series_history(h.NS, h.series(conn, provider, key)["series_id"], scopes=h.READ_ONLY)

    kinds = {n["kind"] for n in history("eurostat-esspros", "A.TOTALNOREROUTE.MIO_EUR.DE")["pairs"][0]["notes"]}
    assert "manual_edition_change" in kinds
    assert "estimate_replaced" in {n["kind"] for n in history(
        "oecd-socx", "DEU.A.SOCX.PT_B1GQ.ES10._T.TP_ALL")["pairs"][0]["notes"]}
    assert "edition_restatement" in {n["kind"] for n in history(
        "ilo-social-protection-coverage", "DEU.A.SDG_0131_RT.SEX_T.SOC_CONTIG_TOTAL")["pairs"][0]["notes"]}
    assert history("ilo-social-protection-coverage", "FRA.A.SDG_0131_RT.SEX_T.SOC_CONTIG_TOTAL")["pairs"][0][
        "comparability"] == "comparability_unknown"

    # A country with no records is reported, never filled.
    portugal = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, place_id=places["pt"], as_of="2099-06-30")
    assert portugal["results"] == [] and portugal["unmatched_place"] is True
    assert {"PT", "PRT"}.isdisjoint({u["code"] for u in identity.unmatched(h.NS, scopes=h.READ_ONLY)})

    # The cited evidence bundle: every item with source, record revision and as-of time.
    profile = place_profile(conn, h.NS, scopes=h.READ_ONLY, place_id=places["de"], as_of="2099-06-30")
    bundle = export_profile(profile)
    from src.evidence_bundle.verifier import verify_bundle

    assert not verify_bundle(bundle).errors
    cited = [o["payload"]["citation"] for o in bundle["objects"]
             if o["payload"].get("kind") == "social-protection-series-vintage"]
    assert len(cited) == len({r["series_id"] for a in profile["answers"].values() for r in a["results"]})
    assert all(c["source"] and c["record_revision"] and c["as_of"] for c in cited)
