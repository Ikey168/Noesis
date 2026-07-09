"""
Actor & source metadata extractor — issue #114.

Extracts structured actor metadata (quoted speakers, attributed institutions,
authors, publishers) from any Document regardless of source_type.

spaCy ``en_core_web_sm`` is used for NER when installed; the module falls back
to regex heuristics so no ML dependency is required for basic operation.

Content-type specifics
----------------------
news / blog   — document.authors (byline), document.source_id (outlet),
                PERSON/ORG entities from body text
paper         — document.authors, APA-cited institutions, bracketed references
transcript    — speaker labels from document.metadata["segments"] or "speakers",
                PERSON NER on body text for named interviewees
book          — document.authors, document.metadata["publisher"],
                dialogue speaker labels ("SPEAKER: " prefix)
note / upload — document.metadata["creator"] or document.authors

Entity linking
--------------
Actors are linked to a stable entity_id derived from their canonical form:
``ent-<sha1[:12] of lower-stripped name>``.  This is consistent with the
``_extract_entities`` + ``_aggregate_entities`` approach in
``local_warehouse_views``, so actor records can be joined to the entity graph
without a separate entity table.

Public API
----------
  extract_actors(document) -> List[ActorRecord]
  store_actors(records, conn) -> None
  run_actor_batch(conn, lock, limit?) -> {"processed": int, "actors": int, "skipped": int}
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Sequence, Tuple

from src.database.news_articles_compat import corpus_table

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled patterns (heuristic fallback path)
# ---------------------------------------------------------------------------

# News "said/told/wrote" attribution — captures the speaker before the verb
_SAID_RE = re.compile(
    r"((?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})|(?:[A-Z]{2,}))"
    r"\s+(?:said|told|wrote|stated|confirmed|noted|argued|added|explained"
    r"|revealed|disclosed|warned|insisted|acknowledged|conceded|announced)",
    re.M,
)
# Quoted speech intro: "…," Name said
_QUOTE_SAID_RE = re.compile(
    r'["“].{5,120}["”]\s*,?\s+'
    r"((?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}))\s+"
    r"(?:said|told|wrote|noted|added|explained)",
    re.M,
)
# Transcript speaker label "Speaker Name: text"
_TRANSCRIPT_SPEAKER_RE = re.compile(
    r"^((?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})|(?:[A-Z]{2,}(?:\s+[A-Z]{2,})?))\s*:",
    re.M,
)
# APA citation institution (after "at X" or "from X" inside parenthetical)
_PAPER_INSTITUTION_RE = re.compile(
    r"\b(?:at|from|of)\s+((?:[A-Z][a-z]+(?:\s+(?:of|and|for|the|University|Institute|College|School|Center|Centre|Lab(?:oratory)?|Department|Hospital)\b)?)*[A-Z][a-z]+)",
)
# Dialogue speaker in books "NAME: " or "Name said"
_BOOK_SPEAKER_RE = re.compile(
    r"^([A-Z][A-Z\s]{1,25}):\s",  # ALL-CAPS label
    re.M,
)
# Org-like multi-word title-case sequences (fallback)
_ORG_CAPS_RE = re.compile(
    r"(?:^|\s)((?:[A-Z][a-z]{1,20}\s){1,4}(?:Inc|Corp|Ltd|LLC|LLP|Group|Bank|Fund|Agency|Commission|Committee|Department|Ministry|Authority|Association|Institute|Foundation|University|College|Hospital|Center|Centre|Lab|WHO|IMF|EU|UN|NATO|FBI|CIA|SEC|ECB|Fed)\b)",
    re.M,
)

# Names to skip (sentence starters, filler)
_SKIP_NAMES = frozenset({
    "the", "a", "an", "this", "that", "it", "they", "he", "she", "we", "you",
    "i", "my", "his", "her", "its", "our", "their", "as", "but", "and", "or",
    "so", "yet", "for", "nor", "the", "said", "told", "wrote",
})


# ---------------------------------------------------------------------------
# spaCy NER helper (optional)
# ---------------------------------------------------------------------------

_nlp = None
_nlp_tried = False


def _get_nlp():
    global _nlp, _nlp_tried
    if _nlp_tried:
        return _nlp
    _nlp_tried = True
    try:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
        logger.info("metadata: spaCy en_core_web_sm loaded")
    except Exception as e:
        logger.debug("metadata: spaCy not available (%s) — using regex fallback", e)
        _nlp = None
    return _nlp


def _ner_actors(text: str) -> List[Tuple[str, str]]:
    """Return (name, spaCy_label) pairs for PERSON and ORG entities."""
    nlp = _get_nlp()
    if nlp is None or not text:
        return []
    try:
        doc = nlp(text[:50_000])  # cap to avoid timeouts on large docs
        return [
            (ent.text.strip(), ent.label_)
            for ent in doc.ents
            if ent.label_ in ("PERSON", "ORG", "GPE", "FAC", "NORP")
            and len(ent.text.strip()) >= 2
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Entity-ID derivation
# ---------------------------------------------------------------------------

def _entity_id(name: str) -> str:
    """Stable, reproducible entity ID derived from the canonical name."""
    canonical = re.sub(r"\s+", " ", name.strip().lower())
    return "ent-" + hashlib.sha1(canonical.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# ActorRecord
# ---------------------------------------------------------------------------

@dataclass
class ActorRecord:
    document_id: str
    source_type: str
    actor_name: str
    entity_id: str
    role: str        # "speaker" | "subject" | "author"
    confidence: float
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @classmethod
    def make(cls, document_id: str, source_type: str,
             name: str, role: str, confidence: float) -> "ActorRecord":
        return cls(
            document_id=document_id,
            source_type=source_type,
            actor_name=name,
            entity_id=_entity_id(name),
            role=role,
            confidence=confidence,
        )


# ---------------------------------------------------------------------------
# Per-type extractors
# ---------------------------------------------------------------------------

def _valid_name(name: str) -> bool:
    name = name.strip()
    if not name or len(name) < 2:
        return False
    if name.lower() in _SKIP_NAMES:
        return False
    words = name.split()
    # Must start with a capital or be an acronym
    if not (words[0][0].isupper() or words[0].isupper()):
        return False
    return True


def _from_authors(document_id: str, source_type: str,
                  authors: Sequence[str], role: str = "author") -> List[ActorRecord]:
    return [
        ActorRecord.make(document_id, source_type, a.strip(), role, 0.95)
        for a in authors
        if a and a.strip() and _valid_name(a.strip())
    ]


def _regex_speakers_from_text(document_id: str, source_type: str,
                               text: str) -> List[ActorRecord]:
    seen: Dict[str, ActorRecord] = {}
    for pat, role, conf in [
        (_SAID_RE, "speaker", 0.72),
        (_QUOTE_SAID_RE, "speaker", 0.78),
    ]:
        for m in pat.finditer(text):
            name = m.group(1).strip()
            if _valid_name(name) and name not in seen:
                seen[name] = ActorRecord.make(document_id, source_type, name, role, conf)
    return list(seen.values())


def _spacy_subjects(document_id: str, source_type: str, text: str,
                    role: str = "subject") -> List[ActorRecord]:
    pairs = _ner_actors(text)
    seen: Dict[str, ActorRecord] = {}
    for name, label in pairs:
        if not _valid_name(name) or name in seen:
            continue
        r = "speaker" if label == "PERSON" else role
        conf = 0.82 if label == "PERSON" else 0.75
        seen[name] = ActorRecord.make(document_id, source_type, name, r, conf)
    return list(seen.values())


def _extract_news_blog(doc) -> List[ActorRecord]:
    did, stype = doc.document_id, doc.source_type
    out: List[ActorRecord] = []

    # Byline / authors
    out.extend(_from_authors(did, stype, doc.authors or [], role="author"))

    # Outlet from source_id
    if doc.source_id and _valid_name(doc.source_id):
        out.append(ActorRecord.make(did, stype, doc.source_id, "subject", 0.90))

    body = (doc.content or "")[:20_000]

    # spaCy NER first, fall back to regex
    if _get_nlp():
        out.extend(_spacy_subjects(did, stype, body, role="subject"))
    else:
        out.extend(_regex_speakers_from_text(did, stype, body))
        for m in _ORG_CAPS_RE.finditer(body):
            name = m.group(1).strip()
            if _valid_name(name):
                out.append(ActorRecord.make(did, stype, name, "subject", 0.65))

    return out


def _extract_paper(doc) -> List[ActorRecord]:
    did, stype = doc.document_id, doc.source_type
    out: List[ActorRecord] = []
    out.extend(_from_authors(did, stype, doc.authors or [], role="author"))

    body = (doc.content or "")[:20_000]

    if _get_nlp():
        out.extend(_spacy_subjects(did, stype, body, role="subject"))
    else:
        for m in _PAPER_INSTITUTION_RE.finditer(body):
            name = m.group(1).strip()
            if _valid_name(name):
                out.append(ActorRecord.make(did, stype, name, "subject", 0.65))

    # Publisher from metadata
    publisher = (doc.metadata or {}).get("publisher") or (doc.metadata or {}).get("journal")
    if publisher and _valid_name(str(publisher)):
        out.append(ActorRecord.make(did, stype, str(publisher), "subject", 0.88))

    return out


def _extract_transcript(doc) -> List[ActorRecord]:
    did, stype = doc.document_id, doc.source_type
    out: List[ActorRecord] = []
    out.extend(_from_authors(did, stype, doc.authors or [], role="author"))

    # Speaker segments from metadata (Whisper output or manual diarization)
    meta = doc.metadata or {}
    speakers_seen: set = set()

    for key in ("segments", "diarization"):
        for seg in meta.get(key, []):
            sp = seg.get("speaker") or seg.get("label") or ""
            if sp and _valid_name(sp) and sp not in speakers_seen:
                speakers_seen.add(sp)
                out.append(ActorRecord.make(did, stype, sp, "speaker", 0.90))

    for sp in meta.get("speakers", []):
        if isinstance(sp, dict):
            name = sp.get("name") or sp.get("label") or ""
        else:
            name = str(sp)
        if name and _valid_name(name) and name not in speakers_seen:
            speakers_seen.add(name)
            out.append(ActorRecord.make(did, stype, name, "speaker", 0.90))

    # Body-text speaker labels ("Name: …")
    body = (doc.content or "")[:20_000]
    for m in _TRANSCRIPT_SPEAKER_RE.finditer(body):
        name = m.group(1).strip()
        if _valid_name(name) and name not in speakers_seen:
            speakers_seen.add(name)
            out.append(ActorRecord.make(did, stype, name, "speaker", 0.80))

    # Named interviewees from NER
    if _get_nlp():
        for rec in _spacy_subjects(did, stype, body, role="subject"):
            out.append(rec)

    return out


def _extract_book(doc) -> List[ActorRecord]:
    did, stype = doc.document_id, doc.source_type
    out: List[ActorRecord] = []
    out.extend(_from_authors(did, stype, doc.authors or [], role="author"))

    meta = doc.metadata or {}
    publisher = meta.get("publisher")
    if publisher and _valid_name(str(publisher)):
        out.append(ActorRecord.make(did, stype, str(publisher), "subject", 0.88))

    body = (doc.content or "")[:20_000]

    # ALL-CAPS dialogue speakers
    seen: set = set()
    for m in _BOOK_SPEAKER_RE.finditer(body):
        name = m.group(1).strip().title()
        if _valid_name(name) and name not in seen:
            seen.add(name)
            out.append(ActorRecord.make(did, stype, name, "speaker", 0.75))

    if _get_nlp():
        out.extend(_spacy_subjects(did, stype, body, role="subject"))

    return out


def _extract_note_upload(doc) -> List[ActorRecord]:
    did, stype = doc.document_id, doc.source_type
    out: List[ActorRecord] = []
    out.extend(_from_authors(did, stype, doc.authors or [], role="author"))

    meta = doc.metadata or {}
    for key in ("creator", "uploader", "owner", "uploaded_by"):
        val = meta.get(key)
        if val and _valid_name(str(val)):
            out.append(ActorRecord.make(did, stype, str(val), "author", 0.92))
            break

    body = (doc.content or "")[:10_000]
    if _get_nlp():
        out.extend(_spacy_subjects(did, stype, body, role="subject"))

    return out


# ---------------------------------------------------------------------------
# Dedup helper
# ---------------------------------------------------------------------------

def _dedup(records: List[ActorRecord]) -> List[ActorRecord]:
    """Keep highest-confidence record per (actor_name, role) pair."""
    best: Dict[Tuple[str, str], ActorRecord] = {}
    for r in records:
        key = (r.actor_name.lower(), r.role)
        if key not in best or r.confidence > best[key].confidence:
            best[key] = r
    return list(best.values())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_EXTRACTORS = {
    "news":       _extract_news_blog,
    "blog":       _extract_news_blog,
    "paper":      _extract_paper,
    "transcript": _extract_transcript,
    "book":       _extract_book,
    "note":       _extract_note_upload,
    "web":        _extract_news_blog,
}


def extract_actors(document) -> List[ActorRecord]:
    """
    Extract actor/source metadata from *document* (a Document instance).

    Returns a deduplicated list of ActorRecord with roles:
      ``author``  — byline, creator, uploader, publisher
      ``speaker`` — quoted speaker, transcript label, dialogue character
      ``subject`` — named organization or institution mentioned as an actor
    """
    fn = _EXTRACTORS.get(document.source_type, _extract_news_blog)
    records = fn(document)
    return _dedup(records)


def store_actors(records: List[ActorRecord], conn) -> None:
    """Persist actor records to ``document_actors`` (INSERT OR REPLACE)."""
    for r in records:
        conn.execute(
            """
            INSERT OR REPLACE INTO document_actors
                (document_id, source_type, actor_name, entity_id,
                 role, confidence, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [r.document_id, r.source_type, r.actor_name, r.entity_id,
             r.role, r.confidence, r.extracted_at],
        )


