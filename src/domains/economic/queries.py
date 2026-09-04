"""Cited economic comparisons over datasets, temporal history, and claim links."""

from __future__ import annotations

import json
import time
from typing import Any

from src.domains.economic.model import (
    EconomicModelError,
    assess_comparability,
    ensure_economic_schema,
)
from src.evidence_bundle import EvidenceBundleBuilder
from src.kb.temporal import parse_source_time, query_temporal

ECONOMIC_RESEARCH_CONTRACT = "noesis-economic-research-v1"
QUERY_TYPES = frozenset(
    {"trend", "series_comparison", "vintage_comparison", "claim_evidence"}
)


class EconomicQueryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _millis(value: Any, field: str) -> int | None:
    if value is None:
        return None
    try:
        return parse_source_time(value, field=field)[0]
    except Exception as exc:
        raise EconomicQueryError(
            "bad_time", f"{field} must be ISO-8601 or epoch milliseconds"
        ) from exc


def _table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]
        ).fetchone()
    )


def _series(
    conn: Any, domain: str, series_ids: list[str], indicator_id: str | None, limit: int
) -> list[dict[str, Any]]:
    clauses, params = ["m.domain = ?"], [domain]
    if series_ids:
        clauses.append("m.series_id IN (" + ",".join("?" for _ in series_ids) + ")")
        params.extend(series_ids)
    if indicator_id:
        clauses.append("m.indicator_id = ?")
        params.append(indicator_id)
    rows = conn.execute(
        "SELECT m.series_id, m.indicator_id, m.provider, m.provider_code, m.provider_definition, "
        "COALESCE(m.source_url, s.source_url), i.canonical_name, i.concept, i.definition, i.unit, "
        "i.scaling, i.currency_basis, i.geography, i.frequency, i.price_basis, i.seasonal_adjustment "
        "FROM economic_series_map m JOIN economic_indicators i ON i.domain=m.domain AND i.indicator_id=m.indicator_id "
        "JOIN dataset_series s ON s.series_id=m.series_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY m.series_id LIMIT ?",
        [*params, limit],
    ).fetchall()
    keys = (
        "series_id",
        "indicator_id",
        "provider",
        "provider_code",
        "provider_definition",
        "source_url",
        "canonical_name",
        "concept",
        "definition",
        "unit",
        "scaling",
        "currency_basis",
        "geography",
        "frequency",
        "price_basis",
        "seasonal_adjustment",
    )
    result = [dict(zip(keys, row)) for row in rows]
    if series_ids:
        order = {series_id: index for index, series_id in enumerate(series_ids)}
        result.sort(key=lambda item: order.get(item["series_id"], len(order)))
    return result


def _vintages(
    conn: Any, domain: str, series_id: str, observed: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT as_of, vintage_id, release_at_ms, retrieved_at_ms, revision_of, source_url, source_document_id "
        "FROM economic_vintages WHERE domain=? AND series_id=? AND retrieved_at_ms<=? ORDER BY as_of",
        [domain, series_id, observed],
    ).fetchall()
    keys = (
        "as_of",
        "vintage_id",
        "release_at_ms",
        "retrieved_at_ms",
        "revision_of",
        "source_url",
        "source_document_id",
    )
    return [dict(zip(keys, row)) for row in rows]


