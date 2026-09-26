"""Versioned market watch rules backed by the shared anomaly delivery store."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time
from typing import Any

from src.domains.market.entitlements import MarketEntitlementStore
from src.kb.knowledge_anomalies import (
    DELIVER_SCOPE as ANOMALY_DELIVER_SCOPE,
    EXECUTE_SCOPE as ANOMALY_EXECUTE_SCOPE,
    READ_SCOPE as ANOMALY_READ_SCOPE,
    WRITE_SCOPE as ANOMALY_WRITE_SCOPE,
    KnowledgeAnomalyStore,
)
from src.domains.market.entitlements import recheck_stored_receipt_rights, withheld_receipt

ALERT_WATCH_CONTRACT = "noesis-market-alert-watch-v1"
ALERT_RUN_CONTRACT = "noesis-market-alert-run-v1"
ALERT_READ_SCOPE = "market:alerts:read"
ALERT_WRITE_SCOPE = "market:alerts:write"
ALERT_EXECUTE_SCOPE = "market:alerts:execute"
ALERT_DELIVER_SCOPE = "market:alerts:deliver"
MAX_RULES = 32
MAX_OBSERVATIONS = 500
MAX_TRIGGERED = 100

_TRIGGER_TYPES = {
    "price_threshold",
    "metric_threshold",
    "new_filing",
    "release",
    "source_correction",
    "thesis_review",
}
_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "in"}
_DDL = """
CREATE TABLE IF NOT EXISTS market_alert_watch_revisions(
 namespace TEXT NOT NULL, watch_key TEXT NOT NULL, version BIGINT NOT NULL,
 watch_id TEXT NOT NULL, owner TEXT NOT NULL, rules_json TEXT NOT NULL,
 notification_json TEXT NOT NULL, status TEXT NOT NULL, record_hash TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, watch_key, version), UNIQUE(namespace, watch_id));
CREATE TABLE IF NOT EXISTS market_alert_run_snapshots(
 namespace TEXT NOT NULL, run_id TEXT NOT NULL, watch_id TEXT NOT NULL,
 owner TEXT NOT NULL, generation BIGINT NOT NULL, input_hash TEXT NOT NULL,
 input_json TEXT NOT NULL, result_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, run_id), UNIQUE(namespace, watch_id, generation, input_hash));
CREATE INDEX IF NOT EXISTS idx_market_alert_watch_owner
 ON market_alert_watch_revisions(namespace, owner, watch_key, version);
CREATE INDEX IF NOT EXISTS idx_market_alert_runs
 ON market_alert_run_snapshots(namespace, owner, recorded_at_ms);
"""


class MarketAlertError(ValueError):
    """Typed market-alert failure safe for REST and MCP boundaries."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def ensure_market_alert_schema(conn: Any) -> None:
    conn.execute(_DDL)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MarketAlertError("invalid_request", "alert payload must be JSON-safe") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str, *, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketAlertError("invalid_request", f"{name} must be bounded text")
    return value.strip()


