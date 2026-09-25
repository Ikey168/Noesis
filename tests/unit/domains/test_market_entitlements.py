"""Fixture-only checks for current provider rights and retention policy."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains.market.entitlements import (
    MarketEntitlementError,
    MarketEntitlementStore,
)

ROOT = Path(__file__).resolve().parents[3]
NAMESPACE = "market:rights-test"
PRINCIPAL = "reviewer:rights-test"
T0 = 1_790_000_000_000
ADMIN = {"operator"}
FULL = ["ingest", "read", "display", "retain", "derive", "cache", "evidence", "export", "redistribute"]


def source_ref(*, retrieved_at_ms=T0, entitlement_id="entitlement:fixture"):
    return {
        "provider": "fixture-provider",
        "license_id": "fixture-license",
        "entitlement_id": entitlement_id,
        "retrieved_at_ms": retrieved_at_ms,
    }


def register(store, *, entitlement_id="entitlement:fixture", capabilities=None, max_retention_ms=None, status="active", evidence_ref="fixture:rights:v1"):
    return store.put_entitlement(
        NAMESPACE,
        entitlement_id,
        provider="fixture-provider",
        license_id="fixture-license",
        capabilities=FULL if capabilities is None else capabilities,
        evidence_ref=evidence_ref,
        decision_ref="fixture:decision:v1",
        principal_id=PRINCIPAL,
        scopes=ADMIN,
        status=status,
        effective_at_ms=T0,
        max_retention_ms=max_retention_ms,
    )


def test_versioned_entitlement_persists_review_and_revocation_is_current():
    conn = duckdb.connect(":memory:")
    clock = {"now": T0}
    store = MarketEntitlementStore(conn, now=lambda: clock["now"])
    policy = register(store)
    assert policy["revision"] == 1
    assert store.put_entitlement(
        NAMESPACE,
        "entitlement:fixture",
        provider="fixture-provider",
        license_id="fixture-license",
        capabilities=FULL,
        evidence_ref="fixture:rights:v1",
        decision_ref="fixture:decision:v1",
        principal_id=PRINCIPAL,
        scopes=ADMIN,
        effective_at_ms=T0,
    )["idempotent"]
    Draft7Validator(
        json.loads(
            (ROOT / "contracts/schemas/jsonschema/noesis-market-entitlement-v1.json").read_text()
        )
    ).validate(policy)

    scope_set = {"market:entitlement:entitlement:fixture:read"}
    allowed = store.authorize_sources(
        NAMESPACE,
        [source_ref()],
        operation="read",
        principal_id=PRINCIPAL,
        scopes=scope_set,
        now_ms=T0,
    )
    assert allowed[0]["policy_revision_id"] == policy["revision_id"]

    clock["now"] = T0 + 1
    revoked = store.put_entitlement(
        NAMESPACE,
        "entitlement:fixture",
        provider="fixture-provider",
        license_id="fixture-license",
        capabilities=FULL,
        evidence_ref="fixture:rights:v1",
        decision_ref="fixture:revocation:v1",
        principal_id=PRINCIPAL,
        scopes=ADMIN,
        status="revoked",
        effective_at_ms=T0 + 1,
        expected_revision=1,
    )
    with pytest.raises(MarketEntitlementError) as exc:
        store.authorize_sources(
            NAMESPACE,
            [source_ref()],
            operation="read",
            principal_id=PRINCIPAL,
            scopes=scope_set,
            now_ms=T0 + 1,
        )
    assert revoked["revision"] == 2
    assert exc.value.code == "entitlement_revoked"
    conn.close()


def test_rights_are_namespace_scoped_and_restricted_operations_fail_closed():
    conn = duckdb.connect(":memory:")
    store = MarketEntitlementStore(conn, now=lambda: T0)
    register(store, capabilities=["ingest", "read", "retain"])
    scope_set = {"market:entitlement:entitlement:fixture:read"}

    with pytest.raises(MarketEntitlementError) as missing_namespace:
        store.authorize_sources(
            "market:other",
            [source_ref()],
            operation="read",
            principal_id=PRINCIPAL,
            scopes=scope_set,
            now_ms=T0,
        )
    assert missing_namespace.value.code == "entitlement_unavailable"

    with pytest.raises(MarketEntitlementError) as display:
        store.authorize_sources(
            NAMESPACE,
            [source_ref()],
            operation="display",
            principal_id=PRINCIPAL,
            scopes=scope_set | {"market:entitlement:entitlement:fixture:display"},
            now_ms=T0,
        )
    assert display.value.code == "operation_restricted"
    with pytest.raises(MarketEntitlementError) as export:
        store.authorize_sources(
            NAMESPACE,
            [source_ref()],
            operation="export",
            principal_id=PRINCIPAL,
            scopes=scope_set | {"market:entitlement:entitlement:fixture:export"},
            external=True,
            now_ms=T0,
        )
    assert export.value.code == "operation_restricted"
    conn.close()


def test_retention_deadline_blocks_read_display_and_export():
    conn = duckdb.connect(":memory:")
    store = MarketEntitlementStore(conn, now=lambda: T0 + 100)
    register(store, max_retention_ms=100)
    scopes = {
        "market:entitlement:entitlement:fixture:read",
        "market:entitlement:entitlement:fixture:display",
    }
    with pytest.raises(MarketEntitlementError) as expired:
        store.authorize_sources(
            NAMESPACE,
            [source_ref()],
            operation="read",
            principal_id=PRINCIPAL,
            scopes=scopes,
            now_ms=T0 + 100,
        )
    assert expired.value.code == "retention_expired"
    assert expired.value.details["retention_expires_at_ms"] == T0 + 100
    conn.close()


def test_unknown_or_secret_bearing_policy_metadata_is_rejected():
    conn = duckdb.connect(":memory:")
    store = MarketEntitlementStore(conn, now=lambda: T0)
    with pytest.raises(MarketEntitlementError) as unknown:
        register(store, capabilities=["ingest", "vendor_api_key"])
    assert unknown.value.code == "invalid_request"
    with pytest.raises(MarketEntitlementError) as blank:
        store.put_entitlement(
            NAMESPACE,
            "entitlement:blank-evidence",
            provider="fixture-provider",
            license_id="fixture-license",
            capabilities=FULL,
            evidence_ref="",
            decision_ref="fixture:decision:v1",
            principal_id=PRINCIPAL,
            scopes=ADMIN,
            effective_at_ms=T0,
        )
    assert blank.value.code == "invalid_request"
    conn.close()
