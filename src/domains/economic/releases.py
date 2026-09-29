"""Immutable economic release snapshots and revision-aware comparisons.

Snapshots read retained vintage rows only. A release date never implies that
Noesis had acquired the data by that date; both clocks are checked explicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

from src.domains.economic.model import assess_comparability, ensure_economic_schema

SNAPSHOT_CONTRACT = "noesis-economic-release-snapshot-v1"
COMPARISON_CONTRACT = "noesis-economic-release-comparison-v1"
READ_SCOPE = "knowledge:economic:read"
WRITE_SCOPE = "knowledge:economic:write"
MAX_SERIES = 50
MAX_OBSERVATIONS = 5000

_DDL = """
CREATE TABLE IF NOT EXISTS economic_release_snapshots (
 snapshot_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 request_key TEXT NOT NULL, request_hash TEXT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, UNIQUE(namespace,owner,request_key));
CREATE TABLE IF NOT EXISTS economic_release_comparisons (
 comparison_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 request_key TEXT NOT NULL, request_hash TEXT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, UNIQUE(namespace,owner,request_key));
"""


class EconomicReleaseError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _text(value: Any, name: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise EconomicReleaseError(
            "invalid_request", f"{name} must be nonempty bounded text"
        )
    return value


def _ms(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise EconomicReleaseError(
            "invalid_request", f"{name} must be epoch milliseconds"
        )
    return value


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise EconomicReleaseError(
            "invalid_number", "comparison value must be finite decimal"
        ) from exc
    if not result.is_finite():
        raise EconomicReleaseError(
            "invalid_number", "comparison value must be finite decimal"
        )
    return result


def _namespace_access(
    namespace: str, principal_id: str, scopes: set[str], *, write: bool
) -> None:
    _text(namespace, "namespace", 100)
    _text(principal_id, "principal_id", 200)
    if "operator" in scopes:
        return
    required = WRITE_SCOPE if write else READ_SCOPE
    if (
        required not in scopes
        or not (
            {f"namespace:{namespace}:write"}
            if write
            else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
        )
        & scopes
    ):
        raise EconomicReleaseError(
            "unauthorized", "current economic and namespace access is required"
        )


def _source_access(source_document_id: str | None, scopes: set[str]) -> None:
    if (
        source_document_id
        and "operator" not in scopes
        and f"document:{source_document_id}:read" not in scopes
    ):
        raise EconomicReleaseError(
            "unauthorized", "current source document access is required"
        )


class EconomicReleaseStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_economic_schema(conn)
            conn.execute(_DDL)

    def _get(
        self,
        table: str,
        identity: str,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _namespace_access(namespace, principal_id, scopes, write=False)
        key = (
            "snapshot_id" if table == "economic_release_snapshots" else "comparison_id"
        )
        try:
            row = self.conn.execute(
                f"SELECT owner,content_json FROM {table} WHERE namespace=? AND {key}=?",
                [namespace, identity],
            ).fetchone()
        except Exception as exc:
            raise EconomicReleaseError(
                "not_found", "economic artifact is unavailable"
            ) from exc
        if not row:
            raise EconomicReleaseError("not_found", "economic artifact is unavailable")
        if "operator" not in scopes and row[0] != principal_id:
            raise EconomicReleaseError(
                "unauthorized", "current artifact ownership is required"
            )
        artifact = json.loads(row[1])
        for series in artifact.get("series", []):
            _source_access(series.get("source_document_id"), scopes)
        if table == "economic_release_comparisons":
            self.inspect_snapshot(
                namespace,
                artifact["left_snapshot_id"],
                principal_id=principal_id,
                scopes=scopes,
            )
            self.inspect_snapshot(
                namespace,
                artifact["right_snapshot_id"],
                principal_id=principal_id,
                scopes=scopes,
            )
        return artifact

    def create_snapshot(
        self,
        namespace: str,
        request_key: str,
        release_id: str,
        *,
        release_cutoff_ms: int,
        acquired_cutoff_ms: int,
        series: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
        domain: str = "economics",
    ) -> dict[str, Any]:
        _namespace_access(namespace, principal_id, scopes, write=True)
        _text(request_key, "request_key")
        _text(release_id, "release_id")
        _text(domain, "domain", 100)
        _ms(release_cutoff_ms, "release_cutoff_ms")
        _ms(acquired_cutoff_ms, "acquired_cutoff_ms")
        if not isinstance(series, (list, tuple)) or not 1 <= len(series) <= MAX_SERIES:
            raise EconomicReleaseError("invalid_request", "select 1 to 50 series")
        selectors = []
        for item in series:
            if not isinstance(item, Mapping):
                raise EconomicReleaseError(
                    "invalid_request", "series selector must be an object"
                )
            selector = {
                "series_id": _text(item.get("series_id"), "series_id"),
                "vintage_id": item.get("vintage_id"),
                "provider_release_id": item.get("provider_release_id"),
                "methodology_id": item.get("methodology_id"),
                "source_revision_id": item.get("source_revision_id"),
            }
            for key in (
                "vintage_id",
                "provider_release_id",
                "methodology_id",
                "source_revision_id",
            ):
                if selector[key] is not None:
                    _text(selector[key], key)
            selectors.append(selector)
        if len({item["series_id"] for item in selectors}) != len(selectors):
            raise EconomicReleaseError(
                "invalid_request", "duplicate series in release snapshot"
            )
        request = {
            "namespace": namespace,
            "owner": principal_id,
            "release_id": release_id,
            "release_cutoff_ms": release_cutoff_ms,
            "acquired_cutoff_ms": acquired_cutoff_ms,
            "domain": domain,
            "series": selectors,
        }
        request_hash = _hash(request)
        prior = self.conn.execute(
            "SELECT request_hash,content_json FROM economic_release_snapshots WHERE namespace=? AND owner=? AND request_key=?",
            [namespace, principal_id, request_key],
        ).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise EconomicReleaseError(
                    "idempotency_conflict",
                    "request key already identifies another snapshot",
                )
            snapshot = json.loads(prior[1])
            self._check_snapshot_sources(snapshot, scopes)
            return {**snapshot, "idempotent": True}

        captured, remaining = [], MAX_OBSERVATIONS
        for selector in selectors:
            item = self._capture_series(
                domain,
                selector,
                release_cutoff_ms,
                acquired_cutoff_ms,
                scopes,
                remaining,
            )
            remaining -= len(item["observations"])
            captured.append(item)
        core = {
            "contract": SNAPSHOT_CONTRACT,
            **request,
            "series": captured,
            "coverage": "complete_for_retained_selection"
            if all(item["status"] == "available" for item in captured)
            else "incomplete",
            "limitations": [
                "Provider availability before local acquisition is not inferred",
                "Fixture data does not establish live-provider validation",
            ],
        }
        if any(
            item.get("release_at_basis") == "provider_vintage_fallback"
            for item in captured
        ):
            core["limitations"].append(
                "At least one provider lacks an official release timestamp; its retained provider vintage time is used for release-cutoff selection"
            )
        if any(
            item.get("retrieved_at_basis") == "record_as_of_fallback"
            for item in captured
        ):
            core["limitations"].append(
                "At least one acquisition time falls back to the record vintage because no local retrieval time was retained"
            )
        snapshot = {
            **core,
            "snapshot_id": "economic-snapshot:" + _hash(core)[:32],
            "created_at_ms": self.now(),
            "request_hash": request_hash,
        }
        self.conn.execute(
            "INSERT INTO economic_release_snapshots VALUES (?,?,?,?,?,?,?)",
            [
                snapshot["snapshot_id"],
                namespace,
                principal_id,
                request_key,
                request_hash,
                _canonical(snapshot),
                snapshot["created_at_ms"],
            ],
        )
        return snapshot

    def _capture_series(
        self, domain, selector, release_cutoff_ms, acquired_cutoff_ms, scopes, remaining
    ):
        series_id = selector["series_id"]
        row = self.conn.execute(
            "SELECT m.indicator_id,m.provider,m.provider_code,m.source_url,i.concept,i.unit,i.scaling,i.currency_basis,i.geography,i.frequency,i.price_basis,i.seasonal_adjustment,i.definition,i.attributes_json "
            "FROM economic_series_map m JOIN economic_indicators i ON i.domain=m.domain AND i.indicator_id=m.indicator_id "
            "WHERE m.domain=? AND m.series_id=?",
            [domain, series_id],
        ).fetchone()
        if not row:
            raise EconomicReleaseError(
                "not_found", f"economic series {series_id!r} is unavailable"
            )
        keys = (
            "indicator_id",
            "provider",
            "provider_code",
            "source_url",
            "concept",
            "unit",
            "scaling",
            "currency_basis",
            "geography",
            "frequency",
            "price_basis",
            "seasonal_adjustment",
            "definition",
            "attributes_json",
        )
        semantics = dict(zip(keys, row))
        attributes = json.loads(semantics.pop("attributes_json") or "{}")
        vintage_clause = "AND v.vintage_id=?" if selector["vintage_id"] else ""
        params = [domain, series_id]
        if selector["vintage_id"]:
            params.append(selector["vintage_id"])
        vintages = self.conn.execute(
            "SELECT v.as_of,v.vintage_id,v.release_at_ms,v.retrieved_at_ms,v.revision_of,v.source_url,v.source_document_id,"
            "v.release_at_basis,v.retrieved_at_basis,v.vintage_basis,v.release_time_status "
            "FROM economic_vintages v WHERE v.domain=? AND v.series_id=? "
            + vintage_clause
            + " ORDER BY v.release_at_ms DESC,v.as_of DESC LIMIT 1001",
            params,
        ).fetchall()
        if len(vintages) > 1000:
            raise EconomicReleaseError(
                "budget_exceeded", "vintage selection exceeds 1000 rows"
            )
        eligible = [
            v
            for v in vintages
            if v[2] <= release_cutoff_ms and v[3] <= acquired_cutoff_ms
        ]
        selected = eligible[0] if eligible else None
        reason = None
        if selected is None:
            if not vintages:
                reason = "historical_vintage_unavailable"
            elif any(v[2] <= release_cutoff_ms for v in vintages):
                reason = "late_acquired"
            else:
                reason = "not_yet_published"
        source_document_id = selected[6] if selected else None
        _source_access(source_document_id, scopes)
        revision_id = selector["source_revision_id"]
        if revision_id is not None:
            if source_document_id is None:
                raise EconomicReleaseError(
                    "invalid_request",
                    "source revision requires a linked source document",
                )
            revision = self.conn.execute(
                "SELECT observed_at_ms FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
                [source_document_id, revision_id],
            ).fetchone()
            if not revision or revision[0] > acquired_cutoff_ms:
                raise EconomicReleaseError(
                    "source_revision_unavailable",
                    "linked source revision was not retained at acquisition cutoff",
                )
        observations = []
        if selected:
            rows = self.conn.execute(
                "SELECT period,value FROM dataset_observations WHERE series_id=? AND as_of=? ORDER BY period LIMIT ?",
                [series_id, selected[0], remaining + 1],
            ).fetchall()
            if len(rows) > remaining:
                raise EconomicReleaseError(
                    "budget_exceeded", "snapshot exceeds 5000 observations"
                )
            for period, value in rows:
                if value is not None and not math.isfinite(value):
                    raise EconomicReleaseError(
                        "invalid_number", "retained observation is nonfinite"
                    )
                observations.append(
                    {
                        "period": period,
                        "value": value,
                        "observation_id": f"{series_id}:{period}@{selected[0]}",
                    }
                )
            if not observations:
                reason = "retained_observations_unavailable"
        return {
            "series_id": series_id,
            **semantics,
            "status": "available" if selected and observations else "unavailable",
            "unavailable_reason": reason,
            "provider_release_id": selector["provider_release_id"],
            "provider_release_id_provenance": "caller_supplied"
            if selector["provider_release_id"]
            else "unavailable",
            "methodology_id": selector["methodology_id"]
            or attributes.get("methodology_id")
            or attributes.get("method"),
            "source_revision_id": revision_id,
            "source_document_id": source_document_id,
            "vintage_id": selected[1] if selected else selector["vintage_id"],
            "provider_vintage_ms": selected[0] if selected else None,
            "release_at_ms": selected[2] if selected else None,
            "retrieved_at_ms": selected[3] if selected else None,
            "release_at_basis": selected[7] if selected else None,
            "retrieved_at_basis": selected[8] if selected else None,
            "vintage_basis": selected[9] if selected else None,
            "release_time_status": selected[10] if selected else None,
            "revision_of": selected[4] if selected else None,
            "vintage_source_url": selected[5] if selected else None,
            "observations": observations,
        }

    def _check_snapshot_sources(self, snapshot, scopes):
        for series in snapshot["series"]:
            _source_access(series.get("source_document_id"), scopes)

    def inspect_snapshot(
        self,
        namespace: str,
        snapshot_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise EconomicReleaseError(
                "invalid_request", "offset and limit must be bounded"
            )
        snapshot = self._get(
            "economic_release_snapshots",
            snapshot_id,
            namespace,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **snapshot,
            "series": snapshot["series"][offset : offset + limit],
            "page": {
                "offset": offset,
                "limit": limit,
                "total": len(snapshot["series"]),
            },
        }

    def _full_snapshot(self, namespace, snapshot_id, *, principal_id, scopes):
        return self._get(
            "economic_release_snapshots",
            snapshot_id,
            namespace,
            principal_id=principal_id,
            scopes=scopes,
        )

    def compare(
        self,
        namespace: str,
        request_key: str,
        left_snapshot_id: str,
        right_snapshot_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        precision: int = 6,
        conversions: Sequence[Mapping[str, Any]] = (),
        assumptions: Sequence[str] = (),
    ) -> dict[str, Any]:
        _namespace_access(namespace, principal_id, scopes, write=True)
        _text(request_key, "request_key")
        if type(precision) is not int or not 0 <= precision <= 12:
            raise EconomicReleaseError("invalid_request", "precision must be 0 to 12")
        if not isinstance(assumptions, (list, tuple)) or len(assumptions) > 30:
            raise EconomicReleaseError("invalid_request", "assumptions must be bounded")
        for assumption in assumptions:
            _text(assumption, "assumption", 1000)
        if not isinstance(conversions, (list, tuple)) or len(conversions) > MAX_SERIES:
            raise EconomicReleaseError("invalid_request", "conversions must be bounded")
        left = self._full_snapshot(
            namespace, left_snapshot_id, principal_id=principal_id, scopes=scopes
        )
        right = self._full_snapshot(
            namespace, right_snapshot_id, principal_id=principal_id, scopes=scopes
        )
        if left["domain"] != right["domain"]:
            raise EconomicReleaseError(
                "incomparable", "snapshots are from different domains"
            )
        conversion_map = {}
        for conversion in conversions:
            if not isinstance(conversion, Mapping):
                raise EconomicReleaseError(
                    "invalid_request", "conversion must be an object"
                )
            identity = _text(conversion.get("indicator_id"), "conversion indicator_id")
            if identity in conversion_map:
                raise EconomicReleaseError("invalid_request", "duplicate conversion")
            for key in ("conversion_id", "method", "evidence_reference"):
                _text(conversion.get(key), key, 1000)
            blockers = conversion.get("addresses")
            if (
                not isinstance(blockers, list)
                or not blockers
                or len(blockers) > 20
                or any(not isinstance(x, str) for x in blockers)
            ):
                raise EconomicReleaseError(
                    "invalid_request", "conversion must name addressed dimensions"
                )
            left_factor, right_factor = (
                _decimal(conversion.get("left_multiplier")),
                _decimal(conversion.get("right_multiplier")),
            )
            if left_factor <= 0 or right_factor <= 0:
                raise EconomicReleaseError(
                    "invalid_request", "conversion multipliers must be positive"
                )
            conversion_map[identity] = {
                **conversion,
                "left_multiplier": str(left_factor),
                "right_multiplier": str(right_factor),
            }
        request = {
            "namespace": namespace,
            "owner": principal_id,
            "left_snapshot_id": left_snapshot_id,
            "right_snapshot_id": right_snapshot_id,
            "precision": precision,
            "conversions": list(conversion_map.values()),
            "assumptions": list(assumptions),
        }
        request_hash = _hash(request)
        prior = self.conn.execute(
            "SELECT request_hash,content_json FROM economic_release_comparisons WHERE namespace=? AND owner=? AND request_key=?",
            [namespace, principal_id, request_key],
        ).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise EconomicReleaseError(
                    "idempotency_conflict", "request key identifies another comparison"
                )
            return {**json.loads(prior[1]), "idempotent": True}
        pairs = self._pair_series(left["series"], right["series"])
        items = [
            self._compare_pair(
                left_item,
                right_item,
                precision,
                conversion_map.get(identity),
                left["domain"],
            )
            for identity, left_item, right_item in pairs
        ]
        core = {
            "contract": COMPARISON_CONTRACT,
            **request,
            "left_release_id": left["release_id"],
            "right_release_id": right["release_id"],
            "items": items,
            "coverage": "complete_for_retained_selection"
            if left["coverage"]
            == right["coverage"]
            == "complete_for_retained_selection"
            else "incomplete",
            "limitations": [
                "Comparisons use captured observations and declared conversions only",
                "No independent human or live-provider validation is implied",
            ],
        }
        comparison = {
            **core,
            "comparison_id": "economic-comparison:" + _hash(core)[:32],
            "created_at_ms": self.now(),
            "request_hash": request_hash,
        }
        self.conn.execute(
            "INSERT INTO economic_release_comparisons VALUES (?,?,?,?,?,?,?)",
            [
                comparison["comparison_id"],
                namespace,
                principal_id,
                request_key,
                request_hash,
                _canonical(comparison),
                comparison["created_at_ms"],
            ],
        )
        return comparison

    @staticmethod
    def _pair_series(left, right):
        def keyed(values):
            result = {}
            for item in values:
                key = (item["indicator_id"], item["geography"])
                if key in result:
                    raise EconomicReleaseError(
                        "ambiguous_identity",
                        "metric and geography must uniquely identify a series in each release",
                    )
                result[key] = item
            return result

        left_map, right_map = keyed(left), keyed(right)
        return [
            (key[0], left_map.get(key), right_map.get(key))
            for key in sorted(set(left_map) | set(right_map), key=str)
        ]

    def _compare_pair(self, left, right, precision, conversion, domain):
        source = left or right
        identity = {
            "indicator_id": source["indicator_id"],
            "geography": source["geography"],
        }
        if (
            not left
            or not right
            or left["status"] != "available"
            or right["status"] != "available"
        ):
            return {
                **identity,
                "status": "unavailable",
                "reason": "series_or_vintage_unavailable",
                "same_period_revisions": [],
                "new_period_changes": [],
                "added_observations": [],
                "removed_observations": [],
                "unchanged_count": 0,
                "missing_value_count": 0,
                "blockers": [],
                "conversion": None,
            }
        blockers = [
            field
            for field in (
                "concept",
                "unit",
                "currency_basis",
                "frequency",
                "price_basis",
                "seasonal_adjustment",
                "definition",
                "methodology_id",
            )
            if left.get(field) != right.get(field)
        ]
        # Apply the existing economic dimension check when series differ.
        if left["series_id"] != right["series_id"]:
            existing = assess_comparability(
                self.conn, left["series_id"], right["series_id"], domain=domain
            )
            blockers.extend(item["dimension"] for item in existing["blockers"])
        blockers = sorted(set(blockers))
        if blockers and (
            not conversion or not set(blockers) <= set(conversion["addresses"])
        ):
            return {
                **identity,
                "status": "blocked",
                "reason": "incompatible_dimensions",
                "same_period_revisions": [],
                "new_period_changes": [],
                "added_observations": [],
                "removed_observations": [],
                "unchanged_count": 0,
                "missing_value_count": 0,
                "blockers": blockers,
                "conversion": conversion,
            }
        before = {item["period"]: item for item in left["observations"]}
        after = {item["period"]: item for item in right["observations"]}
        left_factor = (
            _decimal(conversion["left_multiplier"])
            if conversion
            else _decimal(left["scaling"])
        )
        right_factor = (
            _decimal(conversion["right_multiplier"])
            if conversion
            else _decimal(right["scaling"])
        )
        revisions, additions, removals, changes, unchanged, missing = (
            [],
            [],
            [],
            [],
            0,
            0,
        )
        for period in sorted(set(before) & set(after)):
            prior, current = before[period], after[period]
            if prior["value"] is None or current["value"] is None:
                missing += 1
            elif (
                _decimal(prior["value"]) * left_factor
                == _decimal(current["value"]) * right_factor
            ):
                unchanged += 1
            else:
                revisions.append(
                    self._calculation(
                        "same_period_revision",
                        prior,
                        current,
                        left_factor,
                        right_factor,
                        precision,
                        left,
                        right,
                    )
                )
        for period in sorted(set(after) - set(before)):
            current = after[period]
            additions.append(
                {
                    "period": period,
                    "observation": current,
                    "citation": self._citation(right, current),
                }
            )
            predecessors = [p for p in before if p < period]
            if predecessors:
                prior = before[max(predecessors)]
                if prior["value"] is not None and current["value"] is not None:
                    changes.append(
                        self._calculation(
                            "new_period_change",
                            prior,
                            current,
                            left_factor,
                            right_factor,
                            precision,
                            left,
                            right,
                        )
                    )
                else:
                    missing += 1
        for period in sorted(set(before) - set(after)):
            prior = before[period]
            removals.append(
                {
                    "period": period,
                    "observation": prior,
                    "citation": self._citation(left, prior),
                }
            )
        return {
            **identity,
            "status": "compared",
            "reason": None,
            "same_period_revisions": revisions,
            "new_period_changes": changes,
            "added_observations": additions,
            "removed_observations": removals,
            "unchanged_count": unchanged,
            "missing_value_count": missing,
            "blockers": blockers,
            "conversion": conversion,
        }

    @staticmethod
    def _citation(series, observation):
        return {
            "series_id": series["series_id"],
            "period": observation["period"],
            "observation_id": observation["observation_id"],
            "vintage_id": series["vintage_id"],
            "provider_release_id": series["provider_release_id"],
            "release_at_ms": series["release_at_ms"],
            "retrieved_at_ms": series["retrieved_at_ms"],
            "release_at_basis": series.get("release_at_basis"),
            "retrieved_at_basis": series.get("retrieved_at_basis"),
            "vintage_basis": series.get("vintage_basis"),
            "source_url": series["vintage_source_url"] or series["source_url"],
            "source_document_id": series["source_document_id"],
            "source_revision_id": series["source_revision_id"],
        }

    def _calculation(
        self,
        operation,
        prior,
        current,
        left_factor,
        right_factor,
        precision,
        left_series,
        right_series,
    ):
        before = _decimal(prior["value"]) * left_factor
        after = _decimal(current["value"]) * right_factor
        quantum = Decimal(1).scaleb(-precision)
        result = {
            "before": str(before),
            "after": str(after),
            "delta": str((after - before).quantize(quantum, rounding=ROUND_HALF_EVEN)),
            "rounding": "half-even",
            "precision": precision,
        }
        receipt = {
            "operation": operation,
            "inputs": [prior["observation_id"], current["observation_id"]],
            "formula": "right_value * right_multiplier - left_value * left_multiplier",
            "left_multiplier": str(left_factor),
            "right_multiplier": str(right_factor),
            "result": result,
        }
        return {
            "period_before": prior["period"],
            "period_after": current["period"],
            "receipt": {
                **receipt,
                "calculation_id": "economic-calculation:" + _hash(receipt)[:32],
            },
            "source_citations": [
                self._citation(left_series, prior),
                self._citation(right_series, current),
            ],
        }

    def inspect_comparison(
        self,
        namespace: str,
        comparison_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise EconomicReleaseError(
                "invalid_request", "offset and limit must be bounded"
            )
        comparison = self._get(
            "economic_release_comparisons",
            comparison_id,
            namespace,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **comparison,
            "items": comparison["items"][offset : offset + limit],
            "page": {
                "offset": offset,
                "limit": limit,
                "total": len(comparison["items"]),
            },
        }

    def export_comparison(
        self, namespace: str, comparison_id: str, *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        comparison = self._get(
            "economic_release_comparisons",
            comparison_id,
            namespace,
            principal_id=principal_id,
            scopes=scopes,
        )
        citations = {}
        lines = [
            f"# Economic release comparison: {comparison['left_release_id']} → {comparison['right_release_id']}",
            "",
            f"Pinned snapshots: {comparison['left_snapshot_id']} and {comparison['right_snapshot_id']}",
            "",
            "## Assumptions",
            "",
            *[f"- {a}" for a in comparison["assumptions"]],
            "",
            "## Findings",
            "",
        ]
        for item in comparison["items"]:
            lines.append(
                f"### {item['indicator_id']} ({item['geography'] or 'unspecified geography'})"
            )
            lines.append(
                f"Status: {item['status']}. Same-period revisions: {len(item['same_period_revisions'])}; new-period changes: {len(item['new_period_changes'])}; added: {len(item['added_observations'])}; removed: {len(item['removed_observations'])}."
            )
            for change in item["same_period_revisions"] + item["new_period_changes"]:
                receipt = change["receipt"]
                source_ids = []
                for source in change["source_citations"]:
                    identity = "source:" + _hash(source)[:24]
                    citations[identity] = source
                    source_ids.append(identity)
                lines.append(
                    f"- {change['period_before']} → {change['period_after']}: {receipt['result']['delta']} [calculation: {receipt['calculation_id']}] "
                    + " ".join(f"[{identity}]" for identity in source_ids)
                )
                citations[receipt["calculation_id"]] = receipt
            lines.append("")
            for kind in ("added_observations", "removed_observations"):
                for entry in item[kind]:
                    source = entry["citation"]
                    identity = "source:" + _hash(source)[:24]
                    citations[identity] = source
                    lines.append(
                        f"- {kind.replace('_', ' ')}: {entry['period']} [{identity}]"
                    )
        lines.extend(["## Citations", ""])
        for identity, citation in sorted(citations.items()):
            lines.append(f"- [{identity}] {_canonical(citation)}")
        lines.extend(
            ["", "## Limitations", "", *[f"- {x}" for x in comparison["limitations"]]]
        )
        return {
            "contract": "noesis-economic-comparison-export-v1",
            "comparison": comparison,
            "sha256": _hash(comparison),
            "markdown": "\n".join(lines),
            "citations": citations,
            "citation_semantics": "Source citations are pinned to captured vintages; calculation receipts show deterministic arithmetic.",
        }

    def create_report(
        self,
        namespace: str,
        request_key: str,
        comparison_id: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Author a versioned cited report from pinned calculations and source revisions.

        A dataset URL alone is insufficient for source-change monitoring. When a
        source revision was not retained, callers can still export the immutable
        derived comparison, but this authored report is explicitly unavailable.
        """
        from src.kb.authored_reports import AuthoredReportStore

        comparison = self._get(
            "economic_release_comparisons",
            comparison_id,
            namespace,
            principal_id=principal_id,
            scopes=scopes,
        )
        findings = [
            change
            for item in comparison["items"]
            for change in item["same_period_revisions"] + item["new_period_changes"]
        ]
        if len(findings) > 200:
            raise EconomicReleaseError(
                "budget_exceeded",
                "authored report supports at most 200 numeric findings",
            )
        if not findings:
            raise EconomicReleaseError(
                "no_findings", "comparison has no numeric findings to cite"
            )
        bibliography, assertions, generations = {}, [], []
        for index, finding in enumerate(findings):
            citations, dependencies = [], []
            for source in finding["source_citations"]:
                document_id, revision_id = (
                    source["source_document_id"],
                    source["source_revision_id"],
                )
                if not document_id or not revision_id:
                    raise EconomicReleaseError(
                        "source_revision_unavailable",
                        "authored report needs retained source revisions for each finding",
                    )
                _source_access(document_id, scopes)
                row = self.conn.execute(
                    "SELECT committed_watermark FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
                    [document_id, revision_id],
                ).fetchone()
                if row is None:
                    raise EconomicReleaseError(
                        "source_revision_unavailable",
                        "cited source revision is not retained",
                    )
                generations.append(int(row[0]))
                citation_id = "source:" + _hash(source)[:24]
                bibliography[citation_id] = {
                    "id": citation_id,
                    "text": f"{source['series_id']} {source['period']}, vintage {source['vintage_id']}, release {source['provider_release_id'] or 'unavailable'}, source {document_id}@{revision_id}, {source['source_url'] or 'URL unavailable'}",
                }
                citations.append(citation_id)
                dependency = {
                    "kind": "source",
                    "id": document_id,
                    "revision": revision_id,
                    "namespace": namespace,
                    "locator": {"document_id": document_id, "revision_id": revision_id},
                }
                if dependency not in dependencies:
                    dependencies.append(dependency)
            receipt = finding["receipt"]
            calculation_id = receipt["calculation_id"]
            bibliography[calculation_id] = {
                "id": calculation_id,
                "text": f"Calculation {calculation_id}: {receipt['formula']}; inputs {', '.join(receipt['inputs'])}; half-even rounding to {receipt['result']['precision']} decimal places; delta {receipt['result']['delta']}.",
            }
            citations.append(calculation_id)
            assertions.append(
                {
                    "id": f"finding:{index + 1}",
                    "kind": "sourced",
                    "text": f"{receipt['operation'].replace('_', ' ').capitalize()} for {finding['period_before']} to {finding['period_after']}: {receipt['result']['delta']}.",
                    "citations": citations,
                    "dependencies": dependencies,
                }
            )
        assumptions = (
            "; ".join(comparison["assumptions"])
            or "No additional assumptions were supplied."
        )
        assertions.append(
            {
                "id": "assumptions",
                "kind": "commentary",
                "text": f"Recorded comparison assumptions: {assumptions}",
                "citations": [],
                "dependencies": [],
            }
        )
        content = {
            "title": f"Economic release comparison: {comparison['left_release_id']} to {comparison['right_release_id']}",
            "snapshot": {
                "id": comparison_id,
                "generations": {namespace: max(generations)},
            },
            "sections": [
                {
                    "id": "comparison",
                    "title": "Pinned release findings",
                    "assertions": assertions,
                }
            ],
            "bibliography": list(bibliography.values()),
            "limitations": [
                *comparison["limitations"],
                "Source dependencies are monitored by the existing evidence-change assessment; calculations are immutable receipts in the pinned comparison.",
            ],
        }
        report = AuthoredReportStore(self.conn).create(
            namespace, request_key, content, principal_id=principal_id, scopes=scopes
        )
        return {
            "report": report,
            "comparison_id": comparison_id,
            "report_update_operation": "assess_authored_report_changes",
        }
