"""Point-in-time company research dashboards and explainable peer sets."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from src.domains.market.actions import MarketCorporateActionStore
from src.domains.market.financial_facts import MarketFinancialFactStore
from src.domains.market.instruments import MarketInstrumentError, MarketInstrumentStore
from src.domains.market.metrics import MarketMetricStore
from src.domains.market.prices import MarketPriceStore
from src.domains.market.quality import summarize_input_quality

MAX_PEERS = 20
MAX_PRICE_ROWS = 250
MAX_FACT_ROWS = 250
MAX_ACTION_ROWS = 100
MAX_METRIC_REQUESTS = 24


class MarketDashboardError(ValueError):
    """Typed dashboard failure safe to return through REST and MCP."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _text(value: Any, name: str, *, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketDashboardError(
            "invalid_request", f"{name} must be nonempty bounded text"
        )
    return value.strip()


def _millis(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketDashboardError(
            "invalid_request", f"{name} must be a nonnegative epoch millisecond"
        )
    return value


def _error(exc: Exception) -> dict[str, Any]:
    return {
        "code": str(getattr(exc, "code", "market_unavailable")),
        "message": str(getattr(exc, "message", str(exc)))[:300],
    }


def _state(items: Sequence[Any], error: Mapping[str, Any] | None = None) -> str:
    if error:
        return "error"
    return "available" if items else "empty"


def _source_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    refs = value.get("source_refs")
    return [dict(item) for item in refs if isinstance(item, Mapping)] if isinstance(refs, list) else []


def _period_key(fact: Mapping[str, Any]) -> str:
    return json.dumps(fact.get("period"), sort_keys=True, separators=(",", ":"))


def _concept_key(fact: Mapping[str, Any]) -> str:
    return str(fact.get("canonical_concept") or fact.get("concept") or "").casefold()


def _latest_facts(facts: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for fact in facts:
        concept = _concept_key(fact)
        if not concept:
            continue
        key = (concept, _period_key(fact))
        prior = selected.get(key)
        if prior is None or (
            int(fact.get("public_at_ms") or 0),
            int(fact.get("recorded_at_ms") or 0),
            str(fact.get("revision_id") or ""),
        ) > (
            int(prior.get("public_at_ms") or 0),
            int(prior.get("recorded_at_ms") or 0),
            str(prior.get("revision_id") or ""),
        ):
            selected[key] = dict(fact)
    return selected


def _default_fact_metric_requests(facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build only fully identified, same-period metric requests.

    The calculation engine remains the sole implementation of formulas. This
    helper only chooses source-pinned inputs already present in the dashboard.
    """

    selected = _latest_facts(facts)
    by_period: dict[str, dict[str, dict[str, Any]]] = {}
    for (concept, period), fact in selected.items():
        by_period.setdefault(period, {})[concept] = fact

    requests: list[dict[str, Any]] = []
    for period, concepts in sorted(by_period.items()):
        for numerator, denominator, kind, name in (
            ("operating_income", "revenue", "margin", "operating_margin"),
            ("net_income", "revenue", "margin", "net_margin"),
            ("gross_profit", "revenue", "margin", "gross_margin"),
            ("liabilities", "assets", "leverage", "liabilities_to_assets"),
            ("liabilities_current", "assets", "liquidity", "current_liabilities_to_assets"),
        ):
            left = concepts.get(numerator)
            right = concepts.get(denominator)
            if left and right:
                requests.append(
                    {
                        "name": name,
                        "kind": kind,
                        "numerator_fact_revision_id": left["revision_id"],
                        "denominator_fact_revision_id": right["revision_id"],
                    }
                )
    # Growth is useful even when a source uses a non-standard period key. The
    # metric engine performs the final comparable-duration validation.
    by_concept: dict[str, list[dict[str, Any]]] = {}
    for fact in selected.values():
        period = fact.get("period")
        if isinstance(period, Mapping) and period.get("kind") == "duration":
            by_concept.setdefault(_concept_key(fact), []).append(fact)
    for concept, values in sorted(by_concept.items()):
        values.sort(key=lambda item: (str(item.get("period", {}).get("end_date", "")), item["revision_id"]))
        if len(values) < 2:
            continue
        current, prior = values[-1], values[-2]
        requests.append(
            {
                "name": f"{concept}_growth_yoy",
                "kind": "growth",
                "numerator_fact_revision_id": current["revision_id"],
                "denominator_fact_revision_id": prior["revision_id"],
            }
        )
    return requests[:MAX_METRIC_REQUESTS]


def _panel_freshness(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    refs: dict[str, dict[str, Any]] = {}
    revision_ids: list[str] = []
    for record in records:
        revision_id = record.get("revision_id")
        if isinstance(revision_id, str) and revision_id not in revision_ids:
            revision_ids.append(revision_id)
        for ref in _source_refs(record):
            ref_id = str(ref.get("source_ref_id") or "")
            if ref_id:
                refs[ref_id] = ref
    public_times = [int(ref["public_at_ms"]) for ref in refs.values() if isinstance(ref.get("public_at_ms"), int)]
    retrieved_times = [int(ref["retrieved_at_ms"]) for ref in refs.values() if isinstance(ref.get("retrieved_at_ms"), int)]
    return {
        "source_ref_count": len(refs),
        "source_refs": list(refs.values()),
        "latest_public_at_ms": max(public_times) if public_times else None,
        "latest_retrieved_at_ms": max(retrieved_times) if retrieved_times else None,
        "record_revision_ids": revision_ids,
    }


class MarketCompanyDashboardStore:
    """Compose bounded company research views without copying source history."""

    def __init__(self, conn: Any, *, initialize: bool = False, now=None) -> None:
        self.conn = conn
        self.now = now
        self.instruments = MarketInstrumentStore(conn, initialize=initialize, now=now)
        self.prices = MarketPriceStore(conn, initialize=initialize, now=now)
        self.facts = MarketFinancialFactStore(conn, initialize=initialize, now=now)
        self.actions = MarketCorporateActionStore(conn, initialize=initialize, now=now)
        self.metrics = MarketMetricStore(conn, initialize=initialize, now=now)

    @staticmethod
    def _validate_list(value: Any, name: str, *, maximum: int) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise MarketDashboardError("invalid_request", f"{name} must be a list")
        if len(value) > maximum:
            raise MarketDashboardError(
                "bound_exceeded", f"{name} exceeds the bounded maximum of {maximum}"
            )
        result = []
        for item in value:
            result.append(_text(item, name, limit=200))
        return list(dict.fromkeys(result))

    def _identity(
        self,
        namespace: str,
        listing_id: str,
        *,
        acquired_by_ms: int,
        public_cutoff_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        try:
            listing = self.instruments.get_instrument(
                namespace,
                "listing",
                _text(listing_id, "listing_id", limit=200),
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            security = self.instruments.get_instrument(
                namespace,
                "security",
                listing["security_id"],
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            issuer = self.instruments.get_instrument(
                namespace,
                "issuer",
                security["issuer_id"],
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            return {"listing": listing, "security": security, "issuer": issuer}
        except MarketInstrumentError as exc:
            raise MarketDashboardError(exc.code, exc.message, **exc.details) from exc

    def _panel(
        self,
        namespace: str,
        identity: Mapping[str, Any],
        *,
        start_ms: int,
        end_ms: int,
        acquired_by_ms: int,
        public_cutoff_ms: int,
        principal_id: str,
        scopes: set[str],
        include_calculations: bool,
        metric_requests: Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        listing = identity["listing"]
        security = identity["security"]
        issuer = identity["issuer"]
        listing_id = str(listing["listing_id"])
        issuer_id = str(issuer["issuer_id"])
        security_id = str(security["security_id"])
        errors: list[dict[str, Any]] = []

        try:
            price_page = self.prices.get_bars(
                namespace,
                listing_id,
                start_ms=start_ms,
                end_ms=end_ms,
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
                limit=MAX_PRICE_ROWS,
                include_page=True,
            )
        except Exception as exc:  # typed store errors become panel diagnostics
            price_page = {"items": [], "next_offset": None, "scanned": 0}
            errors.append({"panel": "prices", **_error(exc)})

        try:
            fact_page = self.facts.get_facts(
                namespace,
                issuer_id,
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
                limit=MAX_FACT_ROWS,
                include_page=True,
            )
        except Exception as exc:
            fact_page = {"items": [], "next_offset": None, "scanned": 0}
            errors.append({"panel": "statements", **_error(exc)})

        try:
            actions = self.actions.get_actions(
                namespace,
                security_id,
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
                limit=MAX_ACTION_ROWS,
            )
        except Exception as exc:
            actions = []
            errors.append({"panel": "corporate_events", **_error(exc)})

        bars = list(price_page.get("items", []))
        facts = list(fact_page.get("items", []))
        metric_report: dict[str, Any] = {
            "price": {"status": "not_requested", "input_revision_ids": []},
            "facts": {"status": "not_requested", "input_revision_ids": []},
        }
        formula_revision_ids: list[str] = []
        if include_calculations:
            try:
                metric_report["price"] = self.metrics.calculate_price_metrics(
                    namespace,
                    listing_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    acquired_by_ms=acquired_by_ms,
                    publicly_available_by_ms=public_cutoff_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                )
            except Exception as exc:
                metric_report["price"] = {"status": "unavailable", "error": _error(exc)}
                errors.append({"panel": "price_metrics", **_error(exc)})
            requested = list(metric_requests or _default_fact_metric_requests(facts))
            if requested:
                try:
                    metric_report["facts"] = self.metrics.calculate_fact_metrics(
                        namespace,
                        issuer_id,
                        requested[:MAX_METRIC_REQUESTS],
                        acquired_by_ms=acquired_by_ms,
                        publicly_available_by_ms=public_cutoff_ms,
                        principal_id=principal_id,
                        scopes=scopes,
                    )
                    formula_revision_ids.extend(
                        str(item)
                        for item in metric_report["facts"].get("formula_registry_revision_ids", [])
                        if isinstance(item, str)
                    )
                except Exception as exc:
                    metric_report["facts"] = {"status": "unavailable", "error": _error(exc)}
                    errors.append({"panel": "fact_metrics", **_error(exc)})
            else:
                metric_report["facts"] = {
                    "status": "unavailable",
                    "reason_code": "missing_inputs",
                    "missing_inputs": ["compatible filed facts for a supported ratio"],
                }

        records = [dict(issuer), dict(security), dict(listing), *bars, *facts, *actions]
        input_revision_ids = [
            str(item["revision_id"])
            for item in records
            if isinstance(item.get("revision_id"), str)
        ]
        quality_exclusions = list(price_page.get("quality_exclusions", []))
        return {
            "listing_id": listing_id,
            "issuer_id": issuer_id,
            "security_id": security_id,
            "identity": dict(identity),
            "prices": {
                "state": _state(bars, next((item for item in errors if item["panel"] == "prices"), None)),
                "items": bars,
                "page": {
                    "next_cursor": price_page.get("next_offset"),
                    "page_size": MAX_PRICE_ROWS,
                    "scanned": price_page.get("scanned", 0),
                    "quality_exclusion_count": len(quality_exclusions),
                    "quality_exclusions_truncated": bool(
                        price_page.get("quality_exclusions_truncated")
                    ),
                },
            },
            "statements": {
                "state": _state(facts, next((item for item in errors if item["panel"] == "statements"), None)),
                "items": facts,
                "page": {
                    "next_cursor": fact_page.get("next_offset"),
                    "page_size": MAX_FACT_ROWS,
                    "scanned": fact_page.get("scanned", 0),
                },
            },
            "corporate_events": {
                "state": _state(actions, next((item for item in errors if item["panel"] == "corporate_events"), None)),
                "items": actions,
            },
            "metrics": metric_report,
            "freshness": _panel_freshness(records),
            "quality": summarize_input_quality(
                self.conn,
                namespace,
                input_revision_ids,
                excluded_findings=quality_exclusions,
            ),
            "drilldown": {
                "identity_revision_ids": [
                    str(item["revision_id"])
                    for item in (issuer, security, listing)
                    if isinstance(item.get("revision_id"), str)
                ],
                "input_revision_ids": input_revision_ids,
                "formula_registry_revision_ids": formula_revision_ids,
                "formulas": {
                    "price_metrics": metric_report["price"].get("formula_versions", {}),
                    "fact_metrics": metric_report["facts"].get("formula_versions", {}),
                },
            },
            "errors": errors,
        }

    def build_dashboard(
        self,
        namespace: str,
        listing_id: str,
        *,
        start_ms: int,
        end_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int,
        principal_id: str,
        scopes: set[str],
        peer_listing_ids: Sequence[str] = (),
        universe_id: str | None = None,
        universe_as_of_ms: int | None = None,
        industry_code: str | None = None,
        common_currency: str | None = None,
        include_calculations: bool = True,
        fact_metric_requests: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Build a bounded subject/peer dashboard with visible exclusions."""

        namespace = _text(namespace, "namespace", limit=100)
        listing_id = _text(listing_id, "listing_id", limit=200)
        start_ms, end_ms = _millis(start_ms, "start_ms"), _millis(end_ms, "end_ms")
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        public_cutoff_ms = _millis(publicly_available_by_ms, "publicly_available_by_ms")
        if start_ms >= end_ms:
            raise MarketDashboardError("invalid_request", "start_ms must precede end_ms")
        if public_cutoff_ms > acquired_by_ms:
            raise MarketDashboardError(
                "invalid_request", "publicly_available_by_ms cannot exceed acquired_by_ms"
            )
        if universe_id is not None:
            universe_id = _text(universe_id, "universe_id", limit=200)
        selection_date = _millis(
            end_ms if universe_as_of_ms is None else universe_as_of_ms,
            "universe_as_of_ms",
        )
        if selection_date > acquired_by_ms:
            raise MarketDashboardError(
                "invalid_request", "universe_as_of_ms cannot exceed acquired_by_ms"
            )
        if industry_code is not None:
            industry_code = _text(industry_code, "industry_code", limit=100)
        if common_currency is not None:
            common_currency = _text(common_currency, "common_currency", limit=3).upper()
            if len(common_currency) != 3:
                raise MarketDashboardError("invalid_request", "common_currency must be ISO currency text")
        if not isinstance(fact_metric_requests, Sequence) or isinstance(
            fact_metric_requests, (str, bytes)
        ):
            raise MarketDashboardError("invalid_request", "fact_metric_requests must be a list")
        if len(fact_metric_requests) > MAX_METRIC_REQUESTS:
            raise MarketDashboardError("bound_exceeded", "too many fact metric requests")

        subject = self._identity(
            namespace,
            listing_id,
            acquired_by_ms=acquired_by_ms,
            public_cutoff_ms=public_cutoff_ms,
            principal_id=principal_id,
            scopes=scopes,
        )
        subject_listing = subject["listing"]
        target_currency = common_currency or str(subject_listing.get("currency") or "")
        exclusions: list[dict[str, Any]] = []
        candidates: list[tuple[str, str]] = []
        for candidate in self._validate_list(
            peer_listing_ids, "peer_listing_ids", maximum=MAX_PEERS
        ):
            candidates.append((candidate, "explicit"))
        if universe_id is not None:
            try:
                members = self.instruments.list_universe_members(
                    namespace,
                    universe_id,
                    as_of_ms=selection_date,
                    acquired_by_ms=acquired_by_ms,
                    publicly_available_by_ms=public_cutoff_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                )
                for member in members:
                    listings = self.instruments.list_listings_for_security(
                        namespace,
                        str(member["security"]["security_id"]),
                        as_of_ms=selection_date,
                        acquired_by_ms=acquired_by_ms,
                        publicly_available_by_ms=public_cutoff_ms,
                        principal_id=principal_id,
                        scopes=scopes,
                        limit=4,
                    )
                    if not listings:
                        exclusions.append(
                            {
                                "object_id": member["security"]["security_id"],
                                "reason": "no_valid_listing",
                                "selection": "universe",
                            }
                        )
                    candidates.extend(
                        (str(item["listing"]["listing_id"]), "universe")
                        for item in listings
                    )
            except Exception as exc:
                exclusions.append(
                    {
                        "object_id": universe_id,
                        "reason": "universe_unavailable",
                        "selection": "universe",
                        "error": _error(exc),
                    }
                )

        selected: list[tuple[dict[str, Any], str]] = []
        seen: set[str] = set()
        for candidate_id, selection in candidates:
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            if candidate_id == listing_id:
                exclusions.append(
                    {"object_id": candidate_id, "reason": "subject_excluded", "selection": selection}
                )
                continue
            try:
                identity = self._identity(
                    namespace,
                    candidate_id,
                    acquired_by_ms=acquired_by_ms,
                    public_cutoff_ms=public_cutoff_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                )
            except MarketDashboardError as exc:
                exclusions.append(
                    {
                        "object_id": candidate_id,
                        "reason": "identity_unavailable",
                        "selection": selection,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                )
                continue
            peer_listing = identity["listing"]
            peer_security = identity["security"]
            if industry_code is not None:
                observed = peer_security.get("industry_code")
                if observed is None:
                    exclusions.append(
                        {"object_id": candidate_id, "reason": "industry_missing", "selection": selection}
                    )
                    continue
                if str(observed) != industry_code:
                    exclusions.append(
                        {
                            "object_id": candidate_id,
                            "reason": "industry_mismatch",
                            "selection": selection,
                            "expected": industry_code,
                            "observed": observed,
                        }
                    )
                    continue
            if str(peer_listing.get("currency") or "") != target_currency:
                exclusions.append(
                    {
                        "object_id": candidate_id,
                        "reason": "currency_mismatch",
                        "selection": selection,
                        "expected": target_currency,
                        "observed": peer_listing.get("currency"),
                    }
                )
                continue
            selected.append((identity, selection))
            if len(selected) >= MAX_PEERS:
                break

        panels = [
            {
                "role": "subject",
                "selection": {"source": "requested", "listing_id": listing_id},
                **self._panel(
                    namespace,
                    subject,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    acquired_by_ms=acquired_by_ms,
                    public_cutoff_ms=public_cutoff_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                    include_calculations=include_calculations,
                    metric_requests=fact_metric_requests,
                ),
            }
        ]
        for identity, selection in selected:
            panels.append(
                {
                    "role": "peer",
                    "selection": {"source": selection, "listing_id": identity["listing"]["listing_id"]},
                    **self._panel(
                        namespace,
                        identity,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        acquired_by_ms=acquired_by_ms,
                        public_cutoff_ms=public_cutoff_ms,
                        principal_id=principal_id,
                        scopes=scopes,
                        include_calculations=include_calculations,
                        metric_requests=(),
                    ),
                }
            )

        all_refs: dict[str, dict[str, Any]] = {}
        all_revisions: list[str] = []
        for panel in panels:
            freshness = panel["freshness"]
            for ref in freshness["source_refs"]:
                all_refs[str(ref["source_ref_id"])] = ref
            for revision_id in panel["drilldown"]["input_revision_ids"]:
                if revision_id not in all_revisions:
                    all_revisions.append(revision_id)
        return {
            "contract": "noesis-market-company-dashboard-v1",
            "namespace": namespace,
            "subject_listing_id": listing_id,
            "selection": {
                "explicit_peer_listing_ids": self._validate_list(
                    peer_listing_ids, "peer_listing_ids", maximum=MAX_PEERS
                ),
                "universe_id": universe_id,
                "universe_as_of_ms": selection_date if universe_id else None,
                "industry_code": industry_code,
                "common_currency": target_currency,
                "currency_conversion": "not_applied",
                "period_alignment": "source_periods_preserved; formulas enforce comparability",
            },
            "as_of": {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "publicly_available_by_ms": public_cutoff_ms,
                "acquired_by_ms": acquired_by_ms,
            },
            "panels": panels,
            "peer_count": len(selected),
            "exclusions": exclusions,
            "freshness": {
                "source_ref_count": len(all_refs),
                "source_refs": list(all_refs.values()),
                "input_revision_ids": all_revisions,
            },
            "drilldown": {
                "source_revisions": all_revisions,
                "formula_revisions": [
                    revision_id
                    for panel in panels
                    for revision_id in panel["drilldown"]["formula_registry_revision_ids"]
                ],
                "source_cutoffs": {
                    "publicly_available_by_ms": public_cutoff_ms,
                    "acquired_by_ms": acquired_by_ms,
                },
            },
            "limitations": [
                "No FX conversion is performed; peers with a different currency are excluded.",
                "Source-reported periods remain distinct; incompatible metric inputs are unavailable rather than coerced.",
                "A local fixture dashboard does not establish live provider coverage or licensing.",
            ],
        }


__all__ = [
    "MAX_ACTION_ROWS",
    "MAX_FACT_ROWS",
    "MAX_METRIC_REQUESTS",
    "MAX_PEERS",
    "MAX_PRICE_ROWS",
    "MarketCompanyDashboardStore",
    "MarketDashboardError",
]
