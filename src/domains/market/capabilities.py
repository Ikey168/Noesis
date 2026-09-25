"""Shared, bounded market capability surface for REST and MCP adapters."""

from __future__ import annotations

from typing import Any, Mapping

from src.domains.economic.dashboard import EconomicDashboardStore
from src.domains.economic.releases import EconomicReleaseStore
from src.domains.market.dashboard import MarketCompanyDashboardStore
from src.domains.market.alerts import MarketAlertStore
from src.domains.market.financial_facts import MarketFinancialFactStore
from src.domains.market.instruments import MarketInstrumentStore
from src.domains.market.metrics import MarketMetricStore
from src.domains.market.prices import MarketPriceStore
from src.domains.market.screeners import MarketScreenerStore
from src.domains.market.quantitative import MarketQuantitativeStore
from src.domains.market.research import MarketResearchStore
from src.domains.market.operations import MarketOperationsStore
from src.domains.market.specialized import MarketSpecializedStore

MAX_PAGE_SIZE = 1000
_READINESS_TABLES = {
    "instrument_master": "market_instrument_object_revisions",
    "price_history": "market_price_bar_revisions",
    "financial_statements": "market_financial_fact_revisions",
    "corporate_actions": "market_corporate_action_revisions",
    "provider_entitlements": "market_entitlement_revisions",
    "economic_snapshots": "economic_release_snapshots",
    "screeners": "market_screener_query_revisions",
    "alerts": "market_alert_watch_revisions",
    "operations": "market_ops_measurements",
    "specialized": "market_specialized_runs",
}


