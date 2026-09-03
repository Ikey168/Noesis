"""Knowledge graph stores.

Stores typed nodes and reified, provenance-bearing triples, enforcing the
ontology on every write (entity/relation types must exist; relations must be
permitted between the subject's and object's types). The interface is backend
agnostic. ``DuckDBKnowledgeGraphStore`` is the durable production foundation;
the in-memory implementation remains useful for isolated unit tests.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.knowledge_graph.foundation.model import Node, Provenance, Triple
from src.knowledge_graph.foundation.ontology import (
    EntityType,
    OntologyViolation,
    RelationType,
    validate_relation,
)


class KnowledgeGraphStore:
    """A typed, provenance-enforcing knowledge graph held in memory."""

    def __init__(self) -> None:
        self._nodes: Dict[str, Node] = {}
        # Triples keyed by fact identity; repeated assertions accumulate provenance.
        self._triples: Dict[tuple, Triple] = {}
        self._provenance: Dict[tuple, List[Provenance]] = {}

    # ---- nodes ---------------------------------------------------------- #

    def add_node(self, node: Node) -> Node:
        """Add or merge a node. Re-adding the same id merges aliases/properties."""
        existing = self._nodes.get(node.node_id)
        if existing is None:
            self._nodes[node.node_id] = node
            return node
        if existing.type != node.type:
            raise OntologyViolation(
                f"Node {node.node_id} already exists as {existing.type.value}, "
                f"cannot redefine as {node.type.value}"
            )
        for alias in node.aliases:
            if alias not in existing.aliases:
                existing.aliases.append(alias)
        existing.properties.update(node.properties)
        return existing

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def nodes_by_type(self, entity_type: EntityType) -> List[Node]:
        return [n for n in self._nodes.values() if n.type == entity_type]

    # ---- triples -------------------------------------------------------- #

    def add_triple(self, triple: Triple) -> Triple:
        """Validate against the ontology and store the triple as a cited fact.

        Both endpoints must already exist as nodes, and the relation must be
        permitted between their types. Re-asserting the same fact appends its
        provenance to the existing triple rather than duplicating it.
        """
        subject = self._nodes.get(triple.subject)
        obj = self._nodes.get(triple.object)
        if subject is None:
            raise OntologyViolation(f"Unknown subject node {triple.subject!r}; add it first")
        if obj is None:
            raise OntologyViolation(f"Unknown object node {triple.object!r}; add it first")

        validate_relation(triple.predicate, subject.type, obj.type)

        key = triple.key
        if key in self._triples:
            self._provenance[key].append(triple.provenance)
            # Keep the highest-confidence provenance as the representative one.
            best = max(self._provenance[key], key=lambda p: p.confidence)
            self._triples[key].provenance = best
            self._triples[key].properties.update(triple.properties)
            return self._triples[key]

        self._triples[key] = triple
        self._provenance[key] = [triple.provenance]
        return triple

    def provenance_for(self, triple: Triple) -> List[Provenance]:
        """All provenance records backing a fact (one per assertion)."""
        return list(self._provenance.get(triple.key, []))

    def triples(
        self,
        subject: Optional[str] = None,
        predicate: Optional[RelationType] = None,
        object: Optional[str] = None,
    ) -> List[Triple]:
        """Query stored facts, optionally filtered by any of subject/predicate/object."""
        results: Iterable[Triple] = self._triples.values()
        if subject is not None:
            results = [t for t in results if t.subject == subject]
        if predicate is not None:
            results = [t for t in results if t.predicate == predicate]
        if object is not None:
            results = [t for t in results if t.object == object]
        return list(results)

    def neighbors(self, node_id: str) -> List[Triple]:
        """All facts with ``node_id`` as subject or object."""
        return [t for t in self._triples.values() if node_id in (t.subject, t.object)]

    # ---- stats ---------------------------------------------------------- #

    def __len__(self) -> int:
        return len(self._triples)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def triple_count(self) -> int:
        return len(self._triples)


_DUCKDB_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_nodes (
    node_id     VARCHAR PRIMARY KEY,
    entity_type VARCHAR NOT NULL,
    name        VARCHAR NOT NULL,
    aliases     VARCHAR NOT NULL,
    properties  VARCHAR NOT NULL,
    updated_at  TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS kg_triples (
    subject     VARCHAR NOT NULL,
    predicate   VARCHAR NOT NULL,
    object      VARCHAR NOT NULL,
    properties  VARCHAR NOT NULL,
    source_doc  VARCHAR NOT NULL,
    confidence  DOUBLE NOT NULL,
    chunk_id    VARCHAR,
    extractor   VARCHAR,
    created_at  TIMESTAMP NOT NULL,
    updated_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (subject, predicate, object)
);
CREATE TABLE IF NOT EXISTS kg_provenance (
    provenance_id VARCHAR PRIMARY KEY,
    subject       VARCHAR NOT NULL,
    predicate     VARCHAR NOT NULL,
    object        VARCHAR NOT NULL,
    source_doc    VARCHAR NOT NULL,
    confidence    DOUBLE NOT NULL,
    chunk_id      VARCHAR,
    extractor     VARCHAR,
    asserted_at   TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS kg_mutation_events (
    event_id    VARCHAR PRIMARY KEY,
    kind        VARCHAR NOT NULL,
    entity_id   VARCHAR NOT NULL,
    label       VARCHAR,
    document_id VARCHAR,
    created_at  TIMESTAMP NOT NULL
);
"""


