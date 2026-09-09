"""Incremental anomaly watches, uncertain attribution, and replay-safe alerts."""

from __future__ import annotations

import hashlib
import json
import statistics
import time

WATCH_CONTRACT = "noesis-anomaly-watch-v1"
RUN_CONTRACT = "noesis-anomaly-run-v1"
ANOMALY_CONTRACT = "noesis-knowledge-anomaly-v1"
ALERT_CONTRACT = "noesis-anomaly-alert-v1"
HEALTH_CONTRACT = "noesis-anomaly-health-v1"
READ_SCOPE = "knowledge:anomalies:read"
WRITE_SCOPE = "knowledge:anomalies:write"
EXECUTE_SCOPE = "knowledge:anomalies:execute"
DELIVER_SCOPE = "knowledge:anomalies:deliver"

_DDL = """
CREATE TABLE IF NOT EXISTS anomaly_watches(watch_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,watch_key TEXT NOT NULL,version BIGINT NOT NULL,signal_type TEXT NOT NULL,scope_json TEXT NOT NULL,baseline_json TEXT NOT NULL,detector_json TEXT NOT NULL,notification_json TEXT NOT NULL,status TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,watch_key,version));
CREATE TABLE IF NOT EXISTS anomaly_runs(run_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,watch_id TEXT NOT NULL,generation BIGINT NOT NULL,input_hash TEXT NOT NULL,status TEXT NOT NULL,receipt_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,watch_id,generation,input_hash));
CREATE TABLE IF NOT EXISTS knowledge_anomalies(anomaly_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,watch_id TEXT NOT NULL,run_id TEXT NOT NULL,signal_key TEXT NOT NULL,observed_at_ms BIGINT NOT NULL,score DOUBLE NOT NULL,severity TEXT NOT NULL,value DOUBLE,baseline_json TEXT NOT NULL,explanations_json TEXT NOT NULL,status TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS anomaly_alerts(alert_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,anomaly_id TEXT NOT NULL,group_key TEXT NOT NULL,subscriber_id TEXT NOT NULL,status TEXT NOT NULL,attempts BIGINT NOT NULL,next_attempt_ms BIGINT,delivery_key TEXT NOT NULL,history_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,delivery_key));
CREATE TABLE IF NOT EXISTS anomaly_audit(audit_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,operation TEXT NOT NULL,object_id TEXT NOT NULL,principal_id TEXT NOT NULL,detail_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""


class KnowledgeAnomalyError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value):
    return hashlib.sha256(_canon(value).encode()).hexdigest()


def _load(value, default):
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _require(scopes, required):
    if required not in scopes and "operator" not in scopes:
        raise KnowledgeAnomalyError(
            "unauthorized", f"missing required scope {required}"
        )


def _limit(value, maximum=1000):
    return min(max(int(value), 1), maximum)


class KnowledgeAnomalyStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail=None):
        now = self.now()
        detail = dict(detail or {})
        self.conn.execute(
            "INSERT OR IGNORE INTO anomaly_audit VALUES (?,?,?,?,?,?,?)",
            [
                "anomaly-audit:"
                + _hash([namespace, operation, object_id, detail, now])[:24],
                namespace,
                operation,
                object_id,
                principal_id,
                _canon(detail),
                now,
            ],
        )

    def register_watch(
        self,
        namespace,
        watch_key,
        version,
        signal_type,
        scope,
        baseline,
        detector,
        notification,
        *,
        principal_id,
        scopes,
        status="active",
    ):
        _require(scopes, WRITE_SCOPE)
        if signal_type not in {
            "metric",
            "event_rate",
            "graph",
            "source",
            "coverage",
            "narrative",
        }:
            raise KnowledgeAnomalyError("invalid_signal", "unsupported signal type")
        if int(baseline.get("window", 0)) < 2:
            raise KnowledgeAnomalyError(
                "invalid_window", "baseline window must be at least two"
            )
        watch_id = "anomaly-watch:" + _hash([namespace, watch_key, version])[:24]
        content = [
            signal_type,
            dict(scope),
            dict(baseline),
            dict(detector),
            dict(notification),
            status,
        ]
        row = self.conn.execute(
            "SELECT signal_type,scope_json,baseline_json,detector_json,notification_json,status FROM anomaly_watches WHERE namespace=? AND watch_key=? AND version=?",
            [namespace, watch_key, version],
        ).fetchone()
        if row:
            existing = [
                row[0],
                _load(row[1], {}),
                _load(row[2], {}),
                _load(row[3], {}),
                _load(row[4], {}),
                row[5],
            ]
            if existing != content:
                raise KnowledgeAnomalyError(
                    "watch_version_conflict", "watch version is immutable"
                )
            return self.watch(namespace, watch_id, scopes={READ_SCOPE}, idempotent=True)
        now = self.now()
        self.conn.execute(
            "INSERT INTO anomaly_watches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                watch_id,
                namespace,
                watch_key,
                version,
                signal_type,
                _canon(scope),
                _canon(baseline),
                _canon(detector),
                _canon(notification),
                status,
                now,
            ],
        )
        self._audit(
            namespace, "register_watch", watch_id, principal_id, {"version": version}
        )
        return self.watch(namespace, watch_id, scopes={READ_SCOPE})

    def watch(self, namespace, watch_id, *, scopes, idempotent=False):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT watch_key,version,signal_type,scope_json,baseline_json,detector_json,notification_json,status FROM anomaly_watches WHERE namespace=? AND watch_id=?",
            [namespace, watch_id],
        ).fetchone()
        if not row:
            raise KnowledgeAnomalyError("watch_not_found", "anomaly watch not found")
        return {
            "contract": WATCH_CONTRACT,
            "watch_id": watch_id,
            "namespace": namespace,
            "watch_key": row[0],
            "version": int(row[1]),
            "signal_type": row[2],
            "scope": _load(row[3], {}),
            "baseline": _load(row[4], {}),
            "detector": _load(row[5], {}),
            "notification": _load(row[6], {}),
            "status": row[7],
            "idempotent": idempotent,
        }

    def preview_baseline(self, namespace, watch_id, observations, *, scopes):
        _require(scopes, READ_SCOPE)
        watch = self.watch(namespace, watch_id, scopes={READ_SCOPE})
        window = int(watch["baseline"]["window"])
        usable = [
            float(o["value"])
            for o in observations[-window:]
            if o.get("value") is not None and not o.get("missing")
        ]
        sparse = len(usable) < max(2, int(watch["baseline"].get("minimum_points", 3)))
        center = statistics.mean(usable) if usable else None
        spread = statistics.pstdev(usable) if len(usable) > 1 else 0.0
        return {
            "watch_id": watch_id,
            "namespace": namespace,
            "window": window,
            "points": len(usable),
            "sparse": sparse,
            "center": center,
            "spread": spread,
            "seasonality": watch["baseline"].get("seasonality"),
            "missing_count": sum(
                o.get("value") is None or o.get("missing", False)
                for o in observations[-window:]
            ),
        }

    def simulate_drift(self, namespace, watch_id, observations, *, scopes):
        _require(scopes, READ_SCOPE)
        watch = self.watch(namespace, watch_id, scopes={READ_SCOPE})
        from src.integrations.drift import detect_drift
        config = watch["detector"]
        return detect_drift(observations, delta=config.get("delta", .002), clock=config.get("clock", 32))

    def simulate(self, namespace, watch_id, observations, *, scopes, limit=1000):
        _require(scopes, READ_SCOPE)
        observations = list(observations)[: _limit(limit)]
        baseline = self.preview_baseline(
            namespace, watch_id, observations[:-1], scopes={READ_SCOPE}
        )
        watch = self.watch(namespace, watch_id, scopes={READ_SCOPE})
        latest = observations[-1] if observations else {}
        value = latest.get("value")
        threshold = float(watch["detector"].get("threshold", 3.0))
        score = (
            0.0
            if value is None or baseline["center"] is None
            else abs(float(value) - baseline["center"])
            / (baseline["spread"] or max(abs(baseline["center"]) * 0.01, 1e-9))
        )
        detected = not baseline["sparse"] and value is not None and score >= threshold
        return {
            "namespace": namespace,
            "watch_id": watch_id,
            "baseline": baseline,
            "score": score,
            "threshold": threshold,
            "detected": detected,
            "late_arrival": bool(latest.get("late_arrival")),
            "observation": latest,
        }

    def run(
        self,
        namespace,
        watch_id,
        observations,
        generation,
        *,
        principal_id,
        scopes,
        cancel_requested=False,
        limit=1000,
    ):
        _require(scopes, EXECUTE_SCOPE)
        bounded = list(observations)[: _limit(limit)]
        input_hash = _hash(bounded)
        run_id = (
            "anomaly-run:" + _hash([namespace, watch_id, generation, input_hash])[:24]
        )
        prior = self.conn.execute(
            "SELECT receipt_json FROM anomaly_runs WHERE run_id=? AND namespace=?",
            [run_id, namespace],
        ).fetchone()
        if prior:
            return {**_load(prior[0], {}), "idempotent": True}
        if cancel_requested:
            receipt = {
                "contract": RUN_CONTRACT,
                "run_id": run_id,
                "namespace": namespace,
                "watch_id": watch_id,
                "generation": generation,
                "status": "cancelled",
                "processed": 0,
                "anomaly_ids": [],
                "input_hash": input_hash,
            }
        else:
            result = self.simulate(
                namespace, watch_id, bounded, scopes={READ_SCOPE}, limit=limit
            )
            anomaly_ids = []
            if result["detected"]:
                observation = result["observation"]
                signal_key = str(observation.get("signal_key", "default"))
                observed = int(observation.get("observed_at_ms", self.now()))
                anomaly_id = (
                    "knowledge-anomaly:"
                    + _hash([watch_id, generation, signal_key, observed, input_hash])[
                        :24
                    ]
                )
                severity = (
                    "critical"
                    if result["score"] >= result["threshold"] * 2
                    else "warning"
                )
                payload = {
                    "contract": ANOMALY_CONTRACT,
                    "anomaly_id": anomaly_id,
                    "namespace": namespace,
                    "watch_id": watch_id,
                    "run_id": run_id,
                    "signal_key": signal_key,
                    "observed_at_ms": observed,
                    "score": result["score"],
                    "severity": severity,
                    "value": observation.get("value"),
                    "baseline": result["baseline"],
                    "explanations": [],
                    "status": "open",
                    "generation": generation,
                    "late_arrival": result["late_arrival"],
                }
                self.conn.execute(
                    "INSERT OR IGNORE INTO knowledge_anomalies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        anomaly_id,
                        namespace,
                        watch_id,
                        run_id,
                        signal_key,
                        observed,
                        result["score"],
                        severity,
                        observation.get("value"),
                        _canon(result["baseline"]),
                        "[]",
                        "open",
                        _canon(payload),
                    ],
                )
                anomaly_ids.append(anomaly_id)
            receipt = {
                "contract": RUN_CONTRACT,
                "run_id": run_id,
                "namespace": namespace,
                "watch_id": watch_id,
                "generation": generation,
                "status": "completed",
                "processed": len(bounded),
                "anomaly_ids": anomaly_ids,
                "input_hash": input_hash,
            }
        now = self.now()
        self.conn.execute(
            "INSERT INTO anomaly_runs VALUES (?,?,?,?,?,?,?,?)",
            [
                run_id,
                namespace,
                watch_id,
                generation,
                input_hash,
                receipt["status"],
                _canon(receipt),
                now,
            ],
        )
        self._audit(
            namespace, "run", run_id, principal_id, {"status": receipt["status"]}
        )
        return {**receipt, "idempotent": False}

    def anomaly(self, namespace, anomaly_id, *, scopes):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT payload_json,explanations_json,status FROM knowledge_anomalies WHERE namespace=? AND anomaly_id=?",
            [namespace, anomaly_id],
        ).fetchone()
        if not row:
            raise KnowledgeAnomalyError("anomaly_not_found", "anomaly not found")
        return {
            **_load(row[0], {}),
            "explanations": _load(row[1], []),
            "status": row[2],
        }

    def correlate(
        self, namespace, anomaly_id, candidates, *, principal_id, scopes, limit=100
    ):
        _require(scopes, WRITE_SCOPE)
        anomaly = self.anomaly(namespace, anomaly_id, scopes={READ_SCOPE})
        ranked = []
        for candidate in list(candidates)[: _limit(limit, 500)]:
            temporal = max(
                0.0,
                1.0
                - abs(
                    int(candidate.get("observed_at_ms", anomaly["observed_at_ms"]))
                    - anomaly["observed_at_ms"]
                )
                / max(int(candidate.get("window_ms", 86_400_000)), 1),
            )
            score = round(temporal * float(candidate.get("relevance", 0.5)), 6)
            ranked.append(
                {
                    **candidate,
                    "attribution_score": score,
                    "causal_status": "plausible_not_proven",
                }
            )
        ranked.sort(
            key=lambda x: (-x["attribution_score"], str(x.get("object_id", "")))
        )
        self.conn.execute(
            "UPDATE knowledge_anomalies SET explanations_json=? WHERE namespace=? AND anomaly_id=?",
            [_canon(ranked), namespace, anomaly_id],
        )
        self._audit(
            namespace,
            "correlate",
            anomaly_id,
            principal_id,
            {"candidates": len(ranked)},
        )
        return {**anomaly, "explanations": ranked}

    def deliver(
        self,
        namespace,
        anomaly_id,
        subscriber_id,
        *,
        principal_id,
        scopes,
        delivery_outcome="delivered",
        cancel_requested=False,
    ):
        _require(scopes, DELIVER_SCOPE)
        anomaly = self.anomaly(namespace, anomaly_id, scopes={READ_SCOPE})
        watch = self.watch(namespace, anomaly["watch_id"], scopes={READ_SCOPE})
        policy = watch["notification"]
        group_key = str(policy.get("group_key", anomaly["signal_key"]))
        bucket = anomaly["observed_at_ms"] // max(
            int(policy.get("dedupe_window_ms", 300_000)), 1
        )
        delivery_key = _hash([watch["watch_id"], group_key, subscriber_id, bucket])
        alert_id = "anomaly-alert:" + delivery_key[:24]
        prior = self.conn.execute(
            "SELECT status,attempts,history_json FROM anomaly_alerts WHERE namespace=? AND delivery_key=?",
            [namespace, delivery_key],
        ).fetchone()
        if prior:
            return {**self._alert(namespace, alert_id), "deduplicated": True}
        quiet = policy.get("quiet_until_ms") and self.now() < int(
            policy["quiet_until_ms"]
        )
        status = (
            "cancelled"
            if cancel_requested
            else "suppressed"
            if quiet
            else "delivered"
            if delivery_outcome == "delivered"
            else "retrying"
        )
        attempts = 0 if status in {"cancelled", "suppressed"} else 1
        next_attempt = (
            self.now() + int(policy.get("retry_delay_ms", 60_000))
            if status == "retrying"
            else None
        )
        history = [{"status": status, "at_ms": self.now(), "outcome": delivery_outcome}]
        self.conn.execute(
            "INSERT INTO anomaly_alerts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                alert_id,
                namespace,
                anomaly_id,
                group_key,
                subscriber_id,
                status,
                attempts,
                next_attempt,
                delivery_key,
                _canon(history),
                self.now(),
            ],
        )
        self._audit(namespace, "deliver", alert_id, principal_id, {"status": status})
        return {**self._alert(namespace, alert_id), "deduplicated": False}

    def _alert(self, namespace, alert_id):
        row = self.conn.execute(
            "SELECT anomaly_id,group_key,subscriber_id,status,attempts,next_attempt_ms,delivery_key,history_json FROM anomaly_alerts WHERE namespace=? AND alert_id=?",
            [namespace, alert_id],
        ).fetchone()
        if not row:
            raise KnowledgeAnomalyError("alert_not_found", "alert not found")
        return {
            "contract": ALERT_CONTRACT,
            "alert_id": alert_id,
            "namespace": namespace,
            "anomaly_id": row[0],
            "group_key": row[1],
            "subscriber_id": row[2],
            "status": row[3],
            "attempts": int(row[4]),
            "next_attempt_ms": row[5],
            "delivery_key": row[6],
            "history": _load(row[7], []),
        }

    def transition_alert(
        self, namespace, alert_id, action, actor_id, *, principal_id, scopes
    ):
        _require(scopes, DELIVER_SCOPE)
        if action not in {"acknowledged", "resolved", "open"}:
            raise KnowledgeAnomalyError(
                "invalid_alert_action", "unsupported alert transition"
            )
        alert = self._alert(namespace, alert_id)
        history = alert["history"] + [
            {"status": action, "actor_id": actor_id, "at_ms": self.now()}
        ]
        self.conn.execute(
            "UPDATE anomaly_alerts SET status=?,history_json=? WHERE namespace=? AND alert_id=?",
            [action, _canon(history), namespace, alert_id],
        )
        self._audit(
            namespace, "transition_alert", alert_id, principal_id, {"action": action}
        )
        return self._alert(namespace, alert_id)

    def history(self, namespace, *, scopes, limit=100, offset=0):
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT alert_id FROM anomaly_alerts WHERE namespace=? ORDER BY created_at_ms DESC LIMIT ? OFFSET ?",
            [namespace, _limit(limit, 500), max(int(offset), 0)],
        ).fetchall()
        return {
            "namespace": namespace,
            "alerts": [self._alert(namespace, row[0]) for row in rows],
            "limit": _limit(limit, 500),
            "offset": max(int(offset), 0),
        }

    def health(self, namespace, *, scopes):
        _require(scopes, READ_SCOPE)
        active = self.conn.execute(
            "SELECT count(*) FROM anomaly_watches WHERE namespace=? AND status='active'",
            [namespace],
        ).fetchone()[0]
        retrying = self.conn.execute(
            "SELECT count(*) FROM anomaly_alerts WHERE namespace=? AND status='retrying'",
            [namespace],
        ).fetchone()[0]
        open_count = self.conn.execute(
            "SELECT count(*) FROM knowledge_anomalies WHERE namespace=? AND status='open'",
            [namespace],
        ).fetchone()[0]
        return {
            "contract": HEALTH_CONTRACT,
            "namespace": namespace,
            "status": "degraded" if retrying else "healthy",
            "active_watches": int(active),
            "open_anomalies": int(open_count),
            "retrying_alerts": int(retrying),
        }
