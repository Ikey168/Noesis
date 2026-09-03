"""
Claim & Evidence Extraction Pipeline.

Two-stage pipeline:
  1. extract_claims(document)   — run ClaimDetector over any Document,
                                   return ClaimRecord list
  2. find_evidence(claim, corpus) — TF-IDF cosine search over a sentence
                                    corpus; classify each match as
                                    "supports" or "contradicts"

run_pipeline(document, conn) wires both stages and persists results to
the DuckDB tables argument_claims / claim_evidence.

Claim detection requires either a fine-tuned checkpoint or the pinned
pretrained weights. Evidence retrieval uses sklearn TF-IDF + cosine.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from services.ingest.common.document_model import Document
from src.argument_mining.models import get_claim_detector
from src.database.news_articles_compat import corpus_table

logger = logging.getLogger(__name__)

# Cap on the default retrieval corpus. Was a hard-coded 500 (news-only); raised
# and named so a corpus with papers/books/blogs is not truncated to an
# unrepresentative slice. Override via NOESIS_EVIDENCE_CORPUS_LIMIT.
_DEFAULT_CORPUS_LIMIT = 5000

# Minimum cosine similarity to consider a sentence as evidence
SIMILARITY_THRESHOLD = 0.20
# Maximum evidence sentences returned per claim
MAX_EVIDENCE = 10

# ---------------------------------------------------------------------------
# Contradiction signals
# ---------------------------------------------------------------------------

_CONTRADICTION_SIGNALS = frozenset({
    "not", "no", "never", "neither", "nor",
    "didn't", "doesn't", "don't", "won't", "wasn't", "weren't", "hasn't",
    "haven't", "wouldn't", "couldn't", "shouldn't", "cannot", "can't",
    "refute", "refutes", "refuted", "dispute", "disputes", "disputed",
    "contradict", "contradicts", "contradicted",
    "deny", "denies", "denied", "reject", "rejects", "rejected",
    "challenge", "challenges", "challenged", "debunk", "debunks", "debunked",
    "false", "incorrect", "wrong", "inaccurate", "misleading", "untrue",
    "contrary", "despite", "however", "though", "although",
    "but", "yet", "while", "whereas", "nevertheless",
})

_CONTRADICTION_THRESHOLD = 2  # signals needed to flip to "contradicts"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ClaimRecord:
    claim_id: str
    claim_text: str
    document_id: str
    source_type: str
    confidence: float
    prediction_mode: str = "unknown"
    extracted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    attributed: Optional[bool] = None
    attribution_text: Optional[str] = None


@dataclass
class EvidenceRecord:
    evidence_id: str
    claim_id: str
    evidence_text: str
    evidence_document_id: str
    evidence_source_type: str
    relation: str   # "supports" | "contradicts"
    similarity_score: float
    found_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# Corpus entry: (sentence_text, document_id, source_type)
CorpusEntry = Tuple[str, str, str]


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def _claim_id(document_id: str, sentence_idx: int) -> str:
    raw = f"{document_id}::{sentence_idx}"
    return "cl-" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def _evidence_id(claim_id: str, evidence_document_id: str, evidence_text: str) -> str:
    raw = f"{claim_id}::{evidence_document_id}::{evidence_text[:64]}"
    return "ev-" + hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Similarity + relation detection
# ---------------------------------------------------------------------------

def _contradiction_score(text: str) -> int:
    """Count contradiction signals present in a sentence."""
    words = set(re.findall(r"\b\w+\b", text.lower()))
    return len(words & _CONTRADICTION_SIGNALS)


def _cosine_similarities(claim_text: str, corpus_texts: List[str]) -> List[float]:
    """TF-IDF cosine similarity between claim and each corpus sentence."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity as sk_cosine
    except ImportError:
        logger.warning("sklearn not available — evidence search disabled")
        return [0.0] * len(corpus_texts)

    if not corpus_texts:
        return []

    all_texts = [claim_text] + corpus_texts
    try:
        vec = TfidfVectorizer(min_df=1, ngram_range=(1, 2), sublinear_tf=True)
        tfidf = vec.fit_transform(all_texts)
        sims = sk_cosine(tfidf[0:1], tfidf[1:]).flatten()
        return sims.tolist()
    except Exception as exc:
        logger.warning("TF-IDF similarity failed: %s", exc)
        return [0.0] * len(corpus_texts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_claims(document: Document) -> List[ClaimRecord]:
    """Run ClaimDetector over `document`; return only sentences classified as claims."""
    from src.argument_mining.attribution import classify_attribution  # lazy — avoids circular import

    detector = get_claim_detector()
    predictions = detector.predict(document)
    records = []
    for pred in predictions:
        if not pred.is_claim:
            continue
        attributed, attribution_text = classify_attribution(pred.text, document.source_type)
        records.append(ClaimRecord(
            claim_id=_claim_id(document.document_id, pred.sentence_idx),
            claim_text=pred.text,
            document_id=document.document_id,
            source_type=document.source_type,
            confidence=pred.confidence,
            prediction_mode=detector.prediction_mode,
            attributed=attributed,
            attribution_text=attribution_text,
        ))
    return records


def find_evidence(
    claim: ClaimRecord,
    corpus: List[CorpusEntry],
    threshold: float = SIMILARITY_THRESHOLD,
    max_results: int = MAX_EVIDENCE,
) -> List[EvidenceRecord]:
    """Search `corpus` for sentences that support or contradict `claim`.

    Args:
        claim:      the claim to search evidence for
        corpus:     list of (sentence_text, document_id, source_type) tuples;
                    sentences from the same document as the claim are skipped
        threshold:  minimum cosine similarity to qualify as evidence
        max_results: cap on number of evidence records returned
    """
    # Exclude same-document sentences to avoid circular evidence
    candidates = [e for e in corpus if e[1] != claim.document_id]
    if not candidates:
        return []

    texts = [e[0] for e in candidates]
    scores = _cosine_similarities(claim.claim_text, texts)

    # Collect matches above threshold, sorted by descending score
    indexed = sorted(
        ((s, i) for i, s in enumerate(scores) if s >= threshold),
        key=lambda x: -x[0],
    )

    evidence: List[EvidenceRecord] = []
    seen_doc_ids: set = set()
    for score, idx in indexed[:max_results]:
        ev_text, ev_doc_id, ev_source_type = candidates[idx]
        # Deduplicate same document_id (keep highest-score sentence only)
        if ev_doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(ev_doc_id)

        cscore = _contradiction_score(ev_text)
        relation = "contradicts" if cscore >= _CONTRADICTION_THRESHOLD else "supports"

        evidence.append(EvidenceRecord(
            evidence_id=_evidence_id(claim.claim_id, ev_doc_id, ev_text),
            claim_id=claim.claim_id,
            evidence_text=ev_text,
            evidence_document_id=ev_doc_id,
            evidence_source_type=ev_source_type,
            relation=relation,
            similarity_score=round(score, 6),
        ))
    return evidence


# ---------------------------------------------------------------------------
# DuckDB persistence
# ---------------------------------------------------------------------------

def store_claim(record: ClaimRecord, conn) -> None:
    # Callers may hand us a warehouse created before prediction provenance was
    # added.  Keep the write path backward-compatible instead of requiring an
    # operator to discover and run a separate migration first.
    conn.execute(
        "ALTER TABLE argument_claims "
        "ADD COLUMN IF NOT EXISTS prediction_mode VARCHAR"
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO argument_claims
            (claim_id, claim_text, document_id, source_type, confidence,
             prediction_mode, extracted_at, attributed, attribution_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [record.claim_id, record.claim_text, record.document_id,
         record.source_type, record.confidence, record.prediction_mode, record.extracted_at,
         record.attributed, record.attribution_text],
    )


def store_evidence(record: EvidenceRecord, conn) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO claim_evidence
            (evidence_id, claim_id, evidence_text, evidence_document_id,
             evidence_source_type, relation, similarity_score, found_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [record.evidence_id, record.claim_id, record.evidence_text,
         record.evidence_document_id, record.evidence_source_type,
         record.relation, record.similarity_score, record.found_at],
    )


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    document: Document,
    conn,
    corpus_fn: Optional[Callable[[], List[CorpusEntry]]] = None,
) -> Tuple[List[ClaimRecord], List[EvidenceRecord]]:
    """Extract claims from `document`, find cross-corpus evidence, persist both.

    Args:
        document:   any Document across all source types
        conn:       DuckDB connection (argument_claims + claim_evidence must exist)
        corpus_fn:  callable returning [(sentence, doc_id, source_type), ...];
                    defaults to loading all news_articles content from DuckDB
    """
    claims = extract_claims(document)
    if not claims:
        return [], []

    corpus = corpus_fn() if corpus_fn else _default_corpus(conn, document.document_id)

    all_evidence: List[EvidenceRecord] = []
    for claim in claims:
        store_claim(claim, conn)
        evidence = find_evidence(claim, corpus)
        for ev in evidence:
            store_evidence(ev, conn)
        all_evidence.extend(evidence)

    logger.info(
        "run_pipeline: doc=%s source=%s claims=%d evidence=%d",
        document.document_id, document.source_type, len(claims), len(all_evidence),
    )
    return claims, all_evidence


def _default_corpus(conn, exclude_document_id: str) -> List[CorpusEntry]:
    """Load content from the whole corpus as a sentence corpus.

    Reads every source type (via ``corpus_table``), not just news, so evidence
    matching is representative once the corpus holds papers, books, or blogs.
    """
    import os

    try:
        limit = int(os.getenv("NOESIS_EVIDENCE_CORPUS_LIMIT", _DEFAULT_CORPUS_LIMIT))
    except (TypeError, ValueError):
        limit = _DEFAULT_CORPUS_LIMIT
    try:
        table = corpus_table(conn)
        has_source_type = table == "corpus_documents"
        rows = conn.execute(
            f"SELECT id, content{', source_type' if has_source_type else ''} FROM {table} "
            "WHERE id != ? AND content IS NOT NULL LIMIT ?",
            [exclude_document_id, limit],
        ).fetchall()
    except Exception as exc:
        logger.warning("Could not load corpus: %s", exc)
        return []

    corpus: List[CorpusEntry] = []
    sent_re = re.compile(r"(?<=[.!?])\s+")
    for row in rows:
        doc_id, content = row[0], row[1]
        source_type = row[2] if len(row) > 2 else "news"
        if not content:
            continue
        for sentence in sent_re.split(content.strip()):
            sentence = sentence.strip()
            if len(sentence) >= 20:
                corpus.append((sentence, doc_id, source_type))
    return corpus
