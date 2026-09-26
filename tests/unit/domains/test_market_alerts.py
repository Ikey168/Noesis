from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains.market.alerts import (
    ALERT_DELIVER_SCOPE,
    ALERT_EXECUTE_SCOPE,
    ALERT_READ_SCOPE,
    ALERT_WRITE_SCOPE,
    MarketAlertError,
    MarketAlertStore,
)
from src.domains.market.entitlements import MarketEntitlementStore

ROOT = Path(__file__).resolve().parents[3]
NS = "market:fixture"
WRITE = {ALERT_WRITE_SCOPE, "namespace:market:fixture:write"}
EXECUTE = {ALERT_EXECUTE_SCOPE, "namespace:market:fixture:write"}
READ = {ALERT_READ_SCOPE, "namespace:market:fixture:read"}
DELIVER = {ALERT_DELIVER_SCOPE, "namespace:market:fixture:write"}


def test_market_alerts_keep_rule_revisions_input_receipts_source_revisions_and_delivery_history():
    conn = duckdb.connect(":memory:")
    store = MarketAlertStore(conn, now=lambda: 1_000)
    watch = store.save_watch(
        NS,
        "subject-watch",
        1,
        {
            "stale_after_ms": 100,
            "triggers": [
                {
                    "rule_id": "close-high",
                    "type": "price_threshold",
                    "listing_id": "listing:subject",
                    "field": "close",
                    "operator": "gte",
                    "value": "100",
                },
                {"rule_id": "filing", "type": "new_filing", "issuer_id": "issuer:subject"},
            ],
        },
        {"dedupe_window_ms": 100, "retry_delay_ms": 10},
        owner="alice",
        principal_id="alice",
        scopes=WRITE,
    )
    result = store.run(
        NS,
        watch["watch_id"],
        [
            {
                "kind": "price",
                "listing_id": "listing:subject",
                "close": "101",
                "observed_at_ms": 950,
                "source_revision_ids": ["bar-rev-1"],
            },
            {"kind": "filing", "issuer_id": "issuer:subject", "observed_at_ms": 940, "source_revision_ids": ["filing-rev-1"]},
            {"kind": "price", "listing_id": "listing:subject", "close": "105", "observed_at_ms": 1},
        ],
        generation=7,
        as_of_ms=1_000,
        publicly_available_by_ms=1_000,
        acquired_by_ms=1_000,
        principal_id="alice",
        scopes=EXECUTE,
    )
    assert result["status"] == "completed"
    assert {item["trigger_type"] for item in result["triggered"]} == {"price_threshold", "new_filing"}
    assert result["triggered"][0]["source_revision_ids"]
    assert any(item["reason"] == "stale_observation" for item in result["decisions"])
    assert result["rule_revision_id"]
    replay = store.run(
        NS,
        watch["watch_id"],
        [
            {"kind": "price", "listing_id": "listing:subject", "close": "101", "observed_at_ms": 950, "source_revision_ids": ["bar-rev-1"]},
            {"kind": "filing", "issuer_id": "issuer:subject", "observed_at_ms": 940, "source_revision_ids": ["filing-rev-1"]},
            {"kind": "price", "listing_id": "listing:subject", "close": "105", "observed_at_ms": 1},
        ],
        generation=7,
        as_of_ms=1_000,
        publicly_available_by_ms=1_000,
        acquired_by_ms=1_000,
        principal_id="alice",
        scopes=EXECUTE,
    )
    assert replay["idempotent"]
    delivered = store.deliver(NS, result["anomaly_ids"][0], "alice", principal_id="alice", scopes=DELIVER)
    assert delivered["status"] == "delivered"
    assert delivered["market_rule_version"] == 1
    assert store.deliver(NS, result["anomaly_ids"][0], "alice", principal_id="alice", scopes=DELIVER)["deduplicated"]
    history = store.history(NS, principal_id="alice", scopes=READ)
    assert len(history["alerts"]) == 1
    Draft7Validator(json.loads((ROOT / "contracts/schemas/jsonschema/noesis-market-alert-watch-v1.json").read_text())).validate(watch)
    Draft7Validator(json.loads((ROOT / "contracts/schemas/jsonschema/noesis-market-alert-run-v1.json").read_text())).validate(result)
    conn.close()


