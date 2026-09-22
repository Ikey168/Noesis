"""Economic semantics layered over ``dataset-series-v1`` and the temporal KB.

Numeric observations remain in the shared dataset tables.  This module stores
only semantic mappings, release/vintage metadata, and provenance-rich links;
each observation is also projected into Noesis' immutable bitemporal ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from calendar import monthrange
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from services.ingest.common.series_model import FREQUENCIES, SeriesRecord
from src.ingestion.connectors.dataset.normalize import (
    normalize_frequency,
    normalize_geography,
    normalize_unit,
)
from src.ingestion.connectors.dataset.store import ObservationStore
from src.kb.temporal import parse_source_time, record_temporal_assertion

ECONOMIC_MODEL = "noesis-economic-model-v1"
OBJECT_TYPES = frozenset(
    {
        "indicator",
        "observation",
        "release",
        "vintage",
        "geography",
        "sector",
        "institution",
        "instrument",
        "company",
        "policy",
    }
)
RELATION_TYPES = frozenset(
    {
        "measures",
        "observation_of",
        "released_by",
        "revision_of",
        "applies_to",
        "issued_by",
        "reported_by",
        "supports",
        "challenges",
        "associated_with",
    }
)
SEASONAL_ADJUSTMENTS = frozenset(
    {"adjusted", "not_adjusted", "not_applicable", "unknown"}
)
PRICE_BASES = frozenset(
    {"current", "constant", "chain_linked", "index", "not_applicable", "unknown"}
)
TARGET_KINDS = frozenset(
    {"series", "observation", "release", "filing", "policy", "organization"}
)
LINK_RELATIONS = frozenset(
    {"supports", "challenges", "contextualizes", "associated_with"}
)
CAUSAL_BASES = frozenset(
    {
        "none",
        "experimental",
        "quasi_experimental",
        "structural_model",
        "source_statement",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS economic_indicators (
    indicator_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    concept TEXT NOT NULL,
    definition TEXT NOT NULL,
    unit TEXT,
    scaling DOUBLE NOT NULL,
    currency_basis TEXT,
    geography TEXT,
    frequency TEXT NOT NULL,
    price_basis TEXT NOT NULL,
    seasonal_adjustment TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    PRIMARY KEY (domain, indicator_id)
);
CREATE TABLE IF NOT EXISTS economic_series_map (
    domain TEXT NOT NULL,
    series_id TEXT NOT NULL,
    indicator_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_code TEXT NOT NULL,
    provider_definition TEXT,
    source_url TEXT,
    PRIMARY KEY (domain, series_id)
);
CREATE TABLE IF NOT EXISTS economic_vintages (
    domain TEXT NOT NULL,
    series_id TEXT NOT NULL,
    as_of BIGINT NOT NULL,
    vintage_id TEXT NOT NULL,
    release_at_ms BIGINT NOT NULL,
    retrieved_at_ms BIGINT NOT NULL,
    revision_of BIGINT,
    source_url TEXT,
    source_document_id TEXT,
    PRIMARY KEY (domain, series_id, as_of)
);
CREATE TABLE IF NOT EXISTS economic_links (
    link_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    match_method TEXT NOT NULL,
    window_from_ms BIGINT,
    window_to_ms BIGINT,
    unit TEXT,
    geography TEXT,
    confidence DOUBLE NOT NULL,
    ambiguity_json TEXT NOT NULL,
    causal_basis TEXT NOT NULL,
    source_document_id TEXT,
    created_at_ms BIGINT NOT NULL
);
"""


class EconomicModelError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}:" + hashlib.sha256(_json(value).encode()).hexdigest()[:24]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:36] or "indicator"


