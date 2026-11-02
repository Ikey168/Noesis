"""Offline country-to-wealth-distribution acceptance for the Society ``society.wealth`` provider (#2882).

The pinned WID, ECB DWA and Fed DFA fixtures (authored in the publishers' documented shapes; every value is synthetic,
reference periods 2093-2098, releases in 2098-2099) run through the source-pack runtime and the real
``wealth-distribution`` adapter with the three Society wealth features selected and sockets blocked. The journey takes
Germany and the United States to cited wealth figures from each source side by side with definitions, methods and
vintages, never combined: a failed run keeps vintages current, the series history shows the revised vintages, an
as-of date selects the vintage released by that date, and pairs without a note are ``comparability_unknown``.
Offline evidence only, never live coverage (``docs/development/wealth-distribution-evidence/``).
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import PROJECTORS, SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.ingestion.wealth_distribution_sources import (
    EXCLUSIONS,
    FIXTURE_SECRET,
    LIVE_VERIFICATION,
    fixture_transport,
)
from src.kb.wealth_distribution_queries import (
    WealthQueries,
    export_profile,
    place_profile,
)
from src.kb.wealth_distribution_records import (
    MINIMISATION,
    enabled_providers,
    forbidden_paths,
    personal_data_paths,
    readiness,
)
from src.kb.wealth_distribution_store import WealthStore
from tests.unit import wealth_distribution_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "society-wealth-distribution"
FEATURES = ["pip", "eu-silc", "oecd-idd", "wid", "dwa", "dfa"]
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
SECRETS = lambda ref: FIXTURE_SECRET if ref == "NOESIS_WID_API_KEY" else None


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
        "SELECT v.series_id, o.period, o.value_text FROM wealth_observations o JOIN wealth_vintages v ON "
        "v.namespace=o.namespace AND v.vintage_id=o.vintage_id").fetchall()}


def test_country_to_cited_wid_dwa_and_dfa_wealth_figures_side_by_side_with_vintages():
    conn, coordinator, bundles, _ = _migrated(h.connection())
    coordinator.select("society", bundles["society"]["version"], features=FEATURES)
    assert coordinator.activate("wealth-acceptance")["status"] == "published"
    assert enabled_providers(conn) == set(h.SOURCES.values())
    assert "noesis-wealth-distribution-record-v2" in PROJECTORS

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
                       "timeout_ms": 60_000}, principal_id="operator", adapters=fixtures, dns_resolver=PUBLIC_DNS,
                      secret_resolver=SECRETS)
    assert run["status"] == "complete" and {s["status"] for s in run["sources"]} == {"complete"}, run
    status = readiness(conn)
    assert {status["providers"][p]["status"] for p in h.SOURCES.values()} == {"fixture-only"}
    assert {p: LIVE_VERIFICATION[p]["status"] for p in status["providers"]} == {
        **{p: "unverified-live" for p in h.SOURCES.values()},
        "ecb-hfcs-statistical-tables": "not-implemented", "bundesbank-phf-aggregates": "not-implemented"}

    # A failed run (schema drift) fails that source with its code and a receipt; vintages stay current.
    store = WealthStore(conn)
    before = {s["series_id"]: s["current_vintage_id"] for s in store.find_series(h.NS)}
    broken = runtime.factory.compile(
        next(s for s in manifest["sources"] if s["source_id"] == "ecb-distributional-wealth-accounts"),
        transport=fixture_transport([dict(p, body="KEY,FREQ\nX,Q\n") for p in h.pages("dwa")]))
    failed = runtime.run({"pack_id": PACK_ID, "run_key": "drift", "operation": "release",
                          "source_ids": ["ecb-distributional-wealth-accounts"], "max_results": 100,
                          "max_bytes": 10_000_000, "timeout_ms": 60_000}, principal_id="operator",
                         adapters={"ecb-distributional-wealth-accounts": broken}, dns_resolver=PUBLIC_DNS)
    assert failed["sources"][0]["status"] == "failed"
    assert {s["series_id"]: s["current_vintage_id"] for s in store.find_series(h.NS)} == before
    receipts = [r for r in store.receipts(h.NS, scopes=h.READ_ONLY) if r["outcome"] == "failed"]
    assert receipts and receipts[-1]["detail"]["failure_code"] == "schema_drift"
    assert readiness(conn)["providers"]["ecb-distributional-wealth-accounts"]["status"] == "stale"

    # Revised releases (2099): appended as vintages, never overwriting the first ones.
    for name in h.SOURCES:
        h.apply(conn, name, revised=True)
    assert all(s["vintage_count"] == 2 for s in store.find_series(h.NS))

    # Germany as of two dates: each source's vintage released by the date, side by side, never combined.
    queries = WealthQueries(conn)
    early = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.DE, indicator="wealth_share",
                                        as_of="2098-12-31")
    later = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.DE, indicator="wealth_share",
                                        as_of="2099-06-30")
    for answer in (early, later):
        assert {r["provider"] for r in answer["results"]} == {"wid-wealth-distribution",
                                                              "ecb-distributional-wealth-accounts"}
        assert answer["nothing_combined"] is True
        assert {p["comparability"] for p in answer["pairs"]} == {"noted"}  # the DWA scope note relates them
        for result in answer["results"]:
            assert result["citation"]["vintage_id"] and result["citation"]["as_of"] and result["definition"]
            assert result["citation"]["live_verification"] == "unverified-live" and result["model_based"]
        shown = {(r["series_id"], o["period"], o["value_text"]) for r in answer["results"] for o in r["observations"]
                 if o["value_text"] is not None}
        assert shown <= stored_values(conn)  # only published values; nothing derived, filled or blended
        assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []

    def cell(answer, key, period):
        row = next(r for r in answer["results"] if r["native_key"] == key)
        return row, {o["period"]: o for o in row["observations"]}.get(period)

    row, estimate = cell(early, "Q.DE.S14.N.LE.NWA.D10.PT.S.N", "2097-Q4")
    assert estimate["publication_status"] == "estimated" and row["vintage"]["release_label"].startswith(
        "DWA release 2098-07")
    row, confirmed = cell(later, "Q.DE.S14.N.LE.NWA.D10.PT.S.N", "2097-Q4")
    assert confirmed["publication_status"] == "normal" and confirmed["value_text"] != estimate["value_text"]
    assert row["vintage"]["release_label"].startswith("DWA release 2099-01")
    assert cell(later, "Q.DE.S14.N.LE.NWA.D10.PT.S.N", "2098-Q1")[1] is not None
    assert cell(early, "Q.DE.S14.N.LE.NWA.D10.PT.S.N", "2098-Q1")[1] is None  # not yet released then
    assert set(EXCLUSIONS) == set(later["exclusions"]) and later["minimisation"] == MINIMISATION["decision"]

    # The United States: WID and DFA side by side; no note relates them, so the pair is comparability_unknown.
    us = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area=h.US, groups=["TopPt1", "p99.9p100"],
                                     as_of="2099-06-30")
    assert {r["provider"] for r in us["results"]} == {"wid-wealth-distribution",
                                                      "fed-distributional-financial-accounts"}
    assert [p["comparability"] for p in us["pairs"]] == ["comparability_unknown"]
    assert {"different_source", "different_population", "different_method", "different_frequency"} <= {
        d["kind"] for d in us["pairs"][0]["differences"]}

    # The history: both vintages, what changed, and per release pair the notes or comparability_unknown.
    def history(provider, key):
        return queries.series_history(h.NS, h.series(conn, provider, key)["series_id"], scopes=h.READ_ONLY)

    wid = history("wid-wealth-distribution", "DE.shweal_p99p100_992_j")
    assert [v["release_label"][:15] for v in wid["vintages"]] == ["WID update 2098", "WID update 2099"]
    assert {"method_change", "estimate_replaced"} <= {n["kind"] for n in wid["pairs"][0]["notes"]}
    dfa = history("fed-distributional-financial-accounts", "TopPt1.Net worth")
    assert dfa["pairs"][0]["comparability"] == "comparability_unknown" and dfa["vintages"][1]["changed_periods"]
    removed = history("ecb-distributional-wealth-accounts", "Q.FR.S14.N.LE.NWA._Z.EUR_MD.S.N")
    assert removed["vintages"][-1]["status"] == "removed"

    # A country no source covers is reported, never filled.
    portugal = queries.indicator_for_place(h.NS, scopes=h.READ_ONLY, area={"scheme": "iso3166-1-alpha2",
                                                                           "code": "PT"}, as_of="2099-06-30")
    assert portugal["results"] == [] and len(portugal["none_on_record"]) == 3

    # The cited evidence bundle: every item with source, record revision and as-of time.
    profile = place_profile(conn, h.NS, scopes=h.READ_ONLY, area=h.DE, as_of="2099-06-30")
    bundle = export_profile(profile)
    from src.evidence_bundle.verifier import verify_bundle

    assert not verify_bundle(bundle).errors
    cited = [o["payload"]["citation"] for o in bundle["objects"] if o["payload"].get("kind") == "wealth-series-vintage"]
    assert len(cited) == len(profile["answer"]["results"])
    assert all(c["source"] and c["record_revision"] and c["as_of"] for c in cited)
