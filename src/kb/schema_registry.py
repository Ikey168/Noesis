"""Runtime schema registry, compatibility analysis, and reversible migrations.

Definitions are immutable and content addressed. Lifecycle state, dependency
edges, migration checkpoints, and audit events live in additive DuckDB tables.
The registry also exposes a small set of built-ins directly from the checkout,
so resolving core contracts never depends on a network service or a prior write.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

MODULE_CONTRACT = "noesis-schema-module-v1"
CROSSWALK_CONTRACT = "noesis-schema-crosswalk-v1"
MIGRATION_CONTRACT = "noesis-schema-migration-v1"
EXPORT_CONTRACT = "noesis-schema-registry-export-v1"
AUDIT_CONTRACT = "noesis-schema-registry-audit-v1"

READ_SCOPE = "knowledge:schema:read"
VALIDATE_SCOPE = "knowledge:schema:validate"
REGISTER_SCOPE = "knowledge:schema:register"
DEPRECATE_SCOPE = "knowledge:schema:deprecate"
MIGRATE_SCOPE = "knowledge:schema:migrate"

MODULE_KINDS = {"schema", "ontology", "constraint", "vocabulary", "crosswalk"}
CONSUMER_KINDS = {
    "connector",
    "extractor",
    "index",
    "tool",
    "pack",
    "stored-object",
    "module",
}
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
REPO_ROOT = Path(__file__).resolve().parents[2]

_WRITE_LOCK = threading.RLock()

_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_schema_modules (
    module_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    semantic_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    compatibility_policy TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at_ms BIGINT NOT NULL,
    deprecated_at_ms BIGINT,
    UNIQUE(kind, name, semantic_version)
);
CREATE TABLE IF NOT EXISTS knowledge_schema_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_schema_dependencies (
    module_id TEXT NOT NULL,
    consumer_kind TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    declared_at_ms BIGINT NOT NULL,
    PRIMARY KEY(module_id, consumer_kind, consumer_id)
);
CREATE TABLE IF NOT EXISTS knowledge_schema_migrations (
    migration_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    owner TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_schema_migration_checkpoints (
    migration_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    last_object_id TEXT,
    processed_count BIGINT NOT NULL,
    status TEXT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    PRIMARY KEY(migration_id, namespace)
);
CREATE TABLE IF NOT EXISTS knowledge_schema_migration_changes (
    migration_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    object_id TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    migrated_revision BIGINT NOT NULL,
    changed_at_ms BIGINT NOT NULL,
    rolled_back BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY(migration_id, namespace, object_id)
);
CREATE TABLE IF NOT EXISTS knowledge_schema_lineage (
    sequence BIGINT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schema_modules_name
    ON knowledge_schema_modules(kind, name, semantic_version);
CREATE INDEX IF NOT EXISTS idx_schema_dependencies_module
    ON knowledge_schema_dependencies(module_id, consumer_kind);
CREATE INDEX IF NOT EXISTS idx_schema_migration_changes
    ON knowledge_schema_migration_changes(migration_id, namespace, object_id);
"""


