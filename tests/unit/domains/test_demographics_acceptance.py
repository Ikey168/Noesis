"""Offline geography-to-series-and-definitions acceptance journey (#2017).

The pinned Eurostat, UNHCR, IOM DTM, Destatis GENESIS and Statistik
Berlin-Brandenburg fixtures are replayed through the source-pack runtime (the
operator declares, per document, the publisher references the metadata names;
fictional here), and the BAMF figure sheet enters through the operator import,
as the audit found no machine-readable BAMF access. The journey then takes a
Berlin district, a Land and a member state to series with definition, unit,
geography level, vintage and comparability notes, boundary projections with
the boundary vintage used and citation links to a legal work and a legislative
dossier. Nothing is merged, breaks stay marked, unknowns stay unknown, and
re-running acquisition after a simulated restart adds no record, vintage or
link. No network is used; nothing here is live coverage.
"""

from __future__ import annotations

import copy
import json

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore
from src.kb.demographics import (
    DemographicError,
    DemographicStore,
    forbidden_keys,
    readiness,
)
from src.kb.demographics_comparability import DemographicComparability
from src.kb.demographics_links import DemographicLinks
from src.kb.demographics_monitoring import DemographicMonitor
from src.kb.demographics_places import DemographicPlaces
from src.kb.subscriptions import SubscriptionStore
from tests.unit import demographics_harness as h
from tests.unit.domains.test_demographic_places import LAND, MARCH_16, import_boundaries

TABLES = (
    "demographic_releases",
    "demographic_definitions",
    "demographic_series",
    "demographic_vintages",
    "demographic_observations",
    "demographic_breaks",
    "demographic_geo_resolutions",
    "demographic_links",
    "knowledge_subscription_events",
)


def manifest_with_references() -> dict:
    manifest = copy.deepcopy(json.loads(h.PACK.read_text()))
    for source in manifest["sources"]:
        if source["source_id"] == h.SOURCES["eurostat"]:
            source["demographics"]["documents"][2]["references"] = h.EUROSTAT_REFERENCES
        if source["source_id"] == h.SOURCES["berlin"]:
            source["demographics"]["documents"][0]["references"] = h.BERLIN_REFERENCES
    return manifest


