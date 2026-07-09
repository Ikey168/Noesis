"""
Relation extraction over the document corpus.

Entity extraction over the corpus already exists (``metadata.py`` →
``document_actors``); what was missing is *relations between* entities. This
extracts subject–relation–object triples from document text and persists them to
``document_relations``, so the KG / ``relationship_path`` layer has typed,
cited relations rather than only co-mention edges.

Heuristic-first, model-optional (as elsewhere): entities are found with the
shared spaCy-or-regex NER from :mod:`src.argument_mining.metadata`; a relation is
emitted when a known relation verb links two consecutive entities within one
sentence. spaCy improves entity recall when installed; the regex fallback keeps
it dependency-light and offline. Stable ``entity_id``\\s are reused from
``metadata`` so relations join to ``document_actors`` and the KG.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from src.argument_mining.metadata import _entity_id, _get_nlp, _valid_name
from src.database.news_articles_compat import corpus_table, ensure_corpus_documents_view

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z']+")
_TITLECASE_RE = re.compile(
    r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}|[A-Z]{2,}(?:\s+[A-Z]{2,})?)\b"
)

# Relation-bearing verbs (present + past). A relation is emitted only when one of
# these links two entities, which keeps precision high and avoids pairing every
# co-mention. Deliberately curated rather than open-vocabulary.
_RELATION_VERBS = frozenset({
    "met", "meets", "joined", "joins", "acquired", "acquires", "bought", "buys",
    "criticized", "criticised", "praised", "praises", "supported", "supports",
    "backed", "backs", "opposed", "opposes", "accused", "accuses", "sued", "sues",
    "led", "leads", "heads", "headed", "founded", "founds", "owns", "owned",
    "hired", "hires", "fired", "fires", "replaced", "replaces", "succeeded",
    "succeeds", "defeated", "defeats", "beat", "beats", "partnered", "partners",
    "attacked", "attacks", "visited", "visits", "endorsed", "endorses", "blamed",
    "blames", "urged", "urges", "warned", "warns", "thanked", "thanks",
    "appointed", "appoints", "elected", "elects", "named", "names", "married",
    "marries", "represents", "represented", "funded", "funds", "invested",
    "invests", "merged", "merges", "signed", "signs", "testified", "testifies",
    "questioned", "questions", "interviewed", "interviews", "challenged",
    "challenges", "met with", "spoke", "quoted", "cited",
})


@dataclass
class RelationRecord:
    document_id: str
    source_type: str
    subject: str
    subject_id: str
    relation: str
    object: str
    object_id: str
    sentence: str
    confidence: float
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


_RELATION_DDL = """
CREATE TABLE IF NOT EXISTS document_relations (
    document_id  TEXT,
    source_type  TEXT,
    subject      TEXT,
    subject_id   TEXT,
    relation     TEXT,
    object       TEXT,
    object_id    TEXT,
    sentence     TEXT,
    confidence   DOUBLE,
    extracted_at TEXT
)
"""

# A processed marker keyed by document, so a document that yields *zero*
# relations is still recorded as done and not re-scanned every pass.
_STATE_DDL = """
CREATE TABLE IF NOT EXISTS document_relations_state (
    document_id  TEXT PRIMARY KEY,
    extracted_at TEXT
)
"""


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENTENCE_RE.split((text or "").strip()) if s.strip()]


def _entity_spans(sentence: str) -> List[Tuple[str, int, int]]:
    """(name, start, end) entity spans in a sentence — spaCy when available,
    else a title-case regex fallback."""
    nlp = _get_nlp()
    if nlp is not None:
        try:
            doc = nlp(sentence[:20_000])
            spans = [
                (e.text.strip(), e.start_char, e.end_char)
                for e in doc.ents
                if e.label_ in ("PERSON", "ORG", "GPE", "FAC", "NORP")
                and len(e.text.strip()) >= 2
            ]
            if spans:
                return sorted(spans, key=lambda s: s[1])
        except Exception:
            pass
    return sorted(
        [(m.group(0), m.start(), m.end()) for m in _TITLECASE_RE.finditer(sentence)
         if _valid_name(m.group(0))],
        key=lambda s: s[1],
    )


def extract_relations(document_id: str, source_type: str, text: str) -> List[RelationRecord]:
    """Subject–relation–object triples: a known relation verb linking two
    consecutive entities within a sentence."""
    out: List[RelationRecord] = []
    seen = set()
    for sentence in _sentences(text):
        spans = _entity_spans(sentence)
        for (n1, _s1, e1), (n2, s2, _e2) in zip(spans, spans[1:]):
            sid, oid = _entity_id(n1), _entity_id(n2)
            if sid == oid:
                continue
            between = sentence[e1:s2].lower()
            tokens = _WORD_RE.findall(between)
            verb = next((t for t in tokens if t in _RELATION_VERBS), None)
            if verb is None:
                continue
            key = (sid, verb, oid)
            if key in seen:
                continue
            seen.add(key)
            confidence = round(max(0.3, 0.8 - 0.05 * len(tokens)), 3)
            out.append(RelationRecord(
                document_id=document_id, source_type=source_type,
                subject=n1, subject_id=sid, relation=verb, object=n2, object_id=oid,
                sentence=sentence[:240], confidence=confidence,
            ))
    return out


def store_relations(conn, records: List[RelationRecord]) -> None:
    conn.execute(_RELATION_DDL)
    for r in records:
        conn.execute(
            "INSERT INTO document_relations (document_id, source_type, subject, "
            "subject_id, relation, object, object_id, sentence, confidence, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [r.document_id, r.source_type, r.subject, r.subject_id, r.relation,
             r.object, r.object_id, r.sentence, r.confidence, r.extracted_at],
        )


def extract_document_relations(conn, limit: Optional[int] = None) -> dict:
    """Extract relations for corpus documents not yet processed; persist them.

    Sweeps ``corpus_documents`` for documents whose id is absent from
    ``document_relations``, extracts relations, and stores them. Returns the
    number of documents processed and relations found.
    """
    ensure_corpus_documents_view(conn)
    conn.execute(_RELATION_DDL)
    conn.execute(_STATE_DDL)

    query = (
        "SELECT d.id, d.source_type, d.title, d.content FROM corpus_documents d "
        "WHERE d.id NOT IN (SELECT document_id FROM document_relations_state) "
        "ORDER BY d.publish_date DESC NULLS LAST"
    )
    params: List[Any] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(query, params).fetchall()
    processed = relations = 0
    for document_id, source_type, title, content in rows:
        text = f"{title or ''}. {content or ''}".strip()
        records = extract_relations(document_id, source_type or "news", text)
        store_relations(conn, records)
        conn.execute(
            "INSERT INTO document_relations_state (document_id, extracted_at) VALUES (?, ?) "
            "ON CONFLICT (document_id) DO NOTHING",
            [document_id, now],
        )
        processed += 1
        relations += len(records)
    return {"documents_processed": processed, "relations_found": relations}


# --------------------------------------------------------------------------- #
# Read-only accessors (safe against a read-only warehouse connection)
# --------------------------------------------------------------------------- #

def _has_relations(conn) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'document_relations'"
        ).fetchone())
    except Exception:
        return False


def document_relations(conn, document_id: str) -> dict:
    """Relations extracted from one document, each cited to its source sentence."""
    if not _has_relations(conn):
        return {"document_id": document_id, "relations": [], "count": 0,
                "note": "no relations extracted yet"}
    rows = conn.execute(
        "SELECT subject, relation, object, confidence, sentence FROM document_relations "
        "WHERE document_id = ? ORDER BY confidence DESC",
        [document_id],
    ).fetchall()
    return {
        "document_id": document_id,
        "relations": [
            {"subject": r[0], "relation": r[1], "object": r[2],
             "confidence": r[3], "sentence": r[4]} for r in rows
        ],
        "count": len(rows),
    }


def entity_relations(conn, entity: str, limit: int = 100) -> dict:
    """Relations involving an entity (as subject or object), by name or entity_id."""
    if not _has_relations(conn):
        return {"entity": entity, "relations": [], "count": 0,
                "note": "no relations extracted yet"}
    eid = _entity_id(entity) if not entity.startswith("ent-") else entity
    rows = conn.execute(
        "SELECT subject, relation, object, document_id, confidence FROM document_relations "
        "WHERE subject_id = ? OR object_id = ? OR subject = ? OR object = ? "
        "ORDER BY confidence DESC LIMIT ?",
        [eid, eid, entity, entity, limit],
    ).fetchall()
    return {
        "entity": entity,
        "relations": [
            {"subject": r[0], "relation": r[1], "object": r[2],
             "document_id": r[3], "confidence": r[4]} for r in rows
        ],
        "count": len(rows),
    }
