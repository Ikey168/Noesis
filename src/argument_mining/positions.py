"""
Policy Position Extraction Pipeline (issue #110).

Identifies sentences where a named actor asserts an intention or commitment on
a policy topic, then returns structured (actor, topic, position_text, date,
document_id, source_type) tuples.

Works across all six Noesis source types using the configured claim model:
  - news / blog  — named-actor commitment sentences, editorial positions
  - paper        — author recommendations and policy-implication statements
  - transcript   — speaker-attributed commitments
  - book         — thesis/position statements
  - note / upload — meeting-minutes commitments and action items

Positions are stored in the ``policy_positions`` DuckDB table and can be
retrieved via ``GET /api/v1/arguments/positions``.

Usage:
    from src.argument_mining.positions import extract_positions, run_position_pipeline
    records = extract_positions(document)                  # pure extraction
    records = run_position_pipeline(document, conn)        # extract + persist
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from services.ingest.common.document_model import Document
from src.argument_mining.dataset import sentences_from_document

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Verbs that signal a policy commitment or stated intention
_COMMITMENT_PATTERN = re.compile(
    r"\b("
    r"will\s+\w+|"
    r"plans?\s+to|"
    r"intends?\s+to|"
    r"aims?\s+to|"
    r"seeks?\s+to|"
    r"committed?\s+to|"
    r"pledged?|"
    r"promised?|"
    r"vowed?|"
    r"proposed?|"
    r"announced?|"
    r"urges?|"
    r"calls?\s+for|"
    r"calls?\s+on|"
    r"demands?|"
    r"requires?|"
    r"mandates?|"
    r"recommends?|"
    r"advocates?|"
    r"supports?|"
    r"opposes?|"
    r"endorses?|"
    r"backs?\s+(?:the|a|an)|"
    r"rejects?|"
    r"vetoes?|"
    r"blocks?"
    r")\b",
    re.IGNORECASE,
)

# Patterns for extracting the actor from a sentence — tried in order.
# Each pattern must have exactly one capture group (the actor string).
_ACTOR_PATTERNS: List[re.Pattern] = [
    # "SPEAKER: text" — transcript all-caps or title-case speaker label
    re.compile(r"^([A-Z][A-Z\s]{2,30}):"),
    re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4}):\s"),
    # "President/Prime Minister/CEO ... Name said/pledged/..." (Title Case names only)
    re.compile(
        r"\b(?:President|Prime\s+Minister|Minister|Secretary(?:\s+of\s+State)?|"
        r"Governor|Senator|Chancellor|Commissioner|General|Admiral|Director|CEO|"
        r"Chair(?:man|woman|person)?|Representative|Ambassador|Mayor|Premier)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b",
    ),
    # "Name, the Title, said/announced/..."
    re.compile(
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+),\s+(?:the\s+)?[a-z]+"
        r"(?:\s+[a-z]+)?,\s+(?:said|announced|stated|pledged|promised|vowed)",
    ),
    # "Name said/announced/pledged/promised/vowed/..."
    re.compile(
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s+"
        r"(?:said|stated|announced|pledged|promised|vowed|committed|proposed|urged|warned)",
    ),
    # "The CFO/board/committee committed/resolved/plans..."
    re.compile(
        r"((?:The\s+)?(?:CFO|CTO|COO|CRO|board|committee|cabinet|council|"
        r"panel|task\s+force|working\s+group|executive\s+team|leadership\s+team))"
        r"(?:\s+|,\s+)(?:committed|resolved|pledged|vowed|plans|will\b|announced|agreed|decided)",
        re.IGNORECASE,
    ),
    # "The government/administration/party/ministry will..."
    re.compile(
        r"((?:The\s+)?(?:government|administration|ruling\s+party|opposition|ministry|"
        r"department|authority|agency|regulator|union|alliance|party|"
        r"senate|congress|parliament|court|treasury|central\s+bank))"
        r"\s+(?:will\b|has\s+pledged|announced|said|plans|committed|vowed)",
        re.IGNORECASE,
    ),
]

# Topic keyword taxonomy — each entry: (topic_label, keyword_set)
_TOPIC_TAXONOMY: List[Tuple[str, frozenset]] = [
    ("healthcare", frozenset({
        "health", "medical", "hospital", "vaccine", "drug", "medicine",
        "patient", "nhs", "medicare", "medicaid", "pharmaceutical", "treatment",
        "disease", "cancer", "mental health", "pandemic", "public health",
    })),
    ("economy", frozenset({
        "economy", "economic", "inflation", "gdp", "unemployment", "tax",
        "budget", "fiscal", "monetary", "trade", "deficit", "debt", "growth",
        "recession", "interest rate", "central bank", "finance", "market",
        "currency", "wage", "pension", "subsidy", "tariff",
    })),
    ("environment", frozenset({
        "climate", "environment", "carbon", "emission", "renewable", "energy",
        "fossil fuel", "net zero", "biodiversity", "deforestation", "pollution",
        "green", "solar", "wind", "nuclear", "sustainability",
    })),
    ("security", frozenset({
        "military", "defence", "defense", "security", "army", "navy", "weapon",
        "nato", "border", "terrorism", "cyberattack", "intelligence", "war",
        "nuclear", "missile", "sanction", "troops",
    })),
    ("law", frozenset({
        "law", "legal", "court", "legislation", "regulation", "bill", "act",
        "rights", "constitution", "crime", "justice", "police", "prison",
        "penalty", "compliance", "enforcement", "verdict",
    })),
    ("politics", frozenset({
        "election", "vote", "party", "government", "minister", "parliament",
        "senate", "congress", "president", "prime minister", "democracy",
        "reform", "policy", "political", "campaign", "referendum",
    })),
    ("social", frozenset({
        "inequality", "poverty", "housing", "education", "school", "university",
        "welfare", "child", "family", "immigration", "refugee", "discrimination",
        "gender", "race", "ethnicity", "labour", "worker", "union",
    })),
    ("technology", frozenset({
        "technology", "tech", "ai", "artificial intelligence", "data",
        "digital", "software", "internet", "cyber", "privacy", "algorithm",
        "robot", "automation", "semiconductor", "platform",
    })),
    ("business", frozenset({
        "company", "corporation", "ceo", "shareholder", "merger", "acquisition",
        "profit", "revenue", "market share", "competition", "antitrust",
        "startup", "investment", "ipo", "stock",
    })),
]

# Minimum claim-detector confidence to treat a sentence as position-bearing
_MIN_CONFIDENCE = 0.45

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class PositionRecord:
    position_id: str
    document_id: str
    source_type: str
    actor: str
    topic: str
    position_text: str
    position_date: Optional[str]
    confidence: float
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_position_bearing(
    sentence: str,
    min_confidence: float,
    claim_detector=None,
) -> Tuple[bool, float]:
    """Return (is_position, confidence) using the claim model and commitment pattern.

    Questions are never position statements even when they contain commitment verbs.
    The model's negative-class confidence is inverted to obtain a claim score before
    applying the commitment boost.
    """
    if sentence.strip().endswith("?"):
        return False, 0.0

    from src.argument_mining.models import get_claim_detector

    detector = claim_detector or get_claim_detector()
    claim_pred = detector.predict_text(sentence)
    raw_score = claim_pred.confidence if claim_pred.is_claim else (1.0 - claim_pred.confidence)
    has_commitment = bool(_COMMITMENT_PATTERN.search(sentence))
    adjusted = min(0.95, raw_score + 0.15) if has_commitment else raw_score
    is_pos = adjusted >= min_confidence and (claim_pred.is_claim or has_commitment)
    return is_pos, adjusted


def _extract_actor(sentence: str, document: Document) -> str:
    """Extract the most likely named actor from a sentence.

    Tries regex patterns in order; falls back to document-level metadata.
    """
    for pattern in _ACTOR_PATTERNS:
        m = pattern.search(sentence)
        if m:
            actor = m.group(1).strip().rstrip(",.:;")
            if 2 < len(actor) < 80:
                return _normalise_actor(actor)

    # Fallback 1: first author
    if document.authors:
        return document.authors[0]
    # Fallback 2: source_id (often the news outlet name)
    if document.source_id:
        return document.source_id
    # Fallback 3: source_type label
    return document.source_type


def _normalise_actor(raw: str) -> str:
    """Collapse excess whitespace and title-case the actor name."""
    return re.sub(r"\s+", " ", raw).strip()


def _infer_topic(document: Document, sentence: str) -> str:
    """Infer the policy topic for a sentence.

    Priority:
      1. `metadata.topics` from the Document (set by upstream enrichment)
      2. Document category field via metadata
      3. Keyword match against _TOPIC_TAXONOMY on the sentence + title
    """
    # 1. Upstream topic enrichment
    topics_meta: list = document.metadata.get("topics", [])
    if topics_meta:
        raw = topics_meta[0].lower() if topics_meta else ""
        for label, keywords in _TOPIC_TAXONOMY:
            if label in raw or any(kw in raw for kw in keywords):
                return label
        return raw[:60] or "general"

    # 2. Document-level category
    category = (document.metadata.get("category") or "").lower()
    if category:
        for label, _ in _TOPIC_TAXONOMY:
            if label in category:
                return label

    # 3. Keyword scan of sentence + title
    combined = ((document.title or "") + " " + sentence).lower()
    best_label = "general"
    best_count = 0
    for label, keywords in _TOPIC_TAXONOMY:
        hits = sum(1 for kw in keywords if kw in combined)
        if hits > best_count:
            best_count = hits
            best_label = label
    return best_label


def _document_date(document: Document) -> Optional[str]:
    """Extract a human-readable date string from the Document."""
    ts_ms = document.created_at or document.ingested_at
    if not ts_ms:
        return None
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        return dt.date().isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def _position_id(document_id: str, sentence: str, actor: str) -> str:
    """Deterministic UUID-style ID from (doc, sentence, actor)."""
    h = hashlib.sha1(f"{document_id}|{actor}|{sentence}".encode()).hexdigest()[:32]
    return f"pos-{h}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_positions(document: Document, claim_detector=None) -> List[PositionRecord]:
    """Extract policy positions from any Document.

    Returns a list of PositionRecord instances — one per distinct
    (actor, topic, position_text) triple found in the document.
    The list is empty when no position-bearing sentences are detected.

    No DB connection is required; this function is pure extraction.
    """
    sentences = sentences_from_document(document)
    if not sentences:
        return []

    pub_date = _document_date(document)
    records: List[PositionRecord] = []
    seen_ids: set = set()

    for sent in sentences:
        is_pos, confidence = _is_position_bearing(
            sent, _MIN_CONFIDENCE, claim_detector
        )
        if not is_pos:
            continue

        actor = _extract_actor(sent, document)
        topic = _infer_topic(document, sent)
        pid = _position_id(document.document_id, sent, actor)

        if pid in seen_ids:
            continue
        seen_ids.add(pid)

        records.append(PositionRecord(
            position_id=pid,
            document_id=document.document_id,
            source_type=document.source_type,
            actor=actor,
            topic=topic,
            position_text=sent,
            position_date=pub_date,
            confidence=round(confidence, 4),
        ))

    logger.debug(
        "extract_positions: doc=%s source=%s sentences=%d positions=%d",
        document.document_id,
        document.source_type,
        len(sentences),
        len(records),
    )
    return records


def store_positions(records: List[PositionRecord], conn) -> None:
    """Persist PositionRecord list to the policy_positions DuckDB table.

    Uses INSERT OR REPLACE semantics keyed on position_id so re-running
    the pipeline on the same document is safe (idempotent).
    """
    if not records:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO policy_positions
            (position_id, document_id, source_type, actor, topic,
             position_text, position_date, confidence, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r.position_id,
                r.document_id,
                r.source_type,
                r.actor,
                r.topic,
                r.position_text,
                r.position_date,
                r.confidence,
                r.extracted_at,
            )
            for r in records
        ],
    )


def run_position_pipeline(document: Document, conn, claim_detector=None) -> List[PositionRecord]:
    """Extract positions and persist them to the DB in one call.

    Designed to be called as a post-ingestion stage after
    ``src.argument_mining.evidence.run_pipeline``.

    Args:
        document: any Document (news/blog/paper/transcript/book/note).
        conn:     DuckDB connection; ``policy_positions`` must already exist
                  (created by ``ensure_schema`` in local_warehouse_seed).

    Returns the list of PositionRecord objects persisted.
    """
    records = extract_positions(document, claim_detector)
    store_positions(records, conn)
    logger.info(
        "run_position_pipeline: doc=%s source=%s positions_stored=%d",
        document.document_id,
        document.source_type,
        len(records),
    )
    return records
