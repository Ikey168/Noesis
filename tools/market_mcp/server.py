"""Market analytics tools backed by the shared market capability service."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

mcp = FastMCP("noesis-market")


def _context() -> tuple[str, set[str]]:
    from src.config.env import resolve_env

    principal = (resolve_env("MCP_PRINCIPAL", "local-reader") or "").strip()
    raw = resolve_env("MCP_SCOPES", "") or ""
    return principal, {
        item.strip() for item in raw.replace(";", ",").split(",") if item.strip()
    }


def _run(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    import duckdb

    from src.config.env import warehouse_path
    from src.domains.market.capabilities import MarketCapabilityService

    principal, scopes = _context()
    conn = None
    writes = operation in {
        "calculate_price_metrics",
        "calculate_fact_metrics",
        "company_dashboard",
        "save_screener_query",
        "screen_market_universe",
        "economic_market_dashboard",
        "save_market_alert_watch",
        "run_market_alerts",
        "deliver_market_alert",
        "market_event_study",
        "market_factor_analysis",
        "market_backtest",
        "market_walk_forward",
        "market_portfolio",
        "market_risk_report",
        "save_market_materials",
        "build_market_company_dossier",
        "build_market_industry_model",
        "calculate_market_sizing",
        "record_market_driver_hypotheses",
        "save_market_thesis",
        "review_market_thesis",
        "generate_market_brief",
        "deliver_market_brief",
        "market_acceptance_journey",
        "review_market_acceptance_journey",
        "schedule_market_brief",
        "run_market_brief_schedules",
        "record_market_operations_measurements",
        "evaluate_market_slos",
        "save_market_budget",
        "consume_market_budget",
        "record_market_recovery_drill",
        "create_market_backup",
        "restore_market_backup",
        "prune_market_operations_audit",
        "save_market_runbook",
        "record_market_repair",
        "market_fixed_income",
        "market_fx_commodity",
        "market_derivatives",
        "market_digital_asset",
        "market_intraday_replay",
        "market_international_coverage",
    }
    try:
        conn = duckdb.connect(
            warehouse_path() or str(ROOT / "data" / "neuronews.duckdb"),
            read_only=not writes,
        )
        return MarketCapabilityService(conn).invoke(
            operation, arguments, principal_id=principal, scopes=scopes
        )
    except Exception:  # noqa: BLE001 - stable MCP availability envelope
        return {
            "ok": False,
            "error": {
                "code": "market_unavailable",
                "message": "market warehouse is unavailable",
            },
        }
    finally:
        if conn is not None:
            conn.close()


@mcp.tool()
def market_readiness(namespace: str) -> dict:
    """Report bounded local source coverage and whether live providers are configured."""
    return _run("readiness", {"namespace": namespace})


@mcp.tool()
def lookup_market_instrument(
    namespace: str,
    acquired_by_ms: int,
    symbol: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    identifier: str | None = None,
    scheme: str | None = None,
    as_of_ms: int | None = None,
    publicly_available_by_ms: int | None = None,
    mic: str | None = None,
) -> dict:
    """Resolve a listing symbol, identifier, or exact instrument revision as of cutoffs."""
    return _run(
        "lookup_instrument",
        {
            "namespace": namespace,
            "acquired_by_ms": acquired_by_ms,
            "symbol": symbol,
            "object_type": object_type,
            "object_id": object_id,
            "identifier": identifier,
            "scheme": scheme,
            "as_of_ms": as_of_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
            "mic": mic,
        },
    )


@mcp.tool()
def get_market_price_history(
    namespace: str,
    listing_id: str,
    start_ms: int,
    end_ms: int,
    acquired_by_ms: int,
    publicly_available_by_ms: int | None = None,
    cursor: int = 0,
    page_size: int = 100,
) -> dict:
    """Read a bounded page of retained end-of-day bars under explicit cutoffs."""
    return _run(
        "price_history",
        {
            "namespace": namespace,
            "listing_id": listing_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "acquired_by_ms": acquired_by_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
            "cursor": cursor,
            "page_size": page_size,
        },
    )


@mcp.tool()
def get_market_financial_statements(
    namespace: str,
    issuer_id: str,
    acquired_by_ms: int,
    publicly_available_by_ms: int,
    taxonomy: str | None = None,
    concept: str | None = None,
    filing_form: str | None = None,
    require_complete: bool = False,
    cursor: int = 0,
    page_size: int = 100,
) -> dict:
    """Read a bounded page of versioned filing facts at public and acquisition cutoffs."""
    return _run(
        "financial_statements",
        {
            "namespace": namespace,
            "issuer_id": issuer_id,
            "acquired_by_ms": acquired_by_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
            "taxonomy": taxonomy,
            "concept": concept,
            "filing_form": filing_form,
            "require_complete": require_complete,
            "cursor": cursor,
            "page_size": page_size,
        },
    )


@mcp.tool()
def get_company_research_dashboard(
    namespace: str,
    listing_id: str,
    start_ms: int,
    end_ms: int,
    acquired_by_ms: int,
    publicly_available_by_ms: int,
    peer_listing_ids: list[str] | None = None,
    universe_id: str | None = None,
    universe_as_of_ms: int | None = None,
    industry_code: str | None = None,
    common_currency: str | None = None,
    include_calculations: bool = True,
    fact_metric_requests: list[dict[str, Any]] | None = None,
) -> dict:
    """Build a point-in-time company/peer dashboard with source drilldowns."""
    return _run(
        "company_dashboard",
        {
            "namespace": namespace,
            "listing_id": listing_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "acquired_by_ms": acquired_by_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
            "peer_listing_ids": peer_listing_ids or [],
            "universe_id": universe_id,
            "universe_as_of_ms": universe_as_of_ms,
            "industry_code": industry_code,
            "common_currency": common_currency,
            "include_calculations": include_calculations,
            "fact_metric_requests": fact_metric_requests or [],
        },
    )


@mcp.tool()
def save_market_screener_query(
    namespace: str,
    query_id: str,
    criteria: dict[str, Any],
    owner: str | None = None,
) -> dict:
    """Persist typed screener criteria as an owner-scoped revision."""
    return _run(
        "save_screener_query",
        {
            "namespace": namespace,
            "query_id": query_id,
            "criteria": criteria,
            "owner": owner,
        },
    )


@mcp.tool()
def screen_market_universe(
    namespace: str,
    universe_id: str,
    as_of_ms: int,
    start_ms: int,
    end_ms: int,
    acquired_by_ms: int,
    publicly_available_by_ms: int,
    criteria: dict[str, Any] | None = None,
    query_id: str | None = None,
) -> dict:
    """Run a bounded historical market screen with result explanations and a receipt."""
    return _run(
        "screen_market_universe",
        {
            "namespace": namespace,
            "universe_id": universe_id,
            "as_of_ms": as_of_ms,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "acquired_by_ms": acquired_by_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
            "criteria": criteria,
            "query_id": query_id,
        },
    )


@mcp.tool()
def inspect_market_screener_run(namespace: str, run_id: str) -> dict:
    """Inspect a persisted owner-scoped screener run receipt."""
    return _run("inspect_market_screener_run", {"namespace": namespace, "run_id": run_id})


@mcp.tool()
def export_market_screener_run(namespace: str, run_id: str) -> dict:
    """Export only permitted, passing derived screener results and revision locators."""
    return _run("export_market_screener_run", {"namespace": namespace, "run_id": run_id})


@mcp.tool()
def get_economic_market_dashboard(
    namespace: str,
    release_id: str,
    request_key: str,
    series: list[Any],
    release_cutoff_ms: int,
    acquired_cutoff_ms: int,
    initial_release_cutoff_ms: int | None = None,
    consensus: list[dict[str, Any]] | None = None,
    universe_id: str | None = None,
    universe_as_of_ms: int | None = None,
    breadth_start_ms: int | None = None,
    breadth_end_ms: int | None = None,
) -> dict:
    """Build a revision-aware macro dashboard with explicit consensus and breadth gaps."""
    return _run(
        "economic_market_dashboard",
        {
            "namespace": namespace,
            "release_id": release_id,
            "request_key": request_key,
            "series": series,
            "release_cutoff_ms": release_cutoff_ms,
            "acquired_cutoff_ms": acquired_cutoff_ms,
            "initial_release_cutoff_ms": initial_release_cutoff_ms,
            "consensus": consensus,
            "universe_id": universe_id,
            "universe_as_of_ms": universe_as_of_ms,
            "breadth_start_ms": breadth_start_ms,
            "breadth_end_ms": breadth_end_ms,
        },
    )


@mcp.tool()
def save_market_alert_watch(
    namespace: str,
    watch_key: str,
    version: int,
    rules: dict[str, Any],
    notification: dict[str, Any] | None = None,
    owner: str | None = None,
    status: str = "active",
) -> dict:
    """Save an immutable, owner-scoped market watch rule revision."""
    return _run(
        "save_market_alert_watch",
        {
            "namespace": namespace,
            "watch_key": watch_key,
            "version": version,
            "rules": rules,
            "notification": notification,
            "owner": owner,
            "status": status,
        },
    )


@mcp.tool()
def run_market_alerts(
    namespace: str,
    watch_id: str,
    observations: list[dict[str, Any]],
    generation: int,
    as_of_ms: int,
    publicly_available_by_ms: int,
    acquired_by_ms: int,
) -> dict:
    """Evaluate bounded market observations and persist an explainable run receipt."""
    return _run(
        "run_market_alerts",
        {
            "namespace": namespace,
            "watch_id": watch_id,
            "observations": observations,
            "generation": generation,
            "as_of_ms": as_of_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
            "acquired_by_ms": acquired_by_ms,
        },
    )


@mcp.tool()
def inspect_market_alert_watch(namespace: str, watch_id: str) -> dict:
    """Inspect an owner-scoped market watch revision."""
    return _run("inspect_market_alert_watch", {"namespace": namespace, "watch_id": watch_id})


@mcp.tool()
def inspect_market_alert_run(namespace: str, run_id: str) -> dict:
    """Inspect a persisted market alert input snapshot and result."""
    return _run("inspect_market_alert_run", {"namespace": namespace, "run_id": run_id})


@mcp.tool()
def deliver_market_alert(
    namespace: str,
    anomaly_id: str,
    subscriber_id: str | None = None,
    delivery_outcome: str = "delivered",
    cancel_requested: bool = False,
) -> dict:
    """Deliver a rights-checked market alert with dedupe, retry, and quiet-period policy."""
    return _run(
        "deliver_market_alert",
        {
            "namespace": namespace,
            "anomaly_id": anomaly_id,
            "subscriber_id": subscriber_id,
            "delivery_outcome": delivery_outcome,
            "cancel_requested": cancel_requested,
        },
    )


@mcp.tool()
def market_alert_history(namespace: str, limit: int = 100, offset: int = 0) -> dict:
    """List owner-scoped market alert delivery history."""
    return _run(
        "market_alert_history",
        {"namespace": namespace, "limit": limit, "offset": offset},
    )


@mcp.tool()
def run_market_event_study(
    namespace: str,
    events: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    estimation_window: int,
    event_window: list[int],
    benchmark_bars: list[dict[str, Any]] | None = None,
    alpha: float = 0.05,
    placebo_events: list[dict[str, Any]] | None = None,
    benchmark_id: str | None = None,
    owner: str | None = None,
) -> dict:
    """Run a bounded event study with session alignment, abnormal returns, and safeguards."""
    return _run("market_event_study", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_factor_analysis(
    namespace: str,
    observations: list[dict[str, Any]],
    factor_definitions: dict[str, Any] | None = None,
    split_at_ms: int | None = None,
    owner: str | None = None,
) -> dict:
    """Estimate dated factor exposures and report missingness, collinearity, and held-out behavior."""
    return _run("market_factor_analysis", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_backtest(
    namespace: str,
    strategy_id: str,
    strategy_version: str,
    signals: list[dict[str, Any]],
    commission_bps: float = 0.0,
    slippage_bps: float = 0.0,
    borrow_bps: float = 0.0,
    initial_capital: float = 100000.0,
    benchmark_returns: list[dict[str, Any]] | None = None,
    owner: str | None = None,
) -> dict:
    """Replay post-signal executions with explicit costs, delisting gaps, and leakage checks."""
    return _run("market_backtest", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_walk_forward(
    namespace: str,
    samples: list[dict[str, Any]],
    feature_names: list[str],
    train_window: int,
    test_window: int,
    gap: int = 0,
    owner: str | None = None,
) -> dict:
    """Evaluate a temporal model through held-out rolling folds with availability checks."""
    return _run("market_walk_forward", locals() | {"namespace": namespace})


@mcp.tool()
def record_market_portfolio(
    namespace: str,
    portfolio_id: str,
    version: int,
    holdings: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    base_currency: str,
    as_of_ms: int,
    fx_rates: dict[str, Any] | None = None,
    valuations: list[dict[str, Any]] | None = None,
    benchmark_returns: list[dict[str, Any]] | None = None,
    owner: str | None = None,
) -> dict:
    """Persist an owner-scoped portfolio revision and reconcile transactions, cash, and returns."""
    return _run("market_portfolio", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_risk_report(
    namespace: str,
    portfolio: dict[str, Any],
    returns: list[dict[str, Any]],
    scenarios: list[dict[str, Any]] | None = None,
    confidence: float = 0.95,
    owner: str | None = None,
) -> dict:
    """Calculate dated exposures, concentration, drawdown, liquidity, VaR/ES, and scenarios."""
    return _run("market_risk_report", locals() | {"namespace": namespace})


@mcp.tool()
def inspect_market_quantitative_run(namespace: str, run_id: str) -> dict:
    """Inspect a persisted quantitative run under current ownership."""
    return _run("inspect_market_quantitative_run", {"namespace": namespace, "run_id": run_id})


@mcp.tool()
def export_market_quantitative_run(namespace: str, run_id: str) -> dict:
    """Export a reproducible quantitative artifact with its immutable input manifest."""
    return _run("export_market_quantitative_run", {"namespace": namespace, "run_id": run_id})


@mcp.tool()
def save_market_materials(namespace: str, issuer_id: str, artifact_id: str, version: int, materials: list[dict[str, Any]], cutoff_ms: int, owner: str | None = None) -> dict:
    """Persist licensed/public earnings materials, estimates, ownership, and disclosure locators."""
    return _run("save_market_materials", locals() | {"namespace": namespace})


@mcp.tool()
def build_market_company_dossier(namespace: str, issuer_id: str, artifact_id: str, version: int, statements: list[dict[str, Any]], materials: list[dict[str, Any]], comparisons: list[dict[str, Any]], evidence: list[dict[str, Any]], cutoff_ms: int, segment_disclosures: list[dict[str, Any]] | None = None, ownership: list[dict[str, Any]] | None = None, dated_peers: list[dict[str, Any]] | None = None, headline_reconciliation: list[dict[str, Any]] | None = None, input_gaps: list[dict[str, Any]] | None = None, material_change_history: list[dict[str, Any]] | None = None, owner: str | None = None) -> dict:
    """Build a cutoff-aware dossier with sourced segments, ownership, dated peers, and earnings analysis."""
    segment_disclosures = segment_disclosures or []
    ownership = ownership or []
    dated_peers = dated_peers or []
    headline_reconciliation = headline_reconciliation or []
    input_gaps = input_gaps or []
    material_change_history = material_change_history or []
    return _run("build_market_company_dossier", locals() | {"namespace": namespace})


@mcp.tool()
def build_market_industry_model(namespace: str, artifact_id: str, version: int, profiles: list[dict[str, Any]], relationships: list[dict[str, Any]], as_of_ms: int, owner: str | None = None) -> dict:
    """Persist dated industry, competitor, product, supplier, customer, and geography relationships."""
    return _run("build_market_industry_model", locals() | {"namespace": namespace})


@mcp.tool()
def calculate_market_sizing(namespace: str, artifact_id: str, version: int, model_type: str, segments: list[dict[str, Any]], scenarios: dict[str, Any] | None = None, sensitivity: list[dict[str, Any]] | None = None, owner: str | None = None) -> dict:
    """Calculate overlap-checked top-down or bottom-up TAM/SAM/SOM scenarios."""
    return _run("calculate_market_sizing", locals() | {"namespace": namespace})


@mcp.tool()
def record_market_driver_hypotheses(namespace: str, artifact_id: str, version: int, hypotheses: list[dict[str, Any]], as_of_ms: int, owner: str | None = None) -> dict:
    """Record industry/company drivers, transmission hypotheses, alternatives, and evidence stances."""
    return _run("record_market_driver_hypotheses", locals() | {"namespace": namespace})


@mcp.tool()
def save_market_thesis(namespace: str, thesis_id: str, version: int, question: str, thesis: str, horizon: str, alternatives: list[str] | None = None, catalysts: list[str] | None = None, assumptions: list[str] | None = None, evidence: list[dict[str, Any]] | None = None, falsification_conditions: list[dict[str, Any]] | None = None, watch_ids: list[str] | None = None, integration_links: dict[str, Any] | None = None, owner: str | None = None) -> dict:
    """Save an immutable falsifiable market thesis revision and linked watch IDs."""
    return _run("save_market_thesis", locals() | {"namespace": namespace})


@mcp.tool()
def review_market_thesis(namespace: str, thesis_id: str, base_version: int, evidence: list[dict[str, Any]], proposed_revision: str | None = None, owner: str | None = None) -> dict:
    """Evaluate supporting, contradicting, or insufficient thesis evidence without overwriting the conclusion."""
    return _run("review_market_thesis", locals() | {"namespace": namespace})


@mcp.tool()
def generate_market_brief(namespace: str, report_id: str, version: int, title: str, sections: list[dict[str, Any]], cutoff_ms: int, formula_versions: list[str], assumptions: list[str], source_locators: list[dict[str, Any]], charts: list[dict[str, Any]] | None = None, artifact_refs: list[dict[str, Any]] | None = None, owner: str | None = None, compose_artifacts: bool = False) -> dict:
    """Generate a brief; optionally render version-pinned dossiers, industry, sizing, and thesis artifacts."""
    return _run("generate_market_brief", locals() | {"namespace": namespace})


@mcp.tool()
def deliver_market_brief(namespace: str, report_id: str, version: int, subscriber_id: str, delivery_outcome: str = "delivered", retry_delay_ms: int = 60000, cooldown_ms: int = 300000, owner: str | None = None) -> dict:
    """Deliver a market brief with idempotent cooldown and retry history."""
    return _run("deliver_market_brief", locals() | {"namespace": namespace})


@mcp.tool()
def export_market_brief(namespace: str, report_id: str, version: int | None = None, output_format: str = "json", external: bool = False, owner: str | None = None) -> dict:
    """Export an authorized market brief as JSON, Markdown, CSV, or optional DOCX/PDF."""
    return _run("export_market_brief", locals() | {"namespace": namespace})


@mcp.tool()
def export_market_brief_evidence_bundle(namespace: str, report_id: str, version: int | None = None, external: bool = False, owner: str | None = None) -> dict:
    """Export a market brief through the shared Evidence Bundle contract after current rights checks."""
    return _run("export_market_brief_evidence_bundle", locals() | {"namespace": namespace})


@mcp.tool()
def schedule_market_brief(namespace: str, schedule_id: str, report_id: str, cadence: str, next_due_ms: int, request: dict[str, Any], owner: str | None = None) -> dict:
    """Create or replace an owner-scoped daily/weekly/monthly market brief schedule."""
    return _run("schedule_market_brief", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_brief_schedules(namespace: str, due_at_ms: int, limit: int = 100, owner: str | None = None) -> dict:
    """Generate due market briefs with durable next-due and retry-safe receipts."""
    return _run("run_market_brief_schedules", locals() | {"namespace": namespace})


@mcp.tool()
def record_market_operations_measurements(namespace: str, measurements: list[dict[str, Any]], owner: str | None = None) -> dict:
    """Record bounded provider freshness, coverage, latency, job, delivery, throughput, and cost observations."""
    return _run("record_market_operations_measurements", locals() | {"namespace": namespace})


@mcp.tool()
def evaluate_market_slos(namespace: str, targets: dict[str, dict[str, Any]], window_start_ms: int, window_end_ms: int, provider: str | None = None, owner: str | None = None) -> dict:
    """Evaluate market SLO targets against retained observations without treating missing telemetry as healthy."""
    return _run("evaluate_market_slos", locals() | {"namespace": namespace})


@mcp.tool()
def market_provider_health(namespace: str, window_start_ms: int, window_end_ms: int, provider: str | None = None) -> dict:
    """Return a bounded provider health view from normalized operations measurements."""
    return _run("market_provider_health", locals() | {"namespace": namespace})


@mcp.tool()
def save_market_budget(namespace: str, budget_id: str, period_start_ms: int, period_end_ms: int, max_requests: int, max_cost_micros: int, owner: str | None = None) -> dict:
    """Create an immutable market request/cost budget for a bounded operating period."""
    return _run("save_market_budget", locals() | {"namespace": namespace})


@mcp.tool()
def consume_market_budget(namespace: str, budget_id: str, charge_id: str, requests: int, cost_micros: int, at_ms: int) -> dict:
    """Charge a market budget exactly once and fail closed when request or cost ceilings are exceeded."""
    return _run("consume_market_budget", locals() | {"namespace": namespace})


@mcp.tool()
def record_market_recovery_drill(namespace: str, drill_id: str, scenario: str, expected_steps: list[dict[str, Any]], observed_steps: list[dict[str, Any]], status: str, restored: bool, duration_ms: int, failure_injected: bool = True, owner: str | None = None) -> dict:
    """Retain a replayable source-outage, corruption, interrupted-backfill, or restore drill receipt."""
    return _run("record_market_recovery_drill", locals() | {"namespace": namespace})


@mcp.tool()
def create_market_backup(namespace: str, backup_id: str, manifest: dict[str, Any], content: dict[str, Any], owner: str | None = None) -> dict:
    """Persist a bounded, hash-verified operations backup manifest and content snapshot."""
    return _run("create_market_backup", locals() | {"namespace": namespace})


@mcp.tool()
def restore_market_backup(namespace: str, backup_id: str, expected_content_hash: str | None = None) -> dict:
    """Verify and restore a retained operations backup payload by content hash."""
    return _run("restore_market_backup", locals() | {"namespace": namespace})


@mcp.tool()
def prune_market_operations_audit(namespace: str, before_ms: int, dry_run: bool = True) -> dict:
    """Preview or execute cutoff-based retention for high-volume market operations audit rows."""
    return _run("prune_market_operations_audit", locals() | {"namespace": namespace})


@mcp.tool()
def save_market_runbook(namespace: str, runbook_id: str, revision: int, scenario: str, steps: list[dict[str, Any]], owner: str | None = None) -> dict:
    """Save an immutable operator repair/recovery runbook revision."""
    return _run("save_market_runbook", locals() | {"namespace": namespace})


@mcp.tool()
def record_market_repair(namespace: str, repair_id: str, runbook_id: str, status: str, inputs: dict[str, Any], outcome: dict[str, Any], owner: str | None = None) -> dict:
    """Record the outcome of a bounded market repair runbook execution."""
    return _run("record_market_repair", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_fixed_income(
    namespace: str,
    instrument: dict[str, Any],
    cashflows: list[dict[str, Any]] | None = None,
    quote: dict[str, Any] | None = None,
    curve: list[dict[str, Any]] | None = None,
    scenarios: list[dict[str, Any]] | None = None,
    credit_evidence: list[dict[str, Any]] | None = None,
    source_entitlements: list[dict[str, Any]] | None = None,
    owner: str | None = None,
) -> dict:
    """Calculate bounded plain-bond analytics with settlement and credit evidence."""
    return _run("market_fixed_income", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_fx_commodity(
    namespace: str,
    observations: list[dict[str, Any]],
    conversion: dict[str, Any] | None = None,
    trades: list[dict[str, Any]] | None = None,
    roll_schedule: list[dict[str, Any]] | None = None,
    research_sources: list[dict[str, Any]] | None = None,
    source_entitlements: list[dict[str, Any]] | None = None,
    owner: str | None = None,
) -> dict:
    """Normalize FX quotes and separate raw-contract commodity P&L from synthetic rolls."""
    return _run("market_fx_commodity", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_derivatives(
    namespace: str,
    contract: dict[str, Any],
    quote: dict[str, Any] | None = None,
    surface: list[dict[str, Any]] | None = None,
    scenarios: list[dict[str, Any]] | None = None,
    source_entitlements: list[dict[str, Any]] | None = None,
    owner: str | None = None,
) -> dict:
    """Calculate bounded European option Greeks, implied volatility, and scenarios."""
    return _run("market_derivatives", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_digital_asset(
    namespace: str,
    asset: dict[str, Any],
    observations: list[dict[str, Any]],
    supply_events: list[dict[str, Any]] | None = None,
    chain_events: list[dict[str, Any]] | None = None,
    source_entitlements: list[dict[str, Any]] | None = None,
    owner: str | None = None,
) -> dict:
    """Analyze digital-asset identity, venue disagreement, supply, and chain reorg evidence."""
    return _run("market_digital_asset", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_intraday_replay(
    namespace: str,
    events: list[dict[str, Any]],
    recovered_events: list[dict[str, Any]] | None = None,
    entitlement_tier: str = "delayed",
    target_events_per_second: float | None = None,
    source_entitlements: list[dict[str, Any]] | None = None,
    owner: str | None = None,
) -> dict:
    """Replay bounded intraday events with gaps, corrections, latency, and backpressure checks."""
    return _run("market_intraday_replay", locals() | {"namespace": namespace})


@mcp.tool()
def run_market_international_coverage(
    namespace: str,
    markets: list[dict[str, Any]],
    taxonomy_mappings: list[dict[str, Any]] | None = None,
    cross_listings: list[dict[str, Any]] | None = None,
    translations: list[dict[str, Any]] | None = None,
    source_entitlements: list[dict[str, Any]] | None = None,
    owner: str | None = None,
) -> dict:
    """Build an explicit international-market coverage and accounting-standard matrix."""
    return _run("market_international_coverage", locals() | {"namespace": namespace})


@mcp.tool()
def inspect_market_specialized_run(namespace: str, run_id: str) -> dict:
    """Inspect an owner-scoped specialized asset-class calculation receipt."""
    return _run("inspect_market_specialized_run", {"namespace": namespace, "run_id": run_id})


@mcp.tool()
def export_market_specialized_run(namespace: str, run_id: str) -> dict:
    """Export a specialized calculation manifest and rights-aware derived artifact."""
    return _run("export_market_specialized_run", {"namespace": namespace, "run_id": run_id})


@mcp.tool()
def market_acceptance_journey(namespace: str, journey_id: str, companies: list[dict[str, Any]], macro_scenario: dict[str, Any], cutoff_ms: int, owner: str | None = None) -> dict:
    """Run the five-company historical market research acceptance journey and retain replay gaps."""
    return _run("market_acceptance_journey", locals() | {"namespace": namespace})


@mcp.tool()
def review_market_acceptance_journey(namespace: str, journey_id: str, review_version: int, reviewer_id: str, reviewed_at_ms: int, decision: str, criteria: list[dict[str, Any]], notes: str, live_evidence_refs: list[str] | None = None, human_attestation: bool = False, owner: str | None = None) -> dict:
    """Record an immutable, explicitly human-attested usefulness review of a five-company journey."""
    return _run("review_market_acceptance_journey", locals() | {"namespace": namespace})


@mcp.tool()
def inspect_market_research_artifact(namespace: str, artifact_id: str, version: int | None = None) -> dict:
    """Inspect an owner-scoped evidence-linked market research artifact."""
    return _run("inspect_market_research_artifact", {"namespace": namespace, "artifact_id": artifact_id, "version": version})


@mcp.tool()
def inspect_economic_release_snapshot(
    namespace: str, snapshot_id: str, cursor: int = 0, page_size: int = 50
) -> dict:
    """Read a page of an owned, source-authorized macroeconomic snapshot."""
    return _run(
        "economic_snapshot",
        {
            "namespace": namespace,
            "snapshot_id": snapshot_id,
            "cursor": cursor,
            "page_size": page_size,
        },
    )


@mcp.tool()
def calculate_market_price_metrics(
    namespace: str,
    listing_id: str,
    start_ms: int,
    end_ms: int,
    acquired_by_ms: int,
    publicly_available_by_ms: int,
    benchmark_listing_id: str | None = None,
    periods_per_year: int = 252,
    risk_free_rate_annual: str | float | None = None,
) -> dict:
    """Calculate replayable price, total-return, volatility, drawdown, and benchmark metrics."""
    return _run(
        "calculate_price_metrics",
        {
            "namespace": namespace,
            "listing_id": listing_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "acquired_by_ms": acquired_by_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
            "benchmark_listing_id": benchmark_listing_id,
            "periods_per_year": periods_per_year,
            "risk_free_rate_annual": risk_free_rate_annual,
        },
    )


@mcp.tool()
def calculate_market_fact_metrics(
    namespace: str,
    issuer_id: str,
    requests: list[dict[str, Any]],
    acquired_by_ms: int,
    publicly_available_by_ms: int,
) -> dict:
    """Calculate replayable growth, margin, leverage, liquidity, or valuation metrics."""
    return _run(
        "calculate_fact_metrics",
        {
            "namespace": namespace,
            "issuer_id": issuer_id,
            "requests": requests,
            "acquired_by_ms": acquired_by_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
        },
    )


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)
