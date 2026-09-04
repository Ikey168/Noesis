"""Ontology concepts, semantic crosswalks, validation, and query expansion."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.kb.schema_registry import (
    MODULE_CONTRACT,
    READ_SCOPE,
    REGISTER_SCOPE,
    VALIDATE_SCOPE,
    SchemaRegistry,
    SchemaRegistryError,
    ensure_schema_registry,
)

ONTOLOGY_CONTRACT = "noesis-ontology-v1"
CROSSWALK_CONTRACT = "noesis-ontology-crosswalk-v1"
VALIDATION_CONTRACT = "noesis-ontology-validation-v1"
EXPANSION_CONTRACT = "noesis-ontology-expansion-v1"
EXPORT_CONTRACT = "noesis-ontology-export-v1"
MAPPING_KINDS = {"equivalent", "broader", "narrower", "related", "incompatible"}
LIFECYCLES = {"active", "deprecated"}

_DDL = """
CREATE TABLE IF NOT EXISTS ontology_validation_quarantine (
  quarantine_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, object_id TEXT NOT NULL,
  object_kind TEXT NOT NULL, ontology_module_id TEXT NOT NULL, concept_id TEXT NOT NULL,
  source_native_json TEXT NOT NULL, errors_json TEXT NOT NULL, input_hash TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS ontology_alignment_audit (
  audit_id TEXT PRIMARY KEY, operation TEXT NOT NULL, object_id TEXT NOT NULL,
  principal_id TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
"""


class OntologyError(SchemaRegistryError):
    """Stable ontology error compatible with the schema registry adapter."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _require(scopes: Iterable[str], required: str) -> None:
    if required not in {str(scope) for scope in scopes}:
        raise OntologyError("unauthorized", f"missing required scope {required}")


def _reference(value: Mapping[str, Any]) -> dict[str, str]:
    try:
        return {
            "kind": "ontology",
            "name": str(value["name"]),
            "version": str(value["version"]),
        }
    except KeyError as exc:
        raise OntologyError(
            "invalid_reference", "ontology reference needs name and version"
        ) from exc


def _validate_concepts(concepts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not concepts:
        raise OntologyError("invalid_ontology", "ontology needs at least one concept")
    if len(concepts) > 5000:
        raise OntologyError("limit_exceeded", "ontology is limited to 5000 concepts")
    values = [json.loads(_canonical(dict(item))) for item in concepts]
    identifiers = [str(item.get("concept_id") or "") for item in values]
    if any(not value for value in identifiers) or len(set(identifiers)) != len(
        identifiers
    ):
        raise OntologyError(
            "invalid_ontology", "concept identifiers must be non-empty and unique"
        )
    known = set(identifiers)
    graph: dict[str, list[str]] = {}
    for item in values:
        concept_id = str(item["concept_id"])
        if not str(item.get("definition") or "").strip():
            raise OntologyError(
                "invalid_ontology", f"concept {concept_id!r} needs a definition"
            )
        labels = item.get("labels") or []
        if not labels or any(
            not str(label.get("value") or "").strip() for label in labels
        ):
            raise OntologyError(
                "invalid_ontology", f"concept {concept_id!r} needs labelled values"
            )
        if item.get("lifecycle", "active") not in LIFECYCLES:
            raise OntologyError("invalid_ontology", "unsupported concept lifecycle")
        parents = [str(value) for value in item.get("broader", [])]
        unknown = sorted(set(parents) - known)
        if unknown:
            raise OntologyError(
                "unknown_concept",
                f"concept {concept_id!r} has unknown parents",
                unknown=unknown,
            )
        graph[concept_id] = parents
        item["broader"] = sorted(set(parents))
        item["labels"] = sorted(
            [dict(label) for label in labels],
            key=lambda label: (
                str(label.get("language", "und")),
                str(label.get("value")),
                str(label.get("kind", "preferred")),
            ),
        )
        item.setdefault("constraints", {})
        item.setdefault("lifecycle", "active")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise OntologyError("ontology_cycle", "broader hierarchy contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for parent in graph[node]:
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for concept_id in sorted(graph):
        visit(concept_id)
    return sorted(values, key=lambda item: item["concept_id"])


class OntologyAlignmentStore:
    """Semantic operations backed by the existing immutable schema module registry."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.registry = SchemaRegistry(conn, clock=self.now, initialize=initialize)
        if initialize:
            ensure_schema_registry(conn)
            conn.execute(_DDL)

    def _audit(self, operation, object_id, principal_id, detail, now) -> None:
        audit_id = (
            "ontology-audit:"
            + _digest([operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO ontology_alignment_audit VALUES (?,?,?,?,?,?)",
            [audit_id, operation, object_id, principal_id, _canonical(detail), now],
        )

    def _prior_observed_at(self, idempotency_key: str) -> int | None:
        try:
            row = self.conn.execute(
                "SELECT result_json FROM knowledge_schema_idempotency "
                "WHERE idempotency_key=?",
                [idempotency_key],
            ).fetchone()
            if not row:
                return None
            return int(json.loads(row[0])["content"]["observed_at_ms"])
        except (KeyError, TypeError, ValueError):
            return None

    def publish(
        self,
        name: str,
        semantic_version: str,
        concepts: Sequence[Mapping[str, Any]],
        *,
        owner: str,
        provenance: Mapping[str, Any],
        idempotency_key: str,
        principal_id: str,
        scopes: set[str],
        namespace_uri: str | None = None,
        dependencies: Sequence[Mapping[str, str]] = (),
        compatibility_policy: str = "backward",
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        _require(scopes, REGISTER_SCOPE)
        if cancel_requested:
            raise OntologyError("cancelled", "ontology publication was cancelled")
        normalized = _validate_concepts(concepts)
        content = {
            "contract": ONTOLOGY_CONTRACT,
            "namespace_uri": namespace_uri,
            "concepts": normalized,
            "object_types": [item["concept_id"] for item in normalized],
            "relation_types": ["broader"],
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms
                if observed_at_ms is not None
                else self._prior_observed_at(idempotency_key) or self.now()
            ),
            "producer": dict(
                producer or {"name": "noesis-ontology-alignment", "version": "1.0.0"}
            ),
            "policy": dict(policy or {"query_expansion": "bounded-v1"}),
        }
        definition = {
            "contract": MODULE_CONTRACT,
            "name": name,
            "kind": "ontology",
            "semantic_version": semantic_version,
            "content": content,
            "owner": owner,
            "dependencies": [dict(item) for item in dependencies],
            "compatibility_policy": compatibility_policy,
            "provenance": dict(provenance),
            "actor": {"principal_id": principal_id, "kind": "user"},
        }
        return self.registry.register(
            definition,
            idempotency_key,
            principal_id=principal_id,
            scopes=scopes,
        )

    def inspect(
        self,
        name: str,
        version: str,
        *,
        scopes: set[str],
        include_deprecated: bool = False,
    ) -> dict[str, Any]:
        return self.registry.resolve(
            "ontology",
            name,
            version,
            scopes=scopes,
            include_deprecated=include_deprecated,
        )

    def deprecate(
        self,
        module_id: str,
        reason: str,
        idempotency_key: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        module = self.registry.inspect(module_id, scopes={READ_SCOPE})
        if module["kind"] != "ontology":
            raise OntologyError("invalid_ontology", "module is not an ontology")
        return self.registry.deprecate(
            module_id,
            reason,
            idempotency_key,
            principal_id=principal_id,
            scopes=scopes,
        )

    @staticmethod
    def _concepts(module: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(item["concept_id"]): dict(item)
            for item in module["content"].get("concepts", [])
        }

    def register_crosswalk(
        self,
        name: str,
        semantic_version: str,
        source: Mapping[str, Any],
        target: Mapping[str, Any],
        mappings: Sequence[Mapping[str, Any]],
        *,
        owner: str,
        provenance: Mapping[str, Any],
        idempotency_key: str,
        principal_id: str,
        scopes: set[str],
        generation: int = 0,
        observed_at_ms: int | None = None,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        _require(scopes, REGISTER_SCOPE)
        if cancel_requested:
            raise OntologyError("cancelled", "crosswalk publication was cancelled")
        source_ref, target_ref = _reference(source), _reference(target)
        source_module = self.registry.resolve(
            "ontology", source_ref["name"], source_ref["version"], scopes={READ_SCOPE}
        )
        target_module = self.registry.resolve(
            "ontology", target_ref["name"], target_ref["version"], scopes={READ_SCOPE}
        )
        source_concepts, target_concepts = (
            self._concepts(source_module),
            self._concepts(target_module),
        )
        normalized = []
        if not mappings:
            raise OntologyError("invalid_crosswalk", "crosswalk needs mappings")
        if len(mappings) > 10_000:
            raise OntologyError(
                "limit_exceeded", "crosswalk is limited to 10000 mappings"
            )
        for mapping in mappings:
            value = json.loads(_canonical(dict(mapping)))
            kind = str(value.get("kind") or "")
            source_id, target_id = (
                str(value.get("source") or ""),
                str(value.get("target") or ""),
            )
            confidence = float(value.get("confidence", 1))
            local_extension = bool(value.get("local_extension", False))
            if kind not in MAPPING_KINDS or source_id not in source_concepts:
                raise OntologyError(
                    "invalid_crosswalk", "mapping kind and source concept must be valid"
                )
            if target_id not in target_concepts and not local_extension:
                raise OntologyError(
                    "unknown_concept", "target concept needs a declared local extension"
                )
            if not 0 <= confidence <= 1:
                raise OntologyError(
                    "invalid_confidence", "mapping confidence must be between 0 and 1"
                )
            value.update(
                {
                    "kind": kind,
                    "source": source_id,
                    "target": target_id,
                    "confidence": confidence,
                    "local_extension": local_extension,
                    "evidence": [dict(item) for item in value.get("evidence", [])],
                    "conditions": [dict(item) for item in value.get("conditions", [])],
                }
            )
            normalized.append(value)
        normalized.sort(key=lambda item: (item["source"], item["target"], item["kind"]))
        content = {
            "contract": CROSSWALK_CONTRACT,
            "source": source_ref,
            "target": target_ref,
            "mappings": normalized,
            "generation": int(generation),
            "observed_at_ms": int(
                observed_at_ms
                if observed_at_ms is not None
                else self._prior_observed_at(idempotency_key) or self.now()
            ),
        }
        definition = {
            "contract": MODULE_CONTRACT,
            "name": name,
            "kind": "crosswalk",
            "semantic_version": semantic_version,
            "content": content,
            "owner": owner,
            "dependencies": [source_ref, target_ref],
            "compatibility_policy": "none",
            "provenance": dict(provenance),
            "actor": {"principal_id": principal_id, "kind": "user"},
        }
        return self.registry.register(
            definition,
            idempotency_key,
            principal_id=principal_id,
            scopes=scopes,
        )

    def _validate_constraints(
        self, concept: Mapping[str, Any], value: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        constraints = concept.get("constraints") or {}
        errors = []
        for field in constraints.get("required", []):
            if field not in value:
                errors.append({"code": "required", "field": field})
        types = constraints.get("properties") or {}
        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        for field, rule in types.items():
            if field not in value or not rule.get("type"):
                continue
            expected = type_map.get(rule["type"])
            actual = value[field]
            if expected and (
                not isinstance(actual, expected)
                or rule["type"] in {"number", "integer"}
                and isinstance(actual, bool)
            ):
                errors.append(
                    {
                        "code": "type",
                        "field": field,
                        "expected": rule["type"],
                    }
                )
        return errors[:50]

    def validate(
        self,
        namespace: str,
        object_id: str,
        object_kind: str,
        ontology: Mapping[str, Any],
        concept_id: str,
        value: Mapping[str, Any],
        *,
        source_native: Mapping[str, Any],
        quarantine: bool,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, VALIDATE_SCOPE)
        if object_kind not in {"entity", "relation", "event", "metric", "claim"}:
            raise OntologyError(
                "invalid_object_kind", "unsupported knowledge object kind"
            )
        module = self.inspect(
            str(ontology["name"]),
            str(ontology["version"]),
            scopes={READ_SCOPE},
            include_deprecated=True,
        )
        concept = self._concepts(module).get(concept_id)
        if not concept:
            raise OntologyError("unknown_concept", f"concept {concept_id!r} is unknown")
        errors = self._validate_constraints(concept, value)
        if concept.get("lifecycle") == "deprecated":
            errors.append({"code": "deprecated_concept", "concept_id": concept_id})
        stable = {
            "namespace": namespace,
            "object_id": object_id,
            "object_kind": object_kind,
            "ontology_module_id": module["module_id"],
            "concept_id": concept_id,
            "value": dict(value),
            "source_native": dict(source_native),
            "errors": errors,
        }
        input_hash = _digest(stable)
        result = {
            "contract": VALIDATION_CONTRACT,
            **stable,
            "valid": not errors,
            "status": "valid"
            if not errors
            else "quarantined"
            if quarantine
            else "invalid",
            "input_hash": input_hash,
            "quarantine_id": None,
        }
        if errors and quarantine:
            ensure_schema_registry(self.conn)
            self.conn.execute(_DDL)
            quarantine_id = "ontology-quarantine:" + input_hash[:24]
            existing = self.conn.execute(
                "SELECT created_at_ms FROM ontology_validation_quarantine WHERE quarantine_id=?",
                [quarantine_id],
            ).fetchone()
            now = int(existing[0]) if existing else self.now()
            if not existing:
                self.conn.execute("BEGIN")
                try:
                    self.conn.execute(
                        "INSERT INTO ontology_validation_quarantine VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        [
                            quarantine_id,
                            namespace,
                            object_id,
                            object_kind,
                            module["module_id"],
                            concept_id,
                            _canonical(source_native),
                            _canonical(errors),
                            input_hash,
                            principal_id,
                            now,
                        ],
                    )
                    self._audit(
                        "quarantine",
                        quarantine_id,
                        principal_id,
                        {"object_id": object_id},
                        now,
                    )
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            result["quarantine_id"] = quarantine_id
            result["idempotent"] = bool(existing)
        return result

    def quarantine(
        self, namespace: str, *, scopes: set[str], limit: int = 100
    ) -> list[dict[str, Any]]:
        _require(scopes, READ_SCOPE)
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name='ontology_validation_quarantine'"
        ).fetchone():
            return []
        rows = self.conn.execute(
            "SELECT quarantine_id,object_id,object_kind,ontology_module_id,concept_id,source_native_json,errors_json,input_hash,principal_id,created_at_ms FROM ontology_validation_quarantine WHERE namespace=? ORDER BY created_at_ms DESC,quarantine_id LIMIT ?",
            [namespace, min(max(limit, 1), 500)],
        ).fetchall()
        return [
            {
                "quarantine_id": row[0],
                "namespace": namespace,
                "object_id": row[1],
                "object_kind": row[2],
                "ontology_module_id": row[3],
                "concept_id": row[4],
                "source_native": json.loads(row[5]),
                "errors": json.loads(row[6]),
                "input_hash": row[7],
                "principal_id": row[8],
                "created_at_ms": int(row[9]),
            }
            for row in rows
        ]

    def _crosswalks(self) -> list[dict[str, Any]]:
        exported = self.registry.export(scopes={READ_SCOPE})
        return [
            module
            for module in exported["modules"]
            if module["kind"] == "crosswalk"
            and module["content"].get("contract") == CROSSWALK_CONTRACT
            and module["status"] == "active"
        ]

    def expand(
        self,
        ontology: Mapping[str, Any],
        concept_id: str,
        *,
        scopes: set[str],
        relationships: Sequence[str] = ("equivalent", "narrower"),
        max_depth: int = 2,
        max_terms: int = 50,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        if not 0 <= max_depth <= 6 or not 1 <= max_terms <= 200:
            raise OntologyError("invalid_bound", "expansion bounds are out of range")
        allowed = set(relationships)
        if not allowed <= MAPPING_KINDS - {"incompatible"}:
            raise OntologyError("invalid_mapping", "unsupported expansion relationship")
        start_module = self.inspect(
            str(ontology["name"]), str(ontology["version"]), scopes=scopes
        )
        if concept_id not in self._concepts(start_module):
            raise OntologyError("unknown_concept", f"concept {concept_id!r} is unknown")
        modules = {
            module["module_id"]: module
            for module in self.registry.export(scopes={READ_SCOPE})["modules"]
            if module["kind"] == "ontology"
        }
        start = (start_module["module_id"], concept_id)
        queue = [(start, 0, 1.0, [])]
        best: dict[tuple[str, str], dict[str, Any]] = {}
        conflicts: set[tuple[str, str]] = set()
        crosswalks = self._crosswalks()
        resolved_crosswalks = []
        incompatible_pairs = set()
        for crosswalk in crosswalks:
            content = crosswalk["content"]
            try:
                source_module = self.registry.resolve(
                    "ontology",
                    content["source"]["name"],
                    content["source"]["version"],
                    scopes={READ_SCOPE},
                    include_deprecated=True,
                )
                target_module = self.registry.resolve(
                    "ontology",
                    content["target"]["name"],
                    content["target"]["version"],
                    scopes={READ_SCOPE},
                    include_deprecated=True,
                )
            except SchemaRegistryError:
                continue
            resolved_crosswalks.append((crosswalk, source_module, target_module))
            incompatible_pairs.update(
                {
                    (
                        source_module["module_id"],
                        mapping["source"],
                        target_module["module_id"],
                        mapping["target"],
                    )
                    for mapping in content["mappings"]
                    if mapping["kind"] == "incompatible"
                }
            )
        while queue and len(best) < max_terms:
            (module_id, current_id), depth, score, path = queue.pop(0)
            key = (module_id, current_id)
            if key in best and best[key]["score"] >= score:
                continue
            module = modules.get(module_id)
            if not module:
                continue
            best[key] = {
                "ontology_module_id": module_id,
                "ontology": module["name"],
                "version": module["semantic_version"],
                "concept_id": current_id,
                "score": round(score, 6),
                "path": path,
            }
            if depth >= max_depth:
                continue
            concepts = self._concepts(module)
            concept = concepts.get(current_id, {})
            if "broader" in allowed:
                for parent in concept.get("broader", []):
                    queue.append(
                        (
                            (module_id, parent),
                            depth + 1,
                            score * 0.8,
                            path + [{"kind": "broader", "to": parent}],
                        )
                    )
            if "narrower" in allowed:
                for child, value in concepts.items():
                    if current_id in value.get("broader", []):
                        queue.append(
                            (
                                (module_id, child),
                                depth + 1,
                                score * 0.8,
                                path + [{"kind": "narrower", "to": child}],
                            )
                        )
            for crosswalk, source_module, target_module in resolved_crosswalks:
                content = crosswalk["content"]
                for mapping in content["mappings"]:
                    if (
                        source_module["module_id"] != module_id
                        or mapping["source"] != current_id
                    ):
                        continue
                    pair = (mapping["source"], mapping["target"])
                    full_pair = (
                        source_module["module_id"],
                        mapping["source"],
                        target_module["module_id"],
                        mapping["target"],
                    )
                    if mapping["kind"] == "incompatible":
                        conflicts.add(pair)
                    elif (
                        mapping["kind"] in allowed
                        and full_pair not in incompatible_pairs
                    ):
                        queue.append(
                            (
                                (target_module["module_id"], mapping["target"]),
                                depth + 1,
                                score * float(mapping["confidence"]),
                                path
                                + [
                                    {
                                        "kind": mapping["kind"],
                                        "crosswalk_module_id": crosswalk["module_id"],
                                        "to": mapping["target"],
                                        "confidence": mapping["confidence"],
                                    }
                                ],
                            )
                        )
        terms = sorted(
            best.values(),
            key=lambda item: (
                -item["score"],
                item["ontology_module_id"],
                item["concept_id"],
            ),
        )
        expanded_terms = [
            item
            for item in terms
            if (item["ontology_module_id"], item["concept_id"]) != start
        ]
        top_expanded_score = max(
            (item["score"] for item in expanded_terms), default=None
        )
        aggregations = [
            {"ontology": name, "count": sum(item["ontology"] == name for item in terms)}
            for name in sorted({item["ontology"] for item in terms})
        ]
        return {
            "contract": EXPANSION_CONTRACT,
            "start": {"ontology_module_id": start[0], "concept_id": start[1]},
            "relationships": sorted(allowed),
            "max_depth": max_depth,
            "max_terms": max_terms,
            "terms": terms[:max_terms],
            "truncated": bool(queue) or len(best) > max_terms,
            "ambiguous": top_expanded_score is not None
            and len(
                [item for item in expanded_terms if item["score"] == top_expanded_score]
            )
            > 1,
            "conflicts": [
                {"source": left, "target": right} for left, right in sorted(conflicts)
            ],
            "aggregations": aggregations,
            "expansion_hash": _digest(
                [
                    start,
                    sorted(allowed),
                    max_depth,
                    terms[:max_terms],
                    sorted(conflicts),
                ]
            ),
        }

    def diff(
        self, name: str, old_version: str, new_version: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        old = self.inspect(name, old_version, scopes=scopes, include_deprecated=True)
        new = self.inspect(name, new_version, scopes=scopes, include_deprecated=True)
        before, after = self._concepts(old), self._concepts(new)
        changed = []
        for concept_id in sorted(set(before) & set(after)):
            fields = [
                field
                for field in (
                    "labels",
                    "definition",
                    "broader",
                    "constraints",
                    "lifecycle",
                )
                if before[concept_id].get(field) != after[concept_id].get(field)
            ]
            if fields:
                changed.append({"concept_id": concept_id, "fields": fields})
        return {
            "name": name,
            "old_module_id": old["module_id"],
            "new_module_id": new["module_id"],
            "added": sorted(set(after) - set(before)),
            "removed": sorted(set(before) - set(after)),
            "changed": changed,
            "compatibility": self.registry.compare(
                {"kind": "ontology", "name": name, "version": old_version},
                {"kind": "ontology", "name": name, "version": new_version},
                scopes=scopes,
            ),
            "diff_hash": _digest([old["module_id"], new["module_id"], changed]),
        }

    def export(self, *, scopes: set[str]) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        modules = [
            module
            for module in self.registry.export(scopes=scopes)["modules"]
            if module["kind"] in {"ontology", "crosswalk"}
        ]
        modules.sort(
            key=lambda item: (
                item["kind"],
                item["name"],
                item["semantic_version"],
                item["module_id"],
            )
        )
        return {
            "contract": EXPORT_CONTRACT,
            "modules": modules,
            "content_hash": "sha256:" + _digest(modules),
        }
