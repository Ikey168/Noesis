"""Current provider-rights policy for market source and derived data."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

ENTITLEMENT_CONTRACT = "noesis-market-entitlement-v1"
READ_SCOPE = "market:entitlements:read"
ADMIN_SCOPE = "market:entitlements:admin"
_CAPABILITIES = {
    "ingest",
    "read",
    "display",
    "retain",
    "derive",
    "cache",
    "evidence",
    "export",
    "redistribute",
}
_DDL = """
CREATE TABLE IF NOT EXISTS market_entitlement_revisions (
 namespace TEXT NOT NULL, entitlement_id TEXT NOT NULL, revision INTEGER NOT NULL,
 revision_id TEXT NOT NULL, policy_json TEXT NOT NULL, record_hash TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, entitlement_id, revision),
 UNIQUE(namespace, revision_id));
CREATE INDEX IF NOT EXISTS idx_market_entitlement_latest
 ON market_entitlement_revisions(namespace, entitlement_id, revision);
CREATE TABLE IF NOT EXISTS market_entitlement_purge_events (
 purge_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, object_kind TEXT NOT NULL,
 object_id TEXT NOT NULL, revision_id TEXT NOT NULL, record_hash TEXT NOT NULL,
 entitlement_ids_json TEXT NOT NULL, reason_code TEXT NOT NULL,
 purged_at_ms BIGINT NOT NULL);
"""


class MarketEntitlementError(ValueError):
    """Typed, payload-free market rights failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def ensure_market_entitlement_schema(conn: Any) -> None:
    conn.execute(_DDL)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MarketEntitlementError("invalid_request", "policy must be JSON-safe") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketEntitlementError(
            "invalid_request", f"{name} must be bounded nonempty text"
        )
    return value.strip()


