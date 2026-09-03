"""
User-driven entity corrections — Issue #44.

Trusted users (PREMIUM+) can submit corrections to knowledge-graph entities.
Admins review, approve (applying the change to the live store), or reject.

Every action is version-stamped per entity so the full history is auditable.

Correction types:
  rename           change the entity's canonical display name
  add_alias        add a surface-form alias
  remove_alias     remove a surface-form alias
  add_property     set/update an arbitrary property key
  remove_property  delete a property key
  merge            absorb another entity's triples into this one

The review queue is process-local, while every approved graph mutation is
synchronously committed by the durable store from ``kg_updater.py``.
"""
from __future__ import annotations

import threading
import uuid
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

class CorrectionType(str, Enum):
    RENAME = "rename"
    ADD_ALIAS = "add_alias"
    REMOVE_ALIAS = "remove_alias"
    ADD_PROPERTY = "add_property"
    REMOVE_PROPERTY = "remove_property"
    MERGE = "merge"


class CorrectionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# Payload shapes per correction type:
#   rename:           {"new_name": str}
#   add_alias:        {"alias": str}
#   remove_alias:     {"alias": str}
#   add_property:     {"key": str, "value": Any}
#   remove_property:  {"key": str}
#   merge:            {"merge_from": str}   # entity_id to absorb


@dataclass
class EntityCorrection:
    """One proposed change to a KG entity."""

    correction_id: str
    entity_id: str
    correction_type: CorrectionType
    payload: Dict[str, Any]
    reason: str
    submitted_by: str
    submitted_at: datetime
    version: int                         # monotonic per entity
    status: CorrectionStatus = CorrectionStatus.PENDING
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "correction_id": self.correction_id,
            "entity_id": self.entity_id,
            "correction_type": self.correction_type.value,
            "payload": self.payload,
            "reason": self.reason,
            "submitted_by": self.submitted_by,
            "submitted_at": self.submitted_at.isoformat(),
            "version": self.version,
            "status": self.status.value,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "review_note": self.review_note,
        }


# ---------------------------------------------------------------------------
# Singleton correction store
# ---------------------------------------------------------------------------

_CORRECTION_DDL = """
CREATE TABLE IF NOT EXISTS kg_entity_corrections (
    correction_id   VARCHAR PRIMARY KEY,
    entity_id       VARCHAR NOT NULL,
    correction_type VARCHAR NOT NULL,
    payload         VARCHAR NOT NULL,
    reason          VARCHAR NOT NULL,
    submitted_by    VARCHAR NOT NULL,
    submitted_at    TIMESTAMP NOT NULL,
    version         INTEGER NOT NULL,
    status          VARCHAR NOT NULL,
    reviewed_by     VARCHAR,
    reviewed_at     TIMESTAMP,
    review_note     VARCHAR
)
"""


