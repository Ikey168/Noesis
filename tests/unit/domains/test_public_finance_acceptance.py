"""Offline budget-line-to-payments acceptance journey (#2006).

The pinned Bundeshaushalt, Berlin, FTS and Eurostat GFS fixtures are replayed
through the source-pack runtime (the operator declares, per plan document, the
act and bill it cites), and the Bundesrechnungshof finding sheet enters through
the operator import path (the audit found no machine access to replay). The
journey then takes budget lines to their plan, supplementary plan, outturn
vintages, audit findings, cited acts and dossiers and district, and an EU budget
line to its beneficiary payments, reviewed identity and award-history context.
Plan, outturn and payment records stay distinct, figures on different bases are
shown side by side without a difference, conflicts are flagged, unknowns stay
unknown, identity links are created, reviewed and reversed, and re-running
acquisition and the monitors after a simulated restart adds no record and no
event. No network is used; nothing here is live coverage.
"""

from __future__ import annotations

import copy
import json

import duckdb

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore
from src.kb.ownership_records import record
from src.kb.ownership_store import OwnershipStore
from src.kb.public_finance import PublicFinanceStore, forbidden_keys, readiness
from src.kb.public_finance_gfs import GovernmentFinanceStatistics
from src.kb.public_finance_identity import PublicFinanceIdentity
from src.kb.public_finance_links import PublicFinanceLinks
from src.kb.public_finance_monitoring import PublicFinanceMonitor
from src.kb.public_finance_places import PublicFinancePlaces
from src.kb.public_finance_queries import PublicFinanceQueries
from src.kb.subscriptions import SubscriptionStore
from tests.unit import public_finance_harness as h
from tests.unit.domains.test_public_finance_identity import GEO, _boundaries

GFS = "estat:gov_10a_main:DE:na_item=TE:sector=S13:unit=MIO_EUR"
BENEFICIARY = "public-finance:beneficiary:eu-fts:vat:DE:DE999999999"
TABLES = (
    "public_finance_releases",
    "public_finance_lines",
    "public_finance_figures",
    "public_finance_payments",
    "public_finance_findings",
    "public_finance_gfs_vintages",
    "economic_vintages",
    "knowledge_subscription_events",
)


def manifest_with_references() -> dict:
    """The deployed manifest plus the citations an operator declares from each plan's title page (fictional)."""
    manifest = copy.deepcopy(json.loads(h.PACK.read_text()))
    for source in manifest["sources"]:
        if source["source_id"] == h.SOURCES["bund"]:
            source["public_finance"]["documents"][0]["references"] = h.PLAN_REFERENCES
            source["public_finance"]["documents"][1]["references"] = (
                h.NACHTRAG_REFERENCES
            )
        if source["source_id"] == h.SOURCES["berlin"]:
            source["public_finance"]["documents"][0]["references"] = h.BERLIN_REFERENCES
    return manifest


def acquire(conn, run_key: str) -> list[dict]:
    """Every public-finance source of the pack through the runtime's fixture adapters."""
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
                    "max_results": 1000,
                    "max_bytes": 20_000_000,
                    "timeout_ms": 60_000,
                },
                principal_id="operator",
                adapters={source_id: adapters[source_id]},
                dns_resolver=lambda _host: ["8.8.8.8"],
            )
        )
    return receipts


def counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def monitor_run(conn, watch, watermark) -> list[dict]:
    SubscriptionStore(conn).commit_watermark("global", watermark)
    return PublicFinanceMonitor(conn).run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )["notifications"]


