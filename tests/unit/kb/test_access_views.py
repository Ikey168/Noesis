from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.access_views import (
    ADMIN_SCOPE,
    EXPORT_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    AccessViewError,
    AccessViewStore,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def rules(**extra):
    return {
        "allowed_principals": ["analyst"],
        "allowed_purposes": ["research"],
        "allowed_classifications": ["public"],
        "allowed_transformations": ["read", "query", "summary"],
        **extra,
    }


def setup_store(now=100):
    store = AccessViewStore(duckdb.connect(":memory:"), now=lambda: now)
    policy = store.register_policy(
        "research", "standard", 1, rules(), principal_id="admin", scopes={ADMIN_SCOPE}
    )
    store.register_object(
        "research",
        "claim",
        "public",
        "public",
        "standard",
        1,
        {"text": "visible"},
        lineage=[{"source_id": "s1"}],
        principal_id="writer",
        scopes={WRITE_SCOPE},
    )
    store.register_object(
        "research",
        "claim",
        "secret",
        "secret",
        "standard",
        1,
        {"text": "classified"},
        lineage=[{"source_id": "s2"}],
        principal_id="writer",
        scopes={WRITE_SCOPE},
    )
    return store, policy


def test_default_deny_conflicts_versions_explanation_and_namespace():
    store, policy = setup_store()
    assert policy["default"] == "deny"
    allowed = store.decide(
        "research",
        "claim",
        "public",
        principal_id="analyst",
        purpose="research",
        scopes={READ_SCOPE},
    )
    denied = store.decide(
        "research",
        "claim",
        "secret",
        principal_id="analyst",
        purpose="research",
        scopes={READ_SCOPE},
    )
    assert allowed["allowed"] and not denied["allowed"]
    assert denied["error"]["code"] == "not_available" and "reason_codes" not in denied
    explained = store.decide(
        "research",
        "claim",
        "secret",
        principal_id="analyst",
        purpose="research",
        scopes={ADMIN_SCOPE},
        disclose=True,
    )
    assert "classification_not_allowed" in explained["reason_codes"]
    isolated = store.decide(
        "other",
        "claim",
        "public",
        principal_id="analyst",
        purpose="research",
        scopes={ADMIN_SCOPE},
        disclose=True,
    )
    assert not isolated["allowed"] and isolated["reason_codes"] == [
        "object_not_visible"
    ]
    upgraded = store.register_policy(
        "research",
        "standard",
        2,
        rules(allowed_classifications=["public", "secret"]),
        principal_id="admin",
        scopes={ADMIN_SCOPE},
    )
    assert upgraded["version"] == 2
    validate("noesis-access-view-policy-v1.json", policy)
    validate("noesis-access-decision-v1.json", explained)


def test_filter_before_counts_pagination_and_mixed_access_join():
    store, _ = setup_store()
    candidates = [
        {"object_type": "claim", "object_id": "secret", "score": 100},
        {"object_type": "claim", "object_id": "public", "score": 1},
    ]
    result = store.filter_query(
        "research",
        candidates,
        principal_id="analyst",
        purpose="research",
        scopes={READ_SCOPE},
        limit=1,
    )
    assert result["visible_count"] == 1
    assert [item["object_id"] for item in result["results"]] == ["public"]
    assert "denied_count" not in result and result["next_offset"] is None
    validate("noesis-access-decision-v1.json", result)


def test_redacted_projection_inference_leakage_policy_invalidation():
    store, _ = setup_store()
    projected = store.derive_redacted(
        "research",
        "claim",
        "public",
        "summary",
        {"summary": "safe"},
        principal_id="analyst",
        purpose="research",
        scopes={WRITE_SCOPE},
    )
    assert projected["safe_lineage"] and "source_id" not in projected["safe_lineage"][0]
    with pytest.raises(AccessViewError, match="unapproved"):
        store.derive_redacted(
            "research",
            "claim",
            "public",
            "summary",
            {"text": "visible"},
            principal_id="analyst",
            purpose="research",
            scopes={WRITE_SCOPE},
        )
    store.register_policy(
        "research", "standard", 2, rules(), principal_id="admin", scopes={ADMIN_SCOPE}
    )
    assert (
        store.health("research", scopes={ADMIN_SCOPE})["invalidated_projections"] == 1
    )
    validate("noesis-redacted-projection-v1.json", projected)


def test_export_expiry_recipient_watermark_redistribution_and_revocation():
    store, _ = setup_store(now=100)
    grant = store.create_grant(
        "research",
        "recipient:a",
        "research",
        200,
        "standard",
        1,
        ["public"],
        principal_id="admin",
        scopes={EXPORT_SCOPE},
    )
    mismatch = store.authorize_export(
        "research",
        grant["grant_id"],
        "recipient:b",
        "research",
        ["public"],
        watermark="w",
        principal_id="admin",
        scopes={EXPORT_SCOPE},
    )
    assert (
        not mismatch["authorized"] and "recipient_mismatch" in mismatch["reason_codes"]
    )
    denied = store.authorize_export(
        "research",
        grant["grant_id"],
        "recipient:a",
        "research",
        ["public"],
        redistribution=True,
        principal_id="admin",
        scopes={EXPORT_SCOPE},
    )
    assert {"redistribution_denied", "watermark_required"} <= set(
        denied["reason_codes"]
    )
    good = store.authorize_export(
        "research",
        grant["grant_id"],
        "recipient:a",
        "research",
        ["public"],
        watermark="offline-package:a",
        principal_id="admin",
        scopes={EXPORT_SCOPE},
    )
    assert good["authorized"]
    assert (
        store.revoke_grant(
            "research", grant["grant_id"], principal_id="admin", scopes={EXPORT_SCOPE}
        )["status"]
        == "revoked"
    )
    validate("noesis-share-grant-v1.json", good)


def test_admin_separation_audit_health_and_six_domains():
    conn = duckdb.connect(":memory:")
    store = AccessViewStore(conn, now=lambda: 100)
    for namespace in (
        "research",
        "political",
        "economic",
        "osint",
        "technical",
        "scientific",
    ):
        store.register_policy(
            namespace, "p", 1, rules(), principal_id="admin", scopes={ADMIN_SCOPE}
        )
    with pytest.raises(AccessViewError, match="admin"):
        store.audit("research", scopes={READ_SCOPE})
    assert store.audit("research", scopes={ADMIN_SCOPE})["events"]
    health = store.health("research", scopes={ADMIN_SCOPE})
    assert health["status"] == "healthy"
    validate("noesis-access-view-health-v1.json", health)
