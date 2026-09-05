"""
Knowledge graph live-update service — Issue #42.

When a document is ingested (POST /documents/ingest) this service runs as a
FastAPI background task and:

1. Extracts entity mentions from the document text via lightweight heuristic
   NER (no external deps required; always available).
2. Upserts canonical nodes and DOCUMENT→entity MENTIONS triples into a
   process-level KnowledgeGraphStore shared across all background tasks.
3. Records wall-clock timestamps on every mutation so callers can query
   "what connections emerged in the last N minutes?" and "which topics are
   accumulating connections right now?".
"""
from __future__ import annotations

import logging
import os
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.knowledge_graph.foundation import (
    EntityResolver,
    EntityType,
    DuckDBKnowledgeGraphStore,
    KnowledgeGraphStore,
    Node,
    Provenance,
    RelationType,
    Triple,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process-level singleton (shared KG store + resolver)
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_store: Optional[KnowledgeGraphStore] = None
_resolver: Optional[EntityResolver] = None


def _shared_store() -> KnowledgeGraphStore:
    global _store, _resolver
    with _store_lock:
        if _store is None:
            from src.config.env import warehouse_path

            path = warehouse_path()
            assert path is not None
            from pathlib import Path
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            _store = DuckDBKnowledgeGraphStore(path)
            _resolver = EntityResolver(decision_sink=_resolution_history_sink(_store.connection))
            _resolver.seed(list(_store._nodes.values()))
    return _store


def _shared_resolver() -> EntityResolver:
    _shared_store()  # ensures both are initialised
    assert _resolver is not None
    return _resolver


def _resolution_history_sink(connection):
    """Retain machine proposals and source mentions in the reversible history ledger."""
    import hashlib
    import json
    from src.kb.entity_history import EntityHistoryStore, WRITE_SCOPE, REVIEW_SCOPE

    history = EntityHistoryStore(connection)
    scopes = [WRITE_SCOPE, REVIEW_SCOPE]

    def record(event):
        subjects = sorted(set([event["entity_id"], *event["candidate_ids"]]))
        for entity_id in subjects:
            history.register_entity("graph", entity_id, principal_id="entity-resolver", scopes=scopes)
        key = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        history.decide(
            "graph", "review", subjects,
            {**event, "producer": {"name": event["producer"]}},
            reviewer_id="machine:entity-resolver", principal_id="entity-resolver",
            scopes=scopes, event_key="resolution:" + key,
        )

    return record


# ---------------------------------------------------------------------------
# Mutation event log (provides the time dimension for "emerging" queries)
# ---------------------------------------------------------------------------

@dataclass
class _MutationEvent:
    kind: str        # "node" | "triple"
    entity_id: str   # node_id  –or–  "subject_id:predicate:object_id"
    label: str       # human-readable description
    doc_id: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_events: List[_MutationEvent] = []
_events_lock = threading.Lock()


def _record(kind: str, entity_id: str, label: str, doc_id: str, store=None) -> None:
    event = _MutationEvent(kind, entity_id, label, doc_id)
    with _events_lock:
        _events.append(event)
    store = store if store is not None else _shared_store()
    if isinstance(store, DuckDBKnowledgeGraphStore):
        store.record_event(kind, entity_id, label, doc_id, event.ts)


# ---------------------------------------------------------------------------
# Lightweight heuristic NER (no spaCy / transformers required)
# ---------------------------------------------------------------------------

# Capitalised-word sequences of 1–4 tokens as candidate named entities.
_CAP_SEQ = re.compile(r"\b([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,}){0,3})\b")

_ORG_SUFFIXES = frozenset([
    "Inc", "Corp", "Ltd", "LLC", "Company", "Group", "Institute",
    "Association", "Foundation", "Ministry", "Department", "University",
    "College", "Bank", "Fund", "Agency", "Bureau", "Committee", "Organisation",
    "Organization",
])

_PERSON_TITLES = frozenset([
    "Mr", "Ms", "Mrs", "Dr", "Prof", "President", "CEO", "CTO", "CFO",
    "Senator", "Representative", "Minister", "Director", "Secretary", "General",
])

# Stop-words that match the capitalised pattern but are not entities.
_STOP = frozenset([
    "The", "A", "An", "In", "On", "At", "By", "For", "With", "From",
    "And", "Or", "But", "To", "Of", "As", "Is", "Are", "Was", "Were",
    "This", "That", "These", "Those", "It", "He", "She", "We", "They",
    "His", "Her", "Their", "Its", "Our", "Your", "My",
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
])


def _infer_type(tokens: List[str], preceding_word: str) -> EntityType:
    last = tokens[-1] if tokens else ""
    first = tokens[0] if tokens else ""
    if first in _PERSON_TITLES or preceding_word.rstrip(".") in _PERSON_TITLES:
        return EntityType.PERSON
    if last in _ORG_SUFFIXES or any(t in _ORG_SUFFIXES for t in tokens):
        return EntityType.ORGANIZATION
    if len(tokens) == 2:
        # "Firstname Lastname" shape → person
        return EntityType.PERSON
    return EntityType.CONCEPT


def _extract_mentions(text: str) -> List[tuple]:
    """Return ``(surface_form, EntityType)`` pairs from plain text."""
    text = text or ""
    words = text.split()

    # Build preceding-word index for type inference
    preceding: Dict[str, str] = {}
    for i in range(1, len(words)):
        surface = words[i].rstrip(".,;:\"'")
        preceding.setdefault(surface, words[i - 1].rstrip(".,;:\"'"))

    seen: set = set()
    results = []
    for m in _CAP_SEQ.finditer(text):
        name = m.group(1).strip()
        tokens = name.split()
        if len(name) < 3:
            continue
        if tokens[0] in _STOP or name in _STOP:
            continue
        if name in seen:
            continue
        seen.add(name)
        etype = _infer_type(tokens, preceding.get(tokens[0], ""))
        results.append((name, etype))
    return results


# ---------------------------------------------------------------------------
# Core update logic
# ---------------------------------------------------------------------------

def update_from_document(doc: Dict[str, Any], *, store=None, resolver=None, strict=False) -> None:
    """
    Extract entities from *doc* and update the shared KnowledgeGraphStore.

    Called as a FastAPI ``BackgroundTasks`` callback after the HTTP response
    for ``POST /documents/ingest`` has already been sent.

    *doc* must contain ``document_id`` and at least one of ``title`` / ``content``.
    """
    doc_id = doc.get("document_id", "unknown")
    text = " ".join(filter(None, [doc.get("title", ""), doc.get("content", "")]))

    if not text.strip():
        logger.debug("KG updater: no text content for doc=%r, skipping", doc_id)
        return

    store = store if store is not None else _shared_store()
    if resolver is None:
        resolver = EntityResolver(decision_sink=_resolution_history_sink(store.connection)) if isinstance(store, DuckDBKnowledgeGraphStore) else EntityResolver()
        resolver.seed(list(store._nodes.values()))

    # 1. Anchor document node
    doc_node = Node(type=EntityType.DOCUMENT, name=doc_id)
    try:
        store.add_node(doc_node)
        _record("node", doc_node.node_id, doc_node.name, doc_id, store)
    except Exception:
        if strict:
            raise
        # Node already exists — fine, we still want to add new entity triples
        pass

    # 2. Extract entities and upsert into the store
    entity_node_ids: List[str] = []
    for name, etype in _extract_mentions(text):
        try:
            # EntityResolver handles deduplication; returns the canonical Node
            canonical = resolver.resolve(etype, name, provenance={"source_doc": doc_id})
            # Add to store (add_node merges if it already exists)
            stored = store.add_node(canonical)
            entity_node_ids.append(stored.node_id)
            _record("node", stored.node_id, stored.name, doc_id, store)

            # Feed the warehouse's canonical-entity layer from this same write
            # path. Downstream dossiers and graph tools now share surfaces.
            if isinstance(store, DuckDBKnowledgeGraphStore):
                from src.kb.entities import ensure_entity_schema

                ensure_entity_schema(store.connection)
                store.connection.execute(
                    """INSERT INTO document_actors
                       (document_id, source_type, actor_name, entity_id, role,
                        confidence, extracted_at)
                       VALUES (?, ?, ?, ?, 'mention', 0.8, ?)
                       ON CONFLICT (document_id, actor_name, role) DO UPDATE SET
                         entity_id=excluded.entity_id,
                         confidence=excluded.confidence,
                         extracted_at=excluded.extracted_at""",
                    [doc_id, doc.get("source_type") or "note", name,
                     stored.node_id, datetime.now(timezone.utc).isoformat()],
                )
        except Exception as exc:
            if strict:
                raise
            logger.debug("KG node upsert skipped for %r: %s", name, exc)

    # 3. Link document → each entity with MENTIONS triples
    new_triples = 0
    for eid in entity_node_ids:
        try:
            triple = Triple(
                subject=doc_node.node_id,
                predicate=RelationType.MENTIONS,
                object=eid,
                provenance=Provenance(
                    source_doc=doc_id,
                    confidence=0.8,
                    extractor="heuristic-ner",
                ),
            )
            store.add_triple(triple)
            # Use "|" as separator; node_ids contain ":" so we can't use that.
            key_str = "|".join([triple.subject, triple.predicate.value, triple.object])
            _record("triple", key_str, f"MENTIONS:{eid}", doc_id, store)
            new_triples += 1
        except Exception as exc:
            if strict:
                raise
            logger.debug("KG triple skipped: %s", exc)

    logger.info(
        "KG background update complete: doc=%r entities=%d triples=%d",
        doc_id, len(entity_node_ids), new_triples,
    )
    if isinstance(store, DuckDBKnowledgeGraphStore):
        try:
            from src.kb.entities import run_entity_canonicalization_pass
            run_entity_canonicalization_pass(store.connection)
        except Exception as exc:
            if strict:
                raise
            logger.debug("canonical entity pass skipped for %s: %s", doc_id, exc)


# ---------------------------------------------------------------------------
# Query helpers consumed by kg_stream_routes
# ---------------------------------------------------------------------------

def get_emerging_connections(since: datetime, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Return triples added to the KG after *since*.

    Each result describes one new edge: subject entity, predicate, object
    entity, which source document triggered it, and when it was added.
    """
    store = _shared_store()
    if isinstance(store, DuckDBKnowledgeGraphStore):
        recent = [
            _MutationEvent(row["kind"], row["entity_id"], row["label"],
                           row["doc_id"], row["ts"])
            for row in store.recent_events(since, kind="triple", limit=limit)
        ]
    else:
        with _events_lock:
            recent = [e for e in _events if e.kind == "triple" and e.ts >= since][-limit:]
    results = []
    for ev in recent:
        parts = ev.entity_id.split("|", 2)
        subj_id = parts[0] if len(parts) > 0 else ""
        pred = parts[1] if len(parts) > 1 else "MENTIONS"
        obj_id = parts[2] if len(parts) > 2 else ""

        entry: Dict[str, Any] = {
            "subject_id": subj_id,
            "predicate": pred,
            "object_id": obj_id,
            "source_doc": ev.doc_id,
            "added_at": ev.ts.isoformat(),
        }

        subj_node = store.get_node(subj_id)
        obj_node = store.get_node(obj_id)
        if subj_node:
            entry["subject_name"] = subj_node.name
            entry["subject_type"] = subj_node.type.value
        if obj_node:
            entry["object_name"] = obj_node.name
            entry["object_type"] = obj_node.type.value

        results.append(entry)
    return results


def get_evolving_topics(window_seconds: int = 3600, top_n: int = 20) -> List[Dict[str, Any]]:
    """
    Return entities ranked by how many new MENTIONS triples they received in
    the last *window_seconds* seconds.

    High counts signal a topic that is rapidly accumulating new documents —
    i.e. an evolving/emerging topic.
    """
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - window_seconds

    store = _shared_store()
    since = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    if isinstance(store, DuckDBKnowledgeGraphStore):
        recent = [
            _MutationEvent(row["kind"], row["entity_id"], row["label"],
                           row["doc_id"], row["ts"])
            for row in store.recent_events(since, limit=10000)
        ]
    else:
        with _events_lock:
            recent = [e for e in _events if e.ts.timestamp() >= cutoff]

    counts: Dict[str, int] = defaultdict(int)
    docs_per_entity: Dict[str, set] = defaultdict(set)

    for ev in recent:
        if ev.kind != "triple":
            continue
        parts = ev.entity_id.split("|", 2)
        # The object of a MENTIONS triple is the entity being mentioned
        obj_id = parts[2] if len(parts) > 2 else ""
        if obj_id:
            counts[obj_id] += 1
            docs_per_entity[obj_id].add(ev.doc_id)

    results = []
    for node_id, count in sorted(counts.items(), key=lambda x: -x[1])[:top_n]:
        node = store.get_node(node_id)
        if node is None:
            continue
        results.append({
            "entity_id": node_id,
            "name": node.name,
            "type": node.type.value,
            "new_connections": count,
            "source_docs": sorted(docs_per_entity[node_id]),
            "window_seconds": window_seconds,
        })
    return results


def get_store_stats() -> Dict[str, Any]:
    """Return current statistics for the shared KnowledgeGraphStore."""
    store = _shared_store()
    if isinstance(store, DuckDBKnowledgeGraphStore):
        counts = store.event_counts()
        total_events = counts["total"]
        triple_events = counts["triple"]
        node_events = counts["node"]
    else:
        with _events_lock:
            total_events = len(_events)
            triple_events = sum(1 for e in _events if e.kind == "triple")
            node_events = sum(1 for e in _events if e.kind == "node")

    return {
        "node_count": store.node_count,
        "triple_count": store.triple_count,
        "total_update_events": total_events,
        "triple_events": triple_events,
        "node_events": node_events,
        "status": "persisted" if isinstance(store, DuckDBKnowledgeGraphStore) else "memory",
        "read_only": getattr(store, "read_only", False),
        "database": getattr(store, "path", None),
    }