def run_actor_batch(conn, lock, limit: int = 200) -> dict:
    """
    Extract actors for corpus documents not yet in ``document_actors``.

    Sweeps the whole corpus (every source type, via ``corpus_table``) for
    documents whose ``id`` is absent from ``document_actors``, extracts actors,
    and persists them.  Safe to call repeatedly.
    """
    from services.ingest.common.document_model import Document  # type: ignore[import]

    try:
        with lock:
            rows = conn.execute(
                f"""
                SELECT id, title, content, source, category
                FROM {corpus_table(conn)}
                WHERE id NOT IN (SELECT DISTINCT document_id FROM document_actors)
                LIMIT ?
                """,
                [limit],
            ).fetchall()
    except Exception as e:
        return {"error": str(e), "processed": 0, "actors": 0, "skipped": 0}

    processed = actors_total = skipped = 0
    import time

    for doc_id, title, content, source, _category in rows:
        try:
            doc = Document(
                document_id=doc_id,
                source_type="news",
                language="en",
                ingested_at=int(time.time() * 1000),
                source_id=source,
                title=title,
                content=content or "",
            )
            records = extract_actors(doc)
            if records:
                with lock:
                    store_actors(records, conn)
            actors_total += len(records)
            processed += 1
        except Exception:
            skipped += 1

    return {"processed": processed, "actors": actors_total, "skipped": skipped}