class EntityCorrectionStore:
    """Thread-safe correction queue, durable when backed by the KG warehouse."""

    def __init__(self, connection=None, *, read_only: bool = False) -> None:
        self._lock = threading.Lock()
        self._connection = connection
        self._read_only = read_only
        # correction_id -> EntityCorrection
        self._corrections: Dict[str, EntityCorrection] = {}
        # entity_id -> monotonic version counter
        self._entity_version: Dict[str, int] = defaultdict(int)
        if connection is not None:
            if not read_only:
                connection.execute(_CORRECTION_DDL)
            self._load()

    def _load(self) -> None:
        try:
            rows = self._connection.execute(
                """SELECT correction_id, entity_id, correction_type, payload,
                          reason, submitted_by, submitted_at, version, status,
                          reviewed_by, reviewed_at, review_note
                   FROM kg_entity_corrections"""
            ).fetchall()
        except Exception:
            return
        for row in rows:
            correction = EntityCorrection(
                correction_id=row[0], entity_id=row[1],
                correction_type=CorrectionType(row[2]), payload=json.loads(row[3]),
                reason=row[4], submitted_by=row[5], submitted_at=row[6],
                version=int(row[7]), status=CorrectionStatus(row[8]),
                reviewed_by=row[9], reviewed_at=row[10], review_note=row[11],
            )
            self._corrections[correction.correction_id] = correction
            self._entity_version[correction.entity_id] = max(
                self._entity_version[correction.entity_id], correction.version
            )

    def _persist(self, correction: EntityCorrection) -> None:
        if self._connection is None:
            return
        if self._read_only:
            raise PermissionError("entity correction store is read-only")
        self._connection.execute(
            """INSERT INTO kg_entity_corrections
               (correction_id, entity_id, correction_type, payload, reason,
                submitted_by, submitted_at, version, status, reviewed_by,
                reviewed_at, review_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (correction_id) DO UPDATE SET
                 status=excluded.status, reviewed_by=excluded.reviewed_by,
                 reviewed_at=excluded.reviewed_at, review_note=excluded.review_note,
                 payload=excluded.payload""",
            [correction.correction_id, correction.entity_id,
             correction.correction_type.value, json.dumps(correction.payload, sort_keys=True),
             correction.reason, correction.submitted_by, correction.submitted_at,
             correction.version, correction.status.value, correction.reviewed_by,
             correction.reviewed_at, correction.review_note],
        )

    # ---- submission --------------------------------------------------------

    def submit(
        self,
        entity_id: str,
        correction_type: CorrectionType,
        payload: Dict[str, Any],
        reason: str,
        submitted_by: str,
    ) -> EntityCorrection:
        """Create a new pending correction and return it."""
        _validate_payload(correction_type, payload)

        with self._lock:
            self._entity_version[entity_id] += 1
            version = self._entity_version[entity_id]
            correction = EntityCorrection(
                correction_id=str(uuid.uuid4()),
                entity_id=entity_id,
                correction_type=correction_type,
                payload=dict(payload),
                reason=reason,
                submitted_by=submitted_by,
                submitted_at=datetime.now(timezone.utc),
                version=version,
            )
            self._corrections[correction.correction_id] = correction
            self._persist(correction)
        return correction

    # ---- review ------------------------------------------------------------

    def approve(
        self,
        correction_id: str,
        reviewed_by: str,
        review_note: Optional[str] = None,
    ) -> EntityCorrection:
        """Mark a correction approved and apply it to the live KG store."""
        with self._lock:
            c = self._get_or_raise(correction_id)
            if c.status != CorrectionStatus.PENDING:
                raise ValueError(
                    f"Correction {correction_id!r} is already {c.status.value}"
                )
            _apply_correction(c)
            c.status = CorrectionStatus.APPROVED
            c.reviewed_by = reviewed_by
            c.reviewed_at = datetime.now(timezone.utc)
            c.review_note = review_note
            self._persist(c)
        return c

    def reject(
        self,
        correction_id: str,
        reviewed_by: str,
        review_note: Optional[str] = None,
    ) -> EntityCorrection:
        """Mark a correction rejected (no KG change)."""
        with self._lock:
            c = self._get_or_raise(correction_id)
            if c.status != CorrectionStatus.PENDING:
                raise ValueError(
                    f"Correction {correction_id!r} is already {c.status.value}"
                )
            c.status = CorrectionStatus.REJECTED
            c.reviewed_by = reviewed_by
            c.reviewed_at = datetime.now(timezone.utc)
            c.review_note = review_note
            self._persist(c)
        return c

    # ---- queries -----------------------------------------------------------

    def list_corrections(
        self,
        entity_id: Optional[str] = None,
        status: Optional[CorrectionStatus] = None,
        limit: int = 50,
    ) -> List[EntityCorrection]:
        with self._lock:
            results = list(self._corrections.values())
        if entity_id is not None:
            results = [c for c in results if c.entity_id == entity_id]
        if status is not None:
            results = [c for c in results if c.status == status]
        results.sort(key=lambda c: c.submitted_at, reverse=True)
        return results[:limit]

    def get(self, correction_id: str) -> Optional[EntityCorrection]:
        with self._lock:
            return self._corrections.get(correction_id)

    # ---- internal ----------------------------------------------------------

    def _get_or_raise(self, correction_id: str) -> EntityCorrection:
        c = self._corrections.get(correction_id)
        if c is None:
            raise KeyError(f"Correction {correction_id!r} not found")
        return c


# ---------------------------------------------------------------------------
# Process-level singleton
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_correction_store: Optional[EntityCorrectionStore] = None


def get_correction_store() -> EntityCorrectionStore:
    global _correction_store
    with _store_lock:
        if _correction_store is None:
            from src.knowledge_graph.kg_updater import _shared_store

            graph = _shared_store()
            _correction_store = EntityCorrectionStore(
                connection=getattr(graph, "connection", None)
            )
    return _correction_store


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

