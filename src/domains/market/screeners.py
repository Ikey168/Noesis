"""Bounded, versioned market screeners over dated universe membership."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import hashlib
import json
import threading
import time
import uuid
from typing import Any

from src.domains.market.financial_facts import MarketFinancialFactStore
from src.domains.market.instruments import MarketInstrumentStore
from src.domains.market.prices import MarketPriceStore
from src.domains.market.entitlements import recheck_stored_receipt_rights, withheld_receipt

MAX_FILTERS = 16
MAX_RESULTS = 100
MAX_FACT_ROWS = 500
MAX_PRICE_ROWS = 500
SCREEN_READ_SCOPE = "market:screeners:read"
SCREEN_WRITE_SCOPE = "market:screeners:write"
_WRITE_LOCK = threading.RLock()

_DDL = """
CREATE TABLE IF NOT EXISTS market_screener_query_revisions (
 namespace TEXT NOT NULL,
 query_id TEXT NOT NULL,
 revision INTEGER NOT NULL,
 revision_id TEXT NOT NULL,
 owner TEXT,
 criteria_json TEXT NOT NULL,
 record_hash TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, query_id, revision),
 UNIQUE(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS market_screener_run_snapshots (
 namespace TEXT NOT NULL,
 run_id TEXT NOT NULL,
 query_revision_id TEXT NOT NULL,
 owner TEXT,
 as_of_json TEXT NOT NULL,
 result_json TEXT NOT NULL,
 record_hash TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, run_id)
);
CREATE INDEX IF NOT EXISTS idx_market_screener_queries
 ON market_screener_query_revisions(namespace, query_id, recorded_at_ms);
CREATE INDEX IF NOT EXISTS idx_market_screener_runs
 ON market_screener_run_snapshots(namespace, recorded_at_ms);
"""


class MarketScreenerError(ValueError):
    """Typed screener failure safe to return through shared adapters."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def ensure_market_screener_schema(conn: Any) -> None:
    conn.execute(_DDL)