class DuckDBKnowledgeGraphStore(KnowledgeGraphStore):
    """Durable DuckDB implementation with the same query API as the memory store.

    Data is loaded into the typed object model at construction, while every
    mutation is synchronously upserted. A standalone MCP process can therefore
    open the warehouse read-only and see the same graph as the API writer.
    """

    def __init__(
        self,
        path: Optional[str | Path] = None,
        *,
        connection: Optional[Any] = None,
        read_only: bool = False,
    ) -> None:
        super().__init__()
        self._rw_lock = threading.RLock()
        self.read_only = bool(read_only)
        self.path = str(path) if path is not None else None
        self._owns_connection = connection is None
        if connection is None:
            import duckdb

            if path is None:
                raise ValueError("DuckDBKnowledgeGraphStore needs a path or connection")
            self.connection = duckdb.connect(str(path), read_only=read_only)
        else:
            self.connection = connection
        if not self.read_only:
            self.connection.execute(_DUCKDB_SCHEMA)
        self._load()

    @property
    def persistent(self) -> bool:
        return self.path not in (None, ":memory:")

    def _table_exists(self, name: str) -> bool:
        try:
            return bool(self.connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
            ).fetchone())
        except Exception:
            return False

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, default=str)

    def _load(self) -> None:
        if not self._table_exists("kg_nodes"):
            return
        for node_id, entity_type, name, aliases, properties in self.connection.execute(
            "SELECT node_id, entity_type, name, aliases, properties FROM kg_nodes"
        ).fetchall():
            node = Node(
                node_id=node_id, type=EntityType(entity_type), name=name,
                aliases=json.loads(aliases or "[]"),
                properties=json.loads(properties or "{}"),
            )
            self._nodes[node_id] = node
        if not self._table_exists("kg_triples"):
            return
        rows = self.connection.execute(
            """SELECT subject, predicate, object, properties, source_doc,
                      confidence, chunk_id, extractor FROM kg_triples"""
        ).fetchall()
        for subject, predicate, obj, properties, source_doc, confidence, chunk_id, extractor in rows:
            triple = Triple(
                subject=subject, predicate=RelationType(predicate), object=obj,
                properties=json.loads(properties or "{}"),
                provenance=Provenance(source_doc=source_doc, confidence=float(confidence),
                                      chunk_id=chunk_id, extractor=extractor),
            )
            self._triples[triple.key] = triple
            self._provenance[triple.key] = []
        if self._table_exists("kg_provenance"):
            for subject, predicate, obj, source_doc, confidence, chunk_id, extractor in self.connection.execute(
                """SELECT subject, predicate, object, source_doc, confidence,
                          chunk_id, extractor FROM kg_provenance ORDER BY asserted_at"""
            ).fetchall():
                key = (subject, predicate, obj)
                if key in self._provenance:
                    self._provenance[key].append(Provenance(
                        source_doc=source_doc, confidence=float(confidence),
                        chunk_id=chunk_id, extractor=extractor,
                    ))
        for key, triple in self._triples.items():
            if not self._provenance[key]:
                self._provenance[key] = [triple.provenance]

    def _require_write(self) -> None:
        if self.read_only:
            raise PermissionError("knowledge graph store is read-only")

    def add_node(self, node: Node) -> Node:
        self._require_write()
        with self._rw_lock:
            stored = super().add_node(node)
            self.connection.execute(
                """INSERT INTO kg_nodes
                   (node_id, entity_type, name, aliases, properties, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (node_id) DO UPDATE SET
                     entity_type=excluded.entity_type, name=excluded.name,
                     aliases=excluded.aliases, properties=excluded.properties,
                     updated_at=excluded.updated_at""",
                [stored.node_id, stored.type.value, stored.name,
                 self._json(stored.aliases), self._json(stored.properties),
                 datetime.now(timezone.utc)],
            )
            return stored

    @staticmethod
    def _provenance_id(key: tuple, provenance: Provenance) -> str:
        value = "|".join(map(str, (*key, provenance.source_doc,
                                   provenance.chunk_id, provenance.extractor,
                                   provenance.confidence)))
        return hashlib.sha256(value.encode()).hexdigest()

    def add_triple(self, triple: Triple) -> Triple:
        self._require_write()
        with self._rw_lock:
            stored = super().add_triple(triple)
            now = datetime.now(timezone.utc)
            p = stored.provenance
            self.connection.execute(
                """INSERT INTO kg_triples
                   (subject, predicate, object, properties, source_doc,
                    confidence, chunk_id, extractor, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (subject, predicate, object) DO UPDATE SET
                     properties=excluded.properties, source_doc=excluded.source_doc,
                     confidence=excluded.confidence, chunk_id=excluded.chunk_id,
                     extractor=excluded.extractor, updated_at=excluded.updated_at""",
                [stored.subject, stored.predicate.value, stored.object,
                 self._json(stored.properties), p.source_doc, p.confidence,
                 p.chunk_id, p.extractor, now, now],
            )
            asserted = triple.provenance
            self.connection.execute(
                """INSERT INTO kg_provenance
                   (provenance_id, subject, predicate, object, source_doc,
                    confidence, chunk_id, extractor, asserted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (provenance_id) DO NOTHING""",
                [self._provenance_id(triple.key, asserted), triple.subject,
                 triple.predicate.value, triple.object, asserted.source_doc,
                 asserted.confidence, asserted.chunk_id, asserted.extractor, now],
            )
            return stored

    def record_event(self, kind: str, entity_id: str, label: str,
                     document_id: str, created_at: Optional[datetime] = None) -> None:
        self._require_write()
        self.connection.execute(
            "INSERT INTO kg_mutation_events VALUES (?, ?, ?, ?, ?, ?)",
            [str(uuid.uuid4()), kind, entity_id, label, document_id,
             created_at or datetime.now(timezone.utc)],
        )

    def recent_events(self, since: datetime, *, kind: Optional[str] = None,
                      limit: int = 50) -> List[Dict[str, Any]]:
        if not self._table_exists("kg_mutation_events"):
            return []
        clause = " AND kind = ?" if kind else ""
        params: List[Any] = [since]
        if kind:
            params.append(kind)
        params.append(limit)
        rows = self.connection.execute(
            f"""SELECT kind, entity_id, label, document_id, created_at
                FROM kg_mutation_events WHERE created_at >= ?{clause}
                ORDER BY created_at DESC LIMIT ?""", params
        ).fetchall()
        return [{"kind": row[0], "entity_id": row[1], "label": row[2],
                 "doc_id": row[3], "ts": row[4]} for row in reversed(rows)]

    def event_counts(self) -> Dict[str, int]:
        if not self._table_exists("kg_mutation_events"):
            return {"total": 0, "node": 0, "triple": 0}
        counts = dict(self.connection.execute(
            "SELECT kind, COUNT(*) FROM kg_mutation_events GROUP BY kind"
        ).fetchall())
        return {"total": sum(counts.values()), "node": counts.get("node", 0),
                "triple": counts.get("triple", 0)}

    def sync(self) -> None:
        """Persist direct object mutations made by reviewed corrections."""
        self._require_write()
        with self._rw_lock:
            self.connection.execute("BEGIN TRANSACTION")
            try:
                self.connection.execute("DELETE FROM kg_provenance")
                self.connection.execute("DELETE FROM kg_triples")
                self.connection.execute("DELETE FROM kg_nodes")
                now = datetime.now(timezone.utc)
                for node in self._nodes.values():
                    self.connection.execute(
                        "INSERT INTO kg_nodes VALUES (?, ?, ?, ?, ?, ?)",
                        [node.node_id, node.type.value, node.name,
                         self._json(node.aliases), self._json(node.properties), now],
                    )
                for triple in self._triples.values():
                    p = triple.provenance
                    self.connection.execute(
                        "INSERT INTO kg_triples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [triple.subject, triple.predicate.value, triple.object,
                         self._json(triple.properties), p.source_doc, p.confidence,
                         p.chunk_id, p.extractor, now, now],
                    )
                    for provenance in self._provenance.get(triple.key, [p]):
                        self.connection.execute(
                            "INSERT INTO kg_provenance VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT (provenance_id) DO NOTHING",
                            [self._provenance_id(triple.key, provenance), triple.subject,
                             triple.predicate.value, triple.object,
                             provenance.source_doc, provenance.confidence,
                             provenance.chunk_id, provenance.extractor, now],
                        )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()