def _millis(value: Any, field: str, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise EconomicModelError("bad_time", f"{field} is required")
        return int(default)
    try:
        return parse_source_time(value, field=field)[0]
    except Exception as exc:
        raise EconomicModelError(
            "bad_time", f"{field} must be ISO-8601 or epoch milliseconds"
        ) from exc


def ensure_economic_schema(conn: Any) -> None:
    conn.execute(_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_economic_indicator_concept ON economic_indicators (domain, concept)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_economic_vintage_lookup ON economic_vintages (domain, series_id, retrieved_at_ms)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_economic_link_claim ON economic_links (domain, claim_id)"
    )


def _period_bounds(period: str, frequency: str) -> tuple[int | None, int | None, str]:
    """Return a half-open UTC interval without inventing irregular periods."""

    try:
        if frequency == "annual" and re.fullmatch(r"\d{4}", period):
            start = datetime(int(period), 1, 1, tzinfo=UTC)
            end = datetime(int(period) + 1, 1, 1, tzinfo=UTC)
            precision = "year"
        elif frequency == "quarterly" and re.fullmatch(r"\d{4}-Q[1-4]", period):
            year, quarter = int(period[:4]), int(period[-1])
            month = (quarter - 1) * 3 + 1
            start = datetime(year, month, 1, tzinfo=UTC)
            end = datetime(
                year + (month == 10), 1 if month == 10 else month + 3, 1, tzinfo=UTC
            )
            precision = "month"
        elif frequency == "monthly" and re.fullmatch(r"\d{4}-\d{2}", period):
            year, month = map(int, period.split("-"))
            start = datetime(year, month, 1, tzinfo=UTC)
            last = monthrange(year, month)[1]
            end = start + timedelta(days=last)
            precision = "month"
        elif frequency in {"daily", "weekly"} and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", period
        ):
            start = datetime.fromisoformat(period).replace(tzinfo=UTC)
            end = start + timedelta(days=7 if frequency == "weekly" else 1)
            precision = "day"
        else:
            return None, None, "unknown"
    except ValueError as exc:
        raise EconomicModelError(
            "bad_period", f"invalid {frequency} period {period!r}"
        ) from exc
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), precision


def _indicator_payload(
    record: SeriesRecord, semantics: Mapping[str, Any]
) -> dict[str, Any]:
    concept = re.sub(
        r"\s+",
        " ",
        str(semantics.get("concept") or semantics.get("canonical_name") or record.title)
        .casefold()
        .strip(),
    )
    canonical_name = str(semantics.get("canonical_name") or concept).strip()
    definition = str(
        semantics.get("definition") or record.metadata.get("definition") or record.title
    ).strip()
    frequency = normalize_frequency(str(semantics.get("frequency") or record.frequency))
    if frequency not in FREQUENCIES:
        raise EconomicModelError(
            "bad_frequency", f"unsupported frequency {frequency!r}"
        )
    unit = normalize_unit(semantics.get("unit") if "unit" in semantics else record.unit)
    geography = normalize_geography(
        semantics.get("geography") if "geography" in semantics else record.geography
    )
    seasonal = str(
        semantics.get("seasonal_adjustment")
        or record.metadata.get("seasonal_adjustment")
        or "unknown"
    )
    price_basis = str(
        semantics.get("price_basis") or record.metadata.get("price_basis") or "unknown"
    )
    currency_raw = semantics.get("currency_basis") or record.metadata.get(
        "currency_basis"
    )
    currency_basis = (
        str(currency_raw).strip().upper() if currency_raw is not None else None
    )
    if seasonal not in SEASONAL_ADJUSTMENTS:
        raise EconomicModelError(
            "bad_dimension", f"unsupported seasonal_adjustment {seasonal!r}"
        )
    if price_basis not in PRICE_BASES:
        raise EconomicModelError(
            "bad_dimension", f"unsupported price_basis {price_basis!r}"
        )
    try:
        scaling = float(semantics.get("scaling", record.metadata.get("scaling", 1.0)))
    except (TypeError, ValueError) as exc:
        raise EconomicModelError("bad_dimension", "scaling must be numeric") from exc
    if scaling <= 0:
        raise EconomicModelError("bad_dimension", "scaling must be positive")
    identity = [
        concept.casefold(),
        unit,
        geography,
        frequency,
        currency_basis,
        price_basis,
        seasonal,
    ]
    indicator_id = str(
        semantics.get("indicator_id")
        or f"indicator:{_slug(concept)}:{hashlib.sha256(_json(identity).encode()).hexdigest()[:12]}"
    )
    return {
        "economic_contract": ECONOMIC_MODEL,
        "indicator_id": indicator_id,
        "canonical_name": canonical_name,
        "concept": concept,
        "definition": definition,
        "unit": unit,
        "scaling": scaling,
        "currency_basis": currency_basis,
        "geography": geography,
        "frequency": frequency,
        "price_basis": price_basis,
        "seasonal_adjustment": seasonal,
        "attributes": dict(semantics.get("attributes") or {}),
    }


