"""Schema registry identity, MCP security, impact, and migration acceptance tests."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.schema_registry import (
    DEPRECATE_SCOPE,
    MIGRATE_SCOPE,
    READ_SCOPE,
    REGISTER_SCOPE,
    VALIDATE_SCOPE,
    SchemaRegistry,
    SchemaRegistryError,
)
from src.kb.transactions import COMMIT_SCOPE, PREVIEW_SCOPE, KnowledgeTransactionStore

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "contracts/examples/schema-registry"
SCHEMAS = ROOT / "contracts/schemas/jsonschema"
ADMIN = "registry-admin"


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def _module(
    name: str,
    version: str,
    content: dict,
    *,
    kind: str = "schema",
    policy: str = "none",
    dependencies: list[dict] | None = None,
) -> dict:
    return {
        "contract": "noesis-schema-module-v1",
        "name": name,
        "kind": kind,
        "semantic_version": version,
        "content": content,
        "owner": "tests",
        "dependencies": dependencies or [],
        "compatibility_policy": policy,
        "provenance": {"kind": "user", "source": "acceptance test"},
        "actor": {"principal_id": ADMIN, "kind": "user"},
    }


def _register(registry: SchemaRegistry, definition: dict, key: str):
    return registry.register(
        definition,
        key,
        principal_id=ADMIN,
        scopes={REGISTER_SCOPE},
    )


def _register_article_versions(registry: SchemaRegistry) -> None:
    _register(registry, _load("article-record-v1.json"), "article-v1-key")
    _register(registry, _load("article-record-v2.json"), "article-v2-key")


def _seed_objects(conn, namespace: str, count: int = 2) -> None:
    store = KnowledgeTransactionStore(conn)
    envelope = {
        "contract": "noesis-knowledge-mutation-v1",
        "batch_id": f"seed:{namespace}",
        "namespace": namespace,
        "actor": {"principal_id": ADMIN, "kind": "user"},
        "reason": "Migration acceptance fixture",
        "provenance": {"kind": "user-assertion", "method": "fixture"},
        "evidence": [{"document_id": "doc:migration-fixture"}],
        "idempotency_key": f"seed-key-{namespace}",
        "partial_batch": "atomic",
        "mutations": [
            {
                "mutation_id": f"seed-{index}",
                "type": "assert",
                "target": {
                    "kind": "object",
                    "id": f"article:{index}",
                    "expected_revision": 0,
                },
                "object_type": "article-record",
                "value": {"headline": f"Article {index}", "untouched": index},
            }
            for index in range(count)
        ],
    }
    scopes = {PREVIEW_SCOPE, COMMIT_SCOPE}
    if namespace != "corpus":
        scopes |= {
            f"knowledge:namespace:{namespace}:read",
            f"knowledge:namespace:{namespace}:write",
        }
    preview = store.preview(envelope, principal_id=ADMIN, scopes=scopes)
    store.commit(
        envelope,
        preview["approval_hash"],
        principal_id=ADMIN,
        scopes=scopes,
    )


def _define_migration(registry: SchemaRegistry, fixture: str) -> dict:
    definition = _load(fixture)
    registry.define_migration(
        definition,
        f"define-{definition['migration_id']}",
        principal_id=ADMIN,
        scopes={MIGRATE_SCOPE},
    )
    return definition


def test_contract_schemas_and_all_domain_crosswalk_fixtures():
    module_schema = json.loads((SCHEMAS / "noesis-schema-module-v1.json").read_text())
    migration_schema = json.loads(
        (SCHEMAS / "noesis-schema-migration-v1.json").read_text()
    )
    crosswalk_schema = json.loads(
        (SCHEMAS / "noesis-schema-crosswalk-v1.json").read_text()
    )
    assert not list(
        Draft7Validator(module_schema).iter_errors(_load("article-record-v1.json"))
    )
    assert list(
        Draft7Validator(module_schema).iter_errors(
            _load("invalid-module-missing-provenance.json")
        )
    )
    for name in ("migration-corpus.json", "migration-namespace.json"):
        assert not list(Draft7Validator(migration_schema).iter_errors(_load(name)))
    for domain in ("political", "economic", "technical", "scientific", "external"):
        fixture = _load(f"crosswalk-{domain}.json")
        assert not list(Draft7Validator(crosswalk_schema).iter_errors(fixture))
        assert {item["kind"] for item in fixture["mappings"]} <= {
            "field",
            "type",
            "relation",
        }


def test_builtins_resolve_and_export_without_initializing_storage():
    conn = duckdb.connect(":memory:")
    registry = SchemaRegistry(conn, initialize=False)
    first = registry.resolve(
        "schema", "knowledge-mutation", "^1.0.0", scopes={READ_SCOPE}
    )
    second = registry.resolve(
        "schema", "knowledge-mutation", "1.0.0", scopes={READ_SCOPE}
    )
    export_a = registry.export(scopes={READ_SCOPE})
    export_b = registry.export(scopes={READ_SCOPE})
    initialized = SchemaRegistry(duckdb.connect(":memory:")).export(scopes={READ_SCOPE})
    assert first["module_id"] == second["module_id"]
    assert first["provenance"]["kind"] == "builtin"
    assert export_a == export_b
    assert initialized == export_a
    assert export_a["content_hash"].startswith("sha256:")
    assert not conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name='knowledge_schema_modules'"
    ).fetchone()


def test_registration_is_immutable_content_addressed_and_durably_idempotent():
    conn = duckdb.connect(":memory:")
    registry = SchemaRegistry(conn, clock=lambda: 100)
    definition = _load("article-record-v1.json")
    created = _register(registry, definition, "register-key-001")
    replay = _register(registry, definition, "register-key-001")
    same_content_new_key = _register(registry, definition, "register-key-002")
    assert created["module_id"].endswith(created["content_hash"].split(":")[1][:16])
    assert replay["idempotent_replay"] is True
    assert same_content_new_key["module_id"] == created["module_id"]

    changed = copy.deepcopy(definition)
    changed["content"]["description"] = "silent replacement"
    with pytest.raises(SchemaRegistryError) as immutable:
        _register(registry, changed, "register-key-003")
    assert immutable.value.code == "immutable_version_conflict"
    with pytest.raises(SchemaRegistryError) as key_reuse:
        _register(registry, changed, "register-key-002")
    assert key_reuse.value.code == "idempotency_key_reused"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_schema_modules WHERE owner='research-platform'"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("ontology", {"object_types": ["Finding"], "relation_types": ["SUPPORTS"]}),
        ("constraint", {"type": "object", "required": ["confidence"]}),
        ("vocabulary", {"terms": {"finding": "A reported research result"}}),
    ],
)
def test_ontology_constraint_and_vocabulary_versions_are_content_addressed(
    kind, content
):
    registry = SchemaRegistry(duckdb.connect(":memory:"))
    result = _register(
        registry,
        _module(f"{kind}-fixture", "1.0.0", content, kind=kind),
        f"register-{kind}-key",
    )
    assert result["kind"] == kind
    assert result["module_id"].endswith(result["content_hash"].split(":")[1][:16])


def test_semver_resolution_validation_deprecation_and_machine_errors():
    conn = duckdb.connect(":memory:")
    registry = SchemaRegistry(conn)
    v1 = _module(
        "measurement",
        "1.0.0",
        {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "number"}},
        },
    )
    v11 = copy.deepcopy(v1)
    v11["semantic_version"] = "1.1.0"
    v11["content"]["properties"]["unit"] = {"type": "string"}
    one = _register(registry, v1, "measurement-v1")
    latest = _register(registry, v11, "measurement-v11")
    assert (
        registry.resolve("schema", "measurement", "^1.0.0", scopes={READ_SCOPE})[
            "module_id"
        ]
        == latest["module_id"]
    )
    assert (
        registry.resolve("schema", "measurement", "~1.0.0", scopes={READ_SCOPE})[
            "module_id"
        ]
        == one["module_id"]
    )

    invalid = registry.validate_instance(
        {"kind": "schema", "name": "measurement", "version": "1.1.0"},
        {"value": "not numeric"},
        scopes={VALIDATE_SCOPE},
    )
    assert invalid["valid"] is False
    assert invalid["errors"][0]["validator"] == "type"
    assert invalid["provenance"]["source"] == "acceptance test"

    deprecated = registry.deprecate(
        latest["module_id"],
        "superseded",
        "deprecate-key-001",
        principal_id=ADMIN,
        scopes={DEPRECATE_SCOPE},
    )
    assert deprecated["status"] == "deprecated"
    assert (
        registry.resolve("schema", "measurement", "^1.0.0", scopes={READ_SCOPE})[
            "module_id"
        ]
        == one["module_id"]
    )
    assert (
        registry.deprecate(
            latest["module_id"],
            "superseded",
            "deprecate-key-001",
            principal_id=ADMIN,
            scopes={DEPRECATE_SCOPE},
        )["idempotent_replay"]
        is True
    )


def test_compatibility_gate_detects_breaking_compatible_and_ambiguous_changes():
    old = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
    }
    optional = copy.deepcopy(old)
    optional["properties"]["description"] = {"type": "string"}
    breaking = copy.deepcopy(old)
    breaking["properties"]["name"]["type"] = "integer"
    ambiguous = {**old, "unevaluatedProperties": False}
    assert (
        SchemaRegistry.compare_content(old, optional)["classification"] == "compatible"
    )
    assert SchemaRegistry.compare_content(old, breaking)["classification"] == "breaking"
    assert (
        SchemaRegistry.compare_content(old, ambiguous)["classification"] == "ambiguous"
    )

    registry = SchemaRegistry(duckdb.connect(":memory:"))
    _register(
        registry,
        _module("compatibility-test", "1.0.0", old, policy="backward"),
        "compat-old-key",
    )
    with pytest.raises(SchemaRegistryError) as caught:
        _register(
            registry,
            _module("compatibility-test", "1.1.0", breaking, policy="backward"),
            "compat-new-key",
        )
    assert caught.value.code == "compatibility_violation"


def test_crosswalk_registration_and_lineage_impact_cover_all_consumer_types():
    conn = duckdb.connect(":memory:")
    registry = SchemaRegistry(conn)
    political = _register(
        registry,
        _module(
            "political",
            "1.0.0",
            {"object_types": ["article-record"], "relation_types": ["HOLDS_OFFICE"]},
            kind="ontology",
        ),
        "political-ontology-key",
    )
    crosswalk = registry.register_crosswalk(
        _load("crosswalk-political.json"),
        "political-crosswalk-key",
        principal_id=ADMIN,
        scopes={REGISTER_SCOPE},
    )
    assert crosswalk["kind"] == "crosswalk"
    assert crosswalk["content"]["mappings"][0]["lossy"] is True

    for kind in ("connector", "extractor", "index", "tool", "pack"):
        registry.declare_dependency(
            political["module_id"],
            kind,
            f"{kind}:fixture",
            {"reason": "acceptance"},
            principal_id=ADMIN,
            scopes={REGISTER_SCOPE},
        )
    dependent = _module(
        "dependent-record",
        "1.0.0",
        {"type": "object"},
        dependencies=[{"kind": "ontology", "name": "political", "version": "^1.0.0"}],
    )
    _register(registry, dependent, "dependent-module-key")
    _seed_objects(conn, "corpus", count=1)
    impact = registry.impact(political["module_id"], scopes={READ_SCOPE})
    for kind in (
        "connector",
        "extractor",
        "index",
        "tool",
        "pack",
        "module",
        "stored-object",
    ):
        assert impact["affected"][kind], kind
    assert impact["affected"]["stored-object"][0]["detail"]["count"] == 1


@pytest.mark.parametrize(
    ("namespace", "fixture"),
    [("corpus", "migration-corpus.json"), ("research_kg", "migration-namespace.json")],
)
def test_checkpointed_migration_preview_resume_and_rollback_have_backing_parity(
    namespace, fixture
):
    conn = duckdb.connect(":memory:")
    if namespace != "corpus":
        conn.execute("CREATE TABLE provisioned_kgs(name TEXT, status TEXT)")
        conn.execute("INSERT INTO provisioned_kgs VALUES (?, 'deployed')", [namespace])
    registry = SchemaRegistry(conn)
    _register_article_versions(registry)
    _seed_objects(conn, namespace)
    definition = _define_migration(registry, fixture)
    read_scopes = {READ_SCOPE}
    write_scopes = {MIGRATE_SCOPE}
    if namespace != "corpus":
        read_scopes.add(f"knowledge:namespace:{namespace}:read")
        write_scopes.add(f"knowledge:namespace:{namespace}:write")

    before = conn.execute(
        "SELECT object_id, value_json, revision FROM knowledge_objects "
        "WHERE namespace=? ORDER BY object_id",
        [namespace],
    ).fetchall()
    preview = registry.preview_migration(
        definition["migration_id"], namespace, scopes=read_scopes, sample_size=1
    )
    same_hash = registry.preview_migration(
        definition["migration_id"], namespace, scopes=read_scopes, sample_size=0
    )
    after_preview = conn.execute(
        "SELECT object_id, value_json, revision FROM knowledge_objects "
        "WHERE namespace=? ORDER BY object_id",
        [namespace],
    ).fetchall()
    assert preview["valid"] is True
    assert preview["change_count"] == 2
    assert len(preview["samples"]) == 1
    assert preview["preview_hash"] == same_hash["preview_hash"]
    assert before == after_preview

    first = registry.execute_migration(
        definition["migration_id"],
        namespace,
        preview["preview_hash"],
        principal_id=ADMIN,
        scopes=write_scopes,
        batch_size=1,
    )
    assert first["status"] == "in_progress"
    assert first["watermark"] == 2
    resumed_preview = registry.preview_migration(
        definition["migration_id"], namespace, scopes=read_scopes
    )
    assert resumed_preview["change_count"] == 1
    completed = registry.execute_migration(
        definition["migration_id"],
        namespace,
        resumed_preview["preview_hash"],
        principal_id=ADMIN,
        scopes=write_scopes,
        batch_size=1,
    )
    assert completed["status"] == "completed"
    assert completed["processed_count"] == 2
    assert completed["watermark"] == 3
    assert (
        registry.execute_migration(
            definition["migration_id"],
            namespace,
            "already-completed",
            principal_id=ADMIN,
            scopes=write_scopes,
        )["idempotent_replay"]
        is True
    )
    migrated = conn.execute(
        "SELECT value_json, revision FROM knowledge_objects WHERE namespace=? "
        "ORDER BY object_id",
        [namespace],
    ).fetchall()
    assert all(json.loads(row[0])["schema_version"] == 2 for row in migrated)
    assert all(row[1] == 2 for row in migrated)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_derivation_invalidations "
            "WHERE namespace=? AND reason='schema-migration'",
            [namespace],
        ).fetchone()[0]
        == 4
    )

    rolled = registry.rollback_migration(
        definition["migration_id"],
        namespace,
        "Target schema withdrawn",
        principal_id=ADMIN,
        scopes=write_scopes,
    )
    assert rolled["status"] == "rolled_back"
    assert rolled["watermark"] == 4
    restored = conn.execute(
        "SELECT value_json, revision FROM knowledge_objects WHERE namespace=? "
        "ORDER BY object_id",
        [namespace],
    ).fetchall()
    assert all("headline" in json.loads(row[0]) for row in restored)
    assert all(row[1] == 3 for row in restored)
    assert (
        registry.rollback_migration(
            definition["migration_id"],
            namespace,
            "Target schema withdrawn",
            principal_id=ADMIN,
            scopes=write_scopes,
        )["idempotent_replay"]
        is True
    )
    with pytest.raises(SchemaRegistryError) as reexecute:
        registry.execute_migration(
            definition["migration_id"],
            namespace,
            resumed_preview["preview_hash"],
            principal_id=ADMIN,
            scopes=write_scopes,
        )
    assert reexecute.value.code == "migration_rolled_back"
    actions = [
        event["action"] for event in registry.lineage(scopes={READ_SCOPE})["events"]
    ]
    assert "execute-migration" in actions
    assert "rollback-migration" in actions


def test_migration_failure_rolls_back_current_batch_and_resumes_from_checkpoint():
    conn = duckdb.connect(":memory:")
    registry = SchemaRegistry(conn)
    _register_article_versions(registry)
    _seed_objects(conn, "corpus")
    definition = _define_migration(registry, "migration-corpus.json")
    preview = registry.preview_migration(
        definition["migration_id"], "corpus", scopes={READ_SCOPE}
    )

    def fail_first(_index, _change):
        raise RuntimeError("simulated migration crash")

    crashing = SchemaRegistry(conn, failure_hook=fail_first)
    with pytest.raises(RuntimeError, match="simulated migration crash"):
        crashing.execute_migration(
            definition["migration_id"],
            "corpus",
            preview["preview_hash"],
            principal_id=ADMIN,
            scopes={MIGRATE_SCOPE},
            batch_size=2,
        )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_schema_migration_changes"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_schema_migration_checkpoints"
        ).fetchone()[0]
        == 0
    )
    assert conn.execute(
        "SELECT DISTINCT revision FROM knowledge_objects"
    ).fetchall() == [(1,)]
    recovered_preview = registry.preview_migration(
        definition["migration_id"], "corpus", scopes={READ_SCOPE}
    )
    assert recovered_preview["preview_hash"] == preview["preview_hash"]


def test_authorization_actor_and_namespace_permissions_are_fail_closed():
    conn = duckdb.connect(":memory:")
    registry = SchemaRegistry(conn)
    definition = _load("article-record-v1.json")
    with pytest.raises(SchemaRegistryError) as read_denied:
        registry.export(scopes=set())
    assert read_denied.value.code == "unauthorized"
    with pytest.raises(SchemaRegistryError) as register_denied:
        registry.register(
            definition,
            "denied-register",
            principal_id=ADMIN,
            scopes={READ_SCOPE},
        )
    assert register_denied.value.code == "unauthorized"
    with pytest.raises(SchemaRegistryError) as actor:
        registry.register(
            definition,
            "actor-mismatch",
            principal_id="intruder",
            scopes={REGISTER_SCOPE},
        )
    assert actor.value.code == "actor_mismatch"

    _register_article_versions(registry)
    migration = _define_migration(registry, "migration-namespace.json")
    with pytest.raises(SchemaRegistryError) as namespace_scope:
        registry.preview_migration(
            migration["migration_id"], "research_kg", scopes={READ_SCOPE}
        )
    assert namespace_scope.value.code == "unauthorized"


def test_mcp_uses_operator_identity_and_separate_lifecycle_scopes(
    tmp_path, monkeypatch
):
    path = tmp_path / "schema-registry.duckdb"
    conn = duckdb.connect(str(path))
    SchemaRegistry(conn)
    conn.close()
    monkeypatch.setenv("NOESIS_DB_PATH", str(path))
    monkeypatch.setenv("NOESIS_MCP_PRINCIPAL", ADMIN)
    monkeypatch.setenv("NOESIS_MCP_SCOPES", f"{READ_SCOPE},{VALIDATE_SCOPE}")

    server = ROOT / "tools/schema_registry_mcp/server.py"
    spec = importlib.util.spec_from_file_location("schema_registry_mcp_test", server)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.get_tools())
    builtin = tools["resolve_schema_module"].fn("schema", "knowledge-mutation", "1.0.0")
    assert builtin["provenance"]["kind"] == "builtin"
    denied = tools["register_schema_module"].fn(
        _load("article-record-v1.json"), "mcp-register-key"
    )
    assert denied["error"]["code"] == "unauthorized"

    monkeypatch.setenv("NOESIS_MCP_SCOPES", REGISTER_SCOPE)
    created = tools["register_schema_module"].fn(
        _load("article-record-v1.json"), "mcp-register-key"
    )
    assert created["module_id"].startswith("schema:article-record@1.0.0")
