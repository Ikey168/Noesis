"""Five-company fixture journey across the core market research surfaces."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import duckdb
import pytest

from src.domains.economic.dashboard import EconomicDashboardStore
from src.domains.economic.model import load_fixture
from src.domains.market.alerts import (
    ALERT_EXECUTE_SCOPE,
    ALERT_WRITE_SCOPE,
    MarketAlertStore,
)
from src.domains.market.dashboard import MarketCompanyDashboardStore
from src.domains.market.entitlements import MarketEntitlementStore
from src.domains.market.instruments import MarketInstrumentStore
from src.domains.market.metrics import MARKET_METRICS_FORMULA_VERSION, PRICE_FORMULAS
from src.domains.market.prices import MarketPriceStore
from src.domains.market.research import (
    RESEARCH_READ_SCOPE,
    MarketResearchError,
    MarketResearchStore,
    verify_market_brief_export,
)
from src.domains.market.screeners import MarketScreenerStore
from tests.unit.domains.market_entitlement_fixtures import FIXTURE_CAPABILITIES
from tests.unit.domains.test_market_dashboard import (
    ACQUIRED_CUTOFF,
    NAMESPACE,
    PRINCIPAL,
    PUBLIC_CUTOFF,
    SCOPES,
    T0,
    bar,
    source_ref,
)

ROOT = Path(__file__).resolve().parents[3]
TICKERS = ("MSFT", "ORCL", "CRM", "ADBE", "NOW")
ECONOMIC_CUTOFF_MS = 1_756_684_800_000


def test_five_company_market_release_journey_has_reference_and_replay_receipts(tmp_path):
    database_path = tmp_path / "market-release.duckdb"
    conn = duckdb.connect(str(database_path))
    MarketEntitlementStore(conn, now=lambda: ACQUIRED_CUTOFF).put_entitlement(
        NAMESPACE,
        "entitlement:fixture",
        provider="fixture-only",
        license_id="fixture-only",
        capabilities=FIXTURE_CAPABILITIES,
        evidence_ref="synthetic:rights-review:v1",
        decision_ref="synthetic:reviewer:v1",
        principal_id="fixture-reviewer",
        scopes={"operator"},
        effective_at_ms=0,
    )
    instruments = MarketInstrumentStore(conn, now=lambda: ACQUIRED_CUTOFF)
    listing_ids: dict[str, str] = {}
    base_by_listing: dict[str, int] = {}
    for index, ticker in enumerate(TICKERS):
        issuer_id = f"issuer:{ticker.lower()}"
        security_id = f"security:{ticker.lower()}"
        listing_id = f"listing:{ticker.lower()}"
        listing_ids[ticker] = listing_id
        base = 100 + index * 20
        base_by_listing[listing_id] = base
        instruments.put_issuer(
            NAMESPACE,
            issuer_id=issuer_id,
            kg_entity_id=f"kg:{issuer_id}",
            display_name=ticker,
            source_refs=[source_ref(f"issuer:{ticker}")],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
        instruments.put_security(
            NAMESPACE,
            issuer_id=issuer_id,
            security_id=security_id,
            security_type="common_equity",
            share_class="Common",
            denomination_currency="USD",
            industry_code="SOFTWARE",
            source_refs=[source_ref(f"security:{ticker}")],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
        listing_ref = source_ref(f"listing:{ticker}")
        instruments.put_listing(
            NAMESPACE,
            listing_id=listing_id,
            security_id=security_id,
            mic="XNAS",
            currency="USD",
            timezone="America/New_York",
            valid_from_ms=T0 - 86_400_000,
            valid_to_ms=None,
            ticker_assertions=[
                {
                    "value": ticker,
                    "valid_from_ms": T0 - 86_400_000,
                    "valid_to_ms": None,
                    "source_ref_id": listing_ref["source_ref_id"],
                }
            ],
            source_refs=[listing_ref],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
        instruments.put_universe_membership(
            NAMESPACE,
            universe_id="universe:software-five",
            security_id=security_id,
            membership_status="included",
            valid_from_ms=T0 - 86_400_000,
            valid_to_ms=None,
            source_refs=[source_ref(f"membership:{ticker}")],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )

    prices = MarketPriceStore(conn, now=lambda: ACQUIRED_CUTOFF)
    stored_bars = {}
    for ticker, listing_id in listing_ids.items():
        base = base_by_listing[listing_id]
        stored_bars[listing_id] = [
            prices.put_bar(
                NAMESPACE,
                bar(listing_id, session, close),
                principal_id=PRINCIPAL,
                scopes=SCOPES,
            )
            for session, close in zip(
                (date(2026, 1, 5), date(2026, 1, 6)),
                (float(base), float(base + 5 + TICKERS.index(ticker))),
                strict=True,
            )
        ]

    resolved = instruments.resolve_symbol(
        NAMESPACE,
        "MSFT",
        as_of_ms=T0 + 10 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert resolved["status"] == "resolved"
    assert resolved["candidates"][0]["listing"]["listing_id"] == listing_ids["MSFT"]

    peers = [listing_ids[ticker] for ticker in TICKERS if ticker != "MSFT"]
    dashboard = MarketCompanyDashboardStore(
        conn, initialize=True, now=lambda: ACQUIRED_CUTOFF
    ).build_dashboard(
        NAMESPACE,
        listing_ids["MSFT"],
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 10 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        peer_listing_ids=peers,
        universe_id="universe:software-five",
        industry_code="SOFTWARE",
        common_currency="USD",
    )
    assert dashboard["peer_count"] == 4
    subject = dashboard["panels"][0]
    assert "metrics" in subject["metrics"]["price"], subject["metrics"]["price"]
    total_return = next(
        item["value"]
        for item in subject["metrics"]["price"]["metrics"]
        if item["name"] == "total_return"
    )
    independent_return = (
        Decimal(str(stored_bars[listing_ids["MSFT"]][-1]["close"]))
        / Decimal(str(stored_bars[listing_ids["MSFT"]][0]["close"]))
        - Decimal(1)
    )
    assert Decimal(total_return) == independent_return
    assert dashboard["drilldown"]["source_revisions"]

    screener = MarketScreenerStore(conn, now=lambda: ACQUIRED_CUTOFF)
    saved_query = screener.save_query(
        NAMESPACE,
        "screen:software-five",
        {
            "filters": [{"field": "price.close", "operator": "gte", "value": 100}],
            "ranking": {"field": "price.close", "direction": "desc"},
            "missing_policy": "exclude",
            "limit": 10,
        },
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    screen = screener.run(
        NAMESPACE,
        universe_id="universe:software-five",
        as_of_ms=T0 + 10 * 86_400_000,
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 10 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        query_id="screen:software-five",
    )
    assert screen["query_revision_id"] == saved_query["revision_id"]
    assert screen["counts"]["passed"] == 5
    assert screen["run_id"]

    fixture = json.loads(
        (ROOT / "tests/fixtures/economic/benchmark.json").read_text()
    )
    load_fixture(conn, fixture)
    macro = EconomicDashboardStore(conn, now=lambda: ECONOMIC_CUTOFF_MS).build(
        "economics",
        release_id="fixture-release",
        request_key="five-company-acceptance",
        series=["fred:GDPC1:US"],
        release_cutoff_ms=ECONOMIC_CUTOFF_MS,
        acquired_cutoff_ms=ECONOMIC_CUTOFF_MS,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert macro["snapshots"]["latest"]["series"]
    assert macro["snapshots"]["latest"]["snapshot_id"]

    alerts = MarketAlertStore(conn, now=lambda: ACQUIRED_CUTOFF)
    alert_write = {ALERT_WRITE_SCOPE, f"namespace:{NAMESPACE}:write"}
    alert_execute = {ALERT_EXECUTE_SCOPE, f"namespace:{NAMESPACE}:write"}
    alert = alerts.save_watch(
        NAMESPACE,
        "watch:msft-threshold",
        1,
        {
            "stale_after_ms": 30 * 86_400_000,
            "triggers": [
                {
                    "rule_id": "close-over-100",
                    "type": "price_threshold",
                    "listing_id": listing_ids["MSFT"],
                    "field": "close",
                    "operator": "gte",
                    "value": 100,
                }
            ],
        },
        {"dedupe_window_ms": 86_400_000},
        owner=PRINCIPAL,
        principal_id=PRINCIPAL,
        scopes=alert_write,
    )
    alert_run = alerts.run(
        NAMESPACE,
        alert["watch_id"],
        [
            {
                "kind": "price",
                "listing_id": listing_ids["MSFT"],
                "close": stored_bars[listing_ids["MSFT"]][-1]["close"],
                "observed_at_ms": stored_bars[listing_ids["MSFT"]][-1]["bar_start_ms"],
                "source_revision_ids": [stored_bars[listing_ids["MSFT"]][-1]["revision_id"]],
            }
        ],
        generation=1,
        as_of_ms=T0 + 10 * 86_400_000,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        acquired_by_ms=ACQUIRED_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=alert_execute,
    )
    assert alert_run["status"] == "completed"
    assert alert_run["triggered"][0]["rule_id"] == "close-over-100"
    assert alert_run["run_id"]

    research = MarketResearchStore(conn, now=lambda: ACQUIRED_CUTOFF)
    journey_companies = []
    source_revision_ids = set()
    company_performance = {}
    for panel in dashboard["panels"]:
        total_return_metric = next(
            item
            for item in panel["metrics"]["price"]["metrics"]
            if item["name"] == "total_return"
        )
        issuer_id = panel["identity"]["issuer"]["issuer_id"]
        revisions = panel["drilldown"]["input_revision_ids"]
        source_revision_ids.update(revisions)
        company_performance[issuer_id] = total_return_metric["value"]
        journey_companies.append(
            {
                "company_id": issuer_id,
                "performance": total_return_metric["value"],
                "valuation_change": None,
                "source_revision_ids": revisions,
                "formula_versions": [
                    MARKET_METRICS_FORMULA_VERSION,
                    PRICE_FORMULAS["total_return"],
                ],
                "evidence": [
                    {
                        "stance": "supporting",
                        "claim": "The short fixture window has a positive calculated return.",
                        "source_revision_ids": revisions,
                    },
                    {
                        "stance": "contradicting",
                        "claim": "Two sessions cannot establish a durable performance trend.",
                        "source_revision_ids": revisions,
                    },
                ],
            }
        )
    macro_snapshot = macro["snapshots"]["latest"]
    macro_scenario = {
        "scenario_id": "fixture-growth-downside",
        "series_id": "fred:GDPC1:US",
        "snapshot_id": macro_snapshot["snapshot_id"],
        "shock": {"periods": 1, "growth_delta": "-0.01"},
        "assumptions": ["One-period growth shock; no causal transmission is inferred."],
        "supporting_evidence": [
            {"claim": "The selected macro snapshot is available at the declared cutoff."}
        ],
        "contradicting_evidence": [
            {"claim": "A single synthetic series does not identify company-level effects."}
        ],
    }
    journey = research.acceptance_journey(
        NAMESPACE,
        journey_id="journey:software-five",
        companies=journey_companies,
        macro_scenario=macro_scenario,
        cutoff_ms=PUBLIC_CUTOFF,
        owner=PRINCIPAL,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert len(journey["companies"]) == 5
    assert len(journey["gaps"]) == 5
    assert journey["analyst_review"]["status"] == "pending_human_review"
    assert journey["supporting_and_contradicting_evidence"]

    brief = research.generate_brief(
        NAMESPACE,
        report_id="brief:software-five-historical",
        version=1,
        title="Software peer fixture at historical cutoff",
        sections=[
            {
                "heading": "Peer performance",
                "body": json.dumps(company_performance, sort_keys=True),
            },
            {
                "heading": "Valuation coverage",
                "body": "Valuation changes are unavailable: no sourced market-value numerator is present.",
            },
            {
                "heading": "Macro scenario",
                "body": json.dumps(macro_scenario, sort_keys=True),
            },
        ],
        cutoff_ms=PUBLIC_CUTOFF,
        formula_versions=[
            MARKET_METRICS_FORMULA_VERSION,
            PRICE_FORMULAS["total_return"],
        ],
        assumptions=macro_scenario["assumptions"],
        source_locators=[
            {
                "locator": f"fixture://market-revision/{revision_id}",
                "source_revision_id": revision_id,
            }
            for revision_id in sorted(source_revision_ids)
        ],
        charts=[
            {
                "chart_id": "chart:five-company-return",
                "chart_type": "bar",
                "title": "Two-session total return (fixture)",
                "series": [
                    {"label": issuer_id, "value": value}
                    for issuer_id, value in sorted(company_performance.items())
                ],
                "source_revision_ids": sorted(source_revision_ids),
            }
        ],
        artifact_refs=[{"artifact_id": journey["journey_id"], "version": 1}],
        owner=PRINCIPAL,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    internal_export = research.export_brief(
        NAMESPACE,
        report_id=brief["report_id"],
        version=1,
        output_format="markdown",
        external=False,
        owner=PRINCIPAL,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert internal_export["payload"]
    assert verify_market_brief_export(internal_export)["valid"] is True
    external_export = research.export_brief(
        NAMESPACE,
        report_id=brief["report_id"],
        version=1,
        output_format="json",
        external=True,
        owner=PRINCIPAL,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert external_export["rights"]["export_withheld"] is True
    assert external_export["payload"] is None
    with pytest.raises(MarketResearchError) as restricted:
        research.inspect(
            NAMESPACE,
            brief["report_id"],
            1,
            principal_id="analyst:without-access",
            scopes={RESEARCH_READ_SCOPE, f"namespace:{NAMESPACE}:read"},
        )
    assert restricted.value.code == "not_found"

    # A later provider correction changes the current view but not the pinned report.
    correction_time = ACQUIRED_CUTOFF + 100
    corrected_bar = bar(
        listing_ids["MSFT"],
        date(2026, 1, 6),
        float(stored_bars[listing_ids["MSFT"]][-1]["close"]) + 2,
    )
    corrected_bar.update(
        {
            "public_at_ms": PUBLIC_CUTOFF + 1,
            "retrieved_at_ms": correction_time,
            "provider_revision_id": "fixture:bar:2",
            "source_refs": [
                source_ref("bar:MSFT:2026-01-06-correction", PUBLIC_CUTOFF + 1)
            ],
        }
    )
    prices.now = lambda: correction_time
    corrected = prices.put_bar(
        NAMESPACE,
        corrected_bar,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        expected_revision=1,
    )
    corrected_dashboard = MarketCompanyDashboardStore(
        conn, now=lambda: correction_time
    ).build_dashboard(
        NAMESPACE,
        listing_ids["MSFT"],
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 10 * 86_400_000,
        acquired_by_ms=correction_time,
        publicly_available_by_ms=PUBLIC_CUTOFF + 1,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        peer_listing_ids=peers,
        universe_id="universe:software-five",
        industry_code="SOFTWARE",
        common_currency="USD",
    )
    corrected_return = next(
        item["value"]
        for item in corrected_dashboard["panels"][0]["metrics"]["price"]["metrics"]
        if item["name"] == "total_return"
    )
    assert corrected["revision"] == 2
    assert corrected["prior_revision_id"] in dashboard["drilldown"]["source_revisions"]
    assert corrected_return != company_performance["issuer:msft"]

    conn.close()

    # Verify that the historical research artifacts survive a process/database reopen.
    conn = duckdb.connect(str(database_path))
    recovered = MarketResearchStore(
        conn, initialize=False, now=lambda: correction_time
    ).inspect(
        NAMESPACE,
        brief["report_id"],
        1,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert recovered["record_hash"] == brief["record_hash"]
    assert recovered["source_locators"] == brief["source_locators"]
    assert recovered["cutoff_ms"] == PUBLIC_CUTOFF
    conn.close()