def acquire(conn, run_key: str) -> list[dict]:
    manifest = manifest_with_references()
    store = SourcePackStore(conn)
    if not conn.execute(
        "SELECT 1 FROM source_pack_versions WHERE pack_id=?", [manifest["pack_id"]]
    ).fetchone():
        store.install(manifest, principal_id="operator", enable=True, now_ms=1)
    clock = iter(range(1_000, 1_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    adapters = runtime.fixture_adapters(manifest["pack_id"], h.ROOT)
    receipts = []
    for key, source_id in sorted(h.SOURCES.items()):
        runtime.accept_license(manifest["pack_id"], source_id, principal_id="operator")
        receipts.append(
            runtime.run(
                {
                    "pack_id": manifest["pack_id"],
                    "run_key": f"{run_key}:{key}",
                    "operation": "release",
                    "source_ids": [source_id],
                    "max_results": 100,
                    "max_bytes": 10_000_000,
                    "timeout_ms": 60_000,
                },
                principal_id="operator",
                adapters={source_id: adapters[source_id]},
                secret_resolver=lambda _ref: "fixture-credential-not-a-real-key",
                dns_resolver=lambda _host: ["8.8.8.8"],
            )
        )
    return receipts


def counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def ids(conn) -> dict[str, list[str]]:
    return {
        "vintages": sorted(
            r[0]
            for r in conn.execute(
                "SELECT vintage_id FROM demographic_vintages"
            ).fetchall()
        ),
        "links": sorted(
            r[0]
            for r in conn.execute("SELECT link_id FROM demographic_links").fetchall()
        ),
    }


def test_geography_to_series_definitions_boundaries_and_citations(tmp_path):
    path = str(tmp_path / "demographics-acceptance.duckdb")
    conn = duckdb.connect(path)
    receipts = acquire(conn, "first")
    assert {r["status"] for r in receipts} == {"complete"}, receipts
    assert h.load_bamf(conn)["status"] == "applied"
    store = DemographicStore(conn)
    releases = store.releases(h.NS)
    assert {r["evidence_origin"] for r in releases} == {
        "fixture",
        "operator",
    }  # offline evidence, never live
    assert readiness(conn)["stores_ready"] is True

    # Boundaries the Geospatial pack holds, resolved by the published code only.
    features = import_boundaries(conn)
    places = DemographicPlaces(conn)
    places.resolve_geographies(
        h.NS, principal_id="op", scopes=h.SCOPES, geo_namespace="geo", collections=LAND
    )
    resolutions = {
        r["geography_code"]: r for r in places.resolutions(h.NS, scopes=h.READ_ONLY)
    }
    assert (
        resolutions["003"]["effective"] == "unresolved"
    )  # Pankow's boundary is not held: left unresolved

    # A Berlin district: register-era definition, census-base break, boundary vintage and a dossier citation.
    district = places.boundary_series(
        h.NS, features["bezirksgrenzen.11000001"], scopes=h.READ_ONLY
    )
    residents = next(c for c in district["series"] if c["indicator"] == "residents")
    assert residents["definition"]["content"]["population_base"] == "census-2022"
    assert residents["unit"] == {"code": "persons", "label": "persons"}
    assert residents["geography_level"]["level"] == "bezirk"
    assert residents["resolution"]["boundary_vintage"]["feature_revision_id"]
    assert [b["kind"] for b in residents["breaks"]] == ["census_base_change"]
    assert {o["availability"] for o in district["other_levels"]} == {
        "available_at_different_level"
    }

    # A Land: Destatis (AGS) and Eurostat (NUTS 1) side by side, unnoted pair unknown, then a reviewed note.
    land = places.boundary_series(
        h.NS, features["lan.11"], scopes=h.READ_ONLY, concept="population_stock"
    )
    assert {c["provider"] for c in land["series"]} == {"destatis", "eurostat"}
    assert {p["comparability"] for p in land["pairs"]} == {"comparability_unknown"}
    comparability = DemographicComparability(conn)
    destatis = next(
        c
        for c in land["series"]
        if c["provider"] == "destatis" and c["dimensions"]["NAT"] == "INSGESAMT"
    )
    eurostat = next(c for c in land["series"] if c["provider"] == "eurostat")
    note = comparability.record(
        h.NS,
        {"series_id": destatis["series_id"]},
        {"series_id": eurostat["series_id"]},
        "same_definition_different_reference_date",
        "31 December stock versus 1 January stock (fictional note)",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    comparability.review(
        h.NS,
        note["note_id"],
        "accept",
        "checked",
        principal_id="rev",
        scopes=h.REVIEW_SCOPES,
    )
    land = places.boundary_series(
        h.NS, features["lan.11"], scopes=h.READ_ONLY, concept="population_stock"
    )
    pair = next(
        p
        for p in land["pairs"]
        if {p["left"]["id"], p["right"]["id"]}
        == {destatis["series_id"], eurostat["series_id"]}
    )
    assert pair["comparability"] == "same_definition_different_reference_date"

    # A member state: two publishers' asylum applications side by side without a merged value.
    applications = comparability.side_by_side(
        h.NS,
        "asylum_applications",
        scopes=h.READ_ONLY,
        geography_codes=["DE"],
        period_from="2098",
        period_to="2098",
    )
    assert {c["provider"] for c in applications["series"]} == {"eurostat", "bamf"}
    assert {c["observations"][0]["value"] for c in applications["series"]} == {
        "220300",
        "205100",
    }
    assert (
        forbidden_keys(applications) == []
        and forbidden_keys(land) == []
        and forbidden_keys(district) == []
    )
    country = places.boundary_series(
        h.NS,
        features["cntr.DE"],
        scopes=h.READ_ONLY,
        as_of_ms=MARCH_16,
        concept="population_stock",
    )
    (pjan,) = [c for c in country["series"] if c["series_code"] == "demo_pjan"]
    assert pjan["observations"][-1]["flags"] == ["p"]
    pinned = places.pin(h.NS, country["receipt"], principal_id="alice", scopes=h.SCOPES)
    unhcr = store.find_series(h.NS, provider="unhcr")
    assert all(any("year-end stocks" in n for n in s["coverage_notes"]) for s in unhcr)
    missing = next(s for s in unhcr if s["indicator"] == "asylum_seekers")
    assert (
        store.values(h.NS, missing["series_id"])["observations"][-1]["value"] is None
    )  # unknown stays unknown

    # Citations, created only from explicit publisher references.
    h.load_acts(conn)
    dossier = h.load_dossier(conn)
    links = DemographicLinks(conn)
    acts = links.link_references(
        h.NS,
        legal_namespace=h.LEGAL_NS,
        principal_id="op",
        scopes=h.SCOPES | h.LEGAL_SCOPES,
    )
    (act_link,) = acts["linked"]
    dossiers = links.link_dossier(
        h.NS,
        h.DOSSIER_NS,
        dossier["dossier"]["dossier_id"],
        principal_id="alice",
        scopes=h.SCOPES | dossier["scopes"],
    )
    assert dossiers["linked"]
    assert (
        links.link(h.NS, act_link, scopes=h.READ_ONLY)["basis"] == "publisher_reference"
    )
    census_act = conn.execute(
        "SELECT work_id FROM legal_works WHERE title LIKE '%censuses%'"
    ).fetchone()[0]
    assert (
        links.series_citing(h.NS, census_act, scopes=h.READ_ONLY)["series"] == []
    )  # no reference, no link

    # A later Eurostat release: the pinned vintage turns stale, the monitor reports the revision and break.
    monitor = DemographicMonitor(conn)
    watch = monitor.create(
        h.NS,
        "pjan",
        series_filter={"series_code": "demo_pjan"},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    h.apply(conn, "eurostat", 0, h.PJAN_SEPTEMBER)
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    revised = monitor.run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert sorted(n["kind"] for n in revised["notifications"]) == [
        "series_break",
        "vintage_revision",
    ]
    assert revised["stale_pins"] and places.pins(
        h.NS, scopes=h.READ_ONLY, receipt_digest=pinned["pins"][0]["receipt_digest"]
    )
    assert (
        places.replay(country["receipt"], scopes=h.READ_ONLY)["status"] == "reproduced"
    )

    # Restart and replay: nothing new is recorded.
    before, before_ids = counts(conn), ids(conn)
    conn.close()
    conn = duckdb.connect(path)
    again = acquire(conn, "second")
    assert {r["status"] for r in again} == {"complete"}
    assert h.load_bamf(conn)["status"] == "unchanged"
    DemographicPlaces(conn).resolve_geographies(
        h.NS, principal_id="op", scopes=h.SCOPES, geo_namespace="geo", collections=LAND
    )
    DemographicLinks(conn).link_references(
        h.NS,
        legal_namespace=h.LEGAL_NS,
        principal_id="op",
        scopes=h.SCOPES | h.LEGAL_SCOPES,
    )
    SubscriptionStore(conn).commit_watermark(h.NS, 3)
    assert (
        DemographicMonitor(conn).run(
            watch["subscription_id"], principal_id="alice", scopes=h.SCOPES
        )["notifications"]
        == []
    )
    assert counts(conn) == before and ids(conn) == before_ids

    # Scope checks: no read without the read scope, no write or review without theirs.
    with pytest.raises(DemographicError):
        DemographicComparability(conn).side_by_side(
            h.NS, "population_stock", scopes={"namespace:global:read"}
        )
    with pytest.raises(DemographicError):
        DemographicStore(conn).import_sheet(
            h.NS, json.loads(h.body(h.BAMF_SHEET)), principal_id="x", scopes=h.READ_ONLY
        )
    with pytest.raises(DemographicError):
        DemographicComparability(conn).review(
            h.NS, note["note_id"], "reject", "x", principal_id="x", scopes=h.SCOPES
        )
    conn.close()


def test_the_suite_runs_with_the_feature_selected_and_the_bundle_resolves_without_it():
    bundles = adapt_all()
    version = bundles["economics"]["version"]
    for features, bound in ((["demographics"], True), ([], False)):
        result = resolve(
            [{"pack": "economics", "version": version, "features": features}],
            list(bundles.values()),
            provider_descriptors(),
        )
        assert result.ok, result.failure
        providers = {
            b["provider"]
            for b in result.plan["bindings"]
            if "economics" in b["consumers"]
        }
        assert ("economics.demographics" in providers) is bound