def register_series(
    conn: Any,
    record: SeriesRecord,
    *,
    semantics: Mapping[str, Any] | None = None,
    domain: str = "economics",
    backing: str = "corpus-view",
    visibility: str = "public",
) -> dict[str, Any]:
    """Persist a dataset-series vintage and its economic/temporal semantics."""

    ensure_economic_schema(conn)
    semantics = dict(semantics or {})
    indicator = _indicator_payload(record, semantics)
    existing = conn.execute(
        "SELECT concept, unit, scaling, geography, frequency, currency_basis, price_basis, seasonal_adjustment "
        "FROM economic_indicators WHERE domain = ? AND indicator_id = ?",
        [domain, indicator["indicator_id"]],
    ).fetchone()
    dimensions = (
        indicator["concept"],
        indicator["unit"],
        indicator["scaling"],
        indicator["geography"],
        indicator["frequency"],
        indicator["currency_basis"],
        indicator["price_basis"],
        indicator["seasonal_adjustment"],
    )
    if existing is not None and tuple(existing) != dimensions:
        raise EconomicModelError(
            "identity_conflict", "an indicator ID cannot change measurement dimensions"
        )
    conn.execute(
        "INSERT OR REPLACE INTO economic_indicators VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            indicator["indicator_id"],
            domain,
            indicator["canonical_name"],
            indicator["concept"],
            indicator["definition"],
            indicator["unit"],
            indicator["scaling"],
            indicator["currency_basis"],
            indicator["geography"],
            indicator["frequency"],
            indicator["price_basis"],
            indicator["seasonal_adjustment"],
            _json(indicator["attributes"]),
        ],
    )
    provider_code = str(
        semantics.get("provider_code")
        or record.metadata.get("indicator")
        or record.metadata.get("code")
        or record.series_id
    )
    conn.execute(
        "INSERT OR REPLACE INTO economic_series_map VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            domain,
            record.series_id,
            indicator["indicator_id"],
            record.provider,
            provider_code,
            str(
                semantics.get("provider_definition")
                or record.metadata.get("definition")
                or record.title
            ),
            record.source_url,
        ],
    )
    ObservationStore(conn).upsert(record)
    release_at = _millis(
        semantics.get("release_at") or record.metadata.get("release_at"),
        "release_at",
        record.as_of,
    )
    retrieved_at = _millis(
        semantics.get("retrieved_at") or record.metadata.get("retrieved_at"),
        "retrieved_at",
        record.as_of,
    )
    if retrieved_at < release_at:
        raise EconomicModelError(
            "bad_time", "retrieved_at cannot precede the declared release_at"
        )
    prior = conn.execute(
        "SELECT MAX(as_of) FROM economic_vintages WHERE domain = ? AND series_id = ? AND as_of < ?",
        [domain, record.series_id, record.as_of],
    ).fetchone()[0]
    requested_prior = semantics.get("revision_of", record.metadata.get("revision_of"))
    revision_of = int(requested_prior) if requested_prior is not None else prior
    if revision_of is not None:
        if revision_of >= record.as_of:
            raise EconomicModelError(
                "bad_revision", "revision_of must precede the current provider vintage"
            )
        exists = conn.execute(
            "SELECT 1 FROM economic_vintages WHERE domain = ? AND series_id = ? AND as_of = ?",
            [domain, record.series_id, revision_of],
        ).fetchone()
        if exists is None:
            raise EconomicModelError(
                "bad_revision", "revision_of must reference an existing vintage"
            )
    vintage_id = str(
        semantics.get("vintage_id")
        or record.metadata.get("vintage_id")
        or f"{record.series_id}@{record.as_of}"
    )
    source_document_id = semantics.get("source_document_id") or record.metadata.get(
        "source_document_id"
    )
    conn.execute(
        "INSERT OR REPLACE INTO economic_vintages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            domain,
            record.series_id,
            record.as_of,
            vintage_id,
            release_at,
            retrieved_at,
            revision_of,
            record.source_url,
            source_document_id,
        ],
    )
    temporal_ids = []
    for observation in record.observations:
        valid_from, valid_to, precision = _period_bounds(
            observation.period, record.frequency
        )
        payload = {
            "economic_contract": ECONOMIC_MODEL,
            "indicator_id": indicator["indicator_id"],
            "series_id": record.series_id,
            "provider": record.provider,
            "provider_code": provider_code,
            "period": observation.period,
            "value": observation.value,
            "unit": indicator["unit"],
            "scaling": indicator["scaling"],
            "currency_basis": indicator["currency_basis"],
            "geography": indicator["geography"],
            "frequency": indicator["frequency"],
            "price_basis": indicator["price_basis"],
            "seasonal_adjustment": indicator["seasonal_adjustment"],
            "release_at_ms": release_at,
            "provider_vintage_ms": record.as_of,
            "vintage_id": vintage_id,
            "revision_of": revision_of,
            "source_url": record.source_url,
        }
        temporal_ids.append(
            record_temporal_assertion(
                conn,
                domain=domain,
                backing=backing,
                assertion_kind="observation",
                assertion_id=f"{record.series_id}:{observation.period}",
                payload=payload,
                observed_at_ms=retrieved_at,
                ingested_at_ms=retrieved_at,
                valid_from_ms=valid_from,
                valid_to_ms=valid_to,
                valid_time_precision=precision,
                source_reported=valid_from is not None,
                inferred=False,
                source_document_id=str(source_document_id)
                if source_document_id
                else None,
                visibility=visibility,
                temporal_provenance={
                    "model": ECONOMIC_MODEL,
                    "release_at_ms": release_at,
                    "provider_vintage_ms": record.as_of,
                    "revision_of": revision_of,
                },
            )
        )
    return {
        "indicator": indicator,
        "series_id": record.series_id,
        "provider_code": provider_code,
        "vintage": {
            "vintage_id": vintage_id,
            "as_of": record.as_of,
            "release_at_ms": release_at,
            "retrieved_at_ms": retrieved_at,
            "revision_of": revision_of,
        },
        "observations": len(record.observations),
        "temporal_ids": temporal_ids,
    }