def _values(
    conn: Any,
    series: dict[str, Any],
    vintage: dict[str, Any],
    period_from: str | None,
    period_to: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    clauses, params = (
        ["series_id=?", "as_of=?"],
        [series["series_id"], vintage["as_of"]],
    )
    if period_from:
        clauses.append("period>=?")
        params.append(period_from)
    if period_to:
        clauses.append("period<=?")
        params.append(period_to)
    rows = conn.execute(
        "SELECT period, value FROM dataset_observations WHERE "
        + " AND ".join(clauses)
        + " ORDER BY period LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [
        {
            "period": period,
            "value": value,
            "normalized_value": None
            if value is None
            else value * float(series["scaling"]),
            "unit": series["unit"],
        }
        for period, value in rows
    ]


def _citation(series: dict[str, Any], vintage: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "series_id": series["series_id"],
        "provider": series["provider"],
        "provider_code": series["provider_code"],
        "source_url": (vintage.get("source_url") if vintage else None)
        or series["source_url"],
        "source_document_id": vintage.get("source_document_id") if vintage else None,
        "provider_vintage_ms": vintage.get("as_of") if vintage else None,
        "release_at_ms": vintage.get("release_at_ms") if vintage else None,
        "retrieved_at_ms": vintage.get("retrieved_at_ms") if vintage else None,
        "locator_available": bool(
            ((vintage.get("source_url") if vintage else None) or series["source_url"])
            or (vintage and vintage.get("source_document_id"))
        ),
    }


def _trend(
    conn: Any,
    domain: str,
    series_rows: list[dict[str, Any]],
    observed: int,
    period_from: str | None,
    period_to: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results, citations = [], []
    for series in series_rows:
        vintages = _vintages(conn, domain, series["series_id"], observed)
        if not vintages:
            continue
        vintage = vintages[-1]
        values = _values(conn, series, vintage, period_from, period_to, limit)
        citation = _citation(series, vintage)
        results.append(
            {
                "series": series,
                "vintage": vintage,
                "observations": values,
                "citation": citation,
            }
        )
        citations.append(citation)
    return results, citations


def _comparison(
    conn: Any,
    domain: str,
    series_rows: list[dict[str, Any]],
    observed: int,
    period_from: str | None,
    period_to: str | None,
    limit: int,
    comparison_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if len(series_rows) != 2:
        raise EconomicQueryError(
            "bad_request", "series_comparison requires exactly two known series_ids"
        )
    try:
        assessment = assess_comparability(
            conn,
            series_rows[0]["series_id"],
            series_rows[1]["series_id"],
            domain=domain,
            comparison_mode=comparison_mode,
        )
    except EconomicModelError as exc:
        raise EconomicQueryError(exc.code, str(exc)) from exc
    vintages = [
        _vintages(conn, domain, item["series_id"], observed) for item in series_rows
    ]
    if any(not values for values in vintages):
        return (
            [],
            [
                _citation(item, values[-1] if values else None)
                for item, values in zip(series_rows, vintages)
            ],
            assessment,
        )
    selected = [values[-1] for values in vintages]
    citations = [
        _citation(item, vintage) for item, vintage in zip(series_rows, selected)
    ]
    if not assessment["comparable"]:
        return [], citations, assessment
    value_maps = [
        {
            row["period"]: row
            for row in _values(conn, series, vintage, period_from, period_to, limit)
        }
        for series, vintage in zip(series_rows, selected)
    ]
    results = []
    for period in sorted(set(value_maps[0]) & set(value_maps[1]))[:limit]:
        left, right = (
            value_maps[0][period]["normalized_value"],
            value_maps[1][period]["normalized_value"],
        )
        delta = None if left is None or right is None else left - right
        percent_delta = (
            None if delta is None or right == 0 else delta / abs(right) * 100
        )
        results.append(
            {
                "period": period,
                "left": left,
                "right": right,
                "delta": delta,
                "percent_delta": percent_delta,
                "unit": series_rows[0]["unit"],
            }
        )
    return results, citations, assessment


def _vintage_comparison(
    conn: Any,
    domain: str,
    series_rows: list[dict[str, Any]],
    observed: int,
    period_from: str | None,
    period_to: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(series_rows) != 1:
        raise EconomicQueryError(
            "bad_request", "vintage_comparison requires exactly one known series_id"
        )
    series = series_rows[0]
    vintages = _vintages(conn, domain, series["series_id"], observed)
    if not vintages:
        return [], [_citation(series, None)]
    initial, latest = vintages[0], vintages[-1]
    first = {
        row["period"]: row
        for row in _values(conn, series, initial, period_from, period_to, limit)
    }
    last = {
        row["period"]: row
        for row in _values(conn, series, latest, period_from, period_to, limit)
    }
    results = []
    for period in sorted(set(first) | set(last))[:limit]:
        before = first.get(period, {}).get("normalized_value")
        after = last.get(period, {}).get("normalized_value")
        revision = None if before is None or after is None else after - before
        results.append(
            {
                "period": period,
                "initial_value": before,
                "latest_value": after,
                "revision": revision,
                "unit": series["unit"],
            }
        )
    return [
        {
            "series": series,
            "initial_vintage": initial,
            "latest_vintage": latest,
            "observations": results,
        }
    ], [_citation(series, initial), _citation(series, latest)]


def _claim_evidence(
    conn: Any, domain: str, claim_id: str | None, limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not claim_id:
        raise EconomicQueryError("bad_request", "claim_evidence requires claim_id")
    rows = conn.execute(
        "SELECT link_id, target_kind, target_id, relation, match_method, window_from_ms, window_to_ms, unit, geography, confidence, ambiguity_json, causal_basis, source_document_id, created_at_ms "
        "FROM economic_links WHERE domain=? AND claim_id=? ORDER BY confidence DESC, link_id LIMIT ?",
        [domain, claim_id, limit],
    ).fetchall()
    keys = (
        "link_id",
        "target_kind",
        "target_id",
        "relation",
        "match_method",
        "window_from_ms",
        "window_to_ms",
        "unit",
        "geography",
        "confidence",
        "ambiguity",
        "causal_basis",
        "source_document_id",
        "created_at_ms",
    )
    results, citations = [], []
    for row in rows:
        item = dict(zip(keys, row))
        item["ambiguity"] = json.loads(item["ambiguity"] or "{}")
        item["causal_interpretation_allowed"] = item["causal_basis"] != "none" and item[
            "match_method"
        ] not in {"temporal_proximity", "correlation"}
        citation = {
            "source_document_id": item["source_document_id"],
            "locator_available": bool(item["source_document_id"]),
        }
        if item["target_kind"] in {"series", "observation"}:
            series_id = (
                item["target_id"].rsplit(":", 1)[0]
                if item["target_kind"] == "observation"
                else item["target_id"]
            )
            series_rows = _series(conn, domain, [series_id], None, 1)
            if series_rows:
                vintages = _vintages(conn, domain, series_id, 2**63 - 1)
                citation = _citation(series_rows[0], vintages[-1] if vintages else None)
        item["citation"] = citation
        results.append(item)
        citations.append(citation)
    return results, citations


def _bundle(response: dict[str, Any], observed: int) -> dict[str, Any]:
    builder = EvidenceBundleBuilder(
        "receipt",
        response["query"],
        created_at_ms=observed,
        as_of_ms=observed,
    )
    root_id = builder.add_object("receipt", {"record": response}, root=True)
    for index, citation in enumerate(response["citations"]):
        locator = citation.get("source_url") or citation.get("source_document_id")
        if locator:
            builder.add_external_reference(
                f"economic-source:{index}", str(locator), required=True
            )
        else:
            builder.add_omission(
                "economic evidence has no resolvable locator", object_id=root_id
            )
    return builder.build()


def economic_research(
    backing: Any,
    *,
    query_type: str,
    series_ids: list[str] | None = None,
    indicator_id: str | None = None,
    claim_id: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    observed_before: Any = None,
    comparison_mode: str = "same_scope",
    include_bundle: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Run a replayable economic query without implying causation from proximity."""

    if query_type not in QUERY_TYPES:
        raise EconomicQueryError(
            "bad_request", f"query_type must be one of {sorted(QUERY_TYPES)}"
        )
    try:
        page_size = int(limit)
    except (TypeError, ValueError) as exc:
        raise EconomicQueryError("bad_request", "limit must be an integer") from exc
    if not 1 <= page_size <= 1000:
        raise EconomicQueryError("bad_request", "limit must be between 1 and 1000")
    requested_ids = list(
        dict.fromkeys(str(value) for value in (series_ids or []) if str(value))
    )
    ensure_economic_schema(backing.conn)
    requested_observed = _millis(observed_before, "observed_before")
    observed = (
        requested_observed
        if requested_observed is not None
        else int(time.time() * 1000)
    )
    series_rows = (
        _series(
            backing.conn,
            backing.definition.name,
            requested_ids,
            indicator_id,
            max(page_size, len(requested_ids) or 1),
        )
        if query_type != "claim_evidence"
        else []
    )
    if query_type == "trend":
        results, citations = _trend(
            backing.conn,
            backing.definition.name,
            series_rows,
            observed,
            period_from,
            period_to,
            page_size,
        )
        comparison = None
    elif query_type == "series_comparison":
        results, citations, comparison = _comparison(
            backing.conn,
            backing.definition.name,
            series_rows,
            observed,
            period_from,
            period_to,
            page_size,
            comparison_mode,
        )
    elif query_type == "vintage_comparison":
        results, citations = _vintage_comparison(
            backing.conn,
            backing.definition.name,
            series_rows,
            observed,
            period_from,
            period_to,
            page_size,
        )
        comparison = None
    else:
        results, citations = _claim_evidence(
            backing.conn, backing.definition.name, claim_id, page_size
        )
        comparison = None
    temporal = query_temporal(
        backing, assertion_kind="observation", observed_before=observed, limit=100
    )
    reasons = []
    if not results:
        reasons.append(
            "no compatible observations or links matched the requested scope and cutoff"
        )
    if any(not item.get("locator_available") for item in citations):
        reasons.append("some structured evidence lacks a resolvable source locator")
    if comparison and not comparison["comparable"]:
        reasons.append(
            "measurement dimensions are incompatible; no numeric comparison was computed"
        )
    response = {
        "economic_contract": ECONOMIC_RESEARCH_CONTRACT,
        "query": {
            "type": query_type,
            "series_ids": requested_ids,
            "indicator_id": indicator_id,
            "claim_id": claim_id,
            "period_from": period_from,
            "period_to": period_to,
            "observed_before_ms": observed,
            "comparison_mode": comparison_mode,
        },
        "results": results,
        "n": len(results),
        "method": "vintage-pinned structured economic query",
        "assumptions": [
            "declared measurement dimensions are authoritative for comparability",
            "provider release and retrieval timestamps are preserved as supplied",
        ],
        "comparison": comparison,
        "citations": citations,
        "coverage": backing.coverage(),
        "uncertainty": {
            "status": "unsupported"
            if not results
            else "partial"
            if reasons
            else "supported",
            "reasons": reasons,
        },
        "causal_safety": {
            "default": "association_only",
            "statement": "Correlation or temporal proximity alone does not establish causation.",
        },
        "temporal": {
            "contract": temporal["temporal_contract"],
            "basis": temporal["temporal_basis"],
            "matching_observations": len(temporal["items"]),
        },
        "composed_contracts": [
            "dataset-series-v1",
            "noesis-kb-v1",
            "noesis-temporal-v1",
            "noesis-evidence-bundle-v1",
            "noesis-economic-model-v1",
        ],
    }
    if include_bundle:
        response["evidence_bundle"] = _bundle(response, observed)
    return response


__all__ = [
    "ECONOMIC_RESEARCH_CONTRACT",
    "QUERY_TYPES",
    "EconomicQueryError",
    "economic_research",
]