def _text(value: Any, name: str, *, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketScreenerError("invalid_request", f"{name} must be bounded text")
    return value.strip()


def _millis(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketScreenerError("invalid_request", f"{name} must be nonnegative milliseconds")
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketScreenerError("invalid_request", "numeric screener values must be finite") from exc


def _authorize(namespace: str, principal_id: str, scopes: set[str], *, write: bool) -> None:
    _text(namespace, "namespace", limit=100)
    _text(principal_id, "principal_id", limit=200)
    if "operator" in scopes:
        return
    required = SCREEN_WRITE_SCOPE if write else SCREEN_READ_SCOPE
    namespace_scope = f"namespace:{namespace}:write" if write else f"namespace:{namespace}:read"
    if required not in scopes or namespace_scope not in scopes:
        raise MarketScreenerError("unauthorized", "market screener access is required")


def _owner(owner: str | None, principal_id: str, scopes: set[str]) -> str | None:
    if owner is None and "operator" not in scopes:
        return principal_id
    if owner not in (None, principal_id) and "operator" not in scopes:
        raise MarketScreenerError("unauthorized", "screener belongs to another principal")
    return owner


def _fact_concept(fact: Mapping[str, Any]) -> str:
    return str(fact.get("canonical_concept") or fact.get("concept") or "").casefold()


def _fact_value(fact: Mapping[str, Any]) -> Decimal:
    value = _decimal(fact.get("value_lexical"))
    scale = fact.get("scale", 0)
    if type(scale) is not int or abs(scale) > 24:
        raise MarketScreenerError("unsupported_scale", "fact scale is outside the safe bound")
    return value * (Decimal(10) ** scale)


def _compare(observed: Any, operator: str, expected: Any) -> bool:
    if operator not in {"eq", "ne", "gt", "gte", "lt", "lte", "in"}:
        raise MarketScreenerError("invalid_request", "unsupported screener operator")
    if operator == "in":
        if not isinstance(expected, list) or len(expected) > 32:
            raise MarketScreenerError("invalid_request", "in expects a bounded list")
        return observed in expected
    if isinstance(observed, (int, float, Decimal)) and isinstance(expected, (int, float, Decimal, str)):
        left, right = _decimal(observed), _decimal(expected)
    else:
        left, right = str(observed), str(expected)
    return {
        "eq": left == right,
        "ne": left != right,
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
    }[operator]


class MarketScreenerStore:
    """Persist versioned criteria and reproducible point-in-time run receipts."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_screener_schema(conn)

    @staticmethod
    def _validate_criteria(criteria: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(criteria, Mapping):
            raise MarketScreenerError("invalid_request", "criteria must be an object")
        filters = criteria.get("filters", [])
        if not isinstance(filters, list) or not 1 <= len(filters) <= MAX_FILTERS:
            raise MarketScreenerError("invalid_request", "criteria.filters must contain 1-16 filters")
        normalized: list[dict[str, Any]] = []
        for raw in filters:
            if not isinstance(raw, Mapping):
                raise MarketScreenerError("invalid_request", "each screener filter must be an object")
            field = _text(raw.get("field"), "filter.field", limit=120)
            operator = _text(raw.get("operator"), "filter.operator", limit=8).lower()
            if "value" not in raw:
                raise MarketScreenerError("invalid_request", "filter.value is required")
            value = raw["value"]
            if isinstance(value, (dict, tuple, set)):
                raise MarketScreenerError("invalid_request", "filter.value must be JSON scalar or list")
            if operator == "in" and (not isinstance(value, list) or len(value) > 32):
                raise MarketScreenerError("invalid_request", "in filter values are bounded lists")
            if not field.startswith(("security.", "listing.", "price.", "fact:")):
                raise MarketScreenerError("unsupported_filter", "unsupported market screener field")
            normalized.append({"field": field, "operator": operator, "value": value})
        ranking = criteria.get("ranking")
        normalized_ranking = None
        if ranking is not None:
            if not isinstance(ranking, Mapping):
                raise MarketScreenerError("invalid_request", "ranking must be an object")
            normalized_ranking = {
                "field": _text(ranking.get("field"), "ranking.field", limit=120),
                "direction": _text(ranking.get("direction", "desc"), "ranking.direction", limit=4).lower(),
            }
            if normalized_ranking["direction"] not in {"asc", "desc"}:
                raise MarketScreenerError("invalid_request", "ranking.direction must be asc or desc")
            if not normalized_ranking["field"].startswith(("security.", "listing.", "price.", "fact:")):
                raise MarketScreenerError("unsupported_filter", "unsupported screener ranking field")
        missing_policy = _text(criteria.get("missing_policy", "exclude"), "missing_policy", limit=10).lower()
        if missing_policy not in {"exclude", "include", "error"}:
            raise MarketScreenerError("invalid_request", "missing_policy must be exclude, include, or error")
        raw_limit = criteria.get("limit", MAX_RESULTS)
        if type(raw_limit) is not int:
            raise MarketScreenerError("invalid_request", "criteria.limit must be an integer")
        stale_after_ms = criteria.get("stale_after_ms")
        if stale_after_ms is not None:
            stale_after_ms = _millis(stale_after_ms, "stale_after_ms")
        return {
            "filters": normalized,
            "ranking": normalized_ranking,
            "missing_policy": missing_policy,
            "limit": min(max(raw_limit, 1), MAX_RESULTS),
            "stale_after_ms": stale_after_ms,
        }

    def save_query(
        self,
        namespace: str,
        query_id: str,
        criteria: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        owner: str | None = None,
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        query_id = _text(query_id, "query_id", limit=200)
        owner = _owner(owner, principal_id, scopes)
        normalized = self._validate_criteria(criteria)
        previous = self.conn.execute(
            """SELECT revision,revision_id,owner,criteria_json,record_hash,recorded_at_ms
               FROM market_screener_query_revisions
               WHERE namespace=? AND query_id=? ORDER BY revision DESC LIMIT 1""",
            [namespace, query_id],
        ).fetchone()
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        if previous and previous[3] == encoded:
            return {
                "contract": "noesis-market-screener-query-v1",
                "namespace": namespace,
                "query_id": query_id,
                "revision": int(previous[0]),
                "revision_id": previous[1],
                "owner": previous[2],
                "criteria": normalized,
                "recorded_at_ms": int(previous[5]),
                "record_hash": previous[4],
            }
        revision = int(previous[0]) + 1 if previous else 1
        recorded_at_ms = int(self.now())
        revision_id = f"market-screener-query:{query_id}@{revision}"
        payload = {
            "contract": "noesis-market-screener-query-v1",
            "namespace": namespace,
            "query_id": query_id,
            "revision": revision,
            "revision_id": revision_id,
            "owner": owner,
            "criteria": normalized,
            "recorded_at_ms": recorded_at_ms,
        }
        record_hash = _digest(payload)
        with _WRITE_LOCK:
            self.conn.execute(
                """INSERT INTO market_screener_query_revisions
                   (namespace,query_id,revision,revision_id,owner,criteria_json,record_hash,recorded_at_ms)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [namespace, query_id, revision, revision_id, owner, encoded, record_hash, recorded_at_ms],
            )
        return {**payload, "record_hash": record_hash}

    def _latest_query(
        self,
        namespace: str,
        query_id: str,
        *,
        acquired_by_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT revision,revision_id,owner,criteria_json,record_hash,recorded_at_ms
               FROM market_screener_query_revisions
               WHERE namespace=? AND query_id=? AND recorded_at_ms<=?
               ORDER BY revision DESC LIMIT 1""",
            [namespace, query_id, acquired_by_ms],
        ).fetchone()
        if row is None:
            return None
        if row[2] not in (None, principal_id) and "operator" not in scopes:
            raise MarketScreenerError("not_found", "saved screener query is unavailable")
        return {
            "contract": "noesis-market-screener-query-v1",
            "namespace": namespace,
            "query_id": query_id,
            "revision": int(row[0]),
            "revision_id": row[1],
            "owner": row[2],
            "criteria": json.loads(row[3]),
            "record_hash": row[4],
            "recorded_at_ms": int(row[5]),
        }

    def _value(
        self,
        field: str,
        *,
        identity: Mapping[str, Any],
        bars: Sequence[Mapping[str, Any]],
        facts: Sequence[Mapping[str, Any]],
    ) -> tuple[Any, dict[str, Any] | None]:
        if field.startswith("security."):
            return identity["security"].get(field.split(".", 1)[1]), identity["security"]
        if field.startswith("listing."):
            return identity["listing"].get(field.split(".", 1)[1]), identity["listing"]
        if field == "price.close":
            if not bars:
                return None, None
            latest = max(bars, key=lambda item: (int(item.get("bar_start_ms", 0)), str(item.get("revision_id", ""))))
            return latest.get("close"), latest
        if field.startswith("fact:"):
            concept = field.split(":", 1)[1].casefold()
            candidates = [item for item in facts if _fact_concept(item) == concept]
            if not candidates:
                return None, None
            latest = max(candidates, key=lambda item: (str(item.get("period", {}).get("end_date", "")), int(item.get("public_at_ms") or 0), str(item.get("revision_id", ""))))
            return _fact_value(latest), latest
        raise MarketScreenerError("unsupported_filter", "unsupported screener field")

    def run(
        self,
        namespace: str,
        *,
        universe_id: str,
        as_of_ms: int,
        start_ms: int,
        end_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int,
        principal_id: str,
        scopes: set[str],
        criteria: Mapping[str, Any] | None = None,
        query_id: str | None = None,
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        universe_id = _text(universe_id, "universe_id", limit=200)
        as_of_ms, start_ms, end_ms = (
            _millis(as_of_ms, "as_of_ms"),
            _millis(start_ms, "start_ms"),
            _millis(end_ms, "end_ms"),
        )
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        public_cutoff_ms = _millis(publicly_available_by_ms, "publicly_available_by_ms")
        if as_of_ms > acquired_by_ms or public_cutoff_ms > acquired_by_ms or start_ms >= end_ms:
            raise MarketScreenerError("invalid_request", "screen dates and cutoffs are inconsistent")
        if criteria is None:
            if query_id is None:
                raise MarketScreenerError("invalid_request", "criteria or query_id is required")
            query = self._latest_query(
                namespace,
                _text(query_id, "query_id", limit=200),
                acquired_by_ms=acquired_by_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            if query is None:
                raise MarketScreenerError("not_found", "saved screener query is unavailable")
        else:
            query = self.save_query(
                namespace,
                _text(query_id or f"screen:{uuid.uuid4().hex}", "query_id", limit=200),
                criteria,
                principal_id=principal_id,
                scopes=scopes,
            )
        normalized = self._validate_criteria(query["criteria"])
        limit = int(normalized["limit"])
        instruments = MarketInstrumentStore(self.conn, initialize=False)
        prices = MarketPriceStore(self.conn, initialize=False)
        facts_store = MarketFinancialFactStore(self.conn, initialize=False)
        members = instruments.list_universe_members(
            namespace,
            universe_id,
            as_of_ms=as_of_ms,
            acquired_by_ms=acquired_by_ms,
            publicly_available_by_ms=public_cutoff_ms,
            principal_id=principal_id,
            scopes=scopes,
        )
        rows: list[dict[str, Any]] = []
        for member in members[:MAX_RESULTS]:
            security_id = str(member["security"]["security_id"])
            listing_candidates = instruments.list_listings_for_security(
                namespace,
                security_id,
                as_of_ms=as_of_ms,
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
                limit=4,
            )
            if not listing_candidates:
                rows.append({"security_id": security_id, "state": "missing", "checks": [{"reason": "no_valid_listing"}]})
                continue
            identity = listing_candidates[0]
            listing_id = str(identity["listing"]["listing_id"])
            try:
                price_page = prices.get_bars(
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
                bars = list(price_page["items"])
                facts_page = facts_store.get_facts(
                    namespace,
                    identity["issuer"]["issuer_id"],
                    acquired_by_ms=acquired_by_ms,
                    publicly_available_by_ms=public_cutoff_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                    limit=MAX_FACT_ROWS,
                    include_page=True,
                )
                facts = list(facts_page["items"])
            except Exception as exc:
                if normalized["missing_policy"] == "error":
                    raise MarketScreenerError("data_unavailable", "screen input is unavailable", security_id=security_id) from exc
                rows.append({"security_id": security_id, "listing_id": listing_id, "state": "missing", "checks": [{"reason": "input_unavailable", "error": {"code": getattr(exc, "code", "data_unavailable"), "message": str(exc)[:300]}}]})
                continue
            checks: list[dict[str, Any]] = []
            missing = False
            latest_bar = max(
                bars,
                key=lambda item: (int(item.get("bar_start_ms", 0)), str(item.get("revision_id", ""))),
                default=None,
            )
            stale_price = bool(
                latest_bar
                and normalized.get("stale_after_ms") is not None
                and as_of_ms - int(latest_bar.get("bar_start_ms", 0)) > int(normalized["stale_after_ms"])
            )
            for criterion in normalized["filters"]:
                if criterion["field"] == "price.close" and stale_price:
                    observed, record = None, {"stale": True}
                else:
                    observed, record = self._value(
                        criterion["field"], identity=identity, bars=bars, facts=facts
                    )
                if observed is None:
                    if record and record.get("stale") and normalized["missing_policy"] == "error":
                        raise MarketScreenerError("data_unavailable", "screen price input is stale", security_id=security_id)
                    missing = True
                    checks.append({**criterion, "status": "missing", "reason": "stale_data" if record and record.get("stale") else "input_not_available"})
                    continue
                passed = _compare(observed, criterion["operator"], criterion["value"])
                checks.append(
                    {
                        **criterion,
                        "status": "passed" if passed else "failed",
                        "observed": str(observed) if isinstance(observed, Decimal) else observed,
                        "input_revision_ids": [record["revision_id"]] if record and record.get("revision_id") else [],
                    }
                )
            if missing and normalized["missing_policy"] == "exclude":
                state = "excluded_missing"
            elif missing and normalized["missing_policy"] == "include":
                state = "missing"
            else:
                state = "passed" if all(item["status"] == "passed" for item in checks) else "failed"
            rows.append(
                {
                    "security_id": security_id,
                    "listing_id": listing_id,
                    "issuer_id": identity["issuer"]["issuer_id"],
                    "state": state,
                    "checks": checks,
                    "identity_revision_ids": [
                        identity[item]["revision_id"]
                        for item in ("issuer", "security", "listing")
                        if identity[item].get("revision_id")
                    ],
                }
            )
        ranking = normalized.get("ranking")
        if ranking:
            def rank_key(row: Mapping[str, Any]):
                for check in row.get("checks", []):
                    if check.get("field") == ranking["field"]:
                        observed = check.get("observed")
                        try:
                            return _decimal(observed)
                        except MarketScreenerError:
                            return str(observed or "")
                return Decimal("-Infinity") if ranking["direction"] == "desc" else Decimal("Infinity")
            rows.sort(key=rank_key, reverse=ranking["direction"] == "desc")
        rows = rows[:limit]
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        as_of = {
            "universe_id": universe_id,
            "as_of_ms": as_of_ms,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "publicly_available_by_ms": public_cutoff_ms,
            "acquired_by_ms": acquired_by_ms,
        }
        result = {
            "contract": "noesis-market-screener-run-v1",
            "namespace": namespace,
            "query_revision_id": query["revision_id"],
            "criteria": normalized,
            "as_of": as_of,
            "results": rows,
            "counts": counts,
            "input_snapshot": {
                "universe_membership_count": len(members),
                "result_count": len(rows),
                "historical_membership_applied": True,
                "input_revision_ids": sorted({
                    revision_id
                    for row in rows
                    for check in row.get("checks", [])
                    for revision_id in check.get("input_revision_ids", [])
                }),
            },
        }
        run_id = f"market-screener-run:{_digest({'query_revision_id': query['revision_id'], 'as_of': as_of})[:32]}"
        result["run_id"] = run_id
        encoded_result = json.dumps(result, sort_keys=True, separators=(",", ":"))
        record_hash = _digest(result)
        with _WRITE_LOCK:
            self.conn.execute(
                """INSERT INTO market_screener_run_snapshots
                   (namespace,run_id,query_revision_id,owner,as_of_json,result_json,record_hash,recorded_at_ms)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(namespace,run_id) DO UPDATE SET result_json=excluded.result_json,
                     record_hash=excluded.record_hash, recorded_at_ms=excluded.recorded_at_ms""",
                [namespace, run_id, query["revision_id"], query.get("owner"), json.dumps(as_of, sort_keys=True), encoded_result, record_hash, int(self.now())],
            )
        return result

    def _run_result(
        self,
        namespace: str,
        run_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        operation: str = "derive",
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=False)
        row = self.conn.execute(
            "SELECT owner,result_json,record_hash,recorded_at_ms FROM market_screener_run_snapshots WHERE namespace=? AND run_id=?",
            [namespace, _text(run_id, "run_id", limit=200)],
        ).fetchone()
        if row is None or row[0] not in (None, principal_id) and "operator" not in scopes:
            raise MarketScreenerError("not_found", "screener run is unavailable")
        result = json.loads(row[1])
        rights = recheck_stored_receipt_rights(
            self.conn, namespace, result, operation=operation,
            principal_id=principal_id, scopes=scopes, now_ms=int(self.now()),
        )
        if rights["state"] == "withheld":
            return withheld_receipt(
                "noesis-market-screener-run-v1", "screener_run", run_id, row[2], rights
            )
        return {**result, "record_hash": row[2], "recorded_at_ms": int(row[3]), "current_rights": rights}

    def inspect_run(
        self,
        namespace: str,
        run_id: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        return self._run_result(namespace, run_id, principal_id=principal_id, scopes=scopes)

    def export_run(
        self,
        namespace: str,
        run_id: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        result = self._run_result(
            namespace, run_id, principal_id=principal_id, scopes=scopes, operation="export"
        )
        if result.get("withheld"):
            return {**result, "contract": "noesis-market-screener-export-v1", "results": []}
        permitted = [
            row for row in result.get("results", []) if row.get("state") == "passed"
        ]
        return {
            "contract": "noesis-market-screener-export-v1",
            "namespace": namespace,
            "run_id": run_id,
            "query_revision_id": result.get("query_revision_id"),
            "as_of": result.get("as_of"),
            "criteria": result.get("criteria"),
            "results": permitted,
            "excluded_result_count": len(result.get("results", [])) - len(permitted),
            "input_revision_ids": result.get("input_snapshot", {}).get("input_revision_ids", []),
            "rights_checked_at_run": True,
            "current_rights": result.get("current_rights"),
            "limitations": [
                "The export contains permitted derived screen results and revision locators; it does not redistribute provider source payloads.",
                "Rows missing inputs or failing criteria are not exported.",
            ],
        }


__all__ = [
    "MAX_FILTERS",
    "MAX_RESULTS",
    "MarketScreenerError",
    "MarketScreenerStore",
    "ensure_market_screener_schema",
]
