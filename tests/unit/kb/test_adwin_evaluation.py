import json

import duckdb
import pytest

pytest.importorskip("river")

from src.kb.adwin_evaluation import ADWINStream
from src.kb.knowledge_anomalies import KnowledgeAnomalyError
from tests.unit.kb.test_knowledge_anomalies import validate

AUTH = {"principal_id": "operator", "scopes": {"operator"}}


def events(values, start=1):
    return [
        {
            "event_id": f"event-{i}",
            "sequence": i,
            "observed_at_ms": i * 1000,
            "value": v,
            "evidence_id": f"extraction-run:{i}",
            **({"missing_reason": "transport did not complete"} if v is None else {}),
        }
        for i, v in enumerate(values, start)
    ]


def test_restart_shift_publication_schema_and_alert_dedup(tmp_path):
    path = str(tmp_path / "drift.duckdb")
    conn = duckdb.connect(path)
    stream = ADWINStream(conn, "operations", "berlin-extractor", **AUTH)
    baseline = stream.consume("baseline", events([0] * 256), **AUTH)
    assert not baseline["anomaly_ids"]
    conn.close()
    conn = duckdb.connect(path)
    try:
        stream = ADWINStream(conn, "operations", "berlin-extractor", **AUTH)
        changed = events([1] * 256, 257)
        receipt = stream.consume("shift", changed, **AUTH)
        assert receipt["anomaly_ids"] and receipt["watermark"] == 512
        assert stream.consume("shift", changed, **AUTH) == receipt
        duplicate = stream.consume("duplicates", changed, **AUTH)
        assert duplicate["accepted"] == 0 and not duplicate["anomaly_ids"]
        anomaly = stream.anomalies.anomaly(
            "operations", receipt["anomaly_ids"][0], scopes={"operator"}
        )
        validate("noesis-knowledge-anomaly-v1.json", anomaly)
        assert anomaly["baseline"]["category"] == "operational"
        assert anomaly["baseline"]["method"] == "river-adwin"
        first = stream.anomalies.deliver(
            "operations", anomaly["anomaly_id"], "oncall", **AUTH
        )
        second = stream.anomalies.deliver(
            "operations", anomaly["anomaly_id"], "oncall", **AUTH
        )
        assert second["deduplicated"] and first["alert_id"] == second["alert_id"]
        reference = ADWINStream(conn, "operations", "single-batch", **AUTH)
        reference.consume("all", events([0] * 256 + [1] * 256), **AUTH)
        assert (
            reference.inspect(**AUTH)["detector"] == stream.inspect(**AUTH)["detector"]
        )
    finally:
        conn.close()


def test_missing_late_duplicate_changed_and_capacity_are_explicit():
    conn = duckdb.connect()
    try:
        stream = ADWINStream(conn, "operations", "limited", max_events=32, **AUTH)
        batch = events([0, None, 1])
        receipt = stream.consume("first", batch, **AUTH)
        assert (
            receipt["detector"]["samples"] == 2 and receipt["detector"]["missing"] == 1
        )
        for request, batch, code in [
            ("gap", events([0], 5), "late_or_missing_event"),
            (
                "time",
                [{**events([0], 4)[0], "observed_at_ms": 1}],
                "late_or_missing_event",
            ),
            ("changed", events([1]), "event_conflict"),
            ("confidence", events([0.9], 4), "invalid_signal"),
            ("capacity", events([0] * 30, 4), "stream_capacity"),
        ]:
            with pytest.raises(KnowledgeAnomalyError) as failure:
                stream.consume(request, batch, **AUTH)
            assert failure.value.code == code
        assert stream.inspect(**AUTH)["watermark"] == 3
        with pytest.raises(KnowledgeAnomalyError, match="immutable"):
            ADWINStream(conn, "operations", "limited", delta=0.1, **AUTH)
    finally:
        conn.close()


def test_quality_needs_independent_labels_access_and_replay_integrity():
    conn = duckdb.connect()
    try:
        stream = ADWINStream(
            conn, "quality", "labelled", signal="labelled_prediction_error", **AUTH
        )
        with pytest.raises(KnowledgeAnomalyError, match="human"):
            stream.consume("unlabelled", events([1]), **AUTH)
        event = {
            **events([1])[0],
            "label_origin": "independent-human",
            "evidence_id": "human-evaluation:fixture:label1",
        }
        stream.consume("label", [event], **AUTH)
        with pytest.raises(KnowledgeAnomalyError, match="scope"):
            stream.consume("label", [event], **{**AUTH, "scopes": set()})
        state = stream.inspect(**AUTH)
        state["detector"]["mean"] = 0.5
        conn.execute("UPDATE adwin_streams SET state=?", [json.dumps(state)])
        with pytest.raises(KnowledgeAnomalyError, match="replay"):
            stream.consume(
                "next",
                [{**events([0], 2)[0], "label_origin": "independent-human"}],
                **AUTH,
            )
    finally:
        conn.close()
