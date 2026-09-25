"""Authenticated REST adapter for shared market capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.api.auth.jwt_auth import require_auth

router = APIRouter(prefix="/api/v1/market", tags=["market"])
_WORKSPACE_DIR = Path(__file__).resolve().parents[3] / "apps" / "market-research"
_WORKSPACE_ASSETS = {
    "app.js": (_WORKSPACE_DIR / "app.js", "text/javascript"),
    "styles.css": (_WORKSPACE_DIR / "styles.css", "text/css"),
}


@router.get("/workspace", include_in_schema=False)
async def market_research_workspace():
    """Serve the same-origin, credential-local market dashboard shell."""

    return FileResponse(
        _WORKSPACE_DIR / "index.html",
        media_type="text/html",
        headers={
            "Cache-Control": "no-cache",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/workspace/assets/{asset_name}", include_in_schema=False)
async def market_research_workspace_asset(asset_name: str):
    """Serve only the two fixed, same-origin workspace assets."""

    asset = _WORKSPACE_ASSETS.get(asset_name)
    if asset is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Workspace asset not found")
    path, media_type = asset
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


class PriceMetricsRequest(BaseModel):
    namespace: str
    listing_id: str
    start_ms: int
    end_ms: int
    acquired_by_ms: int
    publicly_available_by_ms: int
    benchmark_listing_id: str | None = None
    periods_per_year: int = 252
    risk_free_rate_annual: str | float | None = None

    class Config:
        extra = "forbid"


class FactMetricsRequest(BaseModel):
    namespace: str
    issuer_id: str
    requests: list[dict[str, Any]]
    acquired_by_ms: int
    publicly_available_by_ms: int

    class Config:
        extra = "forbid"


class CompanyDashboardRequest(BaseModel):
    namespace: str
    listing_id: str
    start_ms: int
    end_ms: int
    acquired_by_ms: int
    publicly_available_by_ms: int
    peer_listing_ids: list[str] = []
    universe_id: str | None = None
    universe_as_of_ms: int | None = None
    industry_code: str | None = None
    common_currency: str | None = None
    include_calculations: bool = True
    fact_metric_requests: list[dict[str, Any]] = []

    class Config:
        extra = "forbid"


class SaveScreenerQueryRequest(BaseModel):
    namespace: str
    query_id: str
    criteria: dict[str, Any]
    owner: str | None = None

    class Config:
        extra = "forbid"


class MarketScreenerRequest(BaseModel):
    namespace: str
    universe_id: str
    as_of_ms: int
    start_ms: int
    end_ms: int
    acquired_by_ms: int
    publicly_available_by_ms: int
    criteria: dict[str, Any] | None = None
    query_id: str | None = None

    class Config:
        extra = "forbid"


class EconomicMarketDashboardRequest(BaseModel):
    namespace: str
    release_id: str
    request_key: str
    series: list[Any]
    release_cutoff_ms: int
    acquired_cutoff_ms: int
    initial_release_cutoff_ms: int | None = None
    consensus: list[dict[str, Any]] | None = None
    universe_id: str | None = None
    universe_as_of_ms: int | None = None
    breadth_start_ms: int | None = None
    breadth_end_ms: int | None = None

    class Config:
        extra = "forbid"


class MarketAlertWatchRequest(BaseModel):
    namespace: str
    watch_key: str
    version: int
    rules: dict[str, Any]
    notification: dict[str, Any] | None = None
    owner: str | None = None
    status: str = "active"

    class Config:
        extra = "forbid"


class MarketAlertRunRequest(BaseModel):
    namespace: str
    watch_id: str
    observations: list[dict[str, Any]]
    generation: int
    as_of_ms: int
    publicly_available_by_ms: int
    acquired_by_ms: int

    class Config:
        extra = "forbid"


class MarketAlertDeliveryRequest(BaseModel):
    namespace: str
    anomaly_id: str
    subscriber_id: str | None = None
    delivery_outcome: str = "delivered"
    cancel_requested: bool = False

    class Config:
        extra = "forbid"


class MarketEventStudyRequest(BaseModel):
    namespace: str
    events: list[dict[str, Any]]
    bars: list[dict[str, Any]]
    estimation_window: int
    event_window: list[int]
    benchmark_bars: list[dict[str, Any]] | None = None
    alpha: float = 0.05
    placebo_events: list[dict[str, Any]] | None = None
    benchmark_id: str | None = None
    owner: str | None = None

    class Config:
        extra = "forbid"


class MarketFactorRequest(BaseModel):
    namespace: str
    observations: list[dict[str, Any]]
    factor_definitions: dict[str, Any] | None = None
    split_at_ms: int | None = None
    owner: str | None = None

    class Config:
        extra = "forbid"


class MarketBacktestRequest(BaseModel):
    namespace: str
    strategy_id: str
    strategy_version: str
    signals: list[dict[str, Any]]
    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    borrow_bps: float = 0.0
    initial_capital: float = 100000.0
    benchmark_returns: list[dict[str, Any]] | None = None
    owner: str | None = None

    class Config:
        extra = "forbid"


class MarketWalkForwardRequest(BaseModel):
    namespace: str
    samples: list[dict[str, Any]]
    feature_names: list[str]
    train_window: int
    test_window: int
    gap: int = 0
    owner: str | None = None

    class Config:
        extra = "forbid"


class MarketPortfolioRequest(BaseModel):
    namespace: str
    portfolio_id: str
    version: int
    holdings: list[dict[str, Any]]
    transactions: list[dict[str, Any]]
    base_currency: str
    as_of_ms: int
    fx_rates: dict[str, Any] | None = None
    valuations: list[dict[str, Any]] | None = None
    benchmark_returns: list[dict[str, Any]] | None = None
    owner: str | None = None

    class Config:
        extra = "forbid"


class MarketRiskRequest(BaseModel):
    namespace: str
    portfolio: dict[str, Any]
    returns: list[dict[str, Any]]
    scenarios: list[dict[str, Any]] | None = None
    confidence: float = 0.95
    owner: str | None = None

    class Config:
        extra = "forbid"


def _identity(claims: dict[str, Any]) -> tuple[str, set[str]]:
    principal_id = claims.get("sub")
    if not isinstance(principal_id, str) or not principal_id.strip():
        from fastapi import HTTPException

        raise HTTPException(
            status_code=401, detail="Authenticated principal is missing"
        )
    scopes: set[str] = set()
    for key in ("scopes", "scope"):
        raw = claims.get(key)
        if isinstance(raw, str):
            scopes.update(item for item in raw.replace(",", " ").split() if item)
        elif isinstance(raw, (list, tuple, set)):
            scopes.update(item for item in raw if isinstance(item, str) and item)
    roles = claims.get("roles")
    if isinstance(roles, list) and "operator" in roles:
        scopes.add("operator")
    return principal_id, scopes


def _run_sync(operation: str, arguments: dict[str, Any], claims: dict[str, Any]):
    from src.database.local_analytics_connector import locked_connection
    from src.domains.market.capabilities import MarketCapabilityService

    principal_id, scopes = _identity(claims)
    with locked_connection() as conn:
        result = MarketCapabilityService(conn).invoke(
            operation, arguments, principal_id=principal_id, scopes=scopes
        )
    if result["ok"]:
        return JSONResponse(status_code=200, content=result)
    code = result["error"]["code"]
    status_code = {
        "unauthorized": 403,
        "not_found": 404,
        "market_unavailable": 503,
        "data_unavailable": 503,
        "contract_invalid": 422,
        "invalid_request": 422,
        "range_too_large": 422,
        "bound_exceeded": 422,
    }.get(code, 400)
    return JSONResponse(status_code=status_code, content=result)


async def _run(operation: str, arguments: dict[str, Any], claims: dict[str, Any]):
    return await run_in_threadpool(_run_sync, operation, arguments, claims)


@router.get("/readiness")
async def market_readiness(
    namespace: str = Query(..., min_length=1, max_length=100),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run("readiness", {"namespace": namespace}, claims)


@router.get("/instruments/lookup")
async def lookup_instrument(
    namespace: str = Query(..., min_length=1, max_length=100),
    acquired_by_ms: int = Query(..., ge=0),
    symbol: str | None = Query(None, max_length=200),
    object_type: str | None = Query(None, max_length=100),
    object_id: str | None = Query(None, max_length=200),
    identifier: str | None = Query(None, max_length=200),
    scheme: str | None = Query(None, max_length=100),
    as_of_ms: int | None = Query(None, ge=0),
    publicly_available_by_ms: int | None = Query(None, ge=0),
    mic: str | None = Query(None, max_length=4),
    claims: dict[str, Any] = Depends(require_auth),
):
    args = {
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
    }
    return await _run("lookup_instrument", args, claims)


@router.get("/prices/history")
async def price_history(
    namespace: str = Query(..., min_length=1, max_length=100),
    listing_id: str = Query(..., min_length=1, max_length=200),
    start_ms: int = Query(..., ge=0),
    end_ms: int = Query(..., ge=0),
    acquired_by_ms: int = Query(..., ge=0),
    publicly_available_by_ms: int | None = Query(None, ge=0),
    cursor: int = Query(0, ge=0, le=20_000),
    page_size: int = Query(100, ge=1, le=1000),
    claims: dict[str, Any] = Depends(require_auth),
):
    args = locals().copy()
    args.pop("claims")
    return await _run("price_history", args, claims)


@router.get("/financial-statements")
async def financial_statements(
    namespace: str = Query(..., min_length=1, max_length=100),
    issuer_id: str = Query(..., min_length=1, max_length=200),
    acquired_by_ms: int = Query(..., ge=0),
    publicly_available_by_ms: int = Query(..., ge=0),
    taxonomy: str | None = Query(None, max_length=200),
    concept: str | None = Query(None, max_length=200),
    filing_form: str | None = Query(None, max_length=100),
    require_complete: bool = False,
    cursor: int = Query(0, ge=0, le=100_000),
    page_size: int = Query(100, ge=1, le=1000),
    claims: dict[str, Any] = Depends(require_auth),
):
    args = locals().copy()
    args.pop("claims")
    return await _run("financial_statements", args, claims)


@router.get("/economic-snapshots/{snapshot_id}")
async def economic_snapshot(
    snapshot_id: str,
    namespace: str = Query(..., min_length=1, max_length=100),
    cursor: int = Query(0, ge=0),
    page_size: int = Query(50, ge=1, le=50),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run(
        "economic_snapshot",
        {
            "namespace": namespace,
            "snapshot_id": snapshot_id,
            "cursor": cursor,
            "page_size": page_size,
        },
        claims,
    )


@router.post("/calculations/prices")
async def calculate_price_metrics(
    body: PriceMetricsRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("calculate_price_metrics", body.dict(), claims)


@router.post("/calculations/facts")
async def calculate_fact_metrics(
    body: FactMetricsRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("calculate_fact_metrics", body.dict(), claims)


@router.post("/company-dashboard")
async def company_dashboard(
    body: CompanyDashboardRequest, claims: dict[str, Any] = Depends(require_auth)
):
    """Return a bounded, point-in-time company and peer research workspace."""
    return await _run("company_dashboard", body.dict(), claims)


@router.post("/screeners/queries")
async def save_screener_query(
    body: SaveScreenerQueryRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("save_screener_query", body.dict(), claims)


@router.post("/screeners/run")
async def screen_market_universe(
    body: MarketScreenerRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("screen_market_universe", body.dict(), claims)


@router.post("/alerts/watches")
async def save_market_alert_watch(
    body: MarketAlertWatchRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("save_market_alert_watch", body.dict(), claims)


@router.post("/alerts/run")
async def run_market_alerts(
    body: MarketAlertRunRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("run_market_alerts", body.dict(), claims)


@router.get("/alerts/watches/{watch_id}")
async def inspect_market_alert_watch(
    watch_id: str,
    namespace: str = Query(..., min_length=1, max_length=100),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run("inspect_market_alert_watch", {"namespace": namespace, "watch_id": watch_id}, claims)


@router.get("/alerts/runs/{run_id}")
async def inspect_market_alert_run(
    run_id: str,
    namespace: str = Query(..., min_length=1, max_length=100),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run("inspect_market_alert_run", {"namespace": namespace, "run_id": run_id}, claims)


@router.post("/alerts/deliver")
async def deliver_market_alert(
    body: MarketAlertDeliveryRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("deliver_market_alert", body.dict(), claims)


@router.get("/alerts/history")
async def market_alert_history(
    namespace: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run("market_alert_history", {"namespace": namespace, "limit": limit, "offset": offset}, claims)


@router.post("/macro-dashboard")
async def economic_market_dashboard(
    body: EconomicMarketDashboardRequest, claims: dict[str, Any] = Depends(require_auth)
):
    """Return vintage-aware macro release, calendar, consensus, and breadth panels."""
    return await _run("economic_market_dashboard", body.dict(), claims)


@router.post("/quantitative/event-study")
async def market_event_study(
    body: MarketEventStudyRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("market_event_study", body.dict(), claims)


@router.post("/quantitative/factors")
async def market_factor_analysis(
    body: MarketFactorRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("market_factor_analysis", body.dict(), claims)


@router.post("/quantitative/backtest")
async def market_backtest(
    body: MarketBacktestRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("market_backtest", body.dict(), claims)


@router.post("/quantitative/walk-forward")
async def market_walk_forward(
    body: MarketWalkForwardRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("market_walk_forward", body.dict(), claims)


@router.post("/quantitative/portfolios")
async def market_portfolio(
    body: MarketPortfolioRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("market_portfolio", body.dict(), claims)


@router.post("/quantitative/risk")
async def market_risk_report(
    body: MarketRiskRequest, claims: dict[str, Any] = Depends(require_auth)
):
    return await _run("market_risk_report", body.dict(), claims)


@router.get("/quantitative/runs/{run_id}")
async def inspect_market_quantitative_run(
    run_id: str,
    namespace: str = Query(..., min_length=1, max_length=100),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run("inspect_market_quantitative_run", {"namespace": namespace, "run_id": run_id}, claims)


@router.get("/quantitative/runs/{run_id}/export")
async def export_market_quantitative_run(
    run_id: str,
    namespace: str = Query(..., min_length=1, max_length=100),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run("export_market_quantitative_run", {"namespace": namespace, "run_id": run_id}, claims)


@router.post("/research/materials")
async def save_market_materials(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("save_market_materials", body, claims)


@router.post("/research/dossier")
async def build_market_company_dossier(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("build_market_company_dossier", body, claims)


@router.post("/research/industry")
async def build_market_industry_model(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("build_market_industry_model", body, claims)


@router.post("/research/sizing")
async def calculate_market_sizing(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("calculate_market_sizing", body, claims)


@router.post("/research/drivers")
async def record_market_driver_hypotheses(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("record_market_driver_hypotheses", body, claims)


@router.post("/research/thesis")
async def save_market_thesis(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("save_market_thesis", body, claims)


@router.post("/research/thesis/review")
async def review_market_thesis(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("review_market_thesis", body, claims)


@router.post("/research/brief")
async def generate_market_brief(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("generate_market_brief", body, claims)


@router.post("/research/brief/deliver")
async def deliver_market_brief(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("deliver_market_brief", body, claims)


@router.post("/research/brief/export")
async def export_market_brief(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("export_market_brief", body, claims)


@router.post("/research/brief/evidence-bundle")
async def export_market_brief_evidence_bundle(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("export_market_brief_evidence_bundle", body, claims)


@router.post("/research/brief/schedule")
async def schedule_market_brief(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("schedule_market_brief", body, claims)


@router.post("/research/brief/schedule/run")
async def run_market_brief_schedules(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("run_market_brief_schedules", body, claims)


@router.post("/operations/measurements")
async def record_market_operations_measurements(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("record_market_operations_measurements", body, claims)


@router.post("/operations/slos")
async def evaluate_market_slos(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("evaluate_market_slos", body, claims)


@router.get("/operations/provider-health")
async def market_provider_health(
    namespace: str = Query(..., min_length=1, max_length=100),
    window_start_ms: int = Query(..., ge=0),
    window_end_ms: int = Query(..., ge=0),
    provider: str | None = Query(None, min_length=1, max_length=200),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run("market_provider_health", {"namespace": namespace, "window_start_ms": window_start_ms, "window_end_ms": window_end_ms, "provider": provider}, claims)


@router.post("/operations/budget")
async def save_market_budget(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("save_market_budget", body, claims)


@router.post("/operations/budget/consume")
async def consume_market_budget(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("consume_market_budget", body, claims)


@router.post("/operations/recovery")
async def record_market_recovery_drill(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("record_market_recovery_drill", body, claims)


@router.post("/operations/backup")
async def create_market_backup(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("create_market_backup", body, claims)


@router.post("/operations/backup/restore")
async def restore_market_backup(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("restore_market_backup", body, claims)


@router.post("/operations/audit/prune")
async def prune_market_operations_audit(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("prune_market_operations_audit", body, claims)


@router.post("/operations/runbook")
async def save_market_runbook(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("save_market_runbook", body, claims)


@router.post("/operations/repair")
async def record_market_repair(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("record_market_repair", body, claims)


@router.post("/specialized/fixed-income")
async def market_fixed_income(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("market_fixed_income", body, claims)


@router.post("/specialized/fx-commodity")
async def market_fx_commodity(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("market_fx_commodity", body, claims)


@router.post("/specialized/derivatives")
async def market_derivatives(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("market_derivatives", body, claims)


@router.post("/specialized/digital-asset")
async def market_digital_asset(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("market_digital_asset", body, claims)


@router.post("/specialized/intraday/replay")
async def market_intraday_replay(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("market_intraday_replay", body, claims)


@router.post("/specialized/international/coverage")
async def market_international_coverage(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("market_international_coverage", body, claims)


@router.get("/specialized/runs/{run_id}")
async def inspect_market_specialized_run(
    run_id: str,
    namespace: str = Query(..., min_length=1, max_length=100),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run("inspect_market_specialized_run", {"namespace": namespace, "run_id": run_id}, claims)


@router.get("/specialized/runs/{run_id}/export")
async def export_market_specialized_run(
    run_id: str,
    namespace: str = Query(..., min_length=1, max_length=100),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run("export_market_specialized_run", {"namespace": namespace, "run_id": run_id}, claims)


@router.post("/research/acceptance")
async def market_acceptance_journey(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("market_acceptance_journey", body, claims)


@router.post("/research/acceptance-journey/review")
async def review_market_acceptance_journey(body: dict[str, Any], claims: dict[str, Any] = Depends(require_auth)):
    return await _run("review_market_acceptance_journey", body, claims)


@router.get("/research/artifacts/{artifact_id}")
async def inspect_market_research_artifact(
    artifact_id: str,
    namespace: str = Query(..., min_length=1, max_length=100),
    version: int | None = Query(None, ge=1),
    claims: dict[str, Any] = Depends(require_auth),
):
    return await _run("inspect_market_research_artifact", {"namespace": namespace, "artifact_id": artifact_id, "version": version}, claims)


__all__ = ["router"]
