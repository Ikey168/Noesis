from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.knowledge_anomalies import (
    DELIVER_SCOPE,
    EXECUTE_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    KnowledgeAnomalyError,
    KnowledgeAnomalyStore,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def make_watch(store, namespace="economic", version=1, **notification):
    return store.register_watch(
        namespace,
        "inflation-shift",
        version,
        "metric",
        {"metric_id": "cpi"},
        {"window": 4, "minimum_points": 3, "seasonality": "monthly"},
        {"kind": "zscore", "version": "1", "threshold": 2},
        {"dedupe_window_ms": 1000, **notification},
        principal_id="p",
        scopes={WRITE_SCOPE},
    )


def observations(last=30, **latest):
    return [
        {"value": 10, "observed_at_ms": 1},
        {"value": 11, "observed_at_ms": 2},
        {"value": 9, "observed_at_ms": 3},
        {"value": 10, "observed_at_ms": 4},
        {"value": last, "observed_at_ms": 5, "signal_key": "cpi", **latest},
    ]


def test_watch_identity_invalid_sparse_policy_version_and_baseline():
    store = KnowledgeAnomalyStore(duckdb.connect(":memory:"), now=lambda: 100)
    with pytest.raises(KnowledgeAnomalyError, match="window"):
        store.register_watch(
            "economic",
            "bad",
            1,
            "metric",
            {},
            {"window": 1},
            {},
            {},
            principal_id="p",
            scopes={WRITE_SCOPE},
        )
    watch = make_watch(store)
    assert (
        make_watch(store)["idempotent"]
        and make_watch(store, version=2)["watch_id"] != watch["watch_id"]
    )
    sparse = store.preview_baseline(
        "economic",
        watch["watch_id"],
        [{"value": 1}, {"missing": True}],
        scopes={READ_SCOPE},
    )
    assert sparse["sparse"] and sparse["missing_count"] == 1
    validate("noesis-anomaly-watch-v1.json", watch)


def test_detector_outlier_missing_late_cancel_and_deterministic_replay():
    store = KnowledgeAnomalyStore(duckdb.connect(":memory:"), now=lambda: 100)
    watch = make_watch(store)
    simulation = store.simulate(
        "economic",
        watch["watch_id"],
        observations(late_arrival=True),
        scopes={READ_SCOPE},
    )
    assert simulation["detected"] and simulation["late_arrival"]
    run = store.run(
        "economic",
        watch["watch_id"],
        observations(late_arrival=True),
        7,
        principal_id="p",
        scopes={EXECUTE_SCOPE},
    )
    replay = store.run(
        "economic",
        watch["watch_id"],
        observations(late_arrival=True),
        7,
        principal_id="p",
        scopes={EXECUTE_SCOPE},
    )
    assert replay["run_id"] == run["run_id"] and replay["idempotent"]
    anomaly = store.anomaly("economic", run["anomaly_ids"][0], scopes={READ_SCOPE})
    assert anomaly["late_arrival"] and anomaly["severity"] in {"warning", "critical"}
    cancelled = store.run(
        "economic",
        watch["watch_id"],
        observations(40),
        8,
        cancel_requested=True,
        principal_id="p",
        scopes={EXECUTE_SCOPE},
    )
    assert cancelled["status"] == "cancelled" and cancelled["processed"] == 0
    validate("noesis-anomaly-run-v1.json", run)
    validate("noesis-knowledge-anomaly-v1.json", anomaly)


def test_uncertain_multiple_cause_correlation_cross_domain():
    store = KnowledgeAnomalyStore(duckdb.connect(":memory:"), now=lambda: 100)
    watch = make_watch(store)
    run = store.run(
        "economic",
        watch["watch_id"],
        observations(),
        1,
        principal_id="p",
        scopes={EXECUTE_SCOPE},
    )
    correlated = store.correlate(
        "economic",
        run["anomaly_ids"][0],
        [
            {
                "object_type": "methodology_change",
                "object_id": "m1",
                "observed_at_ms": 5,
                "relevance": 0.9,
            },
            {
                "object_type": "political_event",
                "object_id": "e1",
                "observed_at_ms": 5,
                "relevance": 0.8,
            },
        ],
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    assert len(correlated["explanations"]) == 2
    assert all(
        x["causal_status"] == "plausible_not_proven" for x in correlated["explanations"]
    )


def test_delivery_dedupe_failure_quiet_cancel_ack_resolution():
    store = KnowledgeAnomalyStore(duckdb.connect(":memory:"), now=lambda: 100)
    quiet = make_watch(store, quiet_until_ms=200)
    run = store.run(
        "economic",
        quiet["watch_id"],
        observations(),
        1,
        principal_id="p",
        scopes={EXECUTE_SCOPE},
    )
    suppressed = store.deliver(
        "economic",
        run["anomaly_ids"][0],
        "sub",
        principal_id="p",
        scopes={DELIVER_SCOPE},
    )
    assert suppressed["status"] == "suppressed"
    assert store.deliver(
        "economic",
        run["anomaly_ids"][0],
        "sub",
        principal_id="p",
        scopes={DELIVER_SCOPE},
    )["deduplicated"]
    active = make_watch(store, version=2)
    run2 = store.run(
        "economic",
        active["watch_id"],
        observations(),
        2,
        principal_id="p",
        scopes={EXECUTE_SCOPE},
    )
    retry = store.deliver(
        "economic",
        run2["anomaly_ids"][0],
        "sub",
        delivery_outcome="failed",
        principal_id="p",
        scopes={DELIVER_SCOPE},
    )
    assert retry["status"] == "retrying" and retry["next_attempt_ms"]
    acknowledged = store.transition_alert(
        "economic",
        retry["alert_id"],
        "acknowledged",
        "analyst",
        principal_id="p",
        scopes={DELIVER_SCOPE},
    )
    resolved = store.transition_alert(
        "economic",
        retry["alert_id"],
        "resolved",
        "analyst",
        principal_id="p",
        scopes={DELIVER_SCOPE},
    )
    assert acknowledged["status"] == "acknowledged" and resolved["status"] == "resolved"
    validate("noesis-anomaly-alert-v1.json", retry)


def test_auth_bounded_history_health_six_domains():
    store = KnowledgeAnomalyStore(duckdb.connect(":memory:"), now=lambda: 100)
    for namespace in (
        "research",
        "political",
        "economic",
        "osint",
        "technical",
        "scientific",
    ):
        make_watch(store, namespace)
    with pytest.raises(KnowledgeAnomalyError, match="scope"):
        store.health("research", scopes=set())
    assert store.history("research", scopes={READ_SCOPE}, limit=10000)["limit"] == 500
    health = store.health("research", scopes={READ_SCOPE})
    assert health["active_watches"] == 1
    validate("noesis-anomaly-health-v1.json", health)
