"""Optional ADWIN operational streams using existing anomaly publications."""

import json
from importlib.metadata import version

from src.kb.knowledge_anomalies import (
    EXECUTE_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    KnowledgeAnomalyError,
    KnowledgeAnomalyStore,
    _hash,
    _require,
)

PIN = "0.26.1"


def detector(config):
    from river.drift import ADWIN

    if version("river") != PIN:
        raise KnowledgeAnomalyError("unsupported_version", "River " + PIN + " required")
    return ADWIN(
        delta=config["delta"],
        clock=32,
        max_buckets=5,
        min_window_length=5,
        grace_period=32,
    )


def summary(model, samples, missing):
    return {
        "method": "river-adwin",
        "version": PIN,
        "samples": samples,
        "missing": missing,
        "width": model.width,
        "mean": model.estimation if samples else None,
        "variance": model.variance,
        "score_semantics": "binary change flag, not accuracy or z-score",
    }


class _Publication(KnowledgeAnomalyStore):
    """Use the native run/alert contract with an already computed detector flag."""

    def __init__(self, conn, measurement):
        super().__init__(conn, initialize=False)
        self.measurement = measurement

    def simulate(self, namespace, watch_id, observations, *, scopes, limit=1000):
        _require(scopes, READ_SCOPE)
        return {
            "detected": True,
            "score": 1.0,
            "threshold": 1.0,
            "baseline": self.measurement,
            "observation": observations[-1],
            "late_arrival": False,
        }