def _series_semantics(conn: Any, domain: str, series_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT m.series_id, m.indicator_id, m.provider, m.provider_code, m.provider_definition, m.source_url, "
        "i.canonical_name, i.concept, i.definition, i.unit, i.scaling, i.currency_basis, i.geography, "
        "i.frequency, i.price_basis, i.seasonal_adjustment, i.attributes_json "
        "FROM economic_series_map m JOIN economic_indicators i ON i.domain=m.domain AND i.indicator_id=m.indicator_id "
        "WHERE m.domain=? AND m.series_id=?",
        [domain, series_id],
    ).fetchone()
    if row is None:
        raise EconomicModelError("not_found", f"unknown economic series {series_id!r}")
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
        "attributes",
    )
    result = dict(zip(keys, row))
    result["attributes"] = json.loads(result["attributes"] or "{}")
    return result


def assess_comparability(
    conn: Any,
    left_series_id: str,
    right_series_id: str,
    *,
    domain: str = "economics",
    comparison_mode: str = "same_scope",
) -> dict[str, Any]:
    """Explain whether two series can be compared; never silently convert."""

    if comparison_mode not in {"same_scope", "cross_section"}:
        raise EconomicModelError(
            "bad_request", "comparison_mode must be same_scope or cross_section"
        )
    left, right = (
        _series_semantics(conn, domain, left_series_id),
        _series_semantics(conn, domain, right_series_id),
    )
    blockers, qualifications = [], []
    for field in (
        "concept",
        "unit",
        "frequency",
        "currency_basis",
        "price_basis",
        "seasonal_adjustment",
    ):
        if left[field] != right[field]:
            blockers.append(
                {"dimension": field, "left": left[field], "right": right[field]}
            )
    if left["geography"] != right["geography"]:
        item = {
            "dimension": "geography",
            "left": left["geography"],
            "right": right["geography"],
        }
        (qualifications if comparison_mode == "cross_section" else blockers).append(
            item
        )
    factor = float(right["scaling"]) / float(left["scaling"])
    if factor != 1.0:
        qualifications.append(
            {
                "dimension": "scaling",
                "left": left["scaling"],
                "right": right["scaling"],
                "normalization": "values multiplied by their declared scaling before comparison",
            }
        )
    return {
        "comparable": not blockers,
        "mode": comparison_mode,
        "blockers": blockers,
        "qualifications": qualifications,
        "normalization": {
            "left_multiplier": left["scaling"],
            "right_multiplier": right["scaling"],
        },
        "series": [left, right],
    }


