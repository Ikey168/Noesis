"""Immutable, support-aware revisions for source-derived knowledge objects.

Document revisions are the ingestion source of truth.  This module maintains the
next layer: claims, entities, relations, summaries, lexical entries, and vector
entries derived from exact document revisions.  A current projection is kept
for inexpensive reads, while every meaningful support or content transition is
append-only and replayable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

REVISION_CONTRACT = "noesis-derived-object-revision-v1"
DELTA_CONTRACT = "noesis-derived-object-generation-delta-v1"
GENERATION_CONTRACT = "noesis-derived-object-generation-v1"
REPLAY_CONTRACT = "noesis-derived-object-replay-v1"
LINEAGE_CONTRACT = "noesis-derived-object-lineage-v1"
OBJECT_TYPES = frozenset(
    {"claim", "entity", "relation", "summary", "index", "embedding"}
)
PROJECTION_KINDS = frozenset({"lexical", "vector", "graph", "summary"})


_DDL = """
CREATE TABLE IF NOT EXISTS derived_object_revisions (
  namespace TEXT NOT NULL, logical_id TEXT NOT NULL, object_type TEXT NOT NULL,
  revision BIGINT NOT NULL, revision_id TEXT NOT NULL UNIQUE,
  predecessor_revision_id TEXT, content_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  support_json TEXT NOT NULL, support_hash TEXT NOT NULL, lifecycle TEXT NOT NULL,
  change_kind TEXT NOT NULL, producer_json TEXT NOT NULL, configuration_hash TEXT NOT NULL,
  generation BIGINT NOT NULL, observed_at_ms BIGINT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace,logical_id,revision)
);
CREATE TABLE IF NOT EXISTS derived_object_current (
  namespace TEXT NOT NULL, logical_id TEXT NOT NULL, object_type TEXT NOT NULL,
  revision BIGINT NOT NULL, revision_id TEXT NOT NULL, lifecycle TEXT NOT NULL,
  updated_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace,logical_id)
);
CREATE TABLE IF NOT EXISTS derived_object_current_supports (
  namespace TEXT NOT NULL, logical_id TEXT NOT NULL, object_type TEXT NOT NULL,
  source_revision_id TEXT NOT NULL, document_id TEXT NOT NULL,
  content_json TEXT NOT NULL, content_hash TEXT NOT NULL, producer_json TEXT NOT NULL,
  configuration_hash TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace,logical_id,source_revision_id)
);
CREATE TABLE IF NOT EXISTS derived_object_generation_changes (
  namespace TEXT NOT NULL, generation BIGINT NOT NULL, ordinal BIGINT NOT NULL,
  change_id TEXT NOT NULL UNIQUE, logical_id TEXT NOT NULL, object_type TEXT NOT NULL,
  change_kind TEXT NOT NULL, predecessor_revision_id TEXT, revision_id TEXT NOT NULL,
  reason_json TEXT NOT NULL, committed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace,generation,ordinal)
);
CREATE TABLE IF NOT EXISTS derived_object_generations (
  namespace TEXT NOT NULL, generation BIGINT NOT NULL, input_hash TEXT NOT NULL,
  change_hash TEXT NOT NULL, item_count BIGINT NOT NULL, counts_json TEXT NOT NULL,
  status TEXT NOT NULL, committed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace,generation)
);
CREATE TABLE IF NOT EXISTS derived_projection_items (
  namespace TEXT NOT NULL, projection_kind TEXT NOT NULL, item_id TEXT NOT NULL,
  logical_id TEXT NOT NULL, object_type TEXT NOT NULL, object_revision_id TEXT NOT NULL,
  content_json TEXT NOT NULL, content_hash TEXT NOT NULL, generation BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace,projection_kind,item_id,object_revision_id)
);
CREATE INDEX IF NOT EXISTS idx_derived_revision_generation
  ON derived_object_revisions(namespace,generation,logical_id);
CREATE INDEX IF NOT EXISTS idx_derived_support_document
  ON derived_object_current_supports(namespace,document_id,logical_id);
CREATE INDEX IF NOT EXISTS idx_derived_change_object
  ON derived_object_generation_changes(namespace,logical_id,generation);
CREATE INDEX IF NOT EXISTS idx_derived_projection_object
  ON derived_projection_items(namespace,logical_id,projection_kind);
"""


class DerivedRevisionError(ValueError):
    """Stable validation error safe to expose through MCP."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _cursor(value: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(_canonical(value).encode()).decode().rstrip("=")