def _validate_payload(correction_type: CorrectionType, payload: Dict[str, Any]) -> None:
    required: Dict[CorrectionType, List[str]] = {
        CorrectionType.RENAME: ["new_name"],
        CorrectionType.ADD_ALIAS: ["alias"],
        CorrectionType.REMOVE_ALIAS: ["alias"],
        CorrectionType.ADD_PROPERTY: ["key", "value"],
        CorrectionType.REMOVE_PROPERTY: ["key"],
        CorrectionType.MERGE: ["merge_from"],
    }
    missing = [k for k in required[correction_type] if k not in payload]
    if missing:
        raise ValueError(
            f"Correction type {correction_type.value!r} requires payload keys: "
            f"{required[correction_type]}; missing: {missing}"
        )


# ---------------------------------------------------------------------------
# Apply an approved correction to the live KG store
# ---------------------------------------------------------------------------

def _apply_correction(correction: EntityCorrection) -> None:
    """
    Mutate the live KnowledgeGraphStore to reflect an approved correction.

    Raises ``KeyError`` if the target entity doesn't exist in the store.
    This is intentional: approving a correction for a non-existent entity is a
    data error that the admin should see rather than silently ignore.
    """
    from src.knowledge_graph.kg_updater import _shared_store

    store = _shared_store()
    entity_id = correction.entity_id
    ct = correction.correction_type
    payload = correction.payload

    # All types except MERGE need the target node to exist
    if ct != CorrectionType.MERGE:
        node = store.get_node(entity_id)
        if node is None:
            raise KeyError(f"Entity {entity_id!r} not found in the knowledge graph")

    if ct == CorrectionType.RENAME:
        node = store.get_node(entity_id)
        node.name = payload["new_name"]

    elif ct == CorrectionType.ADD_ALIAS:
        node = store.get_node(entity_id)
        alias = payload["alias"]
        if alias not in node.aliases:
            node.aliases.append(alias)

    elif ct == CorrectionType.REMOVE_ALIAS:
        node = store.get_node(entity_id)
        alias = payload["alias"]
        if alias in node.aliases:
            node.aliases.remove(alias)

    elif ct == CorrectionType.ADD_PROPERTY:
        node = store.get_node(entity_id)
        node.properties[payload["key"]] = payload["value"]

    elif ct == CorrectionType.REMOVE_PROPERTY:
        node = store.get_node(entity_id)
        node.properties.pop(payload["key"], None)

    elif ct == CorrectionType.MERGE:
        merge_from_id = payload["merge_from"]
        _apply_merge(store, target_id=entity_id, source_id=merge_from_id)

    # Corrections often mutate Node objects directly. Flush those mutations so
    # rename/alias/property/merge decisions survive process restarts.
    sync = getattr(store, "sync", None)
    if callable(sync):
        sync()


def _apply_merge(store: Any, target_id: str, source_id: str) -> None:
    """
    Absorb ``source_id`` into ``target_id``:
      1. Copy source's aliases and properties onto the target.
      2. Rewrite every triple that references source_id to target_id.
      3. Remove the source node and its triples from the store.
    """
    target = store.get_node(target_id)
    source = store.get_node(source_id)
    if target is None:
        raise KeyError(f"Merge target {target_id!r} not found")
    if source is None:
        raise KeyError(f"Merge source {source_id!r} not found")

    # Merge aliases (dedup)
    for alias in source.aliases + [source.name]:
        if alias not in target.aliases and alias != target.name:
            target.aliases.append(alias)

    # Merge properties (target wins on conflict)
    for k, v in source.properties.items():
        target.properties.setdefault(k, v)

    # Rewrite triples; collect keys to delete
    to_delete = []
    to_add = []
    for key, triple in list(store._triples.items()):
        if triple.subject == source_id or triple.object == source_id:
            to_delete.append(key)
            from src.knowledge_graph.foundation.model import Triple
            new_subj = target_id if triple.subject == source_id else triple.subject
            new_obj = target_id if triple.object == source_id else triple.object
            # Skip self-loops that the merge would create
            if new_subj == new_obj:
                continue
            new_triple = Triple(
                subject=new_subj,
                predicate=triple.predicate,
                object=new_obj,
                provenance=triple.provenance,
                properties=dict(triple.properties),
            )
            to_add.append(new_triple)

    for key in to_delete:
        store._triples.pop(key, None)
        store._provenance.pop(key, None)

    for triple in to_add:
        try:
            store.add_triple(triple)
        except Exception:
            pass  # ontology violation after rewrite — skip

    # Remove source node
    store._nodes.pop(source_id, None)