def record_economic_link(
    conn: Any,
    *,
    claim_id: str,
    target_kind: str,
    target_id: str,
    relation: str,
    match_method: str,
    confidence: float,
    time_window: tuple[Any | None, Any | None] = (None, None),
    unit: str | None = None,
    geography: str | None = None,
    ambiguity: Mapping[str, Any] | None = None,
    causal_basis: str = "none",
    source_document_id: str | None = None,
    created_at_ms: int | None = None,
    domain: str = "economics",
) -> str:
    """Link a textual claim to structured evidence with explicit limitations."""

    ensure_economic_schema(conn)
    if target_kind not in TARGET_KINDS or relation not in LINK_RELATIONS:
        raise EconomicModelError(
            "bad_link", "unsupported economic target kind or relation"
        )
    if causal_basis not in CAUSAL_BASES:
        raise EconomicModelError("bad_link", "unsupported causal_basis")
    try:
        score = float(confidence)
    except (TypeError, ValueError) as exc:
        raise EconomicModelError("bad_link", "confidence must be numeric") from exc
    if not 0 <= score <= 1:
        raise EconomicModelError("bad_link", "confidence must be between 0 and 1")
    start = (
        _millis(time_window[0], "time_window.start")
        if time_window[0] is not None
        else None
    )
    end = (
        _millis(time_window[1], "time_window.end")
        if time_window[1] is not None
        else None
    )
    if start is not None and end is not None and start >= end:
        raise EconomicModelError("bad_link", "time window must satisfy start < end")
    if target_kind in {"series", "observation"}:
        series_id = (
            target_id.rsplit(":", 1)[0] if target_kind == "observation" else target_id
        )
        _series_semantics(conn, domain, series_id)
    ambiguity_payload = dict(ambiguity or {})
    link_id = _stable_id(
        "el",
        [domain, claim_id, target_kind, target_id, relation, match_method, start, end],
    )
    conn.execute(
        "INSERT OR REPLACE INTO economic_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            link_id,
            domain,
            claim_id,
            target_kind,
            target_id,
            relation,
            str(match_method),
            start,
            end,
            normalize_unit(unit),
            normalize_geography(geography),
            score,
            _json(ambiguity_payload),
            causal_basis,
            source_document_id,
            int(created_at_ms if created_at_ms is not None else time.time() * 1000),
        ],
    )
    return link_id


def load_fixture(
    conn: Any, payload: Mapping[str, Any], *, domain: str = "economics"
) -> dict[str, int]:
    if payload.get("contract") != "economic-benchmark-v1":
        raise EconomicModelError(
            "bad_fixture", "fixture must declare economic-benchmark-v1"
        )
    for item in payload.get("series", []):
        record = SeriesRecord.from_dict(dict(item["record"]))
        register_series(conn, record, semantics=item.get("semantics"), domain=domain)
    for link in payload.get("links", []):
        record_economic_link(conn, domain=domain, **dict(link))
    return {
        "series_vintages": len(payload.get("series", [])),
        "links": len(payload.get("links", [])),
    }


__all__ = [
    "ECONOMIC_MODEL",
    "OBJECT_TYPES",
    "RELATION_TYPES",
    "EconomicModelError",
    "assess_comparability",
    "ensure_economic_schema",
    "load_fixture",
    "record_economic_link",
    "register_series",
]