def test_budget_line_to_plans_outturns_payments_findings_acts_dossiers_and_award_context(
    tmp_path,
):
    path = str(tmp_path / "public-finance-acceptance.duckdb")
    conn = duckdb.connect(path)
    receipts = acquire(conn, "first")
    assert {r["status"] for r in receipts} == {"complete"}, receipts
    # Later publications arrive after the pinned ones: the October EDP vintage and the audit report.
    h.apply(conn, "gfs", 0, h.GFS_OCTOBER)
    h.load_findings(conn)
    h.load_acts(conn)
    dossiers = h.budget_dossiers(conn)
    h.load_awards(conn)
    _boundaries(conn)
    queries = PublicFinanceQueries(conn)
    scopes = (
        h.REVIEW_SCOPES
        | h.LEGAL_SCOPES
        | h.PROCUREMENT_SCOPES
        | dossiers["scopes"]
        | GEO
    )
    links = PublicFinanceLinks(conn)
    links.link_acts(
        h.NS, legal_namespace=h.LEGAL_NS, principal_id="alice", scopes=scopes
    )
    for dossier in (dossiers["budget"], dossiers["supplementary"]):
        links.link_dossier(
            h.NS,
            h.DOSSIER_NS,
            dossier["dossier_id"],
            principal_id="alice",
            scopes=scopes,
        )
    PublicFinancePlaces(conn).link_districts(
        h.NS, principal_id="alice", scopes=scopes, geo_namespace="geo"
    )

    # --- a federal line: plan, supplementary plan, outturn vintages, findings, cited act and bills
    grant = h.line_id(conn, "de-bund-haushalt", titel="68101")
    dossier = queries.budget_dossier(h.NS, grant, scopes=scopes)
    history = dossier["history"]["2099"]
    # Three distinct record kinds, never merged into one number.
    assert [c["figure_kind"] for c in history["columns"]] == [
        "plan",
        "supplementary_plan",
        "outturn",
    ]
    for column in history["columns"]:
        assert (
            column["current"]["citation"]["file_sha256"]
            and column["current"]["accounting_basis"] == "cash"
        )
        assert column["current"]["citation"]["evidence_origin"] == "fixture"
    (vintages,) = history["vintage_comparison"]
    assert [v["source_revision"]["document"] for v in vintages["vintages"]] == [
        "Ist (vorläufig)",
        "Haushaltsrechnung (Ist)",
    ] and vintages["revision"] == "150000.00"
    assert {d["status"] for d in history["differences"]} == {"computed"}
    assert len(dossier["findings"]) == 2 and all(
        "verdict" not in f for f in dossier["findings"]
    )
    act = next(
        a
        for a in dossier["acts"]
        if a["reference"]["identifier"] == "BGBl. 2098 I Nr. 999"
    )
    assert (
        act["state"] == "unresolved"
        and "cited act BGBl. 2098 I Nr. 999 is not acquired" in dossier["unknowns"]
    )
    bills = {(d["state"], d["basis"]) for d in dossier["dossiers"]}
    assert ("linked", "dossier-identifier") in bills and (
        "candidate",
        "discovery",
    ) in bills
    assert (
        "no acquired source publishes beneficiary payments against this line"
        in dossier["unknowns"]
    )
    assert forbidden_keys(dossier) == []

    # --- a Berlin district line: its act by GVBl citation and its district by the published code only
    mitte = h.line_id(conn, "de-be-haushalt", bereich="31")
    berlin = queries.budget_dossier(h.NS, mitte, scopes=scopes)
    assert (
        berlin["place"]["state"] == "linked"
        and berlin["place"]["feature_title"] == "Mitte"
    )
    assert any(
        a["state"] == "linked" and a["reference"]["identifier"] == "GVBl. 2098 S. 999"
        for a in berlin["acts"]
    )
    assert [c["figure_kind"] for c in berlin["history"]["2099"]["columns"]] == [
        "plan",
        "outturn",
    ]

    # --- unknowns stay unknown; conflicting sources are flagged side by side
    revenue = h.line_id(conn, "de-bund-haushalt", titel="11901")
    blank = queries.compare_budget_line(h.NS, revenue, "2099", scopes=scopes)
    assert "haushaltsplan: amount not published" in blank["unknowns"]
    assert {d["status"] for d in blank["differences"]} == {"not-computed"}

    # --- different accounting bases: ESA 2010 beside cash, commitments beside payments, never differenced
    beside = queries.compare_budget_line(
        h.NS, grant, "2098", scopes=scopes, gfs_series_id=GFS
    )
    assert (
        beside["esa2010_context"]["difference"] is None
        and beside["esa2010_context"]["value"] == 1085.0
    )
    gfs = GovernmentFinanceStatistics(conn).compare(h.NS, GFS, scopes=scopes)
    (item,) = gfs["items"]
    assert [r["period_after"] for r in item["same_period_revisions"]] == ["2097"]
    eu_line = h.line_id(conn, "eu-budget-line", budget_line="99010201")
    eu = queries.budget_dossier(
        h.NS, eu_line, scopes=scopes, procurement_namespace=h.PROCUREMENT_NS
    )
    (pair,) = eu["history"]["2099"]["payments"]["commitment_payment_pairs"]
    assert pair["status"] == "different-bases" and pair["difference"] is None
    assert {p["record_type"] for p in eu["payments"]} == {"beneficiary_payment"}

    # --- beneficiary identity: created, reviewed and reversed; award history only as context while matched
    OwnershipStore(conn).apply(
        "global",
        [
            record(
                "legal_entity",
                "bods:entity:beispiel",
                {"provider": "open-ownership", "provider_record_id": "s1"},
                name="Beispiel Forschung GmbH",
                jurisdiction="DE",
                identifiers=[{"scheme": "vat", "value": "999999999"}],
            )
        ],
        run_id="own",
        observed_at_ms=1,
        principal_id="p",
    )
    identity = PublicFinanceIdentity(conn)
    proposed = identity.propose(
        h.NS, principal_id="alice", scopes=scopes, ownership_namespace="global"
    )
    ownership = next(
        c for c in proposed["candidates"] if "bods:entity:beispiel" in c["records"]
    )
    supplier = next(
        c
        for c in links.propose_award_parties(
            h.NS, h.PROCUREMENT_NS, principal_id="alice", scopes=scopes
        )["candidates"]
    )
    for candidate in (ownership, supplier):
        identity.service.review(
            h.NS,
            candidate["candidate_id"],
            "accept",
            "same VAT number",
            principal_id="rev",
            scopes=scopes,
        )
    matched = queries.beneficiary_dossier(
        h.NS, BENEFICIARY, scopes=scopes, procurement_namespace=h.PROCUREMENT_NS
    )
    assert (
        matched["identity"]["state"] == "matched"
        and len(matched["identity"]["links"]) == 2
    )
    (award,) = matched["award_context"]["awards"]
    assert "never shows that a payment was made" in award["semantics"]
    for candidate in (ownership, supplier):
        identity.service.revert(
            h.NS,
            candidate["candidate_id"],
            "reviewer withdrew",
            principal_id="rev",
            scopes=scopes,
        )
    reverted = queries.beneficiary_dossier(
        h.NS, BENEFICIARY, scopes=scopes, procurement_namespace=h.PROCUREMENT_NS
    )
    assert (
        reverted["identity"]["state"] == "unmatched"
        and reverted["award_context"]["awards"] == []
    )
    assert forbidden_keys(matched) == [] and forbidden_keys(reverted) == []

    # --- monitors, then a simulated restart: re-acquisition and monitor replay add nothing
    monitor = PublicFinanceMonitor(conn)
    watches = [
        monitor.create(
            h.NS,
            "grant",
            watch="budget_line",
            key=grant,
            principal_id="alice",
            scopes=h.SCOPES,
        ),
        monitor.create(
            h.NS,
            "horizon",
            watch="programme",
            key="Fictional Horizon Programme",
            principal_id="alice",
            scopes=h.SCOPES,
        ),
    ]
    first = [monitor_run(conn, w, 1) for w in watches]
    assert (
        len(first[0]) == 6 and len(first[1]) == 3
    )  # 4 figure publications + 2 findings; 3 payment rows
    before = counts(conn)
    conn.close()
    conn = duckdb.connect(path)
    again = acquire(conn, "after-restart")
    assert {r["status"] for r in again} == {"complete"}
    h.apply(conn, "gfs", 0, h.GFS_OCTOBER)
    h.load_findings(conn)
    assert [monitor_run(conn, w, 2) for w in watches] == [[], []]
    assert counts(conn) == before
    status = readiness(conn)
    assert (
        status["store_ready"]
        and status["providers"]["bundeshaushalt"]["live"] == "outstanding"
    )
    assert status["providers"]["bundesrechnungshof"]["live"] == "not-implemented"
    # Offline evidence only: fixture replays and the operator's finding sheet, never a live release.
    origins = {
        r["evidence_origin"]
        for r in PublicFinanceStore(conn, initialize=False).releases(h.NS)
    }
    assert origins == {"fixture", "operator"}
    conn.close()