def _millis(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketAlertError("invalid_request", f"{name} must be a nonnegative epoch millisecond")
    return value


def _decimal(value: Any, name: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketAlertError("invalid_request", f"{name} must be a finite number") from exc
    if not result.is_finite():
        raise MarketAlertError("invalid_request", f"{name} must be a finite number")
    return result


def _authorize(namespace: str, principal_id: str, scopes: set[str], scope: str, *, write: bool) -> None:
    _text(namespace, "namespace", limit=100)
    _text(principal_id, "principal_id", limit=200)
    if "operator" in scopes:
        return
    namespace_scope = f"namespace:{namespace}:{'write' if write else 'read'}"
    if scope not in scopes or namespace_scope not in scopes:
        raise MarketAlertError("unauthorized", "market alert and namespace access is required")


def _owner(owner: str | None, principal_id: str, scopes: set[str]) -> str:
    if owner is None:
        return principal_id
    owner = _text(owner, "owner", limit=200)
    if owner != principal_id and "operator" not in scopes:
        raise MarketAlertError("unauthorized", "market alert belongs to another principal")
    return owner


def _check_owner(owner: str, principal_id: str, scopes: set[str]) -> None:
    if owner != principal_id and "operator" not in scopes:
        raise MarketAlertError("unauthorized", "market alert belongs to another principal")


def _compare(observed: Any, operator: str, expected: Any) -> bool:
    if operator not in _OPERATORS:
        raise MarketAlertError("invalid_request", "unsupported alert comparison operator")
    if operator == "in":
        if not isinstance(expected, list) or len(expected) > 32:
            raise MarketAlertError("invalid_request", "alert in comparison expects a bounded list")
        return any(_compare(observed, "eq", candidate) for candidate in expected)
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


def _rule_list(rules: Any) -> list[dict[str, Any]]:
    if not isinstance(rules, Mapping):
        raise MarketAlertError("invalid_request", "rules must be an object")
    triggers = rules.get("triggers")
    if not isinstance(triggers, Sequence) or isinstance(triggers, (str, bytes)) or not 1 <= len(triggers) <= MAX_RULES:
        raise MarketAlertError("invalid_request", f"rules.triggers must contain 1 to {MAX_RULES} entries")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(triggers):
        if not isinstance(raw, Mapping):
            raise MarketAlertError("invalid_request", "each alert trigger must be an object")
        item = dict(raw)
        trigger_type = _text(item.get("type"), "trigger.type", limit=50)
        if trigger_type not in _TRIGGER_TYPES:
            raise MarketAlertError("invalid_request", "unsupported market alert trigger type")
        item["type"] = trigger_type
        item["rule_id"] = _text(item.get("rule_id", f"rule-{index + 1}"), "trigger.rule_id", limit=100)
        if trigger_type in {"price_threshold", "metric_threshold"}:
            item["operator"] = _text(item.get("operator"), "trigger.operator", limit=10)
            if item["operator"] not in _OPERATORS:
                raise MarketAlertError("invalid_request", "unsupported alert comparison operator")
            if "value" not in item:
                raise MarketAlertError("invalid_request", "threshold trigger requires value")
            if item["operator"] != "in":
                _decimal(item["value"], "trigger.value")
            elif not isinstance(item["value"], list) or not item["value"]:
                raise MarketAlertError("invalid_request", "in threshold requires a nonempty list")
        result.append(item)
    return result


def _notification(value: Any, owner: str) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise MarketAlertError("invalid_request", "notification must be an object")
    result = dict(value)
    for name in ("cooldown_ms", "dedupe_window_ms", "retry_delay_ms", "quiet_until_ms"):
        if name in result and result[name] is not None:
            result[name] = _millis(result[name], f"notification.{name}")
    result.setdefault("dedupe_window_ms", 300_000)
    result.setdefault("retry_delay_ms", 60_000)
    result["owner_id"] = owner
    return result


class MarketAlertStore:
    """Evaluate bounded market observations and delegate delivery to Noesis alerts."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_alert_schema(conn)
            # Shared anomaly rows are the canonical delivery substrate.
            KnowledgeAnomalyStore(conn, initialize=True, now=self.now)

    def save_watch(
        self,
        namespace: str,
        watch_key: str,
        version: int,
        rules: Mapping[str, Any],
        notification: Mapping[str, Any] | None,
        *,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
        status: str = "active",
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, ALERT_WRITE_SCOPE, write=True)
        watch_key = _text(watch_key, "watch_key", limit=200)
        if type(version) is not int or version < 1:
            raise MarketAlertError("invalid_request", "version must be a positive integer")
        if status not in {"active", "paused", "retired"}:
            raise MarketAlertError("invalid_request", "status must be active, paused, or retired")
        owner = _owner(owner, principal_id, scopes)
        triggers = _rule_list(rules)
        rule_body = dict(rules)
        rule_body["triggers"] = triggers
        stale_after = rule_body.get("stale_after_ms")
        if stale_after is not None:
            rule_body["stale_after_ms"] = _millis(stale_after, "rules.stale_after_ms")
        notification_body = _notification(notification, owner)
        content = {
            "owner": owner,
            "rules": rule_body,
            "notification": notification_body,
            "status": status,
        }
        record_hash = _digest(content)
        watch_id = "anomaly-watch:" + _digest([namespace, watch_key, version])[:24]
        prior = self.conn.execute(
            "SELECT watch_id,owner,record_hash FROM market_alert_watch_revisions WHERE namespace=? AND watch_key=? AND version=?",
            [namespace, watch_key, version],
        ).fetchone()
        if prior:
            if prior[1] != owner or prior[2] != record_hash:
                raise MarketAlertError("watch_version_conflict", "alert watch version is immutable")
            return self.inspect_watch(namespace, prior[0], principal_id=principal_id, scopes=scopes, idempotent=True)
        # Register a same-identity generic watch so the common anomaly delivery
        # and history tools can consume market anomalies.
        generic = KnowledgeAnomalyStore(self.conn, initialize=True, now=self.now).register_watch(
            namespace,
            watch_key,
            version,
            "metric",
            {"domain": "market", "watch_id": watch_id},
            {"window": 2, "minimum_points": 2},
            {"kind": "market-rule", "version": str(version), "threshold": 1},
            notification_body,
            status="active" if status == "active" else "paused",
            principal_id=principal_id,
            scopes={ANOMALY_WRITE_SCOPE},
        )
        watch_id = generic["watch_id"]
        now = _millis(self.now(), "recorded_at_ms")
        self.conn.execute(
            "INSERT INTO market_alert_watch_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                watch_key,
                version,
                watch_id,
                owner,
                _canonical(rule_body),
                _canonical(notification_body),
                status,
                record_hash,
                now,
            ],
        )
        return {
            "contract": ALERT_WATCH_CONTRACT,
            "namespace": namespace,
            "watch_id": watch_id,
            "watch_key": watch_key,
            "version": version,
            "owner": owner,
            "rules": rule_body,
            "notification": notification_body,
            "status": status,
            "record_hash": record_hash,
            "recorded_at_ms": now,
            "idempotent": False,
        }

    def _row(self, namespace: str, watch_id: str) -> tuple[Any, ...]:
        row = self.conn.execute(
            "SELECT watch_key,version,owner,rules_json,notification_json,status,record_hash,recorded_at_ms FROM market_alert_watch_revisions WHERE namespace=? AND watch_id=?",
            [namespace, watch_id],
        ).fetchone()
        if not row:
            raise MarketAlertError("watch_not_found", "market alert watch not found")
        return row

    def inspect_watch(self, namespace: str, watch_id: str, *, principal_id: str, scopes: set[str], idempotent: bool = False) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, ALERT_READ_SCOPE, write=False)
        row = self._row(namespace, watch_id)
        _check_owner(row[2], principal_id, scopes)
        return {
            "contract": ALERT_WATCH_CONTRACT,
            "namespace": namespace,
            "watch_id": watch_id,
            "watch_key": row[0],
            "version": int(row[1]),
            "owner": row[2],
            "rules": json.loads(row[3]),
            "notification": json.loads(row[4]),
            "status": row[5],
            "record_hash": row[6],
            "recorded_at_ms": int(row[7]),
            "idempotent": idempotent,
        }

    def _observation(self, value: Any, index: int) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise MarketAlertError("invalid_request", f"observation {index} must be an object")
        item = dict(value)
        item["observed_at_ms"] = _millis(item.get("observed_at_ms"), f"observation[{index}].observed_at_ms")
        kind = item.get("kind", item.get("type"))
        item["kind"] = _text(kind, f"observation[{index}].kind", limit=50)
        revisions = item.get("source_revision_ids", [])
        if not isinstance(revisions, list) or len(revisions) > 32 or not all(isinstance(ref, str) and ref for ref in revisions):
            raise MarketAlertError("invalid_request", "source_revision_ids must be a bounded list of text")
        item["source_revision_ids"] = revisions
        refs = item.get("source_refs", [])
        if not isinstance(refs, list) or len(refs) > 16 or not all(isinstance(ref, Mapping) for ref in refs):
            raise MarketAlertError("invalid_request", "source_refs must be a bounded list of objects")
        item["source_refs"] = [dict(ref) for ref in refs]
        return item

    @staticmethod
    def _rule_matches(rule: Mapping[str, Any], observation: Mapping[str, Any]) -> tuple[bool, Any]:
        trigger_type = rule["type"]
        kind = str(observation.get("kind", ""))
        payload = observation.get("payload") if isinstance(observation.get("payload"), Mapping) else {}
        if trigger_type == "price_threshold":
            if kind not in {"price", "price_bar"}:
                return False, None
            if rule.get("listing_id") and observation.get("listing_id") != rule["listing_id"]:
                return False, None
            observed = observation.get(rule.get("field", "close"), payload.get(rule.get("field", "close")))
            return observed is not None and _compare(observed, rule["operator"], rule["value"]), observed
        if trigger_type == "metric_threshold":
            if kind != "metric":
                return False, None
            if rule.get("metric_id") and observation.get("metric_id", payload.get("metric_id")) != rule["metric_id"]:
                return False, None
            observed = observation.get("value", payload.get("value"))
            return observed is not None and _compare(observed, rule["operator"], rule["value"]), observed
        expected_kind = {
            "new_filing": "filing",
            "release": "release",
            "source_correction": "source_correction",
            "thesis_review": "thesis_review",
        }[trigger_type]
        if kind not in {expected_kind, trigger_type}:
            return False, None
        for key in ("issuer_id", "series_id", "source_id", "thesis_id"):
            if rule.get(key) is not None and observation.get(key, payload.get(key)) != rule[key]:
                return False, None
        return True, observation.get("value", payload.get("value"))

    def run(
        self,
        namespace: str,
        watch_id: str,
        observations: Sequence[Mapping[str, Any]],
        *,
        generation: int,
        as_of_ms: int,
        publicly_available_by_ms: int,
        acquired_by_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, ALERT_EXECUTE_SCOPE, write=True)
        row = self._row(namespace, watch_id)
        _check_owner(row[2], principal_id, scopes)
        if type(generation) is not int or generation < 0:
            raise MarketAlertError("invalid_request", "generation must be nonnegative")
        as_of_ms = _millis(as_of_ms, "as_of_ms")
        publicly_available_by_ms = _millis(publicly_available_by_ms, "publicly_available_by_ms")
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        if as_of_ms > acquired_by_ms or publicly_available_by_ms > acquired_by_ms:
            raise MarketAlertError("invalid_request", "cutoffs cannot exceed acquired_by_ms")
        if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)) or len(observations) > MAX_OBSERVATIONS:
            raise MarketAlertError("bound_exceeded", f"observations must contain at most {MAX_OBSERVATIONS} entries")
        normalized = [self._observation(item, index) for index, item in enumerate(observations)]
        input_hash = _digest({"watch_id": watch_id, "generation": generation, "observations": normalized, "cutoffs": [as_of_ms, publicly_available_by_ms, acquired_by_ms]})
        run_id = "market-alert-run:" + _digest([namespace, watch_id, generation, input_hash])[:24]
        prior = self.conn.execute(
            "SELECT result_json FROM market_alert_run_snapshots WHERE namespace=? AND run_id=?",
            [namespace, run_id],
        ).fetchone()
        if prior:
            return {**json.loads(prior[0]), "idempotent": True}
        rules = json.loads(row[3])
        stale_after = rules.get("stale_after_ms")
        triggered: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        generic = KnowledgeAnomalyStore(self.conn, initialize=True, now=self.now)
        for observation in normalized:
            base = {
                "observed_at_ms": observation["observed_at_ms"],
                "kind": observation["kind"],
                "source_revision_ids": observation["source_revision_ids"],
            }
            if observation["observed_at_ms"] > as_of_ms:
                decisions.append({**base, "status": "suppressed", "reason": "future_observation"})
                continue
            if observation.get("public_at_ms") is not None and _millis(observation["public_at_ms"], "public_at_ms") > publicly_available_by_ms:
                decisions.append({**base, "status": "suppressed", "reason": "not_public_asof"})
                continue
            if observation.get("acquired_at_ms") is not None and _millis(observation["acquired_at_ms"], "acquired_at_ms") > acquired_by_ms:
                decisions.append({**base, "status": "suppressed", "reason": "not_acquired_asof"})
                continue
            if stale_after is not None and as_of_ms - observation["observed_at_ms"] > int(stale_after):
                decisions.append({**base, "status": "suppressed", "reason": "stale_observation", "stale_after_ms": int(stale_after)})
                continue
            if observation["source_refs"]:
                try:
                    MarketEntitlementStore(self.conn, initialize=True, now=self.now).authorize_sources(
                        namespace,
                        observation["source_refs"],
                        operation="display",
                        principal_id=principal_id,
                        scopes=scopes,
                        now_ms=as_of_ms,
                    )
                except Exception as exc:  # rights failures must not leak source payloads
                    decisions.append({**base, "status": "suppressed", "reason": getattr(exc, "code", "entitlement_unavailable")})
                    continue
            matched = False
            for rule in rules["triggers"]:
                is_match, observed = self._rule_matches(rule, observation)
                if not is_match:
                    continue
                matched = True
                if len(triggered) >= MAX_TRIGGERED:
                    decisions.append({**base, "status": "suppressed", "reason": "trigger_bound_exceeded"})
                    break
                trigger = {
                    "status": "triggered",
                    "rule_id": rule["rule_id"],
                    "rule_version": int(row[1]),
                    "trigger_type": rule["type"],
                    "observed_value": observed,
                    "threshold": rule.get("value"),
                    "observed_at_ms": observation["observed_at_ms"],
                    "source_revision_ids": observation["source_revision_ids"],
                    "source_refs": observation["source_refs"],
                    "owner": row[2],
                }
                anomaly_id = "market-anomaly:" + _digest([watch_id, generation, rule["rule_id"], observation, input_hash])[:24]
                payload = {
                    "contract": "noesis-knowledge-anomaly-v1",
                    "market_contract": ALERT_RUN_CONTRACT,
                    "anomaly_id": anomaly_id,
                    "namespace": namespace,
                    "watch_id": watch_id,
                    "run_id": run_id,
                    "signal_key": rule["rule_id"],
                    "observed_at_ms": observation["observed_at_ms"],
                    "score": 1.0,
                    "severity": str(rule.get("severity", "warning")),
                    "value": observed,
                    "baseline": {"rule": rule, "cutoffs": {"as_of_ms": as_of_ms, "publicly_available_by_ms": publicly_available_by_ms, "acquired_by_ms": acquired_by_ms}},
                    "explanations": [],
                    "status": "open",
                    "rule_version": int(row[1]),
                    "trigger_type": rule["type"],
                    "rule_id": rule["rule_id"],
                    "source_revision_ids": observation["source_revision_ids"],
                    "source_refs": observation["source_refs"],
                    "owner": row[2],
                }
                recorded = generic.record_external_anomaly(
                    namespace,
                    anomaly_id=anomaly_id,
                    watch_id=watch_id,
                    run_id=run_id,
                    signal_key=rule["rule_id"],
                    observed_at_ms=observation["observed_at_ms"],
                    score=1.0,
                    severity=payload["severity"],
                    value=observed,
                    baseline=payload["baseline"],
                    payload=payload,
                    principal_id=principal_id,
                    scopes={ANOMALY_EXECUTE_SCOPE},
                )
                trigger["anomaly_id"] = recorded["anomaly_id"]
                triggered.append(trigger)
            if not matched:
                decisions.append({**base, "status": "ignored", "reason": "no_rule_match"})
        result = {
            "contract": ALERT_RUN_CONTRACT,
            "namespace": namespace,
            "run_id": run_id,
            "watch_id": watch_id,
            "watch_version": int(row[1]),
            "owner": row[2],
            "generation": generation,
            "status": "completed",
            "processed": len(normalized),
            "triggered": triggered,
            "decisions": decisions,
            "anomaly_ids": [item["anomaly_id"] for item in triggered],
            "input_hash": input_hash,
            "input_snapshot": normalized,
            "cutoffs": {"as_of_ms": as_of_ms, "publicly_available_by_ms": publicly_available_by_ms, "acquired_by_ms": acquired_by_ms},
            "rule_revision_id": f"market-alert:{_digest([namespace, watch_id, row[1]])[:24]}",
        }
        self.conn.execute(
            "INSERT INTO market_alert_run_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, run_id, watch_id, row[2], generation, input_hash, _canonical(normalized), _canonical(result), _millis(self.now(), "recorded_at_ms")],
        )
        return {**result, "idempotent": False}

    def deliver(
        self,
        namespace: str,
        anomaly_id: str,
        subscriber_id: str | None,
        *,
        delivery_outcome: str = "delivered",
        cancel_requested: bool = False,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, ALERT_DELIVER_SCOPE, write=True)
        generic = KnowledgeAnomalyStore(self.conn, initialize=False, now=self.now)
        anomaly = generic.anomaly(namespace, anomaly_id, scopes={ANOMALY_READ_SCOPE})
        owner = _text(anomaly.get("owner"), "owner", limit=200)
        _check_owner(owner, principal_id, scopes)
        refs = anomaly.get("source_refs")
        suppression_reason = None
        if refs:
            try:
                MarketEntitlementStore(self.conn, initialize=True, now=self.now).authorize_sources(
                    namespace,
                    refs,
                    operation="display",
                    principal_id=principal_id,
                    scopes=scopes,
                    now_ms=int(anomaly["observed_at_ms"]),
                )
            except Exception as exc:
                suppression_reason = getattr(exc, "code", "entitlement_unavailable")
        delivered = generic.deliver(
            namespace,
            anomaly_id,
            _text(subscriber_id or owner, "subscriber_id", limit=200),
            delivery_outcome=delivery_outcome,
            cancel_requested=cancel_requested,
            suppression_reason=suppression_reason,
            principal_id=principal_id,
            scopes={ANOMALY_DELIVER_SCOPE},
        )
        return {
            **delivered,
            "market_rule_version": anomaly.get("rule_version"),
            "source_revision_ids": anomaly.get("source_revision_ids", []),
            "suppression_reason": suppression_reason,
        }

    def history(self, namespace: str, *, principal_id: str, scopes: set[str], limit: int = 100, offset: int = 0) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, ALERT_READ_SCOPE, write=False)
        generic = KnowledgeAnomalyStore(self.conn, initialize=False, now=self.now)
        return generic.history(namespace, scopes={ANOMALY_READ_SCOPE}, owner_id=None if "operator" in scopes else principal_id, limit=limit, offset=offset)

    def inspect_run(self, namespace: str, run_id: str, *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, ALERT_READ_SCOPE, write=False)
        row = self.conn.execute(
            "SELECT owner,result_json FROM market_alert_run_snapshots WHERE namespace=? AND run_id=?",
            [namespace, _text(run_id, "run_id", limit=200)],
        ).fetchone()
        if not row:
            raise MarketAlertError("run_not_found", "market alert run not found")
        _check_owner(row[0], principal_id, scopes)
        result = json.loads(row[1])
        rights = recheck_stored_receipt_rights(
            self.conn, namespace, result, operation="derive",
            principal_id=principal_id, scopes=scopes, now_ms=int(self.now()),
        )
        if rights["state"] == "withheld":
            return withheld_receipt(ALERT_RUN_CONTRACT, "alert_run", run_id, result.get("record_hash"), rights)
        return {**result, "current_rights": rights}


__all__ = [
    "ALERT_DELIVER_SCOPE",
    "ALERT_EXECUTE_SCOPE",
    "ALERT_READ_SCOPE",
    "ALERT_RUN_CONTRACT",
    "ALERT_WATCH_CONTRACT",
    "ALERT_WRITE_SCOPE",
    "MarketAlertError",
    "MarketAlertStore",
    "ensure_market_alert_schema",
]
