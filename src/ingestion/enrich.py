"""
Document enrichment pass (orchestration reintegration, #922).

The scheduled pipeline used to fake NLP (hardcoded sentiment, regex "entities").
This is the real enrichment step: read documents that have no enrichment yet,
run a pluggable analyzer, and persist sentiment/topics into the
``document_enrichments`` sink (#908) keyed by ``document_id`` — the ``nlp`` stage
the ``news_pipeline`` DAG now delegates to.

The default analyzer is a dependency-free lexicon sentiment + keyword topics, so
the enrichment pass runs (and is gate-tested) offline; a heavier model can be
injected via the ``analyzer`` parameter without touching the wiring.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from src.ingestion.document_store import DocumentStore
from src.ingestion.enrichment_store import EnrichmentStore

Analyzer = Callable[[Dict[str, Any]], Dict[str, Any]]

# Small, transparent sentiment lexicons (news/finance-leaning).
_POSITIVE = frozenset({
    "good", "great", "gain", "gains", "gained", "rise", "rises", "rose", "up",
    "boost", "boosts", "strong", "win", "wins", "won", "success", "approve",
    "approved", "growth", "grow", "record", "surge", "surges", "beat", "beats",
    "improve", "improved", "recovery", "optimistic", "positive",
})
_NEGATIVE = frozenset({
    "bad", "loss", "losses", "lost", "fall", "falls", "fell", "down", "weak",
    "fail", "fails", "failed", "crisis", "decline", "declines", "cut", "cuts",
    "drop", "drops", "plunge", "plunges", "recession", "concern", "concerns",
    "fear", "fears", "warning", "slump", "negative", "risk", "risks",
})
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "it",
    "its", "this", "that", "these", "those", "has", "have", "had", "will",
    "would", "could", "should", "after", "over", "into", "about", "than", "then",
})


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z']+", (text or "").lower())


def lexicon_sentiment(text: str) -> Dict[str, Any]:
    """Deterministic lexicon sentiment: score in [-1, 1] + a label."""
    words = _tokens(text)
    pos = sum(w in _POSITIVE for w in words)
    neg = sum(w in _NEGATIVE for w in words)
    total = pos + neg
    if total == 0:
        return {"sentiment_score": 0.0, "sentiment_label": "neutral"}
    score = round((pos - neg) / total, 3)
    label = "positive" if score > 0.1 else "negative" if score < -0.1 else "neutral"
    return {"sentiment_score": score, "sentiment_label": label}


def keyword_topics(text: str, top_n: int = 5) -> List[str]:
    """Top-N frequent, non-trivial keywords as lightweight topics."""
    freq: Dict[str, int] = {}
    for w in _tokens(text):
        if len(w) > 4 and w not in _STOPWORDS:
            freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:top_n]]


def default_analyzer(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Lexicon sentiment + keyword topics over the document's title + content."""
    text = f"{doc.get('title') or ''} {doc.get('content') or ''}"
    result = lexicon_sentiment(text)
    result["topics"] = keyword_topics(text)
    return result


def enrich_documents(
    conn,
    analyzer: Optional[Analyzer] = None,
    limit: Optional[int] = None,
) -> int:
    """Enrich documents that have no enrichment yet; return how many were enriched.

    Reads ``documents`` LEFT JOIN ``document_enrichments`` for the un-enriched
    rows, runs ``analyzer`` (default: lexicon sentiment + keyword topics), and
    upserts the result into ``document_enrichments``. Idempotent: an already-
    enriched document is skipped, so re-running only fills the gaps.
    """
    analyzer = analyzer or default_analyzer
    DocumentStore(conn)          # ensure documents table
    store = EnrichmentStore(conn)  # ensure document_enrichments table

    query = (
        "SELECT d.document_id, d.title, d.content FROM documents d "
        "LEFT JOIN document_enrichments e ON e.document_id = d.document_id "
        "WHERE e.document_id IS NULL ORDER BY d.ingested_at DESC"
    )
    params: List[Any] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    enriched = 0
    for document_id, title, content in rows:
        result = analyzer({"document_id": document_id, "title": title, "content": content})
        store.upsert(
            document_id,
            sentiment_score=result.get("sentiment_score"),
            sentiment_label=result.get("sentiment_label"),
            topics=result.get("topics"),
        )
        enriched += 1
    return enriched
