from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.knowledge_retention import (
    ADMIN_SCOPE,
    EXECUTE_SCOPE,
    READ_SCOPE,
    KnowledgeRetentionError,
    KnowledgeRetentionStore,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def setup_store():
    store = KnowledgeRetentionStore(duckdb.connect(":memory:"), now=lambda: 1_000)
    store.register_policy(
        "research",
        "base",
        1,
        {
            "minimum_age_ms": 100,
            "maximum_value_score": 0.8,
            "object_classes": ["document", "claim"],
        },
        principal_id="a",
        scopes={ADMIN_SCOPE},
    )
    policy = store.register_policy(
        "research",
        "derived",
        1,
        {"minimum_age_ms": 200},
        parent_policy_id="base",
        principal_id="a",
        scopes={ADMIN_SCOPE},
    )
    return store, policy


def add(store, object_id, *, age=500, dependencies=(), pins=(), value=0.1):
    return store.register_object(
        "research",
        object_id,
        "document",
        "derived",
        1,
        {"id": object_id},
        created_at_ms=1_000 - age,
        dependencies=dependencies,
        pins=pins,
        value_score=value,
        principal_id="a",
        scopes={ADMIN_SCOPE},
    )


def test_policy_conflict_inheritance_indefinite_hold_and_dry_run():
    store, policy = setup_store()
    assert policy["effective_rules"]["object_classes"] == ["document", "claim"]
    assert policy["effective_rules"]["minimum_age_ms"] == 200
    add(store, "d1")
    assert store.explain("research", "d1", scopes={READ_SCOPE})["eligible"]
    hold = store.place_hold(
        "research", "d1", "litigation", principal_id="a", scopes={ADMIN_SCOPE}
    )
    explanation = store.explain("research", "d1", scopes={READ_SCOPE})
    assert (
        not explanation["eligible"]
        and explanation["reason_codes"] == ["legal_hold"]
        and hold["expires_at_ms"] is None
    )
    store.release_hold(
        "research", hold["hold_id"], principal_id="a", scopes={ADMIN_SCOPE}
    )
    with pytest.raises(KnowledgeRetentionError, match="immutable"):
        store.register_policy(
            "research",
            "base",
            1,
            {"minimum_age_ms": 0},
            principal_id="a",
            scopes={ADMIN_SCOPE},
        )
    validate("noesis-retention-policy-v1.json", policy)


def test_content_addressed_checkpoint_interrupt_checksum_and_schema_upgrade():
    store, _ = setup_store()
    records = [{"revision": n, "value": f"v{n}"} for n in range(50)]
    checkpoint = store.checkpoint(
        "research",
        1,
        50,
        records,
        schema_version="2",
        tombstones=["old:1"],
        principal_id="a",
        scopes={EXECUTE_SCOPE},
    )
    verified = store.verify_checkpoint(
        "research", checkpoint["checkpoint_id"], scopes={READ_SCOPE}
    )
    mismatch = store.verify_checkpoint(
        "research",
        checkpoint["checkpoint_id"],
        records=[{"tampered": True}],
        scopes={READ_SCOPE},
    )
    assert verified["verified"] and not mismatch["verified"]
    cancelled = store.checkpoint(
        "research",
        51,
        60,
        records,
        schema_version="3",
        cancel_requested=True,
        principal_id="a",
        scopes={EXECUTE_SCOPE},
    )
    assert cancelled["status"] == "cancelled"
    validate("noesis-retention-checkpoint-v1.json", verified)


def test_archive_partial_unavailable_encryption_cancel_restore_atomic(tmp_path):
    store, _ = setup_store()
    checkpoint = store.checkpoint(
        "research",
        1,
        2,
        [{"id": "a"}],
        schema_version="1",
        principal_id="a",
        scopes={EXECUTE_SCOPE},
    )
    with pytest.raises(KnowledgeRetentionError, match="encrypted bytes"):
        store.archive("research", checkpoint["checkpoint_id"],
            {"driver": "filesystem", "uri": str(tmp_path / "encrypted")},
            encryption={"algorithm": "age", "key_id": "k1"},
            principal_id="a", scopes={EXECUTE_SCOPE})
    with pytest.raises(KnowledgeRetentionError, match="determined by byte I/O"):
        store.archive("research", checkpoint["checkpoint_id"],
            {"driver": "filesystem", "uri": str(tmp_path / "partial")},
            partial=True, principal_id="a", scopes={EXECUTE_SCOPE})
    unavailable = store.archive(
        "research",
        checkpoint["checkpoint_id"],
        {"driver": "offline"},
        storage_available=False,
        principal_id="a",
        scopes={EXECUTE_SCOPE},
    )
    assert unavailable["status"] == "unavailable"
    archived = store.archive(
        "research",
        checkpoint["checkpoint_id"],
        {"driver": "filesystem", "uri": str(tmp_path / "final")},
        principal_id="a",
        scopes={EXECUTE_SCOPE},
    )
    restored = store.restore(
        "research", archived["archive_id"], principal_id="a", scopes={EXECUTE_SCOPE}
    )
    assert restored["restored_atomically"] and restored["identity_verified"]
    validate("noesis-archive-manifest-v1.json", restored)


def test_gc_shared_dependencies_new_pin_race_failure_cancel_and_replay():
    store, _ = setup_store()
    add(store, "dependency")
    add(store, "owner", dependencies=["dependency"])
    add(store, "free")
    plan = store.plan_gc(
        "research", ["dependency", "free"], principal_id="a", scopes={ADMIN_SCOPE}
    )
    assert (
        plan["eligible"] == ["free"]
        and "reachable_from_retained_object"
        in plan["blocked"]["dependency"]["reason_codes"]
    )
    failed = store.execute_gc(
        "research",
        plan,
        deletion_outcome="io_error",
        principal_id="a",
        scopes={EXECUTE_SCOPE},
    )
    assert (
        failed["status"] == "failed"
        and store.explain("research", "free", scopes={READ_SCOPE})["eligible"]
    )
    fresh = store.plan_gc("research", ["free"], principal_id="a", scopes={ADMIN_SCOPE})
    store.conn.execute(
        "UPDATE retention_objects SET pins_json='[\"snapshot:new\"]' WHERE object_id='free'"
    )
    with pytest.raises(KnowledgeRetentionError, match="changed"):
        store.execute_gc("research", fresh, principal_id="a", scopes={EXECUTE_SCOPE})
    store.conn.execute(
        "UPDATE retention_objects SET pins_json='[]' WHERE object_id='free'"
    )
    final = store.plan_gc("research", ["free"], principal_id="a", scopes={ADMIN_SCOPE})
    done = store.execute_gc("research", final, principal_id="a", scopes={EXECUTE_SCOPE})
    assert (
        done["tombstoned"] == ["free"]
        and store.execute_gc(
            "research", final, principal_id="a", scopes={EXECUTE_SCOPE}
        )["idempotent"]
    )
    validate("noesis-retention-gc-plan-v1.json", plan)
    validate("noesis-retention-job-v1.json", done)


def test_auth_bounded_audit_health_and_six_domains():
    store = KnowledgeRetentionStore(duckdb.connect(":memory:"), now=lambda: 1_000)
    for namespace in (
        "research",
        "political",
        "economic",
        "osint",
        "technical",
        "scientific",
    ):
        store.register_policy(
            namespace,
            "p",
            1,
            {"minimum_age_ms": 0},
            principal_id="a",
            scopes={ADMIN_SCOPE},
        )
    with pytest.raises(KnowledgeRetentionError, match="scope"):
        store.health("research", scopes=set())
    assert store.health("research", scopes={READ_SCOPE})["status"] == "healthy"
    assert store.conn.execute("SELECT count(*) FROM retention_audit").fetchone()[0] == 6