class SchemaRegistryError(RuntimeError):
    """Typed registry failure safe to return from an MCP adapter."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _contract_errors(payload: Any, schema_name: str) -> list[dict[str, Any]]:
    from jsonschema import Draft7Validator

    path = REPO_ROOT / "contracts/schemas/jsonschema" / schema_name
    schema = json.loads(path.read_text())
    errors = sorted(
        Draft7Validator(schema).iter_errors(payload),
        key=lambda error: (tuple(str(part) for part in error.path), error.message),
    )
    return [
        {
            "path": "/".join(str(part) for part in error.path),
            "validator": error.validator,
            "message": error.message,
        }
        for error in errors[:50]
    ]


def _table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]
        ).fetchone()
    )


def _semver(value: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(str(value))
    if not match:
        raise SchemaRegistryError(
            "invalid_semver", f"semantic version {value!r} must be MAJOR.MINOR.PATCH"
        )
    return tuple(int(part) for part in match.groups())


def _satisfies(version: tuple[int, int, int], spec: str) -> bool:
    spec = spec.strip()
    if spec in {"", "*", "latest"}:
        return True
    if SEMVER_RE.fullmatch(spec):
        return version == _semver(spec)
    if spec.startswith("^"):
        base = _semver(spec[1:])
        if base[0]:
            ceiling = (base[0] + 1, 0, 0)
        elif base[1]:
            ceiling = (0, base[1] + 1, 0)
        else:
            ceiling = (0, 0, base[2] + 1)
        return base <= version < ceiling
    if spec.startswith("~"):
        base = _semver(spec[1:])
        return base <= version < (base[0], base[1] + 1, 0)
    clauses = [part.strip() for part in spec.split(",") if part.strip()]
    if not clauses:
        return False
    for clause in clauses:
        match = re.fullmatch(r"(>=|<=|>|<|=)(\d+\.\d+\.\d+)", clause)
        if not match:
            raise SchemaRegistryError(
                "invalid_version_range", f"unsupported version range {spec!r}"
            )
        operator, raw = match.groups()
        target = _semver(raw)
        if operator == ">=" and not version >= target:
            return False
        if operator == "<=" and not version <= target:
            return False
        if operator == ">" and not version > target:
            return False
        if operator == "<" and not version < target:
            return False
        if operator == "=" and version != target:
            return False
    return True


def _builtin_definitions() -> list[dict[str, Any]]:
    mutation_path = (
        REPO_ROOT / "contracts/schemas/jsonschema/noesis-knowledge-mutation-v1.json"
    )
    mutation = json.loads(mutation_path.read_text())
    ontology = {
        "object_types": [
            "Entity",
            "Person",
            "Organization",
            "Concept",
            "Document",
            "Claim",
            "Method",
            "Dataset",
        ],
        "relation_types": [
            "AUTHORED_BY",
            "CITES",
            "INSTANCE_OF",
            "PART_OF",
            "DEFINES",
            "SUPPORTS",
            "CONTRADICTS",
            "MENTIONS",
        ],
    }
    common = {
        "contract": MODULE_CONTRACT,
        "owner": "noesis-core",
        "status": "active",
        "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {"kind": "builtin", "source": "Noesis checkout"},
    }
    return [
        {
            **common,
            "name": "knowledge-mutation",
            "kind": "schema",
            "semantic_version": "1.0.0",
            "content": mutation,
        },
        {
            **common,
            "name": "canonical-entity-relation",
            "kind": "ontology",
            "semantic_version": "1.0.0",
            "content": ontology,
        },
    ]


def _module_result(definition: Mapping[str, Any]) -> dict[str, Any]:
    content_hash = _digest(definition["content"])
    module_id = (
        f"{definition['kind']}:{definition['name']}@"
        f"{definition['semantic_version']}:{content_hash[:16]}"
    )
    return {
        "contract": MODULE_CONTRACT,
        "module_id": module_id,
        "name": definition["name"],
        "kind": definition["kind"],
        "semantic_version": definition["semantic_version"],
        "content_hash": f"sha256:{content_hash}",
        "content": definition["content"],
        "owner": definition["owner"],
        "status": definition.get("status", "active"),
        "dependencies": definition.get("dependencies", []),
        "compatibility_policy": definition.get("compatibility_policy", "backward"),
        "provenance": definition["provenance"],
    }


def ensure_schema_registry(conn: Any) -> None:
    conn.execute(_DDL)


class SchemaRegistry:
    """Immutable module registry with deterministic resolution and migrations."""

    def __init__(
        self,
        conn: Any,
        *,
        clock: Callable[[], int] | None = None,
        initialize: bool = True,
        failure_hook: Callable[[int, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.conn = conn
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.failure_hook = failure_hook
        self._builtins = [
            {
                **_module_result(item),
                "created_at_ms": 0,
                "deprecated_at_ms": None,
            }
            for item in _builtin_definitions()
        ]
        if initialize:
            ensure_schema_registry(conn)
            self._seed_builtins()

    @staticmethod
    def _require(scopes: Iterable[str], required: str) -> None:
        if required not in {str(scope) for scope in scopes}:
            raise SchemaRegistryError(
                "unauthorized",
                f"missing required scope {required}",
                required_scope=required,
            )

    @staticmethod
    def _validate_actor(actor: Mapping[str, Any], principal_id: str) -> None:
        if str(actor.get("principal_id", "")) != str(principal_id):
            raise SchemaRegistryError(
                "actor_mismatch", "authenticated principal does not match actor"
            )

    @staticmethod
    def _validate_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
        try:
            payload = json.loads(_canonical(dict(definition)))
        except (TypeError, ValueError) as exc:
            raise SchemaRegistryError("invalid_definition", str(exc)) from exc
        errors = _contract_errors(payload, "noesis-schema-module-v1.json")
        if errors:
            raise SchemaRegistryError(
                "invalid_definition",
                "module failed contract validation",
                validation_errors=errors,
            )
        required = {
            "contract",
            "name",
            "kind",
            "semantic_version",
            "content",
            "owner",
            "dependencies",
            "compatibility_policy",
            "provenance",
            "actor",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise SchemaRegistryError(
                "invalid_definition", "module definition is incomplete", missing=missing
            )
        if payload["contract"] != MODULE_CONTRACT:
            raise SchemaRegistryError("invalid_definition", "unsupported contract")
        if payload["kind"] not in MODULE_KINDS:
            raise SchemaRegistryError("invalid_definition", "unsupported module kind")
        _semver(payload["semantic_version"])
        if not re.fullmatch(r"[a-z][a-z0-9._-]{1,127}", payload["name"]):
            raise SchemaRegistryError("invalid_definition", "invalid module name")
        if not isinstance(payload["content"], Mapping):
            raise SchemaRegistryError("invalid_definition", "content must be an object")
        if not isinstance(payload["dependencies"], list):
            raise SchemaRegistryError(
                "invalid_definition", "dependencies must be a list"
            )
        if payload["compatibility_policy"] not in {"none", "backward", "full"}:
            raise SchemaRegistryError(
                "invalid_definition", "invalid compatibility policy"
            )
        if payload["kind"] in {"schema", "constraint"}:
            try:
                from jsonschema import Draft7Validator

                Draft7Validator.check_schema(payload["content"])
            except Exception as exc:
                raise SchemaRegistryError(
                    "invalid_schema", f"module content is not valid JSON Schema: {exc}"
                ) from exc
        return payload

    def _seed_builtins(self) -> None:
        for module in self._builtins:
            existing = self.conn.execute(
                "SELECT module_id, content_hash FROM knowledge_schema_modules "
                "WHERE kind=? AND name=? AND semantic_version=?",
                [module["kind"], module["name"], module["semantic_version"]],
            ).fetchone()
            if existing and existing[1] != module["content_hash"]:
                raise SchemaRegistryError(
                    "builtin_conflict",
                    "persisted definition conflicts with a shipped built-in",
                    existing_module_id=existing[0],
                    builtin_module_id=module["module_id"],
                )
            self.conn.execute(
                "INSERT OR IGNORE INTO knowledge_schema_modules VALUES "
                "(?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL)",
                [
                    module["module_id"],
                    module["name"],
                    module["kind"],
                    module["semantic_version"],
                    module["content_hash"],
                    _canonical(module["content"]),
                    module["owner"],
                    _canonical(module["dependencies"]),
                    module["compatibility_policy"],
                    _canonical(module["provenance"]),
                    module["created_at_ms"],
                ],
            )

    def _require_namespace(
        self, namespace: str, scopes: Iterable[str], *, write: bool
    ) -> None:
        if namespace == "corpus":
            return
        access = "write" if write else "read"
        self._require(scopes, f"knowledge:namespace:{namespace}:{access}")
        if not _table_exists(self.conn, "provisioned_kgs"):
            raise SchemaRegistryError(
                "namespace_not_found", f"namespace {namespace!r} is not provisioned"
            )
        row = self.conn.execute(
            "SELECT 1 FROM provisioned_kgs WHERE name=? AND status='deployed'",
            [namespace],
        ).fetchone()
        if not row:
            raise SchemaRegistryError(
                "namespace_not_found", f"namespace {namespace!r} is not deployed"
            )

    @staticmethod
    def _row_module(row: Any) -> dict[str, Any]:
        return {
            "contract": MODULE_CONTRACT,
            "module_id": row[0],
            "name": row[1],
            "kind": row[2],
            "semantic_version": row[3],
            "content_hash": row[4],
            "content": json.loads(row[5]),
            "owner": row[6],
            "status": row[7],
            "dependencies": json.loads(row[8]),
            "compatibility_policy": row[9],
            "provenance": json.loads(row[10]),
            "created_at_ms": int(row[11]),
            "deprecated_at_ms": int(row[12]) if row[12] is not None else None,
        }

    def _modules(self, kind: str, name: str) -> list[dict[str, Any]]:
        modules = [
            dict(item)
            for item in self._builtins
            if item["kind"] == kind and item["name"] == name
        ]
        if _table_exists(self.conn, "knowledge_schema_modules"):
            rows = self.conn.execute(
                "SELECT module_id, name, kind, semantic_version, content_hash, "
                "content_json, owner, status, dependencies_json, compatibility_policy, "
                "provenance_json, created_at_ms, deprecated_at_ms "
                "FROM knowledge_schema_modules WHERE kind=? AND name=?",
                [kind, name],
            ).fetchall()
            persisted = [self._row_module(row) for row in rows]
            by_id = {item["module_id"]: item for item in [*modules, *persisted]}
            modules = list(by_id.values())
        return modules

    def resolve(
        self,
        kind: str,
        name: str,
        version_spec: str,
        *,
        scopes: Iterable[str],
        include_deprecated: bool = False,
    ) -> dict[str, Any]:
        """Resolve an exact version or explicit compatible range deterministically."""

        self._require(scopes, READ_SCOPE)
        if kind not in MODULE_KINDS:
            raise SchemaRegistryError(
                "invalid_kind", f"unsupported module kind {kind!r}"
            )
        candidates = []
        for module in self._modules(kind, name):
            if not include_deprecated and module["status"] != "active":
                continue
            if _satisfies(_semver(module["semantic_version"]), version_spec):
                candidates.append(module)
        if not candidates:
            raise SchemaRegistryError(
                "not_found",
                f"no active {kind} {name!r} satisfies {version_spec!r}",
            )
        return max(
            candidates,
            key=lambda item: (_semver(item["semantic_version"]), item["content_hash"]),
        )

    def inspect(
        self,
        module_id: str,
        *,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        self._require(scopes, READ_SCOPE)
        for builtin in self._builtins:
            if builtin["module_id"] == module_id:
                return dict(builtin)
        if _table_exists(self.conn, "knowledge_schema_modules"):
            row = self.conn.execute(
                "SELECT module_id, name, kind, semantic_version, content_hash, "
                "content_json, owner, status, dependencies_json, compatibility_policy, "
                "provenance_json, created_at_ms, deprecated_at_ms "
                "FROM knowledge_schema_modules WHERE module_id=?",
                [module_id],
            ).fetchone()
            if row:
                return self._row_module(row)
        raise SchemaRegistryError("not_found", f"module {module_id!r} was not found")

    @staticmethod
    def compare_content(
        old: Mapping[str, Any], new: Mapping[str, Any], *, kind: str = "schema"
    ) -> dict[str, Any]:
        """Classify structural changes as compatible, breaking, or ambiguous."""

        breaking: list[dict[str, Any]] = []
        compatible: list[dict[str, Any]] = []
        ambiguous: list[dict[str, Any]] = []
        if _digest(old) == _digest(new):
            return {
                "classification": "compatible",
                "breaking": [],
                "compatible": [{"code": "identical"}],
                "ambiguous": [],
            }
        if kind == "ontology":
            for key in ("object_types", "relation_types"):
                before, after = set(old.get(key, [])), set(new.get(key, []))
                breaking.extend(
                    {"code": "removed_type", "field": key, "value": value}
                    for value in sorted(before - after)
                )
                compatible.extend(
                    {"code": "added_type", "field": key, "value": value}
                    for value in sorted(after - before)
                )
        elif kind == "schema":
            before_props = old.get("properties", {})
            after_props = new.get("properties", {})
            before_required = set(old.get("required", []))
            after_required = set(new.get("required", []))
            for field in sorted(set(before_props) - set(after_props)):
                breaking.append({"code": "removed_field", "field": field})
            for field in sorted(set(after_props) - set(before_props)):
                target = breaking if field in after_required else compatible
                target.append(
                    {
                        "code": "added_required_field"
                        if field in after_required
                        else "added_optional_field",
                        "field": field,
                    }
                )
            for field in sorted(set(before_props) & set(after_props)):
                before_type = before_props[field].get("type")
                after_type = after_props[field].get("type")
                if before_type != after_type:
                    breaking.append(
                        {
                            "code": "changed_type",
                            "field": field,
                            "before": before_type,
                            "after": after_type,
                        }
                    )
                before_enum = set(before_props[field].get("enum", []))
                after_enum = set(after_props[field].get("enum", []))
                if before_enum and after_enum:
                    if before_enum - after_enum:
                        breaking.append({"code": "narrowed_enum", "field": field})
                    elif after_enum - before_enum:
                        compatible.append({"code": "expanded_enum", "field": field})
                handled = {"type", "enum", "description", "title"}
                before_constraints = {
                    key: value
                    for key, value in before_props[field].items()
                    if key not in handled
                }
                after_constraints = {
                    key: value
                    for key, value in after_props[field].items()
                    if key not in handled
                }
                if before_constraints != after_constraints:
                    ambiguous.append(
                        {"code": "property_constraint_changed", "field": field}
                    )
            for field in sorted(after_required - before_required):
                if field in before_props:
                    breaking.append({"code": "field_became_required", "field": field})
            if (
                old.get("additionalProperties", True) is not False
                and new.get("additionalProperties", True) is False
            ):
                breaking.append({"code": "additional_properties_forbidden"})
            if old.get("definitions", old.get("$defs")) != new.get(
                "definitions", new.get("$defs")
            ):
                ambiguous.append({"code": "definitions_changed"})
            known = {
                "$schema",
                "$id",
                "title",
                "description",
                "type",
                "properties",
                "required",
                "additionalProperties",
                "definitions",
                "$defs",
            }
            changed_unknown = [
                key
                for key in sorted(set(old) | set(new))
                if key not in known and old.get(key) != new.get(key)
            ]
            ambiguous.extend(
                {"code": "unclassified_keyword", "field": key}
                for key in changed_unknown
            )
        else:
            ambiguous.append({"code": "semantic_review_required", "kind": kind})
        classification = (
            "breaking" if breaking else "ambiguous" if ambiguous else "compatible"
        )
        return {
            "classification": classification,
            "breaking": breaking,
            "compatible": compatible,
            "ambiguous": ambiguous,
        }

    def compare(
        self,
        old_ref: Mapping[str, str],
        new_ref: Mapping[str, str],
        *,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        old = self.resolve(
            old_ref["kind"],
            old_ref["name"],
            old_ref["version"],
            scopes=scopes,
            include_deprecated=True,
        )
        new = self.resolve(
            new_ref["kind"],
            new_ref["name"],
            new_ref["version"],
            scopes=scopes,
            include_deprecated=True,
        )
        if old["kind"] != new["kind"]:
            return {
                "classification": "ambiguous",
                "breaking": [],
                "compatible": [],
                "ambiguous": [{"code": "kind_changed"}],
                "old": old["module_id"],
                "new": new["module_id"],
            }
        return {
            **self.compare_content(old["content"], new["content"], kind=old["kind"]),
            "old": old["module_id"],
            "new": new["module_id"],
        }

    def _idempotent(
        self, key: str, operation: str, request: Any
    ) -> dict[str, Any] | None:
        if len(str(key)) < 8:
            raise SchemaRegistryError(
                "invalid_idempotency_key",
                "idempotency key must have at least 8 characters",
            )
        row = self.conn.execute(
            "SELECT operation, request_hash, result_json "
            "FROM knowledge_schema_idempotency WHERE idempotency_key=?",
            [key],
        ).fetchone()
        if not row:
            return None
        if row[0] != operation or row[1] != _digest(request):
            raise SchemaRegistryError(
                "idempotency_key_reused",
                "idempotency key is bound to a different operation or request",
            )
        return {**json.loads(row[2]), "idempotent_replay": True}

    def _save_idempotency(
        self, key: str, operation: str, request: Any, result: Any, now: int
    ) -> None:
        self.conn.execute(
            "INSERT INTO knowledge_schema_idempotency VALUES (?, ?, ?, ?, ?)",
            [key, operation, _digest(request), _canonical(result), now],
        )

    def _lineage(
        self, action: str, subject_id: str, actor: str, detail: Any, now: int
    ) -> None:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM knowledge_schema_lineage"
        ).fetchone()
        sequence = int(row[0])
        event_id = f"schema-event:{sequence}:{_digest([action, subject_id, now])[:12]}"
        self.conn.execute(
            "INSERT INTO knowledge_schema_lineage VALUES (?, ?, ?, ?, ?, ?, ?)",
            [sequence, event_id, action, subject_id, actor, _canonical(detail), now],
        )

    def register(
        self,
        definition: Mapping[str, Any],
        idempotency_key: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        self._require(scopes, REGISTER_SCOPE)
        payload = self._validate_definition(definition)
        self._validate_actor(payload["actor"], principal_id)
        ensure_schema_registry(self.conn)
        self._seed_builtins()
        request = {"definition": payload, "idempotency_key": idempotency_key}
        with _WRITE_LOCK:
            replay = self._idempotent(idempotency_key, "register", request)
            if replay:
                return replay
            module = _module_result(payload)
            existing = self.conn.execute(
                "SELECT module_id, content_hash FROM knowledge_schema_modules "
                "WHERE kind=? AND name=? AND semantic_version=?",
                [payload["kind"], payload["name"], payload["semantic_version"]],
            ).fetchone()
            if existing:
                if existing[1] == module["content_hash"]:
                    now = int(self.clock())
                    result = {
                        **self.inspect(existing[0], scopes={READ_SCOPE}),
                        "idempotent_replay": True,
                    }
                    self._save_idempotency(
                        idempotency_key, "register", request, result, now
                    )
                    return result
                raise SchemaRegistryError(
                    "immutable_version_conflict",
                    "a semantic version cannot be silently replaced",
                    existing_module_id=existing[0],
                )
            previous = self._modules(payload["kind"], payload["name"])
            if previous and payload["compatibility_policy"] != "none":
                latest = max(
                    previous, key=lambda item: _semver(item["semantic_version"])
                )
                compatibility = self.compare_content(
                    latest["content"], payload["content"], kind=payload["kind"]
                )
                if compatibility["classification"] == "breaking":
                    raise SchemaRegistryError(
                        "compatibility_violation",
                        "registration violates the module compatibility policy",
                        previous=latest["module_id"],
                        compatibility=compatibility,
                    )
            for dependency in payload["dependencies"]:
                if (
                    not isinstance(dependency, Mapping)
                    or not {"kind", "name", "version"} <= dependency.keys()
                ):
                    raise SchemaRegistryError(
                        "invalid_dependency",
                        "dependencies require kind, name, and version",
                    )
                self.resolve(
                    dependency["kind"],
                    dependency["name"],
                    dependency["version"],
                    scopes={READ_SCOPE},
                    include_deprecated=True,
                )
            now = int(self.clock())
            result = {
                **module,
                "created_at_ms": now,
                "deprecated_at_ms": None,
                "idempotent_replay": False,
            }
            self.conn.execute("BEGIN TRANSACTION")
            try:
                self.conn.execute(
                    "INSERT INTO knowledge_schema_modules VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL)",
                    [
                        module["module_id"],
                        module["name"],
                        module["kind"],
                        module["semantic_version"],
                        module["content_hash"],
                        _canonical(module["content"]),
                        module["owner"],
                        _canonical(module["dependencies"]),
                        module["compatibility_policy"],
                        _canonical(module["provenance"]),
                        now,
                    ],
                )
                self._save_idempotency(
                    idempotency_key, "register", request, result, now
                )
                self._lineage(
                    "register", module["module_id"], principal_id, result, now
                )
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def deprecate(
        self,
        module_id: str,
        reason: str,
        idempotency_key: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        self._require(scopes, DEPRECATE_SCOPE)
        if not reason.strip():
            raise SchemaRegistryError(
                "invalid_reason", "deprecation reason is required"
            )
        ensure_schema_registry(self.conn)
        request = {"module_id": module_id, "reason": reason}
        with _WRITE_LOCK:
            replay = self._idempotent(idempotency_key, "deprecate", request)
            if replay:
                return replay
            module = self.inspect(module_id, scopes={READ_SCOPE})
            if module["provenance"].get("kind") == "builtin":
                raise SchemaRegistryError(
                    "builtin_immutable", "built-ins cannot be deprecated"
                )
            now = int(self.clock())
            if module["status"] == "deprecated":
                result = {
                    "module_id": module_id,
                    "status": "deprecated",
                    "reason": reason,
                    "deprecated_at_ms": module["deprecated_at_ms"],
                    "idempotent_replay": True,
                }
                self._save_idempotency(
                    idempotency_key, "deprecate", request, result, now
                )
                return result
            result = {
                "module_id": module_id,
                "status": "deprecated",
                "reason": reason,
                "deprecated_at_ms": now,
                "idempotent_replay": False,
            }
            self.conn.execute("BEGIN TRANSACTION")
            try:
                self.conn.execute(
                    "UPDATE knowledge_schema_modules SET status='deprecated', "
                    "deprecated_at_ms=? WHERE module_id=?",
                    [now, module_id],
                )
                self._save_idempotency(
                    idempotency_key, "deprecate", request, result, now
                )
                self._lineage("deprecate", module_id, principal_id, result, now)
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def validate_instance(
        self,
        reference: Mapping[str, str],
        instance: Any,
        *,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        self._require(scopes, VALIDATE_SCOPE)
        module = self.resolve(
            reference["kind"],
            reference["name"],
            reference["version"],
            scopes={READ_SCOPE},
            include_deprecated=True,
        )
        try:
            from jsonschema import Draft7Validator

            errors = sorted(
                Draft7Validator(module["content"]).iter_errors(instance),
                key=lambda error: (
                    tuple(str(part) for part in error.path),
                    error.message,
                ),
            )
        except Exception as exc:
            raise SchemaRegistryError("invalid_schema", str(exc)) from exc
        return {
            "valid": not errors,
            "module_id": module["module_id"],
            "content_hash": module["content_hash"],
            "provenance": module["provenance"],
            "errors": [
                {
                    "path": "/".join(str(part) for part in error.path),
                    "schema_path": "/".join(str(part) for part in error.schema_path),
                    "validator": error.validator,
                    "message": error.message,
                }
                for error in errors[:50]
            ],
        }

    def register_crosswalk(
        self,
        crosswalk: Mapping[str, Any],
        idempotency_key: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        payload = json.loads(_canonical(dict(crosswalk)))
        errors = _contract_errors(payload, "noesis-schema-crosswalk-v1.json")
        if errors:
            raise SchemaRegistryError(
                "invalid_crosswalk",
                "crosswalk failed contract validation",
                validation_errors=errors,
            )
        if payload.get("contract") != CROSSWALK_CONTRACT:
            raise SchemaRegistryError("invalid_crosswalk", "unsupported contract")
        if payload.get("direction") not in {"forward", "reverse", "bidirectional"}:
            raise SchemaRegistryError("invalid_crosswalk", "invalid direction")
        confidence = payload.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise SchemaRegistryError("invalid_crosswalk", "confidence must be 0..1")
        if not payload.get("mappings") or not isinstance(payload["mappings"], list):
            raise SchemaRegistryError("invalid_crosswalk", "mappings are required")
        definition = {
            "contract": MODULE_CONTRACT,
            "name": payload["name"],
            "kind": "crosswalk",
            "semantic_version": payload["semantic_version"],
            "content": payload,
            "owner": payload["owner"],
            "dependencies": [payload["source"], payload["target"]],
            "compatibility_policy": "none",
            "provenance": payload["provenance"],
            "actor": payload["actor"],
        }
        return self.register(
            definition, idempotency_key, principal_id=principal_id, scopes=scopes
        )

    def declare_dependency(
        self,
        module_id: str,
        consumer_kind: str,
        consumer_id: str,
        detail: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        self._require(scopes, REGISTER_SCOPE)
        if consumer_kind not in CONSUMER_KINDS:
            raise SchemaRegistryError("invalid_consumer", "unsupported consumer kind")
        ensure_schema_registry(self.conn)
        self.inspect(module_id, scopes={READ_SCOPE})
        now = int(self.clock())
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO knowledge_schema_dependencies "
                "VALUES (?, ?, ?, ?, ?)",
                [module_id, consumer_kind, consumer_id, _canonical(detail), now],
            )
            self._lineage(
                "declare-dependency",
                module_id,
                principal_id,
                {"consumer_kind": consumer_kind, "consumer_id": consumer_id},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "module_id": module_id,
            "consumer_kind": consumer_kind,
            "consumer_id": consumer_id,
        }

    def impact(self, module_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        self._require(scopes, READ_SCOPE)
        module = self.inspect(module_id, scopes={READ_SCOPE})
        affected: dict[str, list[dict[str, Any]]] = {
            kind: [] for kind in sorted(CONSUMER_KINDS)
        }
        if _table_exists(self.conn, "knowledge_schema_dependencies"):
            rows = self.conn.execute(
                "SELECT consumer_kind, consumer_id, detail_json "
                "FROM knowledge_schema_dependencies WHERE module_id=? "
                "ORDER BY consumer_kind, consumer_id",
                [module_id],
            ).fetchall()
            for kind, identifier, detail in rows:
                affected[kind].append({"id": identifier, "detail": json.loads(detail)})
        if _table_exists(self.conn, "knowledge_schema_modules"):
            rows = self.conn.execute(
                "SELECT module_id, dependencies_json FROM knowledge_schema_modules"
            ).fetchall()
            for dependent_id, raw in rows:
                for dependency in json.loads(raw):
                    if (
                        dependency.get("kind") == module["kind"]
                        and dependency.get("name") == module["name"]
                        and _satisfies(
                            _semver(module["semantic_version"]), dependency["version"]
                        )
                    ):
                        affected["module"].append(
                            {"id": dependent_id, "detail": dependency}
                        )
        object_types = set(module["content"].get("object_types", []))
        if object_types and _table_exists(self.conn, "knowledge_objects"):
            placeholders = ",".join("?" for _ in object_types)
            rows = self.conn.execute(
                "SELECT namespace, object_type, COUNT(*) FROM knowledge_objects "
                f"WHERE object_type IN ({placeholders}) GROUP BY namespace, object_type "
                "ORDER BY namespace, object_type",
                sorted(object_types),
            ).fetchall()
            affected["stored-object"].extend(
                {"id": f"{namespace}:{object_type}", "detail": {"count": int(count)}}
                for namespace, object_type, count in rows
            )
        return {
            "module_id": module_id,
            "affected": affected,
            "total": sum(len(values) for values in affected.values()),
        }

    def export(self, *, scopes: Iterable[str]) -> dict[str, Any]:
        self._require(scopes, READ_SCOPE)
        modules: dict[str, dict[str, Any]] = {
            item["module_id"]: dict(item) for item in self._builtins
        }
        if _table_exists(self.conn, "knowledge_schema_modules"):
            rows = self.conn.execute(
                "SELECT module_id, name, kind, semantic_version, content_hash, "
                "content_json, owner, status, dependencies_json, compatibility_policy, "
                "provenance_json, created_at_ms, deprecated_at_ms "
                "FROM knowledge_schema_modules"
            ).fetchall()
            modules.update({row[0]: self._row_module(row) for row in rows})
        ordered = sorted(
            modules.values(),
            key=lambda item: (
                item["kind"],
                item["name"],
                _semver(item["semantic_version"]),
            ),
        )
        digest = _digest(ordered)
        return {
            "contract": EXPORT_CONTRACT,
            "content_hash": f"sha256:{digest}",
            "modules": ordered,
        }

    @staticmethod
    def _validate_migration(definition: Mapping[str, Any]) -> dict[str, Any]:
        payload = json.loads(_canonical(dict(definition)))
        errors = _contract_errors(payload, "noesis-schema-migration-v1.json")
        if errors:
            raise SchemaRegistryError(
                "invalid_migration",
                "migration failed contract validation",
                validation_errors=errors,
            )
        required = {
            "contract",
            "migration_id",
            "from",
            "to",
            "affected_types",
            "preconditions",
            "transforms",
            "postconditions",
            "reverse_transforms",
            "owner",
            "provenance",
            "actor",
        }
        missing = sorted(required - payload.keys())
        if payload.get("contract") != MIGRATION_CONTRACT or missing:
            raise SchemaRegistryError(
                "invalid_migration",
                "migration definition is incomplete",
                missing=missing,
            )
        if not payload["affected_types"] or not payload["transforms"]:
            raise SchemaRegistryError(
                "invalid_migration", "affected types and transforms are required"
            )
        allowed = {
            "rename_field",
            "set_default",
            "remove_field",
            "map_value",
            "set",
            "rename_type",
        }
        for transform in [*payload["transforms"], *payload["reverse_transforms"]]:
            if transform.get("op") not in allowed:
                raise SchemaRegistryError(
                    "invalid_migration",
                    f"unsupported transform {transform.get('op')!r}",
                )
        return payload

    def define_migration(
        self,
        definition: Mapping[str, Any],
        idempotency_key: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        self._require(scopes, MIGRATE_SCOPE)
        payload = self._validate_migration(definition)
        self._validate_actor(payload["actor"], principal_id)
        ensure_schema_registry(self.conn)
        request = {"definition": payload}
        with _WRITE_LOCK:
            replay = self._idempotent(idempotency_key, "define-migration", request)
            if replay:
                return replay
            for reference in (payload["from"], payload["to"]):
                self.resolve(
                    reference["kind"],
                    reference["name"],
                    reference["version"],
                    scopes={READ_SCOPE},
                    include_deprecated=True,
                )
            content_hash = f"sha256:{_digest(payload)}"
            existing = self.conn.execute(
                "SELECT content_hash FROM knowledge_schema_migrations WHERE migration_id=?",
                [payload["migration_id"]],
            ).fetchone()
            if existing:
                if existing[0] == content_hash:
                    now = int(self.clock())
                    result = {
                        "migration_id": payload["migration_id"],
                        "content_hash": content_hash,
                        "idempotent_replay": True,
                    }
                    self._save_idempotency(
                        idempotency_key,
                        "define-migration",
                        request,
                        result,
                        now,
                    )
                    return result
                raise SchemaRegistryError(
                    "immutable_migration_conflict", "migration id cannot be replaced"
                )
            now = int(self.clock())
            result = {
                "migration_id": payload["migration_id"],
                "content_hash": content_hash,
                "idempotent_replay": False,
            }
            self.conn.execute("BEGIN TRANSACTION")
            try:
                self.conn.execute(
                    "INSERT INTO knowledge_schema_migrations VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        payload["migration_id"],
                        content_hash,
                        _canonical(payload),
                        payload["owner"],
                        _canonical(payload["provenance"]),
                        now,
                    ],
                )
                self._save_idempotency(
                    idempotency_key, "define-migration", request, result, now
                )
                self._lineage(
                    "define-migration",
                    payload["migration_id"],
                    principal_id,
                    result,
                    now,
                )
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def _migration(self, migration_id: str) -> dict[str, Any]:
        if not _table_exists(self.conn, "knowledge_schema_migrations"):
            raise SchemaRegistryError("not_found", "migration registry is empty")
        row = self.conn.execute(
            "SELECT definition_json FROM knowledge_schema_migrations WHERE migration_id=?",
            [migration_id],
        ).fetchone()
        if not row:
            raise SchemaRegistryError(
                "not_found", f"migration {migration_id!r} not found"
            )
        return json.loads(row[0])

    @staticmethod
    def _transform(
        value: Mapping[str, Any], object_type: str, transforms: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], str]:
        output = json.loads(_canonical(dict(value)))
        target_type = object_type
        for transform in transforms:
            op = transform["op"]
            if op == "rename_field":
                source, target = transform["from"], transform["to"]
                if source in output:
                    if target in output and target != source:
                        raise SchemaRegistryError(
                            "migration_conflict",
                            f"cannot rename {source!r}; {target!r} already exists",
                        )
                    output[target] = output.pop(source)
            elif op == "set_default":
                output.setdefault(transform["field"], transform.get("value"))
            elif op == "remove_field":
                output.pop(transform["field"], None)
            elif op == "map_value" and transform["field"] in output:
                current = str(output[transform["field"]])
                if current in transform["mapping"]:
                    output[transform["field"]] = transform["mapping"][current]
            elif op == "set":
                output[transform["field"]] = transform.get("value")
            elif op == "rename_type" and target_type == transform["from"]:
                target_type = transform["to"]
        return output, target_type

    @staticmethod
    def _precondition_errors(
        definition: Mapping[str, Any], value: Mapping[str, Any], revision: int
    ) -> list[dict[str, Any]]:
        preconditions = definition.get("preconditions", {})
        errors: list[dict[str, Any]] = []
        for field in preconditions.get("required_fields", []):
            if field not in value:
                errors.append({"code": "missing_required_field", "field": field})
        for field, expected in preconditions.get("field_equals", {}).items():
            if value.get(field) != expected:
                errors.append(
                    {
                        "code": "field_value_mismatch",
                        "field": field,
                        "expected": expected,
                        "actual": value.get(field),
                    }
                )
        minimum = preconditions.get("minimum_revision")
        if minimum is not None and revision < int(minimum):
            errors.append(
                {
                    "code": "revision_precondition",
                    "minimum": int(minimum),
                    "actual": revision,
                }
            )
        return errors

    def _migration_rows(
        self,
        definition: Mapping[str, Any],
        namespace: str,
        after: str = "",
        limit: int | None = None,
    ) -> list[Any]:
        if not _table_exists(self.conn, "knowledge_objects"):
            return []
        types = definition["affected_types"]
        placeholders = ",".join("?" for _ in types)
        sql = (
            "SELECT object_id, object_type, value_json, revision, retracted "
            "FROM knowledge_objects WHERE namespace=? AND object_type IN ("
            f"{placeholders}) AND object_id>? AND retracted=FALSE ORDER BY object_id"
        )
        params: list[Any] = [namespace, *types, after]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, min(int(limit), 10000)))
        return self.conn.execute(sql, params).fetchall()

    def _validate_migrated(
        self, definition: Mapping[str, Any], value: Any
    ) -> list[dict[str, Any]]:
        if not definition["postconditions"].get("validate_target", False):
            return []
        target = definition["to"]
        result = self.validate_instance(target, value, scopes={VALIDATE_SCOPE})
        return result["errors"]

    def preview_migration(
        self,
        migration_id: str,
        namespace: str,
        *,
        scopes: Iterable[str],
        sample_size: int = 10,
    ) -> dict[str, Any]:
        self._require(scopes, READ_SCOPE)
        self._require_namespace(namespace, scopes, write=False)
        definition = self._migration(migration_id)
        samples, conflicts, fingerprints, changed = [], [], [], 0
        after = ""
        if _table_exists(self.conn, "knowledge_schema_migration_checkpoints"):
            checkpoint = self.conn.execute(
                "SELECT last_object_id, status FROM "
                "knowledge_schema_migration_checkpoints "
                "WHERE migration_id=? AND namespace=?",
                [migration_id, namespace],
            ).fetchone()
            if checkpoint and checkpoint[1] in {"in_progress", "completed"}:
                after = checkpoint[0] or ""
        rows = self._migration_rows(definition, namespace, after)
        for object_id, object_type, raw, revision, _retracted in rows:
            before = json.loads(raw)
            precondition_errors = self._precondition_errors(
                definition, before, int(revision)
            )
            if precondition_errors:
                conflicts.append(
                    {"object_id": object_id, "errors": precondition_errors}
                )
                continue
            try:
                after, after_type = self._transform(
                    before, object_type, definition["transforms"]
                )
                errors = self._validate_migrated(definition, after)
                if errors:
                    conflicts.append({"object_id": object_id, "errors": errors})
                    continue
            except SchemaRegistryError as exc:
                conflicts.append({"object_id": object_id, "error": exc.as_dict()})
                continue
            if before != after or object_type != after_type:
                changed += 1
                fingerprints.append(
                    {
                        "object_id": object_id,
                        "revision": int(revision),
                        "after_hash": _digest(
                            {"object_type": after_type, "value": after}
                        ),
                    }
                )
                if len(samples) < max(0, min(sample_size, 100)):
                    samples.append(
                        {
                            "object_id": object_id,
                            "revision": int(revision),
                            "before": {"object_type": object_type, "value": before},
                            "after": {"object_type": after_type, "value": after},
                        }
                    )
        plan = {
            "migration_id": migration_id,
            "namespace": namespace,
            "scanned_count": len(rows),
            "change_count": changed,
            "samples": samples,
            "conflicts": conflicts,
        }
        return {
            **plan,
            "valid": not conflicts,
            "preview_hash": f"sha256:{_digest({**plan, 'samples': fingerprints})}",
        }

    def execute_migration(
        self,
        migration_id: str,
        namespace: str,
        preview_hash: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        batch_size: int = 500,
    ) -> dict[str, Any]:
        self._require(scopes, MIGRATE_SCOPE)
        self._require_namespace(namespace, scopes, write=True)
        ensure_schema_registry(self.conn)
        from src.kb.transactions import ensure_transaction_schema

        ensure_transaction_schema(self.conn)
        definition = self._migration(migration_id)
        invalidated_artifacts = ["consolidation", "search-index"]
        if any(item["op"] == "rename_type" for item in definition["transforms"]):
            invalidated_artifacts.append("graph-index")
        checkpoint = self.conn.execute(
            "SELECT processed_count, status FROM "
            "knowledge_schema_migration_checkpoints "
            "WHERE migration_id=? AND namespace=?",
            [migration_id, namespace],
        ).fetchone()
        if checkpoint and checkpoint[1] == "completed":
            return {
                "migration_id": migration_id,
                "namespace": namespace,
                "status": "completed",
                "processed_count": int(checkpoint[0]),
                "idempotent_replay": True,
            }
        if checkpoint and checkpoint[1] == "rolled_back":
            raise SchemaRegistryError(
                "migration_rolled_back",
                "a compensated migration cannot be re-executed; define a new migration",
            )
        preview_scopes = {READ_SCOPE}
        if namespace != "corpus":
            preview_scopes.add(f"knowledge:namespace:{namespace}:read")
        preview = self.preview_migration(
            migration_id, namespace, scopes=preview_scopes, sample_size=10
        )
        if preview["preview_hash"] != preview_hash:
            raise SchemaRegistryError(
                "stale_preview", "migration preview hash no longer matches"
            )
        if preview["conflicts"]:
            raise SchemaRegistryError(
                "migration_conflict",
                "migration preview contains conflicts",
                conflicts=preview["conflicts"],
            )
        with _WRITE_LOCK:
            checkpoint = self.conn.execute(
                "SELECT last_object_id, processed_count, status "
                "FROM knowledge_schema_migration_checkpoints "
                "WHERE migration_id=? AND namespace=?",
                [migration_id, namespace],
            ).fetchone()
            if checkpoint and checkpoint[2] == "completed":
                return {
                    "migration_id": migration_id,
                    "namespace": namespace,
                    "status": "completed",
                    "processed_count": int(checkpoint[1]),
                    "idempotent_replay": True,
                }
            after = checkpoint[0] if checkpoint and checkpoint[0] else ""
            processed = int(checkpoint[1]) if checkpoint else 0
            rows = self._migration_rows(definition, namespace, after, batch_size)
            now = int(self.clock())
            self.conn.execute("BEGIN TRANSACTION")
            try:
                changed_ids = []
                for index, row in enumerate(rows, start=1):
                    object_id, object_type, raw, revision, retracted = row
                    before_value = json.loads(raw)
                    precondition_errors = self._precondition_errors(
                        definition, before_value, int(revision)
                    )
                    if precondition_errors:
                        raise SchemaRegistryError(
                            "migration_precondition",
                            f"preconditions failed for {object_id}",
                            errors=precondition_errors,
                        )
                    after_value, after_type = self._transform(
                        before_value, object_type, definition["transforms"]
                    )
                    errors = self._validate_migrated(definition, after_value)
                    if errors:
                        raise SchemaRegistryError(
                            "migration_postcondition",
                            f"target validation failed for {object_id}",
                            errors=errors,
                        )
                    if before_value == after_value and object_type == after_type:
                        continue
                    before = {
                        "object_type": object_type,
                        "value": before_value,
                        "revision": int(revision),
                        "retracted": bool(retracted),
                    }
                    migrated_revision = int(revision) + 1
                    after_state = {
                        "object_type": after_type,
                        "value": after_value,
                        "revision": migrated_revision,
                        "retracted": bool(retracted),
                    }
                    self.conn.execute(
                        "UPDATE knowledge_objects SET object_type=?, value_json=?, "
                        "revision=?, updated_at_ms=?, last_batch_id=? "
                        "WHERE namespace=? AND object_id=? AND revision=?",
                        [
                            after_type,
                            _canonical(after_value),
                            migrated_revision,
                            now,
                            f"migration:{migration_id}",
                            namespace,
                            object_id,
                            revision,
                        ],
                    )
                    self.conn.execute(
                        "INSERT INTO knowledge_schema_migration_changes VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, FALSE)",
                        [
                            migration_id,
                            namespace,
                            object_id,
                            _canonical(before),
                            _canonical(after_state),
                            migrated_revision,
                            now,
                        ],
                    )
                    for artifact in invalidated_artifacts:
                        self.conn.execute(
                            "INSERT OR REPLACE INTO knowledge_derivation_invalidations "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            [
                                f"migration:{migration_id}:{namespace}",
                                namespace,
                                artifact,
                                object_id,
                                "schema-migration",
                                now,
                            ],
                        )
                    changed_ids.append(object_id)
                    if self.failure_hook:
                        self.failure_hook(index, {"object_id": object_id})
                last = rows[-1][0] if rows else after
                remaining = self._migration_rows(definition, namespace, last, 1)
                status = "in_progress" if remaining else "completed"
                total = processed + len(rows)
                watermark_row = self.conn.execute(
                    "SELECT watermark FROM knowledge_consolidation_watermarks "
                    "WHERE namespace=?",
                    [namespace],
                ).fetchone()
                watermark = int(watermark_row[0]) if watermark_row else 0
                if changed_ids:
                    watermark += 1
                    self.conn.execute(
                        "INSERT OR REPLACE INTO knowledge_consolidation_watermarks "
                        "VALUES (?, ?, ?, ?)",
                        [namespace, watermark, now, f"migration:{migration_id}"],
                    )
                self.conn.execute(
                    "INSERT OR REPLACE INTO knowledge_schema_migration_checkpoints "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [migration_id, namespace, last or None, total, status, now],
                )
                result = {
                    "migration_id": migration_id,
                    "namespace": namespace,
                    "status": status,
                    "processed_count": total,
                    "processed_in_call": len(rows),
                    "affected_object_ids": changed_ids,
                    "checkpoint": last or None,
                    "watermark": watermark,
                    "idempotent_replay": False,
                }
                self._lineage(
                    "execute-migration", migration_id, principal_id, result, now
                )
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def rollback_migration(
        self,
        migration_id: str,
        namespace: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        self._require(scopes, MIGRATE_SCOPE)
        self._require_namespace(namespace, scopes, write=True)
        if not reason.strip():
            raise SchemaRegistryError("invalid_reason", "rollback reason is required")
        ensure_schema_registry(self.conn)
        definition = self._migration(migration_id)
        invalidated_artifacts = ["consolidation", "search-index"]
        if any(item["op"] == "rename_type" for item in definition["transforms"]):
            invalidated_artifacts.append("graph-index")
        if not _table_exists(self.conn, "knowledge_schema_migration_changes"):
            raise SchemaRegistryError("not_found", "migration has no applied changes")
        rows = self.conn.execute(
            "SELECT object_id, before_json, migrated_revision FROM "
            "knowledge_schema_migration_changes WHERE migration_id=? AND namespace=? "
            "AND rolled_back=FALSE ORDER BY object_id DESC",
            [migration_id, namespace],
        ).fetchall()
        if not rows:
            checkpoint = self.conn.execute(
                "SELECT status, processed_count FROM knowledge_schema_migration_checkpoints "
                "WHERE migration_id=? AND namespace=?",
                [migration_id, namespace],
            ).fetchone()
            if checkpoint and checkpoint[0] == "rolled_back":
                return {
                    "migration_id": migration_id,
                    "namespace": namespace,
                    "status": "rolled_back",
                    "processed_count": int(checkpoint[1]),
                    "idempotent_replay": True,
                }
            raise SchemaRegistryError("not_found", "migration has no applied changes")
        conflicts = []
        for object_id, _before, migrated_revision in rows:
            current = self.conn.execute(
                "SELECT revision FROM knowledge_objects WHERE namespace=? AND object_id=?",
                [namespace, object_id],
            ).fetchone()
            if not current or int(current[0]) != int(migrated_revision):
                conflicts.append(
                    {
                        "object_id": object_id,
                        "expected_revision": int(migrated_revision),
                        "actual_revision": int(current[0]) if current else 0,
                    }
                )
        if conflicts:
            raise SchemaRegistryError(
                "rollback_conflict",
                "later revisions prevent compensation",
                conflicts=conflicts,
            )
        with _WRITE_LOCK:
            now = int(self.clock())
            self.conn.execute("BEGIN TRANSACTION")
            try:
                affected = []
                for object_id, raw_before, migrated_revision in rows:
                    before = json.loads(raw_before)
                    revision = int(migrated_revision) + 1
                    self.conn.execute(
                        "UPDATE knowledge_objects SET object_type=?, value_json=?, "
                        "revision=?, retracted=?, updated_at_ms=?, last_batch_id=? "
                        "WHERE namespace=? AND object_id=?",
                        [
                            before["object_type"],
                            _canonical(before["value"]),
                            revision,
                            before["retracted"],
                            now,
                            f"rollback:migration:{migration_id}",
                            namespace,
                            object_id,
                        ],
                    )
                    self.conn.execute(
                        "UPDATE knowledge_schema_migration_changes SET rolled_back=TRUE "
                        "WHERE migration_id=? AND namespace=? AND object_id=?",
                        [migration_id, namespace, object_id],
                    )
                    for artifact in invalidated_artifacts:
                        self.conn.execute(
                            "INSERT OR REPLACE INTO knowledge_derivation_invalidations "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            [
                                f"rollback:migration:{migration_id}:{namespace}",
                                namespace,
                                artifact,
                                object_id,
                                "schema-migration-rollback",
                                now,
                            ],
                        )
                    affected.append({"object_id": object_id, "revision": revision})
                watermark_row = self.conn.execute(
                    "SELECT watermark FROM knowledge_consolidation_watermarks "
                    "WHERE namespace=?",
                    [namespace],
                ).fetchone()
                watermark = (int(watermark_row[0]) if watermark_row else 0) + 1
                self.conn.execute(
                    "INSERT OR REPLACE INTO knowledge_consolidation_watermarks "
                    "VALUES (?, ?, ?, ?)",
                    [namespace, watermark, now, f"rollback:migration:{migration_id}"],
                )
                self.conn.execute(
                    "UPDATE knowledge_schema_migration_checkpoints SET status='rolled_back', "
                    "updated_at_ms=? WHERE migration_id=? AND namespace=?",
                    [now, migration_id, namespace],
                )
                result = {
                    "migration_id": migration_id,
                    "namespace": namespace,
                    "status": "rolled_back",
                    "reason": reason,
                    "affected": affected,
                    "watermark": watermark,
                    "idempotent_replay": False,
                }
                self._lineage(
                    "rollback-migration", migration_id, principal_id, result, now
                )
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def lineage(
        self, *, scopes: Iterable[str], after_sequence: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        self._require(scopes, READ_SCOPE)
        if not _table_exists(self.conn, "knowledge_schema_lineage"):
            return {
                "contract": AUDIT_CONTRACT,
                "events": [],
                "next_sequence": after_sequence,
            }
        rows = self.conn.execute(
            "SELECT sequence, event_id, action, subject_id, actor_id, detail_json, "
            "created_at_ms FROM knowledge_schema_lineage WHERE sequence>? "
            "ORDER BY sequence LIMIT ?",
            [max(0, int(after_sequence)), max(1, min(int(limit), 500))],
        ).fetchall()
        events = [
            {
                "sequence": int(row[0]),
                "event_id": row[1],
                "action": row[2],
                "subject_id": row[3],
                "actor_id": row[4],
                "detail": json.loads(row[5]),
                "created_at_ms": int(row[6]),
            }
            for row in rows
        ]
        return {
            "contract": AUDIT_CONTRACT,
            "events": events,
            "next_sequence": events[-1]["sequence"] if events else int(after_sequence),
        }


__all__ = [
    "AUDIT_CONTRACT",
    "CROSSWALK_CONTRACT",
    "DEPRECATE_SCOPE",
    "EXPORT_CONTRACT",
    "MIGRATE_SCOPE",
    "MIGRATION_CONTRACT",
    "MODULE_CONTRACT",
    "READ_SCOPE",
    "REGISTER_SCOPE",
    "VALIDATE_SCOPE",
    "SchemaRegistry",
    "SchemaRegistryError",
    "ensure_schema_registry",
]