def _millis(value: Any, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or value < 0:
        raise MarketEntitlementError("invalid_request", f"{name} must be epoch milliseconds")
    return value


class MarketEntitlementStore:
    """Append-only provider license metadata and current operation decisions.

    Rights are namespace-scoped and resolved at use time. Operator access can
    satisfy the Noesis scope check, but cannot override a provider policy.
    """

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_entitlement_schema(conn)

    @staticmethod
    def _authorize_admin(namespace: str, principal_id: str, scopes: set[str]) -> None:
        if "operator" in scopes:
            return
        if (
            ADMIN_SCOPE not in scopes
            or f"namespace:{namespace}:write" not in scopes
        ):
            raise MarketEntitlementError(
                "unauthorized", "market entitlement administration is required"
            )

    def _latest(self, namespace: str, entitlement_id: str):
        if "market_entitlement_revisions" not in _existing_tables(self.conn):
            return None
        row = self.conn.execute(
            "SELECT policy_json,record_hash FROM market_entitlement_revisions "
            "WHERE namespace=? AND entitlement_id=? ORDER BY revision DESC LIMIT 1",
            [namespace, entitlement_id],
        ).fetchone()
        return None if row is None else (json.loads(row[0]), str(row[1]))

    def put_entitlement(
        self,
        namespace: str,
        entitlement_id: str,
        *,
        provider: str,
        license_id: str,
        capabilities: list[str] | tuple[str, ...] | set[str],
        evidence_ref: str,
        decision_ref: str,
        principal_id: str,
        scopes: set[str],
        status: str = "active",
        effective_at_ms: int | None = None,
        expires_at_ms: int | None = None,
        max_retention_ms: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Append a reviewed policy revision; never store credentials or terms text."""

        namespace = _text(namespace, "namespace", 100)
        principal_id = _text(principal_id, "principal_id", 200)
        self._authorize_admin(namespace, principal_id, scopes)
        entitlement_id = _text(entitlement_id, "entitlement_id", 200)
        provider = _text(provider, "provider", 100)
        license_id = _text(license_id, "license_id", 200)
        evidence_ref = _text(evidence_ref, "evidence_ref", 500)
        decision_ref = _text(decision_ref, "decision_ref", 200)
        if status not in {"active", "revoked"}:
            raise MarketEntitlementError("invalid_request", "status must be active or revoked")
        if not isinstance(capabilities, (list, tuple, set)):
            raise MarketEntitlementError("invalid_request", "capabilities must be a bounded list")
        caps = sorted({_text(item, "capability", 40) for item in capabilities})
        if len(caps) > len(_CAPABILITIES) or set(caps) - _CAPABILITIES:
            raise MarketEntitlementError("invalid_request", "unsupported market capability")
        effective_at_ms = _millis(
            self.now() if effective_at_ms is None else effective_at_ms,
            "effective_at_ms",
        )
        expires_at_ms = _millis(expires_at_ms, "expires_at_ms", optional=True)
        max_retention_ms = _millis(
            max_retention_ms, "max_retention_ms", optional=True
        )
        if expires_at_ms is not None and expires_at_ms <= effective_at_ms:
            raise MarketEntitlementError("invalid_request", "expiry must follow policy effectiveness")
        if max_retention_ms == 0:
            raise MarketEntitlementError("invalid_request", "max_retention_ms must be positive")
        if max_retention_ms is not None and "retain" not in caps:
            raise MarketEntitlementError(
                "invalid_request", "a retention duration requires retain capability"
            )
        current = self._latest(namespace, entitlement_id)
        current_revision = 0 if current is None else int(current[0]["revision"])
        if expected_revision is not None and expected_revision != current_revision:
            raise MarketEntitlementError(
                "revision_conflict",
                "entitlement policy changed since it was reviewed",
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        body = {
            "contract": ENTITLEMENT_CONTRACT,
            "namespace": namespace,
            "entitlement_id": entitlement_id,
            "provider": provider,
            "license_id": license_id,
            "status": status,
            "capabilities": caps,
            "effective_at_ms": effective_at_ms,
            "expires_at_ms": expires_at_ms,
            "max_retention_ms": max_retention_ms,
            "evidence_ref": evidence_ref,
            "decision_ref": decision_ref,
            "reviewer_id": principal_id,
        }
        if current:
            if current[0]["provider"] != provider or current[0]["license_id"] != license_id:
                raise MarketEntitlementError(
                    "entitlement_identity_conflict",
                    "provider and license identity require a new entitlement_id",
                )
            if {key: current[0].get(key) for key in body} == body:
                return {**current[0], "record_hash": current[1], "idempotent": True}
        revision = current_revision + 1
        now_ms = _millis(self.now(), "recorded_at_ms")
        policy = {
            **body,
            "revision": revision,
            "revision_id": f"market-entitlement:{_digest([namespace, entitlement_id])[:24]}@{revision}",
            "recorded_at_ms": now_ms,
        }
        record_hash = _digest(policy)
        policy["record_hash"] = record_hash
        self.conn.execute(
            "INSERT INTO market_entitlement_revisions VALUES (?,?,?,?,?,?,?)",
            [namespace, entitlement_id, revision, policy["revision_id"], _canonical(policy), record_hash, now_ms],
        )
        return policy

    def inspect_entitlement(
        self,
        namespace: str,
        entitlement_id: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        namespace = _text(namespace, "namespace", 100)
        if "operator" not in scopes and not (
            (ADMIN_SCOPE in scopes and f"namespace:{namespace}:write" in scopes)
            or (
                READ_SCOPE in scopes
                and f"namespace:{namespace}:read" in scopes
            )
        ):
            raise MarketEntitlementError("unauthorized", "market entitlement read access is required")
        latest = self._latest(namespace, _text(entitlement_id, "entitlement_id", 200))
        if latest is None:
            raise MarketEntitlementError("entitlement_unavailable", "entitlement is unavailable")
        policy, record_hash = latest
        if _digest({key: value for key, value in policy.items() if key != "record_hash"}) != record_hash:
            raise MarketEntitlementError("entitlement_integrity_error", "entitlement policy hash is invalid")
        return policy

    def authorize_sources(
        self,
        namespace: str,
        source_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        operation: str,
        principal_id: str,
        scopes: set[str],
        external: bool = False,
        now_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Require a current policy for every source backing an operation."""

        if operation not in {"ingest", "read", "display", "derive", "cache", "evidence", "export", "retain"}:
            raise MarketEntitlementError("invalid_request", "unsupported entitlement operation")
        if not isinstance(source_refs, (list, tuple)) or not source_refs:
            raise MarketEntitlementError("entitlement_unavailable", "source provenance is required")
        namespace = _text(namespace, "namespace", 100)
        principal_id = _text(principal_id, "principal_id", 200)
        now_ms = _millis(self.now() if now_ms is None else now_ms, "now_ms")
        required = {
            "ingest": {"ingest", "retain"},
            "read": {"read", "retain"},
            "display": {"read", "display", "retain"},
            "derive": {"read", "derive", "retain"},
            "cache": {"read", "cache", "retain"},
            "evidence": {"read", "evidence", "retain"},
            "export": {"read", "export", "retain"},
            "retain": {"retain"},
        }[operation]
        if external and operation == "export":
            required = required | {"redistribute"}
        decisions = []
        for ref in source_refs:
            if not isinstance(ref, dict):
                raise MarketEntitlementError("entitlement_unavailable", "source provenance is invalid")
            entitlement_id = _text(ref.get("entitlement_id"), "entitlement_id", 200)
            license_id = _text(ref.get("license_id"), "license_id", 200)
            provider = _text(ref.get("provider"), "provider", 100)
            latest = self._latest(namespace, entitlement_id)
            if latest is None:
                raise MarketEntitlementError(
                    "entitlement_unavailable",
                    "a current provider entitlement policy is missing",
                    entitlement_id=entitlement_id,
                )
            policy, policy_hash = latest
            if _digest({key: value for key, value in policy.items() if key != "record_hash"}) != policy_hash:
                raise MarketEntitlementError("entitlement_integrity_error", "entitlement policy hash is invalid")
            if policy["license_id"] != license_id or policy["provider"] != provider:
                raise MarketEntitlementError(
                    "entitlement_source_mismatch",
                    "source provider or license does not match its entitlement",
                    entitlement_id=entitlement_id,
                )
            if (
                policy["status"] != "active"
                or now_ms < int(policy["effective_at_ms"])
                or (policy.get("expires_at_ms") is not None and now_ms >= int(policy["expires_at_ms"]))
            ):
                raise MarketEntitlementError(
                    "entitlement_revoked",
                    "the current provider entitlement is inactive",
                    entitlement_id=entitlement_id,
                )
            missing = sorted(required - set(policy["capabilities"]))
            if missing:
                raise MarketEntitlementError(
                    "operation_restricted",
                    "the provider policy does not permit this operation",
                    entitlement_id=entitlement_id,
                    missing_capabilities=missing,
                )
            if "operator" not in scopes and f"market:entitlement:{entitlement_id}:{operation}" not in scopes:
                raise MarketEntitlementError(
                    "entitlement_unavailable",
                    "current source entitlement access is required",
                    entitlement_id=entitlement_id,
                )
            retention_expires_at_ms = None
            max_retention_ms = policy.get("max_retention_ms")
            if max_retention_ms is not None:
                retrieved_at_ms = _millis(
                    ref.get("retrieved_at_ms"), "source retrieved_at_ms", optional=True
                )
                if retrieved_at_ms is None or retrieved_at_ms > now_ms:
                    raise MarketEntitlementError(
                        "retention_clock_unavailable",
                        "source retrieval time is required by the retention policy",
                        entitlement_id=entitlement_id,
                    )
                retention_expires_at_ms = retrieved_at_ms + int(max_retention_ms)
                if now_ms >= retention_expires_at_ms:
                    raise MarketEntitlementError(
                        "retention_expired",
                        "the source retention period has expired",
                        entitlement_id=entitlement_id,
                        retention_expires_at_ms=retention_expires_at_ms,
                    )
            decisions.append(
                {
                    "source_ref_id": str(ref.get("source_ref_id") or ""),
                    "entitlement_id": entitlement_id,
                    "license_id": license_id,
                    "policy_revision_id": policy["revision_id"],
                    "operation": operation,
                    "allowed": True,
                    "retention_expires_at_ms": retention_expires_at_ms,
                }
            )
        return decisions

    def purge_derived_receipts(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
        max_rows_per_table: int = 10_000,
        after_id_by_kind: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Delete stored derived receipts whose sources may no longer be retained.

        Run after :meth:`purge_expired_source_revisions`: a receipt referencing a
        purged, revoked or retention-expired source is deleted and leaves only a
        tombstone (kind, ID, hash of the removed payload, reason code).
        Receipts built only from caller-supplied inputs are kept.
        """

        namespace = _text(namespace, "namespace", 100)
        principal_id = _text(principal_id, "principal_id", 200)
        self._authorize_admin(namespace, principal_id, scopes)
        if type(max_rows_per_table) is not int or not 1 <= max_rows_per_table <= 100_000:
            raise MarketEntitlementError(
                "invalid_request", "max_rows_per_table must be between one and 100000"
            )
        return _purge_derived_receipts(
            self,
            namespace,
            principal_id=principal_id,
            scopes=scopes,
            max_rows_per_table=max_rows_per_table,
            after_id_by_kind=after_id_by_kind,
        )

    def run_retention(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
        max_rows_per_table: int = 10_000,
    ) -> dict[str, Any]:
        """One bounded retention pass: source revisions, then derived receipts."""

        sources = self.purge_expired_source_revisions(
            namespace, principal_id=principal_id, scopes=scopes,
            max_rows_per_table=max_rows_per_table,
        )
        derived = self.purge_derived_receipts(
            namespace, principal_id=principal_id, scopes=scopes,
            max_rows_per_table=max_rows_per_table,
        )
        return {
            "contract": "noesis-market-retention-run-v1",
            "namespace": namespace,
            "source_revisions": sources,
            "derived_receipts": derived,
            "complete": not sources["more_to_scan"] and not derived["more_to_scan"],
        }

    def purge_expired_source_revisions(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
        max_rows_per_table: int = 50_000,
        after_revision_id_by_kind: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Remove stored source revisions whose current policy no longer permits retention.

        A bounded tombstone keeps only stable Noesis IDs, the source hash and the
        policy failure code. It never copies the rejected source payload.
        """

        namespace = _text(namespace, "namespace", 100)
        principal_id = _text(principal_id, "principal_id", 200)
        self._authorize_admin(namespace, principal_id, scopes)
        if type(max_rows_per_table) is not int or not 1 <= max_rows_per_table <= 100_000:
            raise MarketEntitlementError(
                "invalid_request", "max_rows_per_table must be between one and 100000"
            )
        cursors = dict(after_revision_id_by_kind or {})
        allowed_kinds = set(SOURCE_REVISION_TABLES)
        if set(cursors) - allowed_kinds:
            raise MarketEntitlementError("invalid_request", "purge cursor kind is unsupported")
        cursors = {
            kind: _text(value, f"{kind} revision cursor", 300)
            for kind, value in cursors.items()
        }
        tables = _existing_tables(self.conn)
        targets = SOURCE_REVISION_TABLES
        now_ms = _millis(self.now(), "now_ms")
        scanned: dict[str, int] = {}
        purged = []
        more_to_scan = []
        next_cursors = {}
        for object_kind, (table, identity_column) in targets.items():
            if table not in tables:
                continue
            rows = self.conn.execute(
                f"SELECT {identity_column},revision_id,payload_json,record_hash "
                f"FROM {table} WHERE namespace=? AND revision_id>? ORDER BY revision_id LIMIT ?",
                [namespace, cursors.get(object_kind, ""), max_rows_per_table],
            ).fetchall()
            scanned[object_kind] = len(rows)
            if len(rows) == max_rows_per_table:
                more_to_scan.append(object_kind)
                next_cursors[object_kind] = str(rows[-1][1])
            for object_id, revision_id, encoded, record_hash in rows:
                payload = json.loads(encoded)
                source_refs = payload.get("source_refs") or []
                if not source_refs:
                    continue
                try:
                    self.authorize_sources(
                        namespace,
                        source_refs,
                        operation="retain",
                        principal_id=principal_id,
                        scopes=scopes,
                        now_ms=now_ms,
                    )
                except MarketEntitlementError as exc:
                    entitlement_ids = sorted(
                        {
                            str(ref.get("entitlement_id"))
                            for ref in source_refs
                            if ref.get("entitlement_id")
                        }
                    )
                    purge_id = "market-entitlement-purge:" + _digest(
                        [namespace, object_kind, revision_id, record_hash]
                    )[:32]
                    self.conn.execute(
                        "INSERT OR IGNORE INTO market_entitlement_purge_events "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        [
                            purge_id,
                            namespace,
                            object_kind,
                            str(object_id),
                            str(revision_id),
                            str(record_hash),
                            _canonical(entitlement_ids),
                            exc.code,
                            now_ms,
                        ],
                    )
                    self.conn.execute(
                        f"DELETE FROM {table} WHERE namespace=? AND revision_id=?",
                        [namespace, revision_id],
                    )
                    if object_kind == "instrument" and "market_instrument_alias_assertions" in tables:
                        self.conn.execute(
                            "DELETE FROM market_instrument_alias_assertions "
                            "WHERE namespace=? AND revision_id=?",
                            [namespace, revision_id],
                        )
                    purged.append(
                        {
                            "object_kind": object_kind,
                            "object_id": str(object_id),
                            "revision_id": str(revision_id),
                            "record_hash": str(record_hash),
                            "entitlement_ids": entitlement_ids,
                            "reason_code": exc.code,
                        }
                    )
        return {
            "contract": "noesis-market-entitlement-purge-report-v1",
            "namespace": namespace,
            "scope": "source_revisions",
            "scanned_by_kind": dict(sorted(scanned.items())),
            "purged": purged,
            "purged_count": len(purged),
            "more_to_scan": sorted(more_to_scan),
            "next_cursors": dict(sorted(next_cursors.items())),
            "purged_at_ms": now_ms,
        }


def _purge_derived_receipts(
    store: "MarketEntitlementStore",
    namespace: str,
    *,
    principal_id: str,
    scopes: set[str],
    max_rows_per_table: int,
    after_id_by_kind: dict[str, str] | None,
) -> dict[str, Any]:
    conn = store.conn
    tables = _existing_tables(conn)
    cursors = dict(after_id_by_kind or {})
    if set(cursors) - set(DERIVED_RECEIPT_TABLES):
        raise MarketEntitlementError("invalid_request", "purge cursor kind is unsupported")
    now_ms = _millis(store.now(), "now_ms")
    scanned: dict[str, int] = {}
    purged = []
    more_to_scan = []
    next_cursors = {}
    for kind, (table, id_column, payload_column, _owner) in DERIVED_RECEIPT_TABLES.items():
        if table not in tables:
            continue
        rows = conn.execute(
            f"SELECT DISTINCT {id_column} FROM {table} WHERE namespace=? AND {id_column}>? "
            f"ORDER BY {id_column} LIMIT ?",
            [namespace, _text(cursors.get(kind, ""), "cursor", 300) if cursors.get(kind) else "", max_rows_per_table],
        ).fetchall()
        scanned[kind] = len(rows)
        if len(rows) == max_rows_per_table:
            more_to_scan.append(kind)
            next_cursors[kind] = str(rows[-1][0])
        for (receipt_id,) in rows:
            payloads = conn.execute(
                f"SELECT {payload_column} FROM {table} WHERE namespace=? AND {id_column}=?",
                [namespace, receipt_id],
            ).fetchall()
            decoded = [json.loads(item[0]) for item in payloads]
            rights = recheck_stored_receipt_rights(
                conn,
                namespace,
                decoded,
                operation="retain",
                principal_id=principal_id,
                scopes=scopes,
                now_ms=now_ms,
            )
            if rights["state"] != "withheld":
                continue
            record_hash = _digest(decoded)
            reason = rights["reason_codes"][0] if rights["reason_codes"] else "entitlement_unavailable"
            purge_id = "market-entitlement-purge:" + _digest(
                [namespace, f"derived:{kind}", receipt_id, record_hash]
            )[:32]
            conn.execute(
                "INSERT OR IGNORE INTO market_entitlement_purge_events VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    purge_id,
                    namespace,
                    f"derived:{kind}",
                    str(receipt_id),
                    str(receipt_id),
                    record_hash,
                    _canonical([rights.get("entitlement_id")] if rights.get("entitlement_id") else []),
                    reason,
                    now_ms,
                ],
            )
            conn.execute(
                f"DELETE FROM {table} WHERE namespace=? AND {id_column}=?",
                [namespace, receipt_id],
            )
            purged.append(
                {
                    "object_kind": f"derived:{kind}",
                    "object_id": str(receipt_id),
                    "revision_id": str(receipt_id),
                    "record_hash": record_hash,
                    "entitlement_ids": [rights["entitlement_id"]] if rights.get("entitlement_id") else [],
                    "reason_code": reason,
                }
            )
    return {
        "contract": "noesis-market-entitlement-purge-report-v1",
        "namespace": namespace,
        "scope": "derived_receipts",
        "scanned_by_kind": dict(sorted(scanned.items())),
        "purged": purged,
        "purged_count": len(purged),
        "more_to_scan": sorted(more_to_scan),
        "next_cursors": dict(sorted(next_cursors.items())),
        "purged_at_ms": now_ms,
    }


# Source revision tables whose payloads carry ``source_refs``. Shared by the
# retention purge and by current-rights rechecks of stored derived receipts.
SOURCE_REVISION_TABLES = {
    "instrument": ("market_instrument_object_revisions", "object_id"),
    "universe_membership": ("market_instrument_universe_revisions", "universe_id"),
    "price_bar": ("market_price_bar_revisions", "bar_id"),
    "trading_session": ("market_trading_session_revisions", "calendar_id"),
    "corporate_action": ("market_corporate_action_revisions", "action_id"),
    "financial_fact": ("market_financial_fact_revisions", "fact_observation_id"),
}
# Stored derived receipts: (table, id column, payload column, owner column).
DERIVED_RECEIPT_TABLES = {
    "screener_run": ("market_screener_run_snapshots", "run_id", "result_json", "owner"),
    "quantitative_run": ("market_quantitative_runs", "run_id", "result_json", "owner"),
    "specialized_run": ("market_specialized_runs", "run_id", "result_json", "owner"),
    "alert_run": ("market_alert_run_snapshots", "run_id", "result_json", "owner"),
    "research_artifact": ("market_research_artifacts", "artifact_id", "result_json", "owner"),
    "quality_assessment": ("market_quality_assessments", "assessment_id", "report_json", "owner"),
    "asof_snapshot": ("market_asof_snapshots", "snapshot_id", "manifest_json", "owner"),
    "adjustment_calculation": (
        "market_price_adjustment_calculations", "calculation_id", "payload_json", None
    ),
}
_REVISION_ID_KEYS = {
    "source_revision_ids",
    "input_revision_ids",
    "identity_revision_ids",
    "source_revision_id",
    "revision_id",
}
_MAX_RECHECK_REVISIONS = 5_000
_MAX_RECHECK_NODES = 200_000


def _existing_tables(conn: Any) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }


def collect_receipt_provenance(payload: Any) -> tuple[set[str], list[dict[str, Any]]]:
    """Source revision IDs and full source refs referenced by a stored receipt.

    A full ref names ``entitlement_id``, ``license_id`` and ``provider``;
    decision summaries without a provider are ignored because the revision
    lookup recovers the original refs. The walk is bounded.
    """

    revision_ids: set[str] = set()
    refs: dict[str, dict[str, Any]] = {}
    stack = [payload]
    visited = 0
    while stack and visited < _MAX_RECHECK_NODES:
        node = stack.pop()
        visited += 1
        if isinstance(node, dict):
            if {"entitlement_id", "license_id", "provider"} <= set(node) and all(
                isinstance(node[key], str) for key in ("entitlement_id", "license_id", "provider")
            ):
                ref = {
                    key: node.get(key)
                    for key in (
                        "source_ref_id",
                        "entitlement_id",
                        "license_id",
                        "provider",
                        "retrieved_at_ms",
                    )
                    if node.get(key) is not None
                }
                refs[_canonical(ref)] = ref
            for key, value in node.items():
                if key in _REVISION_ID_KEYS:
                    if isinstance(value, str) and value:
                        revision_ids.add(value)
                    elif isinstance(value, list):
                        revision_ids.update(item for item in value if isinstance(item, str) and item)
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return revision_ids, list(refs.values())


def resolve_source_revisions(
    conn: Any, namespace: str, revision_ids: set[str] | list[str]
) -> dict[str, Any]:
    """Map source revision IDs to their stored source refs or purge tombstones."""

    wanted = sorted({str(item) for item in revision_ids if item})[:_MAX_RECHECK_REVISIONS]
    tables = _existing_tables(conn)
    refs: dict[str, dict[str, Any]] = {}
    found: set[str] = set()
    purged: dict[str, str] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        marks = ",".join("?" for _ in chunk)
        for table, _identity in SOURCE_REVISION_TABLES.values():
            if table not in tables:
                continue
            for revision_id, encoded in conn.execute(
                f"SELECT revision_id,payload_json FROM {table} "
                f"WHERE namespace=? AND revision_id IN ({marks})",
                [namespace, *chunk],
            ).fetchall():
                found.add(str(revision_id))
                for ref in json.loads(encoded).get("source_refs") or []:
                    if isinstance(ref, dict) and ref.get("entitlement_id"):
                        refs[_canonical(ref)] = ref
        if "market_entitlement_purge_events" in tables:
            for revision_id, reason in conn.execute(
                "SELECT revision_id,reason_code FROM market_entitlement_purge_events "
                f"WHERE namespace=? AND revision_id IN ({marks})",
                [namespace, *chunk],
            ).fetchall():
                purged[str(revision_id)] = str(reason)
    return {
        "refs": list(refs.values()),
        "found": found,
        "purged": purged,
        "unknown": sorted(set(wanted) - found - set(purged)),
        "truncated": len(set(revision_ids)) > _MAX_RECHECK_REVISIONS,
    }


def recheck_stored_receipt_rights(
    conn: Any,
    namespace: str,
    payload: Any,
    *,
    operation: str,
    principal_id: str,
    scopes: set[str],
    external: bool = False,
    now_ms: int | None = None,
    include_direct_refs: bool = True,
) -> dict[str, Any]:
    """Re-evaluate current provider rights for a stored derived receipt.

    Derived receipts (screens, quantitative runs, alerts, research artifacts)
    persist values computed from licensed sources and so act as caches. Their
    rights are rechecked at read time rather than trusted from run time.

    ``state`` is ``authorized`` (every source has current rights),
    ``unverified`` (only caller-supplied inputs with no stored source
    provenance), ``partially_verified`` (authorized stored sources plus
    caller-supplied inputs) or ``withheld`` (a source was purged, revoked,
    expired or does not permit ``operation``). Callers must not return
    derived values for a ``withheld`` receipt.

    ``include_direct_refs=False`` rechecks only refs recovered from stored
    source revisions, for receipts whose embedded refs are author-supplied
    citations governed by their own export rules.
    """

    revision_ids, direct_refs = collect_receipt_provenance(payload)
    if not include_direct_refs:
        direct_refs = []
    resolved = resolve_source_revisions(conn, namespace, revision_ids)
    refs = {_canonical(ref): ref for ref in [*resolved["refs"], *direct_refs]}
    base = {
        "operation": operation,
        "external": external,
        "source_revision_count": len(revision_ids),
        "unknown_revision_count": len(resolved["unknown"]),
        "purged_revision_ids": sorted(resolved["purged"]),
        "truncated": resolved["truncated"],
    }
    if resolved["purged"]:
        return {
            **base,
            "state": "withheld",
            "reason_codes": sorted({"source_purged", *resolved["purged"].values()}),
            "decisions": [],
        }
    if resolved["truncated"]:
        return {**base, "state": "withheld", "reason_codes": ["provenance_too_large"], "decisions": []}
    decisions: list[dict[str, Any]] = []
    if refs:
        try:
            decisions = MarketEntitlementStore(conn, initialize=False).authorize_sources(
                namespace,
                list(refs.values()),
                operation=operation,
                principal_id=principal_id,
                scopes=scopes,
                external=external,
                now_ms=now_ms,
            )
        except MarketEntitlementError as exc:
            return {
                **base,
                "state": "withheld",
                "reason_codes": [exc.code],
                "entitlement_id": exc.details.get("entitlement_id"),
                "decisions": [],
            }
    state = (
        "authorized"
        if refs and not resolved["unknown"]
        else "partially_verified"
        if refs
        else "unverified"
    )
    return {**base, "state": state, "reason_codes": [], "decisions": decisions}


def withheld_receipt(
    contract: str, kind: str, receipt_id: str, record_hash: Any, rights: dict[str, Any]
) -> dict[str, Any]:
    """Content-free stand-in for a stored receipt whose sources lost rights."""

    return {
        "contract": contract,
        "receipt_kind": kind,
        "receipt_id": receipt_id,
        "record_hash": record_hash,
        "withheld": True,
        "rights": rights,
        "limitations": [
            "Derived values are withheld because current provider rights no longer permit this operation; only identifiers and the stored hash are returned."
        ],
    }


def recheck_market_dependencies(
    conn: Any,
    dependencies: list[dict[str, Any]],
    *,
    operation: str,
    principal_id: str,
    scopes: set[str],
    external: bool = False,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Recheck market rights for evidence dependencies cited outside the market domain.

    Authored reports and similar documents cite ``{kind, id, revision,
    namespace, locator}`` dependencies. A dependency is market-backed when its
    ID, revision or locator revision is a stored market source revision, a
    purged one, or a stored derived market receipt in that namespace. Other
    dependencies are ignored, so documents without market evidence pass.
    """

    tables = _existing_tables(conn)
    market_tables = {table for table, _ in SOURCE_REVISION_TABLES.values()} | {
        table for table, *_ in DERIVED_RECEIPT_TABLES.values()
    } | {"market_entitlement_purge_events"}
    if not tables & market_tables:
        return {"state": "not_market_backed", "market_dependencies": 0, "reason_codes": []}
    by_namespace: dict[str, set[str]] = {}
    for dep in dependencies[:10_000]:
        if not isinstance(dep, dict) or not isinstance(dep.get("namespace"), str):
            continue
        locator = dep.get("locator") if isinstance(dep.get("locator"), dict) else {}
        candidates = {
            str(value)
            for value in (dep.get("id"), dep.get("revision"), locator.get("revision_id"))
            if isinstance(value, str) and value
        }
        by_namespace.setdefault(dep["namespace"], set()).update(candidates)
    market_dependencies = 0
    decisions: list[dict[str, Any]] = []
    for namespace, candidates in sorted(by_namespace.items()):
        resolved = resolve_source_revisions(conn, namespace, candidates)
        payloads: list[Any] = []
        for table, id_column, payload_column, _owner in DERIVED_RECEIPT_TABLES.values():
            if table not in tables:
                continue
            ids = sorted(candidates)[:_MAX_RECHECK_REVISIONS]
            marks = ",".join("?" for _ in ids)
            if not ids:
                continue
            payloads.extend(
                json.loads(row[0])
                for row in conn.execute(
                    f"SELECT {payload_column} FROM {table} WHERE namespace=? AND {id_column} IN ({marks})",
                    [namespace, *ids],
                ).fetchall()
            )
        stored = resolved["found"] | set(resolved["purged"])
        market_dependencies += len(stored) + len(payloads)
        if not stored and not payloads:
            continue
        rights = recheck_stored_receipt_rights(
            conn,
            namespace,
            {"source_revision_ids": sorted(stored), "receipts": payloads},
            operation=operation,
            principal_id=principal_id,
            scopes=scopes,
            external=external,
            now_ms=now_ms,
            include_direct_refs=False,
        )
        if rights["state"] == "withheld":
            return {
                "state": "withheld",
                "namespace": namespace,
                "market_dependencies": market_dependencies,
                "reason_codes": rights["reason_codes"],
                "entitlement_id": rights.get("entitlement_id"),
            }
        decisions.extend(rights["decisions"])
    return {
        "state": "authorized" if market_dependencies else "not_market_backed",
        "market_dependencies": market_dependencies,
        "reason_codes": [],
        "decisions": decisions,
    }


class MarketRetentionSchedule:
    """Opt-in periodic retention for the maintenance worker.

    Configuration (``market_retention`` in the worker config)::

        {"enabled": true, "namespaces": ["market:prod"],
         "interval_s": 3600, "max_rows_per_table": 10000}

    The worker runs as a deployment-owned operator. Each due namespace gets one
    bounded :meth:`MarketEntitlementStore.run_retention` pass per interval; an
    incomplete pass is resumed on the next tick instead of waiting an interval.
    """

    def __init__(self, conn: Any, config: dict[str, Any] | None, *, now=None) -> None:
        config = dict(config or {})
        self.enabled = config.get("enabled") is True
        namespaces = config.get("namespaces") or []
        if self.enabled and (
            not isinstance(namespaces, list)
            or not namespaces
            or len(namespaces) > 100
            or not all(isinstance(item, str) and item for item in namespaces)
        ):
            raise ValueError("market_retention.namespaces must list 1-100 namespaces")
        self.namespaces = list(namespaces)
        self.interval_ms = max(60, min(int(config.get("interval_s", 3600)), 7 * 86_400)) * 1000
        self.max_rows = max(1, min(int(config.get("max_rows_per_table", 10_000)), 100_000))
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self._next_due_ms: dict[str, int] = {}

    def readiness(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "namespaces": len(self.namespaces), "interval_ms": self.interval_ms}

    def tick(self, worker_id: str) -> dict[str, Any]:
        now_ms = int(self.now())
        runs = []
        if not self.enabled:
            return {"contract": "noesis-market-retention-tick-v1", "enabled": False, "runs": runs}
        store = MarketEntitlementStore(self.conn, now=lambda: now_ms)
        for namespace in self.namespaces:
            if now_ms < self._next_due_ms.get(namespace, 0):
                continue
            try:
                report = store.run_retention(
                    namespace,
                    principal_id=worker_id,
                    scopes={"operator"},
                    max_rows_per_table=self.max_rows,
                )
            except MarketEntitlementError as exc:
                runs.append({"namespace": namespace, "status": "failed", "code": exc.code})
                self._next_due_ms[namespace] = now_ms + self.interval_ms
                continue
            self._next_due_ms[namespace] = now_ms + (0 if not report["complete"] else self.interval_ms)
            runs.append(
                {
                    "namespace": namespace,
                    "status": "complete" if report["complete"] else "partial",
                    "purged_source_revisions": report["source_revisions"]["purged_count"],
                    "purged_derived_receipts": report["derived_receipts"]["purged_count"],
                }
            )
        return {"contract": "noesis-market-retention-tick-v1", "enabled": True, "runs": runs}


def authorize_market_sources(
    conn: Any,
    namespace: str,
    source_refs,
    *,
    operation: str,
    principal_id: str,
    scopes: set[str],
    now_ms: int | None = None,
    external: bool = False,
) -> list[dict[str, Any]]:
    """Shared enforcement entry point used by market domain stores."""

    return MarketEntitlementStore(conn, initialize=False).authorize_sources(
        namespace,
        source_refs,
        operation=operation,
        principal_id=principal_id,
        scopes=scopes,
        external=external,
        now_ms=now_ms,
    )


__all__ = [
    "ADMIN_SCOPE",
    "DERIVED_RECEIPT_TABLES",
    "SOURCE_REVISION_TABLES",
    "collect_receipt_provenance",
    "recheck_market_dependencies",
    "recheck_stored_receipt_rights",
    "resolve_source_revisions",
    "withheld_receipt",
    "ENTITLEMENT_CONTRACT",
    "MarketEntitlementError",
    "MarketEntitlementStore",
    "MarketRetentionSchedule",
    "READ_SCOPE",
    "authorize_market_sources",
    "ensure_market_entitlement_schema",
]