def test_market_alerts_retry_owner_isolation_and_rights_suppression():
    conn = duckdb.connect(":memory:")
    clock = [100]
    store = MarketAlertStore(conn, now=lambda: clock[0])
    watch = store.save_watch(
        NS,
        "retry-watch",
        1,
        {"triggers": [{"type": "source_correction", "rule_id": "correction"}]},
        {"retry_delay_ms": 10},
        owner="alice",
        principal_id="alice",
        scopes=WRITE,
    )
    with pytest.raises(MarketAlertError, match="another principal"):
        store.inspect_watch(NS, watch["watch_id"], principal_id="bob", scopes=READ)
    result = store.run(
        NS,
        watch["watch_id"],
        [{"kind": "source_correction", "observed_at_ms": 100, "source_revision_ids": ["src-1"]}],
        generation=1,
        as_of_ms=100,
        publicly_available_by_ms=100,
        acquired_by_ms=100,
        principal_id="alice",
        scopes=EXECUTE,
    )
    failed = store.deliver(NS, result["anomaly_ids"][0], "alice", delivery_outcome="failed", principal_id="alice", scopes=DELIVER)
    assert failed["status"] == "retrying" and failed["attempts"] == 1
    clock[0] = 111
    retried = store.deliver(NS, result["anomaly_ids"][0], "alice", principal_id="alice", scopes=DELIVER)
    assert retried["status"] == "delivered" and retried["retried"]
    conn.close()


def test_market_alerts_reject_changed_revision_and_suppress_revoked_source():
    conn = duckdb.connect(":memory:")
    entitlements = MarketEntitlementStore(conn, now=lambda: 100)
    entitlements.put_entitlement(
        NS,
        "ent-1",
        provider="fixture-provider",
        license_id="fixture-license",
        capabilities=["read", "display", "retain"],
        evidence_ref="evidence:fixture",
        decision_ref="decision:fixture",
        principal_id="operator",
        scopes={"operator"},
        effective_at_ms=0,
    )
    store = MarketAlertStore(conn, now=lambda: 100)
    watch = store.save_watch(
        NS,
        "rights-watch",
        1,
        {"triggers": [{"type": "source_correction", "rule_id": "correction"}]},
        {},
        owner="alice",
        principal_id="alice",
        scopes=WRITE,
    )
    with pytest.raises(MarketAlertError, match="immutable"):
        store.save_watch(
            NS,
            "rights-watch",
            1,
            {"triggers": [{"type": "release", "rule_id": "different"}]},
            {},
            owner="alice",
            principal_id="alice",
            scopes=WRITE,
        )
    observation = {
        "kind": "source_correction",
        "observed_at_ms": 100,
        "source_revision_ids": ["src-1"],
        "source_refs": [
            {
                "source_ref_id": "src-1",
                "entitlement_id": "ent-1",
                "provider": "fixture-provider",
                "license_id": "fixture-license",
                "retrieved_at_ms": 0,
            }
        ],
    }
    entitlement_scope = {"market:entitlement:ent-1:display"}
    allowed = store.run(
        NS,
        watch["watch_id"],
        [observation],
        generation=1,
        as_of_ms=100,
        publicly_available_by_ms=100,
        acquired_by_ms=100,
        principal_id="alice",
        scopes=EXECUTE | entitlement_scope,
    )
    assert allowed["triggered"]
    entitlements.put_entitlement(
        NS,
        "ent-1",
        provider="fixture-provider",
        license_id="fixture-license",
        capabilities=["read", "display", "retain"],
        evidence_ref="evidence:fixture",
        decision_ref="decision:fixture-revoked",
        principal_id="operator",
        scopes={"operator"},
        status="revoked",
        effective_at_ms=100,
        expected_revision=1,
    )
    suppressed = store.run(
        NS,
        watch["watch_id"],
        [observation],
        generation=2,
        as_of_ms=100,
        publicly_available_by_ms=100,
        acquired_by_ms=100,
        principal_id="alice",
        scopes=EXECUTE | entitlement_scope,
    )
    assert suppressed["decisions"][0]["reason"] == "entitlement_revoked"
    revoked_delivery = store.deliver(
        NS,
        allowed["anomaly_ids"][0],
        "alice",
        principal_id="alice",
        scopes=DELIVER | entitlement_scope,
    )
    assert revoked_delivery["status"] == "suppressed"
    assert revoked_delivery["suppression_reason"] == "entitlement_revoked"
    conn.close()