def _decode_cursor(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except Exception as exc:
        raise DerivedRevisionError(
            "invalid_cursor", "derived delta cursor is malformed"
        ) from exc


def logical_identity(
    object_type: str, content: Mapping[str, Any], explicit: str | None = None
) -> str:
    """Return a stable, content-semantic logical identity for one object."""

    if object_type not in OBJECT_TYPES:
        raise DerivedRevisionError(
            "invalid_object_type", f"unsupported derived type {object_type!r}"
        )
    if explicit and str(explicit).strip():
        return f"{object_type}:{str(explicit).strip()}"
    identifiers = (
        content.get("logical_id"),
        content.get(f"{object_type}_id"),
        content.get("canonical_id"),
        content.get("id"),
    )
    identifier = next((str(value).strip() for value in identifiers if value), "")
    if identifier:
        return f"{object_type}:{identifier}"
    if object_type == "claim":
        basis: Any = {
            "statement": _normalized_text(
                content.get("statement") or content.get("text")
            )
        }
    elif object_type == "entity":
        basis = {
            "name": _normalized_text(content.get("name") or content.get("label")),
            "kind": _normalized_text(content.get("kind") or content.get("type")),
        }
    elif object_type == "relation":
        basis = {
            "subject": _normalized_text(
                content.get("subject_id") or content.get("subject")
            ),
            "predicate": _normalized_text(
                content.get("predicate") or content.get("relation")
            ),
            "object": _normalized_text(
                content.get("object_id") or content.get("object")
            ),
        }
    else:
        document_id = content.get("document_id")
        basis = {"document_id": str(document_id)} if document_id else content
    return f"{object_type}:sha256:{_digest(basis)}"


def _text(content: Mapping[str, Any]) -> str:
    for key in ("statement", "text", "summary", "title", "name", "label"):
        if content.get(key):
            return str(content[key])
    if {"subject", "predicate", "object"} <= set(content):
        return f"{content['subject']} {content['predicate']} {content['object']}"
    return _canonical(content)


def _vector(text: str, dimensions: int = 12) -> list[float]:
    raw = hashlib.sha256(text.encode()).digest()
    values = [(raw[index] - 127.5) / 127.5 for index in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 8) for value in values]