class ADWINStream:
    def __init__(
        self,
        conn,
        namespace,
        stream_id,
        *,
        principal_id,
        scopes,
        signal="extraction_failure",
        delta=0.002,
        max_events=20000,
    ):
        self.conn, self.namespace, self.stream_id = conn, namespace, stream_id
        self._auth(principal_id, scopes, WRITE_SCOPE)
        if (
            signal not in {"extraction_failure", "labelled_prediction_error"}
            or not stream_id
            or len(stream_id) > 200
            or not 0.000001 <= delta <= 0.1
            or not 32 <= max_events <= 50000
        ):
            raise KnowledgeAnomalyError(
                "invalid_stream", "unsupported signal or detector bounds"
            )
        self.config = {
            "signal": signal,
            "delta": delta,
            "max_events": max_events,
            "river": PIN,
            "clock": 32,
            "max_buckets": 5,
            "min_window_length": 5,
            "grace_period": 32,
            "category": "operational",
        }
        self.anomalies = KnowledgeAnomalyStore(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS adwin_streams(namespace TEXT,stream_id TEXT,config TEXT,state TEXT,PRIMARY KEY(namespace,stream_id))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS adwin_deliveries(namespace TEXT,stream_id TEXT,request_id TEXT,input_hash TEXT,receipt TEXT,PRIMARY KEY(namespace,stream_id,request_id))"
        )
        row = conn.execute(
            "SELECT config FROM adwin_streams WHERE namespace=? AND stream_id=?",
            [namespace, stream_id],
        ).fetchone()
        if row and json.loads(row[0]) != self.config:
            raise KnowledgeAnomalyError(
                "stream_conflict", "stream configuration is immutable"
            )
        self.watch = self.anomalies.register_watch(
            namespace,
            "adwin:" + stream_id,
            1,
            "event_rate",
            {"category": "operational", "stream_id": stream_id, "signal": signal},
            {"window": 32, "minimum_points": 32},
            {"kind": "river-adwin", **self.config},
            {"dedupe_window_ms": 300000, "group_key": stream_id},
            principal_id=principal_id,
            scopes=scopes,
        )
        if not row:
            model = detector(self.config)
            state = {
                "events": [],
                "watermark": 0,
                "observed_at_ms": 0,
                "detector": summary(model, 0, 0),
            }
            conn.execute(
                "INSERT INTO adwin_streams VALUES(?,?,?,?)",
                [namespace, stream_id, json.dumps(self.config), json.dumps(state)],
            )

    def _auth(self, principal, scopes, operation):
        _require(scopes, operation)
        if (
            not principal
            or "operator" not in scopes
            and f"namespace:{self.namespace}:write" not in scopes
        ):
            raise KnowledgeAnomalyError(
                "unauthorized", "authenticated namespace writer required"
            )

    def inspect(self, *, principal_id, scopes):
        self._auth(principal_id, scopes, READ_SCOPE)
        return json.loads(
            self.conn.execute(
                "SELECT state FROM adwin_streams WHERE namespace=? AND stream_id=?",
                [self.namespace, self.stream_id],
            ).fetchone()[0]
        )

    def consume(self, request_id, events, *, principal_id, scopes):
        self._auth(principal_id, scopes, EXECUTE_SCOPE)
        if (
            not request_id
            or len(request_id) > 200
            or not isinstance(events, list)
            or not 1 <= len(events) <= 1000
            or len(json.dumps(events)) > 1_000_000
        ):
            raise KnowledgeAnomalyError(
                "invalid_batch", "one to 1000 bounded events required"
            )
        input_hash = _hash([principal_id, events])
        previous = self.conn.execute(
            "SELECT input_hash,receipt FROM adwin_deliveries WHERE namespace=? AND stream_id=? AND request_id=?",
            [self.namespace, self.stream_id, request_id],
        ).fetchone()
        if previous:
            if previous[0] != input_hash:
                raise KnowledgeAnomalyError(
                    "request_conflict", "request ID reused with changed input"
                )
            return json.loads(previous[1])
        state = self.inspect(principal_id=principal_id, scopes=scopes | {READ_SCOPE})
        retained = {e["event_id"]: e for e in state["events"]}
        accepted = []
        watermark, timestamp = state["watermark"], state["observed_at_ms"]
        for event in events:
            if (
                not isinstance(event, dict)
                or not {
                    "event_id",
                    "sequence",
                    "observed_at_ms",
                    "value",
                    "evidence_id",
                }
                <= set(event)
                or not set(event)
                <= {
                    "event_id",
                    "sequence",
                    "observed_at_ms",
                    "value",
                    "evidence_id",
                    "missing_reason",
                    "label_origin",
                }
            ):
                raise KnowledgeAnomalyError(
                    "invalid_event",
                    "event identity, ordering, value and evidence required",
                )
            if any(
                not isinstance(event[k], str) or not 1 <= len(event[k]) <= 300
                for k in ("event_id", "evidence_id")
            ) or any(
                type(event[k]) is not int or not 0 <= event[k] < 2**63
                for k in ("sequence", "observed_at_ms")
            ):
                raise KnowledgeAnomalyError(
                    "invalid_event", "invalid event identity or timestamp"
                )
            value = event["value"]
            if value is None:
                if (
                    not isinstance(event.get("missing_reason"), str)
                    or not 1 <= len(event["missing_reason"]) <= 300
                ):
                    raise KnowledgeAnomalyError(
                        "missing_reason", "missing samples need an explicit reason"
                    )
            elif type(value) is not int or value not in {0, 1}:
                raise KnowledgeAnomalyError(
                    "invalid_signal", "a measured binary failure/error is required"
                )
            if (
                value is not None
                and self.config["signal"] == "labelled_prediction_error"
                and event.get("label_origin") != "independent-human"
            ):
                raise KnowledgeAnomalyError(
                    "unlabelled_quality",
                    "prediction error requires independent human label provenance",
                )
            existing = retained.get(event["event_id"])
            if existing:
                if existing != event:
                    raise KnowledgeAnomalyError(
                        "event_conflict", "duplicate event changed"
                    )
                continue
            if (
                event["sequence"] != watermark + 1
                or event["observed_at_ms"] < timestamp
            ):
                raise KnowledgeAnomalyError(
                    "late_or_missing_event",
                    "events must be consecutive and time ordered; missing samples are explicit null events",
                )
            watermark, timestamp = event["sequence"], event["observed_at_ms"]
            retained[event["event_id"]] = event
            accepted.append(event)
        if len(state["events"]) + len(accepted) > self.config["max_events"]:
            raise KnowledgeAnomalyError(
                "stream_capacity",
                "stream retention bound reached; start a separately calibrated stream",
            )
        model = detector(self.config)
        samples = missing = 0
        for event in state["events"]:
            if event["value"] is None:
                missing += 1
            else:
                model.update(event["value"])
                samples += 1
        if summary(model, samples, missing) != state["detector"]:
            raise KnowledgeAnomalyError(
                "state_mismatch", "retained detector state failed replay"
            )
        detections = []
        for event in accepted:
            if event["value"] is None:
                missing += 1
                continue
            model.update(event["value"])
            samples += 1
            if model.drift_detected:
                detections.append(
                    (
                        event,
                        {
                            **summary(model, samples, missing),
                            "signal": self.config["signal"],
                            "category": "operational",
                            "event_evidence_id": event["evidence_id"],
                        },
                    )
                )
        state.update(
            events=[*state["events"], *accepted],
            watermark=watermark,
            observed_at_ms=timestamp,
            detector=summary(model, samples, missing),
        )
        receipt = {
            "contract": "noesis-adwin-delivery-v1",
            "river": PIN,
            "stream_id": self.stream_id,
            "namespace": self.namespace,
            "request_id": request_id,
            "accepted": len(accepted),
            "duplicates": len(events) - len(accepted),
            "watermark": watermark,
            "detector": state["detector"],
            "state_hash": _hash(state),
            "anomaly_ids": [],
        }
        self.conn.execute("BEGIN TRANSACTION")
        try:
            for event, measurement in detections:
                publication = _Publication(self.conn, measurement).run(
                    self.namespace,
                    self.watch["watch_id"],
                    [{**event, "signal_key": self.config["signal"]}],
                    event["sequence"],
                    principal_id=principal_id,
                    scopes=scopes,
                )
                receipt["anomaly_ids"].extend(publication["anomaly_ids"])
            self.conn.execute(
                "UPDATE adwin_streams SET state=? WHERE namespace=? AND stream_id=?",
                [json.dumps(state), self.namespace, self.stream_id],
            )
            self.conn.execute(
                "INSERT INTO adwin_deliveries VALUES(?,?,?,?,?)",
                [
                    self.namespace,
                    self.stream_id,
                    request_id,
                    input_hash,
                    json.dumps(receipt),
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return receipt
