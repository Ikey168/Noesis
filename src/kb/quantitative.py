"""Versioned quantitative semantics, observations, conversions, and comparability."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

METRIC_CONTRACT = "noesis-quantitative-metric-v1"
OBSERVATION_CONTRACT = "noesis-quantitative-observation-v1"
CALCULATION_CONTRACT = "noesis-quantitative-calculation-v1"
COMPARABILITY_CONTRACT = "noesis-quantitative-comparability-v1"
READ_SCOPE = "knowledge:quantitative:read"
WRITE_SCOPE = "knowledge:quantitative:write"
CALCULATE_SCOPE = "knowledge:quantitative:calculate"
FREQUENCIES = {
    "instant",
    "daily",
    "weekly",
    "monthly",
    "quarterly",
    "annual",
    "irregular",
}
ADJUSTMENTS = {"seasonally-adjusted", "not-adjusted", "trend", "unknown"}
BREAK_TYPES = {
    "definition",
    "methodology",
    "geography",
    "rebase",
    "basket",
    "provider-switch",
}

_DDL = """
CREATE TABLE IF NOT EXISTS quantitative_units (
  unit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, symbol TEXT NOT NULL,
  dimension_json TEXT NOT NULL, factor_text TEXT NOT NULL, offset_text TEXT NOT NULL,
  aliases_json TEXT NOT NULL, currency_code TEXT, successor_unit_id TEXT,
  redenomination_factor_text TEXT, semantic_version TEXT NOT NULL,
  content_hash TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,symbol,semantic_version)
);
CREATE TABLE IF NOT EXISTS quantitative_metrics (
  metric_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, created_by TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,idempotency_key)
);
CREATE TABLE IF NOT EXISTS quantitative_metric_revisions (
  revision_id TEXT PRIMARY KEY, metric_id TEXT NOT NULL, namespace TEXT NOT NULL,
  revision BIGINT NOT NULL, predecessor_revision_id TEXT, canonical_name TEXT NOT NULL,
  definition TEXT NOT NULL, unit_id TEXT NOT NULL, dimension_json TEXT NOT NULL,
  frequency TEXT NOT NULL, population_json TEXT NOT NULL, synonyms_json TEXT NOT NULL,
  mappings_json TEXT NOT NULL, formula_json TEXT, generation BIGINT NOT NULL,
  valid_from_ms BIGINT, valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL,
  producer_json TEXT NOT NULL, policy_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  input_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL, UNIQUE(metric_id,revision)
);
CREATE TABLE IF NOT EXISTS quantitative_metric_current (
  metric_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, revision BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS quantitative_observations (
  observation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, metric_id TEXT NOT NULL,
  provider TEXT NOT NULL, provider_series_id TEXT NOT NULL, period TEXT NOT NULL,
  value_text TEXT, missing BOOLEAN NOT NULL, unit_id TEXT NOT NULL, currency_code TEXT,
  valid_from_ms BIGINT, valid_to_ms BIGINT, release_at_ms BIGINT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, vintage_id TEXT NOT NULL, adjustment TEXT NOT NULL,
  preliminary BOOLEAN NOT NULL, revision_of TEXT, provenance_json TEXT NOT NULL,
  generation BIGINT NOT NULL, producer_json TEXT NOT NULL, policy_json TEXT NOT NULL,
  principal_id TEXT NOT NULL, input_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,metric_id,provider,provider_series_id,period,vintage_id)
);
CREATE TABLE IF NOT EXISTS quantitative_series_breaks (
  break_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, metric_id TEXT NOT NULL,
  break_type TEXT NOT NULL, boundary_ms BIGINT NOT NULL, before_json TEXT NOT NULL,
  after_json TEXT NOT NULL, evidence_json TEXT NOT NULL, confidence DOUBLE NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS quantitative_calculations (
  calculation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  request_json TEXT NOT NULL, result_json TEXT NOT NULL, input_ids_json TEXT NOT NULL,
  formula_revision_id TEXT, rounding TEXT NOT NULL, calculation_hash TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS quantitative_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_series (
  series_id TEXT PRIMARY KEY, provider TEXT NOT NULL, title TEXT NOT NULL, unit TEXT,
  frequency TEXT NOT NULL, geography TEXT, license TEXT, as_of BIGINT NOT NULL,
  source_url TEXT, metadata JSON
);
CREATE TABLE IF NOT EXISTS dataset_observations (
  series_id TEXT NOT NULL, period TEXT NOT NULL, as_of BIGINT NOT NULL, value DOUBLE,
  PRIMARY KEY(series_id,period,as_of)
);
CREATE INDEX IF NOT EXISTS idx_quantitative_metric_name
  ON quantitative_metric_revisions(namespace,canonical_name);
CREATE INDEX IF NOT EXISTS idx_quantitative_observation_vintage
  ON quantitative_observations(namespace,metric_id,period,retrieved_at_ms);
"""

_BUILTIN_UNITS = {
    "count": ({"count": 1}, "1", "0", ["number"]),
    "ratio": ({}, "1", "0", ["fraction"]),
    "percent": ({}, "0.01", "0", ["%"]),
    "m": ({"length": 1}, "1", "0", ["meter", "metre"]),
    "km": ({"length": 1}, "1000", "0", ["kilometer", "kilometre"]),
    "s": ({"time": 1}, "1", "0", ["second"]),
    "h": ({"time": 1}, "3600", "0", ["hour"]),
    "kg": ({"mass": 1}, "1", "0", ["kilogram"]),
    "g": ({"mass": 1}, "0.001", "0", ["gram"]),
    "K": ({"temperature": 1}, "1", "0", ["kelvin"]),
    "C": ({"temperature": 1}, "1", "273.15", ["celsius"]),
    "USD": ({"currency": 1}, "1", "0", ["usd", "US dollar"]),
    "EUR": ({"currency": 1}, "1", "0", ["eur", "euro"]),
}


class QuantitativeError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise QuantitativeError("unauthorized", f"missing required scope {required}")


def _decimal(value: Any, field: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise QuantitativeError(
            "invalid_number", f"{field} must be decimal-compatible"
        ) from exc
    if not result.is_finite():
        raise QuantitativeError("invalid_number", f"{field} must be finite")
    return result


def _dimension(value: Mapping[str, Any]) -> dict[str, int]:
    result = {str(key): int(power) for key, power in value.items() if int(power)}
    if any(abs(power) > 8 for power in result.values()):
        raise QuantitativeError(
            "invalid_dimension", "dimension powers must be between -8 and 8"
        )
    return dict(sorted(result.items()))


def _combine_dimensions(
    left: Mapping[str, int], right: Mapping[str, int], sign: int
) -> dict[str, int]:
    result = dict(left)
    for key, value in right.items():
        result[key] = result.get(key, 0) + sign * value
        if not result[key]:
            result.pop(key)
    return dict(sorted(result.items()))


class QuantitativeStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
            for symbol, (dimension, factor, offset, aliases) in _BUILTIN_UNITS.items():
                self._register_unit(
                    "global",
                    symbol,
                    dimension,
                    factor=factor,
                    offset=offset,
                    aliases=aliases,
                    semantic_version="1.0.0",
                    principal_id="system",
                    currency_code=symbol if dimension == {"currency": 1} else None,
                )

    def _audit(
        self,
        namespace: str,
        operation: str,
        object_id: str,
        principal_id: str,
        detail: Mapping[str, Any],
        now: int,
    ) -> None:
        audit_id = (
            "quantitative-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO quantitative_audit VALUES (?,?,?,?,?,?,?)",
            [
                audit_id,
                namespace,
                operation,
                object_id,
                principal_id,
                _canonical(detail),
                now,
            ],
        )

    def register_unit(
        self,
        namespace: str,
        symbol: str,
        dimension: Mapping[str, Any],
        *,
        scopes: set[str],
        principal_id: str,
        factor: Any = "1",
        offset: Any = "0",
        aliases: Sequence[str] = (),
        currency_code: str | None = None,
        successor_unit_id: str | None = None,
        redenomination_factor: Any | None = None,
        semantic_version: str = "1.0.0",
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        self.conn.execute("BEGIN")
        try:
            result = self._register_unit(
                namespace,
                symbol,
                dimension,
                factor=factor,
                offset=offset,
                aliases=aliases,
                currency_code=currency_code,
                successor_unit_id=successor_unit_id,
                redenomination_factor=redenomination_factor,
                semantic_version=semantic_version,
                principal_id=principal_id,
            )
            if not result["idempotent"]:
                self._audit(
                    namespace,
                    "register-unit",
                    result["unit_id"],
                    principal_id,
                    {"semantic_version": semantic_version},
                    result["created_at_ms"],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def _register_unit(
        self,
        namespace: str,
        symbol: str,
        dimension: Mapping[str, Any],
        *,
        factor: Any,
        offset: Any,
        aliases: Sequence[str],
        semantic_version: str,
        principal_id: str,
        currency_code: str | None = None,
        successor_unit_id: str | None = None,
        redenomination_factor: Any | None = None,
    ) -> dict[str, Any]:
        dimension_value = _dimension(dimension)
        factor_value, offset_value = (
            str(_decimal(factor, "factor")),
            str(_decimal(offset, "offset")),
        )
        redenomination = (
            str(_decimal(redenomination_factor, "redenomination_factor"))
            if redenomination_factor is not None
            else None
        )
        stable = {
            "namespace": namespace,
            "symbol": symbol,
            "dimension": dimension_value,
            "factor": factor_value,
            "offset": offset_value,
            "aliases": sorted({str(item) for item in aliases}),
            "currency_code": currency_code,
            "successor_unit_id": successor_unit_id,
            "redenomination_factor": redenomination,
            "semantic_version": semantic_version,
        }
        content_hash = _digest(stable)
        unit_id = (
            "quantitative-unit:" + _digest([namespace, symbol, semantic_version])[:24]
        )
        existing = self.conn.execute(
            "SELECT content_hash,created_at_ms FROM quantitative_units WHERE unit_id=?",
            [unit_id],
        ).fetchone()
        if existing and existing[0] != content_hash:
            raise QuantitativeError(
                "immutable_version", "unit version already has different content"
            )
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO quantitative_units VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                unit_id,
                namespace,
                symbol,
                _canonical(dimension_value),
                factor_value,
                offset_value,
                _canonical(stable["aliases"]),
                currency_code,
                successor_unit_id,
                redenomination,
                semantic_version,
                content_hash,
                principal_id,
                now,
            ],
        )
        return {
            "unit_id": unit_id,
            **stable,
            "content_hash": content_hash,
            "created_at_ms": int(existing[1]) if existing else now,
            "idempotent": bool(existing),
        }

    def _unit(self, unit: str, namespace: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT unit_id,symbol,dimension_json,factor_text,offset_text,aliases_json,currency_code,"
            "successor_unit_id,redenomination_factor_text,semantic_version,content_hash "
            "FROM quantitative_units WHERE (namespace=? OR namespace='global') "
            "AND (unit_id=? OR symbol=? OR list_contains(from_json(aliases_json,'[\"VARCHAR\"]'),?)) "
            "ORDER BY CASE WHEN namespace=? THEN 0 ELSE 1 END,semantic_version DESC LIMIT 1",
            [namespace, unit, unit, unit, namespace],
        ).fetchone()
        if not row:
            raise QuantitativeError("unknown_unit", f"unit {unit!r} is not registered")
        return {
            "unit_id": row[0],
            "symbol": row[1],
            "dimension": _load(row[2], {}),
            "factor": row[3],
            "offset": row[4],
            "aliases": _load(row[5], []),
            "currency_code": row[6],
            "successor_unit_id": row[7],
            "redenomination_factor": row[8],
            "semantic_version": row[9],
            "content_hash": row[10],
        }

    def register_metric(
        self,
        namespace: str,
        canonical_name: str,
        definition: str,
        unit: str,
        *,
        principal_id: str,
        scopes: set[str],
        frequency: str = "irregular",
        population: Mapping[str, Any] | None = None,
        synonyms: Sequence[str] = (),
        mappings: Mapping[str, str] | None = None,
        formula: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if (
            frequency not in FREQUENCIES
            or not canonical_name.strip()
            or not definition.strip()
        ):
            raise QuantitativeError(
                "invalid_metric",
                "name, definition, and supported frequency are required",
            )
        unit_value = self._unit(unit, namespace)
        key = idempotency_key or _digest([canonical_name.casefold(), mappings or {}])
        existing = self.conn.execute(
            "SELECT metric_id FROM quantitative_metrics WHERE namespace=? AND idempotency_key=?",
            [namespace, key],
        ).fetchone()
        if existing:
            current = self.metric(namespace, existing[0], scopes={READ_SCOPE})
            candidate = {
                "canonical_name": canonical_name.strip(),
                "definition": definition.strip(),
                "unit_id": unit_value["unit_id"],
                "dimension": unit_value["dimension"],
                "frequency": frequency,
                "population": dict(population or {}),
                "synonyms": sorted({str(item) for item in synonyms}),
                "mappings": dict(sorted((mappings or {}).items())),
                "formula": dict(formula) if formula else None,
            }
            if all(current[key] == value for key, value in candidate.items()):
                return {**current, "idempotent": True}
            raise QuantitativeError(
                "metric_conflict", "metric key was reused with different semantics"
            )
        metric_id = "quantitative-metric:" + _digest([namespace, key])[:24]
        now = self.now()
        context = {
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-quantitative", "version": "1.0.0"}
            ),
            "policy": dict(policy or {"comparability": "strict-v1"}),
        }
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO quantitative_metrics VALUES (?,?,?,?,?)",
                [metric_id, namespace, principal_id, key, now],
            )
            revision_id = self._write_metric(
                metric_id,
                namespace,
                1,
                None,
                canonical_name.strip(),
                definition.strip(),
                unit_value,
                frequency,
                dict(population or {}),
                synonyms,
                mappings or {},
                formula,
                context=context,
                principal_id=principal_id,
                now=now,
            )
            self._audit(
                namespace, "register-metric", revision_id, principal_id, {}, now
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.metric(namespace, metric_id, scopes={READ_SCOPE})

    def _write_metric(
        self,
        metric_id: str,
        namespace: str,
        revision: int,
        predecessor: str | None,
        name: str,
        definition: str,
        unit: Mapping[str, Any],
        frequency: str,
        population: Mapping[str, Any],
        synonyms: Sequence[str],
        mappings: Mapping[str, str],
        formula: Mapping[str, Any] | None,
        *,
        context: Mapping[str, Any],
        principal_id: str,
        now: int,
    ) -> str:
        stable = {
            "metric_id": metric_id,
            "revision": revision,
            "canonical_name": name,
            "definition": definition,
            "unit_id": unit["unit_id"],
            "dimension": unit["dimension"],
            "frequency": frequency,
            "population": population,
            "synonyms": sorted({str(item) for item in synonyms}),
            "mappings": dict(sorted(mappings.items())),
            "formula": dict(formula) if formula else None,
            **context,
        }
        input_hash = _digest(stable)
        revision_id = "quantitative-metric-revision:" + input_hash[:24]
        self.conn.execute(
            "INSERT INTO quantitative_metric_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                revision_id,
                metric_id,
                namespace,
                revision,
                predecessor,
                name,
                definition,
                unit["unit_id"],
                _canonical(unit["dimension"]),
                frequency,
                _canonical(population),
                _canonical(stable["synonyms"]),
                _canonical(stable["mappings"]),
                _canonical(stable["formula"]) if stable["formula"] else None,
                context["generation"],
                context["valid_from_ms"],
                context["valid_to_ms"],
                context["observed_at_ms"],
                _canonical(context["producer"]),
                _canonical(context["policy"]),
                principal_id,
                input_hash,
                now,
            ],
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO quantitative_metric_current VALUES (?,?,?)",
            [metric_id, revision_id, revision],
        )
        return revision_id

    def metric(
        self,
        namespace: str,
        metric_id: str,
        *,
        scopes: set[str],
        revision: int | None = None,
        include_history: bool = False,
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT revision_id,revision,predecessor_revision_id,canonical_name,definition,unit_id,"
            "dimension_json,frequency,population_json,synonyms_json,mappings_json,formula_json,generation,"
            "valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_json,principal_id,input_hash,created_at_ms "
            "FROM quantitative_metric_revisions WHERE namespace=? AND metric_id=? "
            "AND (? IS NULL OR revision=?) ORDER BY revision DESC",
            [namespace, metric_id, revision, revision],
        ).fetchall()
        if not rows:
            return None

        def render(row):
            return {
                "contract": METRIC_CONTRACT,
                "metric_id": metric_id,
                "namespace": namespace,
                "revision_id": row[0],
                "revision": int(row[1]),
                "predecessor_revision_id": row[2],
                "canonical_name": row[3],
                "definition": row[4],
                "unit_id": row[5],
                "dimension": _load(row[6], {}),
                "frequency": row[7],
                "population": _load(row[8], {}),
                "synonyms": _load(row[9], []),
                "mappings": _load(row[10], {}),
                "formula": _load(row[11], None),
                "generation": int(row[12]),
                "valid_from_ms": row[13],
                "valid_to_ms": row[14],
                "observed_at_ms": int(row[15]),
                "producer": _load(row[16], {}),
                "policy": _load(row[17], {}),
                "principal_id": row[18],
                "input_hash": row[19],
                "created_at_ms": int(row[20]),
            }

        result = render(rows[0])
        if include_history:
            result["history"] = [render(row) for row in rows]
        return result

    def revise_metric(
        self,
        namespace: str,
        metric_id: str,
        expected_revision: int,
        patch: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        prior = self.metric(namespace, metric_id, scopes={READ_SCOPE})
        if not prior:
            raise QuantitativeError("not_found", "metric does not exist in namespace")
        if prior["revision"] != int(expected_revision):
            raise QuantitativeError("revision_conflict", "metric revision changed")
        unit = self._unit(str(patch.get("unit") or prior["unit_id"]), namespace)
        frequency = str(patch.get("frequency") or prior["frequency"])
        if frequency not in FREQUENCIES:
            raise QuantitativeError("invalid_metric", "metric frequency is unsupported")
        context = {
            key: prior[key]
            for key in (
                "generation",
                "valid_from_ms",
                "valid_to_ms",
                "observed_at_ms",
                "producer",
                "policy",
            )
        }
        candidate = {
            "canonical_name": str(
                patch.get("canonical_name") or prior["canonical_name"]
            ),
            "definition": str(patch.get("definition") or prior["definition"]),
            "unit_id": unit["unit_id"],
            "dimension": unit["dimension"],
            "frequency": frequency,
            "population": dict(
                patch["population"] if "population" in patch else prior["population"]
            ),
            "synonyms": sorted(
                {str(item) for item in patch.get("synonyms", prior["synonyms"])}
            ),
            "mappings": dict(
                patch["mappings"] if "mappings" in patch else prior["mappings"]
            ),
            "formula": (
                dict(patch["formula"]) if patch.get("formula") is not None else None
            )
            if "formula" in patch
            else prior["formula"],
        }
        if all(prior[key] == value for key, value in candidate.items()):
            return {**prior, "idempotent": True}
        now = self.now()
        context["observed_at_ms"] = now
        self.conn.execute("BEGIN")
        try:
            revision_id = self._write_metric(
                metric_id,
                namespace,
                prior["revision"] + 1,
                prior["revision_id"],
                candidate["canonical_name"],
                candidate["definition"],
                unit,
                frequency,
                candidate["population"],
                candidate["synonyms"],
                candidate["mappings"],
                candidate["formula"],
                context=context,
                principal_id=principal_id,
                now=now,
            )
            self._audit(
                namespace,
                "revise-metric",
                revision_id,
                principal_id,
                {"from_revision": prior["revision"]},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.metric(namespace, metric_id, scopes={READ_SCOPE})

    def discover(
        self, namespace: str, *, scopes: set[str], query: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT c.metric_id FROM quantitative_metric_current c JOIN quantitative_metric_revisions r "
            "USING(revision_id) WHERE r.namespace=? ORDER BY c.metric_id",
            [namespace],
        ).fetchall()
        needle = query.casefold()
        values = [self.metric(namespace, row[0], scopes=scopes) for row in rows]
        return [
            item
            for item in values
            if item
            and (
                not needle
                or needle in item["canonical_name"].casefold()
                or any(needle in synonym.casefold() for synonym in item["synonyms"])
                or any(
                    needle in key.casefold() or needle in value.casefold()
                    for key, value in item["mappings"].items()
                )
            )
        ][: min(max(int(limit), 1), 100)]

    def observe(
        self,
        namespace: str,
        metric_id: str,
        period: str,
        value: Any | None,
        *,
        provider: str,
        provider_series_id: str,
        vintage_id: str,
        release_at_ms: int,
        retrieved_at_ms: int,
        principal_id: str,
        scopes: set[str],
        unit: str | None = None,
        currency_code: str | None = None,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        adjustment: str = "unknown",
        preliminary: bool = False,
        revision_of: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        generation: int = 0,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        metric = self.metric(namespace, metric_id, scopes={READ_SCOPE})
        if (
            not metric
            or adjustment not in ADJUSTMENTS
            or release_at_ms > retrieved_at_ms
        ):
            raise QuantitativeError(
                "invalid_observation",
                "metric, adjustment, and release/retrieval order are required",
            )
        unit_value = self._unit(unit or metric["unit_id"], namespace)
        if unit_value["dimension"] != metric["dimension"]:
            raise QuantitativeError(
                "dimensional_error", "observation unit dimension differs from metric"
            )
        if revision_of:
            predecessor = self.observation(namespace, revision_of, scopes={READ_SCOPE})
            if not predecessor or predecessor["metric_id"] != metric_id:
                raise QuantitativeError(
                    "invalid_revision",
                    "revision_of must identify an observation of the same metric",
                )
        missing = value is None
        value_text = None if missing else str(_decimal(value))
        currency_value = currency_code or unit_value["currency_code"]
        stable = {
            "namespace": namespace,
            "metric_id": metric_id,
            "provider": provider,
            "provider_series_id": provider_series_id,
            "period": period,
            "value": value_text,
            "missing": missing,
            "unit_id": unit_value["unit_id"],
            "currency_code": currency_value,
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "release_at_ms": int(release_at_ms),
            "retrieved_at_ms": int(retrieved_at_ms),
            "vintage_id": vintage_id,
            "adjustment": adjustment,
            "preliminary": bool(preliminary),
            "revision_of": revision_of,
            "provenance": dict(provenance or {}),
            "generation": int(generation),
            "producer": dict(
                producer or {"name": "noesis-quantitative", "version": "1.0.0"}
            ),
            "policy": dict(policy or {"missing": "explicit-v1"}),
        }
        input_hash = _digest(stable)
        observation_id = (
            "quantitative-observation:"
            + _digest(
                [namespace, metric_id, provider, provider_series_id, period, vintage_id]
            )[:24]
        )
        existing = self.conn.execute(
            "SELECT input_hash FROM quantitative_observations WHERE observation_id=?",
            [observation_id],
        ).fetchone()
        if existing:
            if existing[0] != input_hash:
                raise QuantitativeError(
                    "observation_conflict",
                    "observation vintage was reused with different content",
                )
            return {
                **self.observation(namespace, observation_id, scopes={READ_SCOPE}),
                "idempotent": True,
            }
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO quantitative_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    observation_id,
                    namespace,
                    metric_id,
                    provider,
                    provider_series_id,
                    period,
                    value_text,
                    missing,
                    unit_value["unit_id"],
                    currency_value,
                    valid_from_ms,
                    valid_to_ms,
                    release_at_ms,
                    retrieved_at_ms,
                    vintage_id,
                    adjustment,
                    bool(preliminary),
                    revision_of,
                    _canonical(stable["provenance"]),
                    generation,
                    _canonical(stable["producer"]),
                    _canonical(stable["policy"]),
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            series_id = f"quantitative:{namespace}:{provider}:{provider_series_id}"
            self.conn.execute(
                "INSERT OR REPLACE INTO dataset_series VALUES (?,?,?,?,?,?,NULL,?,?,?)",
                [
                    series_id,
                    provider,
                    metric["canonical_name"],
                    unit_value["symbol"],
                    metric["frequency"],
                    _canonical(metric["population"]),
                    retrieved_at_ms,
                    stable["provenance"].get("url"),
                    _canonical({"metric_id": metric_id, "namespace": namespace}),
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO dataset_observations VALUES (?,?,?,?)",
                [
                    series_id,
                    period,
                    retrieved_at_ms,
                    float(value_text) if value_text is not None else None,
                ],
            )
            self._audit(
                namespace,
                "observe",
                observation_id,
                principal_id,
                {"metric_id": metric_id, "vintage_id": vintage_id},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.observation(namespace, observation_id, scopes={READ_SCOPE})

    def observation(
        self, namespace: str, observation_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT metric_id,provider,provider_series_id,period,value_text,missing,unit_id,currency_code,"
            "valid_from_ms,valid_to_ms,release_at_ms,retrieved_at_ms,vintage_id,adjustment,preliminary,"
            "revision_of,provenance_json,generation,producer_json,policy_json,principal_id,input_hash,created_at_ms "
            "FROM quantitative_observations WHERE namespace=? AND observation_id=?",
            [namespace, observation_id],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": OBSERVATION_CONTRACT,
            "observation_id": observation_id,
            "namespace": namespace,
            "metric_id": row[0],
            "provider": row[1],
            "provider_series_id": row[2],
            "period": row[3],
            "value": row[4],
            "missing": bool(row[5]),
            "unit_id": row[6],
            "currency_code": row[7],
            "valid_from_ms": row[8],
            "valid_to_ms": row[9],
            "release_at_ms": int(row[10]),
            "retrieved_at_ms": int(row[11]),
            "vintage_id": row[12],
            "adjustment": row[13],
            "preliminary": bool(row[14]),
            "revision_of": row[15],
            "provenance": _load(row[16], {}),
            "generation": int(row[17]),
            "producer": _load(row[18], {}),
            "policy": _load(row[19], {}),
            "principal_id": row[20],
            "input_hash": row[21],
            "created_at_ms": int(row[22]),
        }

    def series(
        self,
        namespace: str,
        metric_id: str,
        *,
        scopes: set[str],
        as_of_ms: int | None = None,
        provider: str | None = None,
        include_vintages: bool = False,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT observation_id FROM quantitative_observations WHERE namespace=? AND metric_id=? "
            "AND (? IS NULL OR retrieved_at_ms<=?) AND (? IS NULL OR provider=?) "
            "QUALIFY ? OR row_number() OVER (PARTITION BY period,provider,provider_series_id "
            "ORDER BY retrieved_at_ms DESC,release_at_ms DESC,observation_id DESC)=1 "
            "ORDER BY period,retrieved_at_ms,provider,observation_id LIMIT ?",
            [
                namespace,
                metric_id,
                as_of_ms,
                as_of_ms,
                provider,
                provider,
                include_vintages,
                min(max(limit, 1), 5000),
            ],
        ).fetchall()
        return [self.observation(namespace, row[0], scopes=scopes) for row in rows]

    def add_break(
        self,
        namespace: str,
        metric_id: str,
        break_type: str,
        boundary_ms: int,
        *,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
        confidence: float,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if break_type not in BREAK_TYPES or not self.metric(
            namespace, metric_id, scopes={READ_SCOPE}
        ):
            raise QuantitativeError(
                "invalid_break", "metric and supported break type are required"
            )
        confidence_value = float(confidence)
        if not math.isfinite(confidence_value) or not 0 <= confidence_value <= 1:
            raise QuantitativeError(
                "invalid_confidence", "confidence must be between 0 and 1"
            )
        stable = [
            namespace,
            metric_id,
            break_type,
            boundary_ms,
            before,
            after,
            evidence,
        ]
        break_id = "quantitative-break:" + _digest(stable)[:24]
        existing = self.conn.execute(
            "SELECT before_json,after_json,evidence_json,confidence,principal_id,created_at_ms "
            "FROM quantitative_series_breaks WHERE break_id=?",
            [break_id],
        ).fetchone()
        now = int(existing[5]) if existing else self.now()
        if not existing:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "INSERT INTO quantitative_series_breaks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        break_id,
                        namespace,
                        metric_id,
                        break_type,
                        boundary_ms,
                        _canonical(before),
                        _canonical(after),
                        _canonical([dict(item) for item in evidence]),
                        confidence_value,
                        principal_id,
                        now,
                    ],
                )
                self._audit(
                    namespace,
                    "add-series-break",
                    break_id,
                    principal_id,
                    {"break_type": break_type, "boundary_ms": boundary_ms},
                    now,
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {
            "break_id": break_id,
            "namespace": namespace,
            "metric_id": metric_id,
            "break_type": break_type,
            "boundary_ms": boundary_ms,
            "before": dict(before),
            "after": dict(after),
            "evidence": [dict(item) for item in evidence],
            "confidence": confidence_value,
            "principal_id": principal_id,
            "created_at_ms": now,
            "idempotent": bool(existing),
        }

    def comparability(
        self, namespace: str, left_id: str, right_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        left = self.observation(namespace, left_id, scopes=scopes)
        right = self.observation(namespace, right_id, scopes=scopes)
        if not left or not right:
            raise QuantitativeError(
                "not_found", "comparison observation does not exist"
            )
        reasons = []
        left_metric = self.metric(namespace, left["metric_id"], scopes=scopes)
        right_metric = self.metric(namespace, right["metric_id"], scopes=scopes)
        if left_metric["dimension"] != right_metric["dimension"]:
            reasons.append("dimension-mismatch")
        if left_metric["definition"] != right_metric["definition"]:
            reasons.append("definition-mismatch")
        if left_metric["frequency"] != right_metric["frequency"]:
            reasons.append("frequency-mismatch")
        if left_metric["population"] != right_metric["population"]:
            reasons.append("population-mismatch")
        if left["adjustment"] != right["adjustment"]:
            reasons.append("seasonal-adjustment-mismatch")
        low, high = sorted([left["valid_from_ms"] or 0, right["valid_from_ms"] or 0])
        breaks = self.conn.execute(
            "SELECT break_id,break_type,boundary_ms,evidence_json,confidence FROM quantitative_series_breaks "
            "WHERE namespace=? AND metric_id IN (?,?) AND boundary_ms>? AND boundary_ms<=? "
            "ORDER BY boundary_ms,break_id",
            [namespace, left["metric_id"], right["metric_id"], low, high],
        ).fetchall()
        reasons.extend(f"series-break:{row[1]}" for row in breaks)
        if left["provider"] != right["provider"] and not any(
            row[1] == "provider-switch" for row in breaks
        ):
            reasons.append("unreviewed-provider-switch")
        result = {
            "contract": COMPARABILITY_CONTRACT,
            "left_observation_id": left_id,
            "right_observation_id": right_id,
            "comparable": not reasons,
            "reasons": reasons,
            "breaks": [
                {
                    "break_id": row[0],
                    "break_type": row[1],
                    "boundary_ms": int(row[2]),
                    "evidence": _load(row[3], []),
                    "confidence": float(row[4]),
                }
                for row in breaks
            ],
            "assessment_hash": _digest([left_id, right_id, reasons, breaks]),
        }
        return result

    def convert_physical(self, namespace, value, from_unit, to_unit, *, scopes, principal_id, precision=6):
        """Optional Pint physical conversion, separate from versioned economic units."""
        _require(scopes, CALCULATE_SCOPE)
        from src.integrations.units import convert_physical
        evaluated = convert_physical(value, from_unit, to_unit, precision=precision)
        return self._calculation(namespace, "physical-conversion", evaluated["request"],
                                 {**evaluated["result"], "producer": evaluated["producer"]},
                                 input_ids=[], principal_id=principal_id, formula_revision_id=None)

    def convert(
        self,
        namespace: str,
        value: Any,
        from_unit: str,
        to_unit: str,
        *,
        scopes: set[str],
        principal_id: str,
        precision: int = 6,
        rate: Mapping[str, Any] | None = None,
        backend: str = "native",
    ) -> dict[str, Any]:
        _require(scopes, CALCULATE_SCOPE)
        source, target = (
            self._unit(from_unit, namespace),
            self._unit(to_unit, namespace),
        )
        if backend not in {"native", "pint"}:
            raise QuantitativeError("unsupported_backend", "Unknown unit conversion backend")
        if backend == "pint":
            from src.integrations.units import convert_registered
            if rate is not None:
                raise QuantitativeError("unsupported_rate", "Pint cannot interpret exchange-rate evidence")
            evaluated = convert_registered(value, source, target, precision=precision)
            return self._calculation(
                namespace, "conversion", evaluated["request"],
                {**evaluated["result"], "producer": evaluated["producer"]},
                input_ids=[source["unit_id"], target["unit_id"]],
                principal_id=principal_id, formula_revision_id=None,
            )
        if source["dimension"] != target["dimension"]:
            raise QuantitativeError(
                "dimensional_error", "units have incompatible dimensions"
            )
        number = _decimal(value)
        inputs = []
        if (
            source["successor_unit_id"] == target["unit_id"]
            and source["redenomination_factor"]
        ):
            number *= _decimal(source["redenomination_factor"], "redenomination_factor")
        elif (
            source["currency_code"] != target["currency_code"]
            and source["currency_code"]
        ):
            if (
                not rate
                or rate.get("from") != source["currency_code"]
                or rate.get("to") != target["currency_code"]
            ):
                raise QuantitativeError(
                    "unavailable_rate",
                    "an exact matching currency rate receipt is required",
                )
            number *= _decimal(rate.get("rate"), "rate")
            inputs.append(dict(rate))
        else:
            base = (number + _decimal(source["offset"])) * _decimal(source["factor"])
            number = base / _decimal(target["factor"]) - _decimal(target["offset"])
        quant = Decimal(1).scaleb(-min(max(int(precision), 0), 12))
        result = str(number.quantize(quant, rounding=ROUND_HALF_EVEN))
        return self._calculation(
            namespace,
            "conversion",
            {
                "value": str(value),
                "from_unit": source["unit_id"],
                "to_unit": target["unit_id"],
                "precision": precision,
                "rate": rate,
            },
            {"value": result, "unit_id": target["unit_id"]},
            input_ids=[
                str(item.get("observation_id") or item.get("rate_id") or _digest(item))
                for item in inputs
            ],
            principal_id=principal_id,
            formula_revision_id=None,
        )

    def evaluate_formula(
        self,
        namespace: str,
        metric_id: str,
        inputs: Mapping[str, Mapping[str, Any]],
        *,
        scopes: set[str],
        principal_id: str,
        precision: int = 6,
        backend: str = "native",
    ) -> dict[str, Any]:
        _require(scopes, CALCULATE_SCOPE)
        metric = self.metric(namespace, metric_id, scopes={READ_SCOPE})
        if not metric or not metric["formula"]:
            raise QuantitativeError(
                "missing_formula", "metric has no versioned formula"
            )
        if backend not in {"native", "pint"}:
            raise QuantitativeError("unsupported_backend", "Unknown formula backend")
        expression = str(metric["formula"].get("expression") or "")
        expected_dimensions = dict(metric["formula"].get("input_dimensions") or {})
        for name, expected in expected_dimensions.items():
            if name not in inputs or _dimension(
                inputs[name].get("dimension") or {}
            ) != _dimension(expected):
                raise QuantitativeError(
                    "dimensional_error",
                    f"formula input {name!r} has an incompatible dimension",
                )
        if backend == "pint":
            from src.integrations.units import evaluate_registered_formula

            resolved = {}
            for name, item in inputs.items():
                if not item.get("unit_id"):
                    raise QuantitativeError("unknown_unit", "Pint formula inputs require explicit unit identities")
                unit = self._unit(item["unit_id"], namespace)
                if _dimension(item.get("dimension") or {}) != unit["dimension"]:
                    raise QuantitativeError("dimensional_error", "Input dimension differs from registered unit")
                resolved[name] = {**item, "unit_definition": unit}
            target = self._unit(metric["unit_id"], namespace)
            evaluated = evaluate_registered_formula(expression, resolved, target, precision=precision)
            return self._calculation(
                namespace, "formula", {**evaluated["request"], "metric_id": metric_id},
                {**evaluated["result"], "producer": evaluated["producer"]},
                input_ids=[str(item.get("observation_id") or _digest(item)) for item in inputs.values()]
                    + [item["unit_definition"]["unit_id"] for item in resolved.values()] + [target["unit_id"]],
                principal_id=principal_id, formula_revision_id=metric["revision_id"],
            )
        tree = ast.parse(expression, mode="eval")
        values = {name: _decimal(item["value"], name) for name, item in inputs.items()}

        def evaluate(node):
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Name) and node.id in values:
                return values[node.id]
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return _decimal(node.value)
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
                return -evaluate(node.operand)
            if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
            ):
                left, right = evaluate(node.left), evaluate(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                return left / right
            raise QuantitativeError(
                "unsafe_formula", "formula contains an unsupported expression"
            )

        result_value = evaluate(tree)
        quant = Decimal(1).scaleb(-min(max(int(precision), 0), 12))
        result = {
            "value": str(result_value.quantize(quant, rounding=ROUND_HALF_EVEN)),
            "unit_id": metric["unit_id"],
            "dimension": metric["dimension"],
        }
        input_ids = [
            str(item.get("observation_id") or _digest(item)) for item in inputs.values()
        ]
        return self._calculation(
            namespace,
            "formula",
            {"metric_id": metric_id, "inputs": inputs, "precision": precision},
            result,
            input_ids=input_ids,
            principal_id=principal_id,
            formula_revision_id=metric["revision_id"],
        )

    def transform_frequency(
        self,
        namespace: str,
        values: Sequence[Mapping[str, Any]],
        *,
        from_frequency: str,
        to_frequency: str,
        aggregation: str,
        scopes: set[str],
        principal_id: str,
        precision: int = 6,
    ) -> dict[str, Any]:
        _require(scopes, CALCULATE_SCOPE)
        if (
            from_frequency not in FREQUENCIES
            or to_frequency not in FREQUENCIES
            or aggregation not in {"sum", "average", "last"}
            or not values
        ):
            raise QuantitativeError(
                "invalid_frequency_transform",
                "frequencies, aggregation, and at least one value are required",
            )
        numbers = []
        for item in values:
            if item.get("value") is None:
                raise QuantitativeError(
                    "missing_input",
                    "frequency transformation cannot hide missing values",
                )
            numbers.append(_decimal(item["value"]))
        if aggregation == "sum":
            result_value = sum(numbers, Decimal(0))
        elif aggregation == "average":
            result_value = sum(numbers, Decimal(0)) / Decimal(len(numbers))
        else:
            result_value = numbers[-1]
        quant = Decimal(1).scaleb(-min(max(int(precision), 0), 12))
        result = {
            "value": str(result_value.quantize(quant, rounding=ROUND_HALF_EVEN)),
            "frequency": to_frequency,
            "aggregation": aggregation,
        }
        return self._calculation(
            namespace,
            "frequency-transform",
            {
                "values": list(values),
                "from_frequency": from_frequency,
                "to_frequency": to_frequency,
                "aggregation": aggregation,
                "precision": precision,
            },
            result,
            input_ids=[
                str(item.get("observation_id") or _digest(item)) for item in values
            ],
            principal_id=principal_id,
            formula_revision_id=None,
        )

    def adjust_inflation(
        self,
        namespace: str,
        value: Any,
        observed_index: Mapping[str, Any],
        target_index: Mapping[str, Any],
        *,
        scopes: set[str],
        principal_id: str,
        precision: int = 6,
    ) -> dict[str, Any]:
        _require(scopes, CALCULATE_SCOPE)
        observed = _decimal(observed_index.get("value"), "observed_index")
        target = _decimal(target_index.get("value"), "target_index")
        if observed == 0:
            raise QuantitativeError(
                "invalid_index", "observed price index cannot be zero"
            )
        result_value = _decimal(value) * target / observed
        quant = Decimal(1).scaleb(-min(max(int(precision), 0), 12))
        result = {
            "value": str(result_value.quantize(quant, rounding=ROUND_HALF_EVEN)),
            "price_basis": target_index.get("period"),
        }
        return self._calculation(
            namespace,
            "inflation-adjustment",
            {
                "value": str(value),
                "observed_index": dict(observed_index),
                "target_index": dict(target_index),
                "precision": precision,
            },
            result,
            input_ids=[
                str(observed_index.get("observation_id") or _digest(observed_index)),
                str(target_index.get("observation_id") or _digest(target_index)),
            ],
            principal_id=principal_id,
            formula_revision_id=None,
        )

    def _calculation(
        self,
        namespace: str,
        operation: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        input_ids: Sequence[str],
        principal_id: str,
        formula_revision_id: str | None,
    ) -> dict[str, Any]:
        stable = [
            namespace,
            operation,
            request,
            result,
            sorted(input_ids),
            formula_revision_id,
            "half-even",
        ]
        calculation_hash = _digest(stable)
        calculation_id = "quantitative-calculation:" + calculation_hash[:24]
        existing = self.conn.execute(
            "SELECT principal_id,created_at_ms FROM quantitative_calculations WHERE calculation_id=?",
            [calculation_id],
        ).fetchone()
        now = int(existing[1]) if existing else self.now()
        if not existing:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "INSERT INTO quantitative_calculations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        calculation_id,
                        namespace,
                        operation,
                        _canonical(request),
                        _canonical(result),
                        _canonical(sorted(input_ids)),
                        formula_revision_id,
                        "half-even",
                        calculation_hash,
                        principal_id,
                        now,
                    ],
                )
                self._audit(
                    namespace,
                    "calculate",
                    calculation_id,
                    principal_id,
                    {"operation": operation},
                    now,
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {
            "contract": CALCULATION_CONTRACT,
            "calculation_id": calculation_id,
            "namespace": namespace,
            "operation": operation,
            "request": dict(request),
            "result": dict(result),
            "input_ids": sorted(input_ids),
            "formula_revision_id": formula_revision_id,
            "rounding": "half-even",
            "calculation_hash": calculation_hash,
            "principal_id": existing[0] if existing else principal_id,
            "created_at_ms": now,
            "idempotent": bool(existing),
        }

    def replay_calculation(
        self, namespace: str, calculation_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT operation,request_json,result_json,input_ids_json,formula_revision_id,rounding,"
            "calculation_hash FROM quantitative_calculations WHERE namespace=? AND calculation_id=?",
            [namespace, calculation_id],
        ).fetchone()
        if not row:
            raise QuantitativeError(
                "not_found", "calculation does not exist in namespace"
            )
        replayed = _digest(
            [
                namespace,
                row[0],
                _load(row[1], {}),
                _load(row[2], {}),
                _load(row[3], []),
                row[4],
                row[5],
            ]
        )
        return {
            "calculation_id": calculation_id,
            "stored_hash": row[6],
            "replayed_hash": replayed,
            "deterministic": replayed == row[6],
            "input_ids": _load(row[3], []),
            "formula_revision_id": row[4],
        }