class MarketCapabilityError(ValueError):
    """An invalid request to the shared market capability adapter."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _as_nonnegative_int(value: Any, name: str, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if type(value) is not int or value < 0:
        raise MarketCapabilityError(
            "invalid_request", f"{name} must be a nonnegative integer"
        )
    return value


def _page_size(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PAGE_SIZE:
        raise MarketCapabilityError(
            "invalid_request", f"page_size must be between 1 and {MAX_PAGE_SIZE}"
        )
    return value


def _required(args: Mapping[str, Any], name: str) -> Any:
    value = args.get(name)
    if value is None or value == "":
        raise MarketCapabilityError("invalid_request", f"{name} is required")
    return value


class MarketCapabilityService:
    """Domain service shared by REST and MCP; adapters supply trusted identity."""

    def __init__(self, conn: Any):
        self.conn = conn

    def invoke(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Run one capability and normalize failures to a stable MCP/REST envelope."""

        try:
            result = self._invoke(
                operation, arguments, principal_id=principal_id, scopes=scopes
            )
            return {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 - one stable boundary for both adapters
            code = getattr(exc, "code", None)
            message = getattr(exc, "message", None)
            if isinstance(code, str):
                if not isinstance(message, str):
                    message = str(exc)[:300] or "market capability request failed"
            else:
                code, message = "market_unavailable", "market capability is unavailable"
            error: dict[str, Any] = {"code": code, "message": message}
            details = getattr(exc, "details", None)
            if isinstance(details, Mapping) and details:
                error["details"] = dict(details)
            return {"ok": False, "error": error}

    def _invoke(
        self,
        operation: str,
        args: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        if operation == "readiness":
            return self._readiness(args, principal_id=principal_id, scopes=scopes)
        if operation == "lookup_instrument":
            return self._lookup_instrument(
                args, principal_id=principal_id, scopes=scopes
            )
        if operation == "price_history":
            return self._price_history(args, principal_id=principal_id, scopes=scopes)
        if operation == "financial_statements":
            return self._financial_statements(
                args, principal_id=principal_id, scopes=scopes
            )
        if operation == "economic_snapshot":
            return self._economic_snapshot(
                args, principal_id=principal_id, scopes=scopes
            )
        if operation == "economic_market_dashboard":
            return self._economic_market_dashboard(
                args, principal_id=principal_id, scopes=scopes
            )
        if operation == "calculate_price_metrics":
            return self._calculate_price_metrics(
                args, principal_id=principal_id, scopes=scopes
            )
        if operation == "calculate_fact_metrics":
            return self._calculate_fact_metrics(
                args, principal_id=principal_id, scopes=scopes
            )
        if operation == "company_dashboard":
            return self._company_dashboard(
                args, principal_id=principal_id, scopes=scopes
            )
        if operation == "save_screener_query":
            return self._save_screener_query(
                args, principal_id=principal_id, scopes=scopes
            )
        if operation == "screen_market_universe":
            return self._screen_market_universe(
                args, principal_id=principal_id, scopes=scopes
            )
        if operation == "inspect_market_screener_run":
            return MarketScreenerStore(self.conn, initialize=True).inspect_run(
                _required(args, "namespace"),
                _required(args, "run_id"),
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "export_market_screener_run":
            return MarketScreenerStore(self.conn, initialize=True).export_run(
                _required(args, "namespace"),
                _required(args, "run_id"),
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "save_market_alert_watch":
            return self._save_market_alert_watch(
                args, principal_id=principal_id, scopes=scopes
            )
        if operation == "run_market_alerts":
            return self._run_market_alerts(args, principal_id=principal_id, scopes=scopes)
        if operation == "inspect_market_alert_watch":
            return MarketAlertStore(self.conn, initialize=True).inspect_watch(
                _required(args, "namespace"),
                _required(args, "watch_id"),
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "inspect_market_alert_run":
            return MarketAlertStore(self.conn, initialize=True).inspect_run(
                _required(args, "namespace"),
                _required(args, "run_id"),
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "deliver_market_alert":
            return self._deliver_market_alert(args, principal_id=principal_id, scopes=scopes)
        if operation == "market_alert_history":
            return MarketAlertStore(self.conn, initialize=True).history(
                _required(args, "namespace"),
                principal_id=principal_id,
                scopes=scopes,
                limit=_as_nonnegative_int(args.get("limit"), "limit", default=100),
                offset=_as_nonnegative_int(args.get("offset"), "offset", default=0),
            )
        if operation == "market_event_study":
            return MarketQuantitativeStore(self.conn, initialize=True).event_study(
                _required(args, "namespace"),
                events=args.get("events", []),
                bars=args.get("bars", []),
                benchmark_bars=args.get("benchmark_bars"),
                estimation_window=args.get("estimation_window"),
                event_window=args.get("event_window"),
                alpha=args.get("alpha", 0.05),
                placebo_events=args.get("placebo_events"),
                benchmark_id=args.get("benchmark_id"),
                owner=args.get("owner") or principal_id,
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation in {
            "market_fixed_income",
            "market_fx_commodity",
            "market_derivatives",
            "market_digital_asset",
            "market_intraday_replay",
            "market_international_coverage",
        }:
            return self._specialized(operation, args, principal_id=principal_id, scopes=scopes)
        if operation == "inspect_market_specialized_run":
            return MarketSpecializedStore(self.conn, initialize=True).inspect_run(
                _required(args, "namespace"),
                _required(args, "run_id"),
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "export_market_specialized_run":
            return MarketSpecializedStore(self.conn, initialize=True).export_run(
                _required(args, "namespace"),
                _required(args, "run_id"),
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "market_factor_analysis":
            return MarketQuantitativeStore(self.conn, initialize=True).factor_analysis(
                _required(args, "namespace"),
                observations=args.get("observations", []),
                factor_definitions=args.get("factor_definitions"),
                split_at_ms=args.get("split_at_ms"),
                owner=args.get("owner") or principal_id,
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "market_backtest":
            return MarketQuantitativeStore(self.conn, initialize=True).backtest(
                _required(args, "namespace"),
                signals=args.get("signals", []),
                strategy_id=_required(args, "strategy_id"),
                strategy_version=_required(args, "strategy_version"),
                commission_bps=args.get("commission_bps", 0.0),
                slippage_bps=args.get("slippage_bps", 0.0),
                borrow_bps=args.get("borrow_bps", 0.0),
                initial_capital=args.get("initial_capital", 100_000.0),
                benchmark_returns=args.get("benchmark_returns"),
                owner=args.get("owner") or principal_id,
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "market_walk_forward":
            return MarketQuantitativeStore(self.conn, initialize=True).walk_forward(
                _required(args, "namespace"),
                samples=args.get("samples", []),
                feature_names=args.get("feature_names", []),
                train_window=args.get("train_window"),
                test_window=args.get("test_window"),
                gap=args.get("gap", 0),
                owner=args.get("owner") or principal_id,
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "market_portfolio":
            return MarketQuantitativeStore(self.conn, initialize=True).record_portfolio(
                _required(args, "namespace"),
                portfolio_id=_required(args, "portfolio_id"),
                version=args.get("version"),
                holdings=args.get("holdings", []),
                transactions=args.get("transactions", []),
                base_currency=_required(args, "base_currency"),
                as_of_ms=args.get("as_of_ms"),
                fx_rates=args.get("fx_rates"),
                valuations=args.get("valuations"),
                benchmark_returns=args.get("benchmark_returns"),
                owner=args.get("owner") or principal_id,
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "market_risk_report":
            return MarketQuantitativeStore(self.conn, initialize=True).risk_report(
                _required(args, "namespace"),
                portfolio=args.get("portfolio", {}),
                returns=args.get("returns", []),
                scenarios=args.get("scenarios"),
                confidence=args.get("confidence", 0.95),
                owner=args.get("owner") or principal_id,
                principal_id=principal_id,
                scopes=scopes,
            )
        if operation == "inspect_market_quantitative_run":
            return MarketQuantitativeStore(self.conn, initialize=True).inspect_run(
                _required(args, "namespace"), _required(args, "run_id"), principal_id=principal_id, scopes=scopes
            )
        if operation == "export_market_quantitative_run":
            return MarketQuantitativeStore(self.conn, initialize=True).export_run(
                _required(args, "namespace"), _required(args, "run_id"), principal_id=principal_id, scopes=scopes
            )
        if operation == "save_market_materials":
            return MarketResearchStore(self.conn, initialize=True).save_materials(
                _required(args, "namespace"), issuer_id=_required(args, "issuer_id"), artifact_id=_required(args, "artifact_id"), version=args.get("version"), materials=args.get("materials", []), cutoff_ms=args.get("cutoff_ms"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "build_market_company_dossier":
            return MarketResearchStore(self.conn, initialize=True).company_dossier(
                _required(args, "namespace"), issuer_id=_required(args, "issuer_id"), artifact_id=_required(args, "artifact_id"), version=args.get("version"), statements=args.get("statements", []), materials=args.get("materials", []), comparisons=args.get("comparisons", []), evidence=args.get("evidence", []), cutoff_ms=args.get("cutoff_ms"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes, segment_disclosures=args.get("segment_disclosures", []), ownership=args.get("ownership", []), dated_peers=args.get("dated_peers", []), headline_reconciliation=args.get("headline_reconciliation", []), input_gaps=args.get("input_gaps", []), material_change_history=args.get("material_change_history", [])
            )
        if operation == "build_market_industry_model":
            return MarketResearchStore(self.conn, initialize=True).industry_model(
                _required(args, "namespace"), artifact_id=_required(args, "artifact_id"), version=args.get("version"), profiles=args.get("profiles", []), relationships=args.get("relationships", []), as_of_ms=args.get("as_of_ms"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "calculate_market_sizing":
            return MarketResearchStore(self.conn, initialize=True).market_sizing(
                _required(args, "namespace"), artifact_id=_required(args, "artifact_id"), version=args.get("version"), model_type=_required(args, "model_type"), segments=args.get("segments", []), scenarios=args.get("scenarios"), sensitivity=args.get("sensitivity"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "record_market_driver_hypotheses":
            return MarketResearchStore(self.conn, initialize=True).driver_hypotheses(
                _required(args, "namespace"), artifact_id=_required(args, "artifact_id"), version=args.get("version"), hypotheses=args.get("hypotheses", []), as_of_ms=args.get("as_of_ms"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "save_market_thesis":
            return MarketResearchStore(self.conn, initialize=True).save_thesis(
                _required(args, "namespace"), thesis_id=_required(args, "thesis_id"), version=args.get("version"), question=_required(args, "question"), thesis=_required(args, "thesis"), alternatives=args.get("alternatives", []), catalysts=args.get("catalysts", []), horizon=_required(args, "horizon"), assumptions=args.get("assumptions", []), evidence=args.get("evidence", []), falsification_conditions=args.get("falsification_conditions", []), watch_ids=args.get("watch_ids"), integration_links=args.get("integration_links"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "review_market_thesis":
            return MarketResearchStore(self.conn, initialize=True).review_thesis(
                _required(args, "namespace"), thesis_id=_required(args, "thesis_id"), base_version=args.get("base_version"), evidence=args.get("evidence", []), proposed_revision=args.get("proposed_revision"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "generate_market_brief":
            return MarketResearchStore(self.conn, initialize=True).generate_brief(
                _required(args, "namespace"), report_id=_required(args, "report_id"), version=args.get("version"), title=_required(args, "title"), sections=args.get("sections", []), cutoff_ms=args.get("cutoff_ms"), formula_versions=args.get("formula_versions", []), assumptions=args.get("assumptions", []), source_locators=args.get("source_locators", []), charts=args.get("charts"), artifact_refs=args.get("artifact_refs"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes, compose_artifacts=args.get("compose_artifacts", False)
            )
        if operation == "deliver_market_brief":
            return MarketResearchStore(self.conn, initialize=True).deliver_brief(
                _required(args, "namespace"), report_id=_required(args, "report_id"), version=args.get("version"), subscriber_id=_required(args, "subscriber_id"), delivery_outcome=args.get("delivery_outcome", "delivered"), retry_delay_ms=args.get("retry_delay_ms", 60_000), cooldown_ms=args.get("cooldown_ms", 300_000), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "export_market_brief":
            return MarketResearchStore(self.conn, initialize=True).export_brief(
                _required(args, "namespace"), report_id=_required(args, "report_id"), version=args.get("version"), output_format=args.get("output_format", "json"), external=bool(args.get("external", False)), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "export_market_brief_evidence_bundle":
            return MarketResearchStore(self.conn, initialize=True).export_brief_evidence_bundle(
                _required(args, "namespace"), report_id=_required(args, "report_id"), version=args.get("version"), external=bool(args.get("external", False)), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "schedule_market_brief":
            return MarketResearchStore(self.conn, initialize=True).schedule_brief(
                _required(args, "namespace"), schedule_id=_required(args, "schedule_id"), report_id=_required(args, "report_id"), cadence=_required(args, "cadence"), next_due_ms=args.get("next_due_ms"), request=args.get("request", {}), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "run_market_brief_schedules":
            return MarketResearchStore(self.conn, initialize=True).run_due_schedules(
                _required(args, "namespace"), due_at_ms=args.get("due_at_ms"), limit=args.get("limit", 100), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "market_acceptance_journey":
            return MarketResearchStore(self.conn, initialize=True).acceptance_journey(
                _required(args, "namespace"), journey_id=_required(args, "journey_id"), companies=args.get("companies", []), macro_scenario=args.get("macro_scenario", {}), cutoff_ms=args.get("cutoff_ms"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "review_market_acceptance_journey":
            return MarketResearchStore(self.conn, initialize=True).review_acceptance_journey(
                _required(args, "namespace"), journey_id=_required(args, "journey_id"), review_version=args.get("review_version"), reviewer_id=_required(args, "reviewer_id"), reviewed_at_ms=args.get("reviewed_at_ms"), decision=_required(args, "decision"), criteria=args.get("criteria", []), notes=_required(args, "notes"), live_evidence_refs=args.get("live_evidence_refs"), human_attestation=args.get("human_attestation", False), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "inspect_market_research_artifact":
            return MarketResearchStore(self.conn, initialize=True).inspect(
                _required(args, "namespace"), _required(args, "artifact_id"), args.get("version"), principal_id=principal_id, scopes=scopes
            )
        if operation == "record_market_operations_measurements":
            return MarketOperationsStore(self.conn, initialize=True).record_measurements(
                _required(args, "namespace"), measurements=args.get("measurements", []), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "evaluate_market_slos":
            return MarketOperationsStore(self.conn, initialize=True).evaluate_slos(
                _required(args, "namespace"), targets=args.get("targets", {}), window_start_ms=args.get("window_start_ms"), window_end_ms=args.get("window_end_ms"), provider=args.get("provider"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "market_provider_health":
            return MarketOperationsStore(self.conn, initialize=True).provider_health(
                _required(args, "namespace"), provider=args.get("provider"), window_start_ms=args.get("window_start_ms"), window_end_ms=args.get("window_end_ms"), principal_id=principal_id, scopes=scopes
            )
        if operation == "save_market_budget":
            return MarketOperationsStore(self.conn, initialize=True).save_budget(
                _required(args, "namespace"), budget_id=_required(args, "budget_id"), period_start_ms=args.get("period_start_ms"), period_end_ms=args.get("period_end_ms"), max_requests=args.get("max_requests"), max_cost_micros=args.get("max_cost_micros"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "consume_market_budget":
            return MarketOperationsStore(self.conn, initialize=True).consume_budget(
                _required(args, "namespace"), budget_id=_required(args, "budget_id"), charge_id=_required(args, "charge_id"), requests=args.get("requests"), cost_micros=args.get("cost_micros"), at_ms=args.get("at_ms"), principal_id=principal_id, scopes=scopes
            )
        if operation == "record_market_recovery_drill":
            return MarketOperationsStore(self.conn, initialize=True).record_recovery_drill(
                _required(args, "namespace"), drill_id=_required(args, "drill_id"), scenario=_required(args, "scenario"), failure_injected=args.get("failure_injected", True), expected_steps=args.get("expected_steps", []), observed_steps=args.get("observed_steps", []), status=_required(args, "status"), restored=args.get("restored", False), duration_ms=args.get("duration_ms"), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "create_market_backup":
            return MarketOperationsStore(self.conn, initialize=True).create_backup(
                _required(args, "namespace"), backup_id=_required(args, "backup_id"), manifest=args.get("manifest", {}), content=args.get("content", {}), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "restore_market_backup":
            return MarketOperationsStore(self.conn, initialize=True).restore_backup(
                _required(args, "namespace"), backup_id=_required(args, "backup_id"), expected_content_hash=args.get("expected_content_hash"), principal_id=principal_id, scopes=scopes
            )
        if operation == "prune_market_operations_audit":
            return MarketOperationsStore(self.conn, initialize=True).prune_audit(
                _required(args, "namespace"), before_ms=args.get("before_ms"), dry_run=args.get("dry_run", True), principal_id=principal_id, scopes=scopes
            )
        if operation == "save_market_runbook":
            return MarketOperationsStore(self.conn, initialize=True).save_runbook(
                _required(args, "namespace"), runbook_id=_required(args, "runbook_id"), revision=args.get("revision"), scenario=_required(args, "scenario"), steps=args.get("steps", []), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        if operation == "record_market_repair":
            return MarketOperationsStore(self.conn, initialize=True).record_repair(
                _required(args, "namespace"), repair_id=_required(args, "repair_id"), runbook_id=_required(args, "runbook_id"), status=_required(args, "status"), inputs=args.get("inputs", {}), outcome=args.get("outcome", {}), owner=args.get("owner"), principal_id=principal_id, scopes=scopes
            )
        raise MarketCapabilityError("invalid_request", "unknown market capability")

    def _readiness(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        namespace = _required(args, "namespace")
        MarketInstrumentStore._authorize(namespace, principal_id, scopes, write=False)
        coverage: dict[str, dict[str, Any]] = {}
        for capability, table in _READINESS_TABLES.items():
            exists = bool(
                self.conn.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name=?",
                    [table],
                ).fetchone()
            )
            if not exists:
                coverage[capability] = {"state": "unavailable"}
                continue
            row = self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE namespace=?", [namespace]
            ).fetchone()
            coverage[capability] = {
                "state": "available" if row and int(row[0]) else "empty"
            }
        return {
            "contract": "noesis-market-readiness-v1",
            "namespace": namespace,
            "source_coverage": coverage,
            "live_provider_adapters": {"state": "not_configured", "providers": []},
            "notes": [
                "Local retained data status does not imply a live feed or a provider license.",
                "Each read and calculation rechecks current namespace, ownership, and source entitlements.",
            ],
        }

    def _lookup_instrument(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        namespace = _required(args, "namespace")
        store = MarketInstrumentStore(self.conn, initialize=False)
        acquired = _as_nonnegative_int(args.get("acquired_by_ms"), "acquired_by_ms")
        public = args.get("publicly_available_by_ms")
        if args.get("symbol") is not None:
            return store.resolve_symbol(
                namespace,
                _required(args, "symbol"),
                as_of_ms=_as_nonnegative_int(args.get("as_of_ms"), "as_of_ms"),
                acquired_by_ms=acquired,
                publicly_available_by_ms=public,
                principal_id=principal_id,
                scopes=scopes,
                mic=args.get("mic"),
            )
        if args.get("object_id") is not None:
            return store.get_instrument(
                namespace,
                _required(args, "object_type"),
                _required(args, "object_id"),
                acquired_by_ms=acquired,
                publicly_available_by_ms=public,
                principal_id=principal_id,
                scopes=scopes,
            )
        if args.get("identifier") is not None:
            return store.resolve_identifier(
                namespace,
                _required(args, "scheme"),
                _required(args, "identifier"),
                object_type=_required(args, "object_type"),
                as_of_ms=_as_nonnegative_int(args.get("as_of_ms"), "as_of_ms"),
                acquired_by_ms=acquired,
                publicly_available_by_ms=public,
                principal_id=principal_id,
                scopes=scopes,
            )
        raise MarketCapabilityError(
            "invalid_request", "provide exactly one of symbol, object_id, or identifier"
        )

    def _price_history(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        namespace = _required(args, "namespace")
        page_size = _page_size(args.get("page_size", 100))
        cursor = _as_nonnegative_int(args.get("cursor"), "cursor", default=0)
        result = MarketPriceStore(self.conn, initialize=False).get_bars(
            namespace,
            _required(args, "listing_id"),
            start_ms=_as_nonnegative_int(args.get("start_ms"), "start_ms"),
            end_ms=_as_nonnegative_int(args.get("end_ms"), "end_ms"),
            acquired_by_ms=_as_nonnegative_int(
                args.get("acquired_by_ms"), "acquired_by_ms"
            ),
            publicly_available_by_ms=args.get("publicly_available_by_ms"),
            principal_id=principal_id,
            scopes=scopes,
            limit=page_size,
            offset=cursor,
            include_page=True,
        )
        next_offset = result["next_offset"]
        scan_bound_reached = next_offset is not None and next_offset >= 20_000
        if scan_bound_reached:
            next_offset = None
        return {
            "bars": result["items"],
            "page": {
                "cursor": cursor,
                "next_cursor": next_offset,
                "page_size": page_size,
                "scanned": result["scanned"],
                "scan_bound_reached": scan_bound_reached,
            },
        }

    def _financial_statements(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        namespace = _required(args, "namespace")
        page_size = _page_size(args.get("page_size", 100))
        cursor = _as_nonnegative_int(args.get("cursor"), "cursor", default=0)
        result = MarketFinancialFactStore(self.conn, initialize=False).get_facts(
            namespace,
            _required(args, "issuer_id"),
            acquired_by_ms=_as_nonnegative_int(
                args.get("acquired_by_ms"), "acquired_by_ms"
            ),
            publicly_available_by_ms=_as_nonnegative_int(
                args.get("publicly_available_by_ms"), "publicly_available_by_ms"
            ),
            principal_id=principal_id,
            scopes=scopes,
            taxonomy=args.get("taxonomy"),
            concept=args.get("concept"),
            filing_form=args.get("filing_form"),
            limit=page_size,
            offset=cursor,
            require_complete=bool(args.get("require_complete", False)),
            include_page=True,
        )
        next_offset = result["next_offset"]
        scan_bound_reached = next_offset is not None and next_offset >= 100_000
        if scan_bound_reached:
            next_offset = None
        return {
            "facts": result["items"],
            "page": {
                "cursor": cursor,
                "next_cursor": next_offset,
                "page_size": page_size,
                "scanned": result["scanned"],
                "scan_bound_reached": scan_bound_reached,
            },
        }

    def _economic_snapshot(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        return EconomicReleaseStore(self.conn, initialize=False).inspect_snapshot(
            _required(args, "namespace"),
            _required(args, "snapshot_id"),
            principal_id=principal_id,
            scopes=scopes,
            offset=_as_nonnegative_int(args.get("cursor"), "cursor", default=0),
            limit=_page_size(args.get("page_size", 50)),
        )

    def _economic_market_dashboard(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        series = args.get("series")
        if not isinstance(series, list):
            raise MarketCapabilityError("invalid_request", "series must be a list")
        consensus = args.get("consensus")
        if consensus is not None and not isinstance(consensus, list):
            raise MarketCapabilityError("invalid_request", "consensus must be a list")
        return EconomicDashboardStore(self.conn, initialize=True).build(
            _required(args, "namespace"),
            release_id=_required(args, "release_id"),
            request_key=_required(args, "request_key"),
            series=series,
            release_cutoff_ms=_as_nonnegative_int(
                args.get("release_cutoff_ms"), "release_cutoff_ms"
            ),
            acquired_cutoff_ms=_as_nonnegative_int(
                args.get("acquired_cutoff_ms"), "acquired_cutoff_ms"
            ),
            initial_release_cutoff_ms=(
                None
                if args.get("initial_release_cutoff_ms") is None
                else _as_nonnegative_int(
                    args.get("initial_release_cutoff_ms"),
                    "initial_release_cutoff_ms",
                )
            ),
            consensus=consensus,
            universe_id=args.get("universe_id"),
            universe_as_of_ms=(
                None
                if args.get("universe_as_of_ms") is None
                else _as_nonnegative_int(
                    args.get("universe_as_of_ms"), "universe_as_of_ms"
                )
            ),
            breadth_start_ms=(
                None
                if args.get("breadth_start_ms") is None
                else _as_nonnegative_int(
                    args.get("breadth_start_ms"), "breadth_start_ms"
                )
            ),
            breadth_end_ms=(
                None
                if args.get("breadth_end_ms") is None
                else _as_nonnegative_int(
                    args.get("breadth_end_ms"), "breadth_end_ms"
                )
            ),
            principal_id=principal_id,
            scopes=scopes,
        )

    def _calculate_price_metrics(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        fields = {
            name: args[name]
            for name in (
                "start_ms",
                "end_ms",
                "acquired_by_ms",
                "publicly_available_by_ms",
            )
            if name in args
        }
        for name in (
            "start_ms",
            "end_ms",
            "acquired_by_ms",
            "publicly_available_by_ms",
        ):
            fields[name] = _as_nonnegative_int(fields.get(name), name)
        return MarketMetricStore(self.conn, initialize=True).calculate_price_metrics(
            _required(args, "namespace"),
            _required(args, "listing_id"),
            **fields,
            principal_id=principal_id,
            scopes=scopes,
            benchmark_listing_id=args.get("benchmark_listing_id"),
            periods_per_year=args.get("periods_per_year", 252),
            risk_free_rate_annual=args.get("risk_free_rate_annual"),
        )

    def _calculate_fact_metrics(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        return MarketMetricStore(self.conn, initialize=True).calculate_fact_metrics(
            _required(args, "namespace"),
            _required(args, "issuer_id"),
            _required(args, "requests"),
            acquired_by_ms=_as_nonnegative_int(
                args.get("acquired_by_ms"), "acquired_by_ms"
            ),
            publicly_available_by_ms=_as_nonnegative_int(
                args.get("publicly_available_by_ms"), "publicly_available_by_ms"
            ),
            principal_id=principal_id,
            scopes=scopes,
        )

    def _company_dashboard(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        requests = args.get("fact_metric_requests", [])
        if not isinstance(requests, list):
            raise MarketCapabilityError(
                "invalid_request", "fact_metric_requests must be a list"
            )
        return MarketCompanyDashboardStore(self.conn, initialize=True).build_dashboard(
            _required(args, "namespace"),
            _required(args, "listing_id"),
            start_ms=_as_nonnegative_int(args.get("start_ms"), "start_ms"),
            end_ms=_as_nonnegative_int(args.get("end_ms"), "end_ms"),
            acquired_by_ms=_as_nonnegative_int(
                args.get("acquired_by_ms"), "acquired_by_ms"
            ),
            publicly_available_by_ms=_as_nonnegative_int(
                args.get("publicly_available_by_ms"), "publicly_available_by_ms"
            ),
            principal_id=principal_id,
            scopes=scopes,
            peer_listing_ids=args.get("peer_listing_ids", []),
            universe_id=args.get("universe_id"),
            universe_as_of_ms=(
                None
                if args.get("universe_as_of_ms") is None
                else _as_nonnegative_int(
                    args.get("universe_as_of_ms"), "universe_as_of_ms"
                )
            ),
            industry_code=args.get("industry_code"),
            common_currency=args.get("common_currency"),
            include_calculations=bool(args.get("include_calculations", True)),
            fact_metric_requests=requests,
        )

    def _save_screener_query(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        criteria = args.get("criteria")
        if not isinstance(criteria, Mapping):
            raise MarketCapabilityError("invalid_request", "criteria must be an object")
        return MarketScreenerStore(self.conn, initialize=True).save_query(
            _required(args, "namespace"),
            _required(args, "query_id"),
            criteria,
            owner=args.get("owner"),
            principal_id=principal_id,
            scopes=scopes,
        )

    def _screen_market_universe(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        criteria = args.get("criteria")
        if criteria is not None and not isinstance(criteria, Mapping):
            raise MarketCapabilityError("invalid_request", "criteria must be an object")
        return MarketScreenerStore(self.conn, initialize=True).run(
            _required(args, "namespace"),
            universe_id=_required(args, "universe_id"),
            as_of_ms=_as_nonnegative_int(args.get("as_of_ms"), "as_of_ms"),
            start_ms=_as_nonnegative_int(args.get("start_ms"), "start_ms"),
            end_ms=_as_nonnegative_int(args.get("end_ms"), "end_ms"),
            acquired_by_ms=_as_nonnegative_int(
                args.get("acquired_by_ms"), "acquired_by_ms"
            ),
            publicly_available_by_ms=_as_nonnegative_int(
                args.get("publicly_available_by_ms"), "publicly_available_by_ms"
            ),
            criteria=criteria,
            query_id=args.get("query_id"),
            principal_id=principal_id,
            scopes=scopes,
        )

    def _save_market_alert_watch(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        rules = args.get("rules")
        if not isinstance(rules, Mapping):
            raise MarketCapabilityError("invalid_request", "rules must be an object")
        notification = args.get("notification")
        if notification is not None and not isinstance(notification, Mapping):
            raise MarketCapabilityError("invalid_request", "notification must be an object")
        return MarketAlertStore(self.conn, initialize=True).save_watch(
            _required(args, "namespace"),
            _required(args, "watch_key"),
            args.get("version"),
            rules,
            notification,
            owner=args.get("owner"),
            principal_id=principal_id,
            scopes=scopes,
            status=args.get("status", "active"),
        )

    def _run_market_alerts(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        observations = args.get("observations")
        if not isinstance(observations, list):
            raise MarketCapabilityError("invalid_request", "observations must be a list")
        return MarketAlertStore(self.conn, initialize=True).run(
            _required(args, "namespace"),
            _required(args, "watch_id"),
            observations,
            generation=args.get("generation"),
            as_of_ms=_as_nonnegative_int(args.get("as_of_ms"), "as_of_ms"),
            publicly_available_by_ms=_as_nonnegative_int(
                args.get("publicly_available_by_ms"), "publicly_available_by_ms"
            ),
            acquired_by_ms=_as_nonnegative_int(
                args.get("acquired_by_ms"), "acquired_by_ms"
            ),
            principal_id=principal_id,
            scopes=scopes,
        )

    def _deliver_market_alert(
        self, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        return MarketAlertStore(self.conn, initialize=True).deliver(
            _required(args, "namespace"),
            _required(args, "anomaly_id"),
            args.get("subscriber_id"),
            delivery_outcome=args.get("delivery_outcome", "delivered"),
            cancel_requested=bool(args.get("cancel_requested", False)),
            principal_id=principal_id,
            scopes=scopes,
        )

    def _specialized(
        self, operation: str, args: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        store = MarketSpecializedStore(self.conn, initialize=True)
        namespace = _required(args, "namespace")
        common = {
            "owner": args.get("owner"),
            "principal_id": principal_id,
            "scopes": scopes,
            "source_entitlements": args.get("source_entitlements"),
        }
        if operation == "market_fixed_income":
            instrument = args.get("instrument")
            if not isinstance(instrument, Mapping):
                raise MarketCapabilityError("invalid_request", "instrument must be an object")
            return store.fixed_income(
                namespace,
                instrument=instrument,
                cashflows=args.get("cashflows"),
                quote=args.get("quote"),
                curve=args.get("curve"),
                scenarios=args.get("scenarios"),
                credit_evidence=args.get("credit_evidence"),
                **common,
            )
        if operation == "market_fx_commodity":
            observations = args.get("observations")
            if not isinstance(observations, list):
                raise MarketCapabilityError("invalid_request", "observations must be a list")
            return store.fx_commodity(
                namespace,
                observations=observations,
                conversion=args.get("conversion"),
                trades=args.get("trades"),
                roll_schedule=args.get("roll_schedule"),
                research_sources=args.get("research_sources"),
                **common,
            )
        if operation == "market_derivatives":
            contract = args.get("contract")
            if not isinstance(contract, Mapping):
                raise MarketCapabilityError("invalid_request", "contract must be an object")
            return store.derivatives(
                namespace,
                contract=contract,
                quote=args.get("quote"),
                surface=args.get("surface"),
                scenarios=args.get("scenarios"),
                **common,
            )
        if operation == "market_digital_asset":
            asset = args.get("asset")
            observations = args.get("observations")
            if not isinstance(asset, Mapping) or not isinstance(observations, list):
                raise MarketCapabilityError("invalid_request", "asset and observations are required")
            return store.digital_asset(
                namespace,
                asset=asset,
                observations=observations,
                supply_events=args.get("supply_events"),
                chain_events=args.get("chain_events"),
                **common,
            )
        if operation == "market_intraday_replay":
            events = args.get("events")
            if not isinstance(events, list):
                raise MarketCapabilityError("invalid_request", "events must be a list")
            return store.intraday_replay(
                namespace,
                events=events,
                recovered_events=args.get("recovered_events"),
                entitlement_tier=args.get("entitlement_tier", "delayed"),
                target_events_per_second=args.get("target_events_per_second"),
                **common,
            )
        return store.international_coverage(
            namespace,
            markets=args.get("markets", []),
            taxonomy_mappings=args.get("taxonomy_mappings"),
            cross_listings=args.get("cross_listings"),
            translations=args.get("translations"),
            **common,
        )


__all__ = ["MAX_PAGE_SIZE", "MarketCapabilityError", "MarketCapabilityService"]