def _projection_values(
    logical_id: str, object_type: str, content: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    text = _text(content)
    values: dict[str, dict[str, Any]] = {}
    if object_type in {"claim", "entity", "relation", "summary", "index"}:
        values["lexical"] = {"text": text, "normalized_text": _normalized_text(text)}
    if object_type in {"claim", "entity", "summary", "index", "embedding"}:
        values["vector"] = {
            "model": "noesis-content-hash-vector-v1",
            "dimensions": 12,
            "vector": _vector(text),
        }
    if object_type == "entity":
        values["graph"] = {
            "kind": "node",
            "node_id": logical_id,
            "label": content.get("name") or content.get("label") or text,
            "entity_type": content.get("kind") or content.get("type"),
        }
    elif object_type == "relation":
        values["graph"] = {
            "kind": "edge",
            "edge_id": logical_id,
            "subject": content.get("subject_id") or content.get("subject"),
            "predicate": content.get("predicate") or content.get("relation"),
            "object": content.get("object_id") or content.get("object"),
        }
    if object_type == "summary":
        values["summary"] = {"text": text, "document_id": content.get("document_id")}
    return values


def maintenance_observations(
    documents: Sequence[Mapping[str, Any]], extraction: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Turn changed workflow inputs into independently supported objects."""

    revisions = {
        str(document["document_id"]): str(
            document.get("_revision_id") or _digest(document)
        )
        for document in documents
    }
    observations: list[dict[str, Any]] = []
    for document in sorted(documents, key=lambda item: str(item["document_id"])):
        document_id = str(document["document_id"])
        revision_id = revisions[document_id]
        metadata = dict(document.get("metadata") or {})
        text = str(document.get("content") or document.get("text") or "")
        common = {
            "document_id": document_id,
            "source_revision_id": revision_id,
            "producer": {"name": "knowledge-maintenance", "version": "1.0.0"},
            "configuration": {"mode": "deterministic-local"},
        }
        index = {
            "document_id": document_id,
            "title": document.get("title"),
            "text": text,
            "url": document.get("url"),
            "domain": metadata.get("domain"),
        }
        observations.extend(
            [
                {
                    **common,
                    "object_type": "index",
                    "logical_id": f"document:{document_id}",
                    "content": index,
                },
                {
                    **common,
                    "object_type": "embedding",
                    "logical_id": f"document:{document_id}",
                    "content": {"document_id": document_id, "text": text},
                },
                {
                    **common,
                    "object_type": "summary",
                    "logical_id": f"document:{document_id}",
                    "content": {"document_id": document_id, "summary": text[:500]},
                },
            ]
        )
    for output in extraction.get("outputs") or []:
        item = dict(output.get("output") or {})
        object_type = str(item.get("output_type") or "")
        if object_type not in {"claim", "entity", "relation"}:
            continue
        document_id = str(output.get("input_id") or "")
        if document_id not in revisions:
            continue
        content = dict(item.get("value") or {})
        observations.append(
            {
                "object_type": object_type,
                "logical_id": item.get("logical_id"),
                "content": content,
                "document_id": document_id,
                "source_revision_id": revisions[document_id],
                "producer": {
                    "name": str(
                        (output.get("provenance") or {}).get("extractor_name")
                        or "extractor"
                    ),
                    "version": str(
                        (output.get("provenance") or {}).get("extractor_version")
                        or "unknown"
                    ),
                    "extractor_id": (output.get("provenance") or {}).get(
                        "extractor_id"
                    ),
                },
                "configuration": {
                    "configuration_hash": (output.get("provenance") or {}).get(
                        "configuration_hash"
                    )
                },
            }
        )
    return observations


class DerivedRevisionStore:
    """Apply support changes and publish object projections in one transaction."""

    def __init__(self, conn: Any, *, initialize: bool = True) -> None:
        self.conn = conn
        if initialize:
            conn.execute(_DDL)
            conn.execute(
                "ALTER TABLE derived_object_generations "
                "ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'committed'"
            )

    @staticmethod
    def _normalize_observation(value: Mapping[str, Any]) -> dict[str, Any]:
        object_type = str(value.get("object_type") or "")
        content = dict(value.get("content") or {})
        document_id = str(value.get("document_id") or "").strip()
        source_revision_id = str(value.get("source_revision_id") or "").strip()
        producer = dict(value.get("producer") or {})
        if object_type not in OBJECT_TYPES:
            raise DerivedRevisionError(
                "invalid_object_type", f"unsupported derived type {object_type!r}"
            )
        if not document_id or not source_revision_id:
            raise DerivedRevisionError(
                "invalid_support", "document and source revision identity are required"
            )
        if not producer.get("name") or not producer.get("version"):
            raise DerivedRevisionError(
                "invalid_producer", "producer name and version are required"
            )
        logical_id = logical_identity(object_type, content, value.get("logical_id"))
        configuration = dict(value.get("configuration") or {})
        return {
            "logical_id": logical_id,
            "object_type": object_type,
            "content": content,
            "content_hash": _digest(content),
            "document_id": document_id,
            "source_revision_id": source_revision_id,
            "producer": producer,
            "configuration_hash": _digest(configuration),
            "observed_at_ms": int(value.get("observed_at_ms") or 0),
        }

    def _generation(self, namespace: str, generation: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT input_hash,change_hash,item_count,counts_json,status,committed_at_ms "
            "FROM derived_object_generations WHERE namespace=? AND generation=?",
            [namespace, generation],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": GENERATION_CONTRACT,
            "namespace": namespace,
            "generation": generation,
            "input_hash": row[0],
            "change_hash": row[1],
            "item_count": int(row[2]),
            "counts": _load(row[3], {}),
            "status": row[4],
            "committed_at_ms": int(row[5]),
        }

    def apply_generation(
        self,
        namespace: str,
        generation: int,
        observations: Sequence[Mapping[str, Any]],
        changes: Sequence[Mapping[str, Any]],
        *,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        if not namespace.strip() or generation < 1:
            raise DerivedRevisionError(
                "invalid_generation", "namespace and positive generation are required"
            )
        normalized = sorted(
            (self._normalize_observation(item) for item in observations),
            key=lambda item: (item["logical_id"], item["source_revision_id"]),
        )
        seen: dict[tuple[str, str], str] = {}
        for item in normalized:
            key = (item["logical_id"], item["source_revision_id"])
            if key in seen and seen[key] != item["content_hash"]:
                raise DerivedRevisionError(
                    "support_conflict",
                    "one source revision produced conflicting object content",
                )
            seen[key] = item["content_hash"]
        normalized = [
            item
            for index, item in enumerate(normalized)
            if index == 0
            or (item["logical_id"], item["source_revision_id"])
            != (
                normalized[index - 1]["logical_id"],
                normalized[index - 1]["source_revision_id"],
            )
        ]
        changed_documents = sorted(
            {
                str(item.get("document_id") or "")
                for item in changes
                if item.get("document_id")
            }
        )
        input_hash = _digest(
            {"observations": normalized, "changes": [dict(item) for item in changes]}
        )
        prior_commit = self._generation(namespace, generation)
        if prior_commit:
            if prior_commit["input_hash"] != input_hash:
                raise DerivedRevisionError(
                    "generation_conflict",
                    "generation was already committed with different inputs",
                )
            return {
                **{
                    key: value for key, value in prior_commit.items() if key != "status"
                },
                "idempotent": True,
                "changed": [],
            }
        last = self.conn.execute(
            "SELECT MAX(generation) FROM derived_object_generations WHERE namespace=?",
            [namespace],
        ).fetchone()[0]
        if last is not None and generation <= int(last):
            raise DerivedRevisionError(
                "generation_conflict", "generation must advance monotonically"
            )
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        affected: set[str] = {item["logical_id"] for item in normalized}
        if changed_documents:
            placeholders = ",".join("?" for _ in changed_documents)
            affected.update(
                row[0]
                for row in self.conn.execute(
                    "SELECT DISTINCT logical_id FROM derived_object_current_supports "
                    f"WHERE namespace=? AND document_id IN ({placeholders})",
                    [namespace, *changed_documents],
                ).fetchall()
            )
        created_changes: list[dict[str, Any]] = []
        self.conn.execute("BEGIN")
        try:
            if changed_documents:
                placeholders = ",".join("?" for _ in changed_documents)
                self.conn.execute(
                    "DELETE FROM derived_object_current_supports "
                    f"WHERE namespace=? AND document_id IN ({placeholders})",
                    [namespace, *changed_documents],
                )
            for item in normalized:
                self.conn.execute(
                    "INSERT OR REPLACE INTO derived_object_current_supports VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        item["logical_id"],
                        item["object_type"],
                        item["source_revision_id"],
                        item["document_id"],
                        _canonical(item["content"]),
                        item["content_hash"],
                        _canonical(item["producer"]),
                        item["configuration_hash"],
                        item["observed_at_ms"] or now,
                    ],
                )
            for logical_id in sorted(affected):
                current = self.conn.execute(
                    "SELECT object_type,revision,revision_id,lifecycle FROM derived_object_current "
                    "WHERE namespace=? AND logical_id=?",
                    [namespace, logical_id],
                ).fetchone()
                supports = self.conn.execute(
                    "SELECT object_type,source_revision_id,document_id,content_json,content_hash,"
                    "producer_json,configuration_hash,observed_at_ms FROM derived_object_current_supports "
                    "WHERE namespace=? AND logical_id=? ORDER BY source_revision_id",
                    [namespace, logical_id],
                ).fetchall()
                if not supports and not current:
                    continue
                if supports:
                    variants = Counter(row[4] for row in supports)
                    selected_hash = min(
                        variants, key=lambda value: (-variants[value], value)
                    )
                    selected = next(row for row in supports if row[4] == selected_hash)
                    object_type = str(selected[0])
                    content = _load(selected[3], {})
                    lifecycle = "active"
                    producer = _load(selected[5], {})
                    configuration_hash = str(selected[6])
                    observed_at = max(int(row[7]) for row in supports)
                    support = [
                        {
                            "source_revision_id": row[1],
                            "document_id": row[2],
                            "content_hash": row[4],
                            "selected_content": row[4] == selected_hash,
                        }
                        for row in supports
                    ]
                else:
                    prior_revision = self.revision(
                        namespace,
                        logical_id,
                        include_retracted=True,
                        include_unpublished=True,
                    )
                    assert prior_revision is not None
                    object_type = str(current[0])
                    content = dict(prior_revision["content"])
                    selected_hash = str(prior_revision["content_hash"])
                    lifecycle = "retracted"
                    producer = dict(prior_revision["producer"])
                    configuration_hash = str(prior_revision["configuration_hash"])
                    observed_at = now
                    support = []
                support_hash = _digest(support)
                if current:
                    prior_revision = self.revision(
                        namespace,
                        logical_id,
                        include_retracted=True,
                        include_unpublished=True,
                    )
                    assert prior_revision is not None
                    if (
                        prior_revision["content_hash"] == selected_hash
                        and prior_revision["support_hash"] == support_hash
                        and prior_revision["lifecycle"] == lifecycle
                    ):
                        continue
                    revision_number = int(current[1]) + 1
                    predecessor = str(current[2])
                    if lifecycle == "retracted":
                        change_kind = "retracted"
                    elif current[3] == "retracted":
                        change_kind = "restored"
                    elif prior_revision["content_hash"] != selected_hash:
                        change_kind = "updated"
                    else:
                        change_kind = "support_updated"
                else:
                    revision_number, predecessor, change_kind = 1, None, "added"
                revision_id = (
                    "derived-revision:"
                    + _digest(
                        [
                            namespace,
                            logical_id,
                            revision_number,
                            selected_hash,
                            support_hash,
                            lifecycle,
                        ]
                    )[:24]
                )
                self.conn.execute(
                    "INSERT INTO derived_object_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        logical_id,
                        object_type,
                        revision_number,
                        revision_id,
                        predecessor,
                        _canonical(content),
                        selected_hash,
                        _canonical(support),
                        support_hash,
                        lifecycle,
                        change_kind,
                        _canonical(producer),
                        configuration_hash,
                        generation,
                        observed_at,
                        now,
                    ],
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO derived_object_current VALUES (?,?,?,?,?,?,?)",
                    [
                        namespace,
                        logical_id,
                        object_type,
                        revision_number,
                        revision_id,
                        lifecycle,
                        now,
                    ],
                )
                if lifecycle == "active":
                    for projection_kind, projection in sorted(
                        _projection_values(logical_id, object_type, content).items()
                    ):
                        item_id = f"{projection_kind}:{logical_id}"
                        self.conn.execute(
                            "INSERT OR IGNORE INTO derived_projection_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                            [
                                namespace,
                                projection_kind,
                                item_id,
                                logical_id,
                                object_type,
                                revision_id,
                                _canonical(projection),
                                _digest(projection),
                                generation,
                                now,
                            ],
                        )
                reason = {
                    "changed_documents": changed_documents,
                    "support_count": len(support),
                    "source_revisions": [
                        item["source_revision_id"] for item in support
                    ],
                }
                created_changes.append(
                    {
                        "logical_id": logical_id,
                        "object_type": object_type,
                        "change_kind": change_kind,
                        "predecessor_revision_id": predecessor,
                        "revision_id": revision_id,
                        "reason": reason,
                    }
                )
            for ordinal, item in enumerate(created_changes):
                change_id = (
                    "derived-change:"
                    + _digest([namespace, generation, ordinal, item])[:24]
                )
                self.conn.execute(
                    "INSERT INTO derived_object_generation_changes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        generation,
                        ordinal,
                        change_id,
                        item["logical_id"],
                        item["object_type"],
                        item["change_kind"],
                        item["predecessor_revision_id"],
                        item["revision_id"],
                        _canonical(item["reason"]),
                        now,
                    ],
                )
                item["change_id"] = change_id
            counts = dict(
                sorted(Counter(item["change_kind"] for item in created_changes).items())
            )
            change_hash = _digest(created_changes)
            self.conn.execute(
                "INSERT INTO derived_object_generations "
                "(namespace,generation,input_hash,change_hash,item_count,counts_json,status,committed_at_ms) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    generation,
                    input_hash,
                    change_hash,
                    len(created_changes),
                    _canonical(counts),
                    "staged",
                    now,
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "contract": GENERATION_CONTRACT,
            "namespace": namespace,
            "generation": generation,
            "input_hash": input_hash,
            "change_hash": change_hash,
            "item_count": len(created_changes),
            "counts": counts,
            "committed_at_ms": now,
            "changed": created_changes,
            "mixed_generations_visible": False,
        }

    def publish_generation(self, namespace: str, generation: int) -> dict[str, Any]:
        """Publish a staged generation inside the caller's transaction."""

        current = self._generation(namespace, generation)
        if not current:
            raise DerivedRevisionError(
                "generation_unavailable", "derived generation is not staged"
            )
        if current["status"] == "committed":
            return {**current, "idempotent": True}
        changed = self.conn.execute(
            "UPDATE derived_object_generations SET status='committed' "
            "WHERE namespace=? AND generation=? AND status='staged' RETURNING generation",
            [namespace, generation],
        ).fetchone()
        if not changed:
            raise DerivedRevisionError(
                "generation_conflict", "derived generation is not publishable"
            )
        return {**current, "status": "committed"}

    def revision(
        self,
        namespace: str,
        logical_id: str,
        *,
        revision: int | None = None,
        generation: int | None = None,
        include_retracted: bool = False,
        include_unpublished: bool = False,
    ) -> dict[str, Any] | None:
        if revision is not None and generation is not None:
            raise DerivedRevisionError(
                "mixed_generation", "revision and generation are mutually exclusive"
            )
        clauses, params = ["namespace=?", "logical_id=?"], [namespace, logical_id]
        if revision is not None:
            clauses.append("revision=?")
            params.append(int(revision))
        if generation is not None:
            committed = self._generation(namespace, int(generation))
            if not committed or committed["status"] != "committed":
                raise DerivedRevisionError(
                    "generation_unavailable", "derived generation is not committed"
                )
            clauses.append("generation<=?")
            params.append(int(generation))
        if not include_unpublished:
            clauses.append(
                "EXISTS (SELECT 1 FROM derived_object_generations g "
                "WHERE g.namespace=derived_object_revisions.namespace "
                "AND g.generation=derived_object_revisions.generation "
                "AND g.status='committed')"
            )
        row = self.conn.execute(
            "SELECT object_type,revision,revision_id,predecessor_revision_id,content_json,"
            "content_hash,support_json,support_hash,lifecycle,change_kind,producer_json,"
            "configuration_hash,generation,observed_at_ms,created_at_ms "
            f"FROM derived_object_revisions WHERE {' AND '.join(clauses)} "
            "ORDER BY revision DESC LIMIT 1",
            params,
        ).fetchone()
        if not row:
            return None
        result = {
            "contract": REVISION_CONTRACT,
            "namespace": namespace,
            "logical_id": logical_id,
            "object_type": row[0],
            "revision": int(row[1]),
            "revision_id": row[2],
            "predecessor_revision_id": row[3],
            "content": _load(row[4], {}),
            "content_hash": row[5],
            "support": _load(row[6], []),
            "support_hash": row[7],
            "lifecycle": row[8],
            "change_kind": row[9],
            "producer": _load(row[10], {}),
            "configuration_hash": row[11],
            "generation": int(row[12]),
            "observed_at_ms": int(row[13]),
            "created_at_ms": int(row[14]),
        }
        return result if include_retracted or result["lifecycle"] == "active" else None

    def history(
        self, namespace: str, logical_id: str, *, include_retracted: bool = True
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision FROM derived_object_revisions r WHERE namespace=? AND logical_id=? "
            "AND EXISTS (SELECT 1 FROM derived_object_generations g WHERE g.namespace=r.namespace "
            "AND g.generation=r.generation AND g.status='committed') ORDER BY revision",
            [namespace, logical_id],
        ).fetchall()
        values = []
        for (number,) in rows:
            value = self.revision(
                namespace,
                logical_id,
                revision=int(number),
                include_retracted=include_retracted,
            )
            if value is not None:
                values.append(value)
        return values

    def delta(
        self,
        namespace: str,
        *,
        from_generation: int,
        to_generation: int,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if from_generation > to_generation:
            raise DerivedRevisionError(
                "reversed_range", "from_generation must not exceed to_generation"
            )
        commits = self.conn.execute(
            "SELECT generation,change_hash,item_count,counts_json FROM derived_object_generations "
            "WHERE namespace=? AND generation BETWEEN ? AND ? AND status='committed' ORDER BY generation",
            [namespace, from_generation, to_generation],
        ).fetchall()
        if (
            not commits
            or int(commits[0][0]) != from_generation
            or int(commits[-1][0]) != to_generation
        ):
            raise DerivedRevisionError(
                "generation_unavailable", "derived generation range is not committed"
            )
        decoded = _decode_cursor(cursor)
        expected = {
            "namespace": namespace,
            "from": from_generation,
            "to": to_generation,
        }
        if decoded and any(
            decoded.get(key) != value for key, value in expected.items()
        ):
            raise DerivedRevisionError(
                "invalid_cursor", "cursor belongs to another derived generation range"
            )
        offset, cap = int(decoded.get("offset", 0)), min(max(int(limit), 1), 1000)
        rows = self.conn.execute(
            "SELECT generation,ordinal,change_id,logical_id,object_type,change_kind,"
            "predecessor_revision_id,revision_id,reason_json,committed_at_ms "
            "FROM derived_object_generation_changes WHERE namespace=? AND generation BETWEEN ? AND ? "
            "ORDER BY generation,ordinal LIMIT ? OFFSET ?",
            [namespace, from_generation, to_generation, cap + 1, offset],
        ).fetchall()
        page, more = rows[:cap], len(rows) > cap
        changes = [
            {
                "generation": int(row[0]),
                "ordinal": int(row[1]),
                "change_id": row[2],
                "logical_id": row[3],
                "object_type": row[4],
                "change_kind": row[5],
                "predecessor_revision_id": row[6],
                "revision_id": row[7],
                "reason": _load(row[8], {}),
                "committed_at_ms": int(row[9]),
            }
            for row in page
        ]
        counts: Counter[str] = Counter()
        for row in commits:
            counts.update(_load(row[3], {}))
        return {
            "contract": DELTA_CONTRACT,
            "namespace": namespace,
            "from_generation": from_generation,
            "to_generation": to_generation,
            "changes": changes,
            "counts": dict(sorted(counts.items())),
            "item_count": sum(int(row[2]) for row in commits),
            "page_count": len(changes),
            "delta_hash": _digest([[int(row[0]), row[1]] for row in commits]),
            "next_cursor": _cursor({**expected, "offset": offset + cap})
            if more
            else None,
        }

    def replay(
        self, namespace: str, from_generation: int, to_generation: int
    ) -> dict[str, Any]:
        commits = self.conn.execute(
            "SELECT generation,change_hash FROM derived_object_generations "
            "WHERE namespace=? AND generation BETWEEN ? AND ? AND status='committed' ORDER BY generation",
            [namespace, from_generation, to_generation],
        ).fetchall()
        if (
            not commits
            or int(commits[0][0]) != from_generation
            or int(commits[-1][0]) != to_generation
        ):
            raise DerivedRevisionError(
                "generation_unavailable", "derived generation range is not committed"
            )
        mismatched: list[int] = []
        missing: list[str] = []
        for generation, expected_hash in commits:
            rows = self.conn.execute(
                "SELECT logical_id,object_type,change_kind,predecessor_revision_id,revision_id,"
                "reason_json,change_id FROM derived_object_generation_changes "
                "WHERE namespace=? AND generation=? ORDER BY ordinal",
                [namespace, generation],
            ).fetchall()
            materialized = [
                {
                    "logical_id": row[0],
                    "object_type": row[1],
                    "change_kind": row[2],
                    "predecessor_revision_id": row[3],
                    "revision_id": row[4],
                    "reason": _load(row[5], {}),
                    "change_id": row[6],
                }
                for row in rows
            ]
            if _digest(materialized) != expected_hash:
                mismatched.append(int(generation))
            for item in materialized:
                if not self.conn.execute(
                    "SELECT 1 FROM derived_object_revisions WHERE revision_id=?",
                    [item["revision_id"]],
                ).fetchone():
                    missing.append(item["revision_id"])
        return {
            "contract": REPLAY_CONTRACT,
            "namespace": namespace,
            "from_generation": from_generation,
            "to_generation": to_generation,
            "verified": not mismatched and not missing,
            "mismatched_generations": mismatched,
            "missing_revisions": sorted(missing),
            "delta_hash": _digest([[int(row[0]), row[1]] for row in commits]),
        }

    def lineage(self, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT namespace,logical_id,predecessor_revision_id,support_json,generation "
            "FROM derived_object_revisions r WHERE revision_id=? AND EXISTS "
            "(SELECT 1 FROM derived_object_generations g WHERE g.namespace=r.namespace "
            "AND g.generation=r.generation AND g.status='committed')",
            [revision_id],
        ).fetchone()
        if not row:
            raise DerivedRevisionError("not_found", "derived revision does not exist")
        downstream = [
            {"projection_kind": item[0], "item_id": item[1]}
            for item in self.conn.execute(
                "SELECT projection_kind,item_id FROM derived_projection_items "
                "WHERE object_revision_id=? ORDER BY projection_kind,item_id",
                [revision_id],
            ).fetchall()
        ]
        return {
            "contract": LINEAGE_CONTRACT,
            "revision_id": revision_id,
            "namespace": row[0],
            "logical_id": row[1],
            "predecessor_revision_id": row[2],
            "generation": int(row[4]),
            "sources": _load(row[3], []),
            "projections": downstream,
            "complete": True,
        }

    def explain_invalidation(
        self, namespace: str, logical_id: str, generation: int | None = None
    ) -> dict[str, Any]:
        clauses, params = ["namespace=?", "logical_id=?"], [namespace, logical_id]
        if generation is not None:
            clauses.append("generation<=?")
            params.append(int(generation))
        rows = self.conn.execute(
            "SELECT generation,change_id,change_kind,predecessor_revision_id,revision_id,reason_json "
            f"FROM derived_object_generation_changes WHERE {' AND '.join(clauses)} "
            "AND EXISTS (SELECT 1 FROM derived_object_generations g "
            "WHERE g.namespace=derived_object_generation_changes.namespace "
            "AND g.generation=derived_object_generation_changes.generation "
            "AND g.status='committed') "
            "ORDER BY generation DESC,ordinal DESC LIMIT 100",
            params,
        ).fetchall()
        return {
            "namespace": namespace,
            "logical_id": logical_id,
            "changes": [
                {
                    "generation": int(row[0]),
                    "change_id": row[1],
                    "change_kind": row[2],
                    "predecessor_revision_id": row[3],
                    "revision_id": row[4],
                    "reason": _load(row[5], {}),
                }
                for row in rows
            ],
            "side_effect_free": True,
        }

    def projection(self, namespace: str, projection_kind: str) -> list[dict[str, Any]]:
        if projection_kind not in PROJECTION_KINDS:
            raise DerivedRevisionError(
                "invalid_projection", "projection kind is unsupported"
            )
        rows = self.conn.execute(
            "WITH latest AS (SELECT r.logical_id,r.revision_id,r.lifecycle,r.generation,"
            "ROW_NUMBER() OVER (PARTITION BY r.logical_id ORDER BY r.revision DESC) AS ordinal "
            "FROM derived_object_revisions r JOIN derived_object_generations g "
            "ON g.namespace=r.namespace AND g.generation=r.generation AND g.status='committed' "
            "WHERE r.namespace=?) "
            "SELECT p.item_id,p.logical_id,p.object_type,p.object_revision_id,p.content_json,"
            "p.content_hash,p.generation FROM derived_projection_items p JOIN latest l "
            "ON l.revision_id=p.object_revision_id WHERE p.namespace=? AND p.projection_kind=? "
            "AND l.ordinal=1 AND l.lifecycle='active' ORDER BY p.item_id",
            [namespace, namespace, projection_kind],
        ).fetchall()
        return [
            {
                "item_id": row[0],
                "logical_id": row[1],
                "object_type": row[2],
                "object_revision_id": row[3],
                "content": _load(row[4], {}),
                "content_hash": row[5],
                "generation": int(row[6]),
            }
            for row in rows
        ]

    def health(self) -> dict[str, Any]:
        lifecycle_rows = self.conn.execute(
            "WITH latest AS (SELECT r.lifecycle,r.support_json,ROW_NUMBER() OVER "
            "(PARTITION BY r.namespace,r.logical_id ORDER BY r.revision DESC) AS ordinal "
            "FROM derived_object_revisions r JOIN derived_object_generations g "
            "ON g.namespace=r.namespace AND g.generation=r.generation AND g.status='committed') "
            "SELECT lifecycle,COUNT(*),SUM(json_array_length(support_json)) FROM latest "
            "WHERE ordinal=1 GROUP BY lifecycle ORDER BY lifecycle"
        ).fetchall()
        counts = {row[0]: int(row[1]) for row in lifecycle_rows}
        return {
            "objects": sum(counts.values()),
            "lifecycle": counts,
            "revisions": int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM derived_object_revisions r WHERE EXISTS "
                    "(SELECT 1 FROM derived_object_generations g WHERE g.namespace=r.namespace "
                    "AND g.generation=r.generation AND g.status='committed')"
                ).fetchone()[0]
            ),
            "supports": sum(int(row[2] or 0) for row in lifecycle_rows),
            "projections": {
                row[0]: int(row[1])
                for row in self.conn.execute(
                    "SELECT projection_kind,COUNT(*) FROM derived_projection_items p "
                    "JOIN derived_object_generations g ON g.namespace=p.namespace "
                    "AND g.generation=p.generation AND g.status='committed' "
                    "GROUP BY projection_kind ORDER BY projection_kind"
                ).fetchall()
            },
            "generations": int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM derived_object_generations WHERE status='committed'"
                ).fetchone()[0]
            ),
        }
