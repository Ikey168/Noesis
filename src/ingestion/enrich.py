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
from src.ingestion.processing_versions import ProcessingVersions, configuration_hash, document_input_hash

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
    *,
    analyzer_version: str | None = None,
    configuration: dict | None = None,
) -> int:
    """Refresh missing or stale enrichment, preserving prior output on failure.

    Custom analyzers should declare an explicit immutable analyzer_version and
    all result-affecting configuration. Unversioned callables run each time;
    guessing their identity from a function name would silently skip changes.
    """
    version = analyzer_version or ("lexicon-keywords-v1" if analyzer is None else getattr(analyzer, "version", None))
    analyzer = analyzer or default_analyzer
    DocumentStore(conn)          # ensure documents table
    store = EnrichmentStore(conn)  # ensure document_enrichments table
    versions = ProcessingVersions(conn)
    config_hash = configuration_hash({"analyzer": version, "configuration": configuration or {}})
    input_sql = document_input_hash()

    query = (
        f"SELECT d.document_id, d.title, d.content, {input_sql} FROM documents d "
        "LEFT JOIN document_enrichments e ON e.document_id = d.document_id "
        "LEFT JOIN document_processing_versions p ON p.document_id=d.document_id AND p.stage='enrichment' "
        f"WHERE (? OR e.document_id IS NULL OR p.input_hash IS DISTINCT FROM {input_sql} "
        "OR p.configuration_hash IS DISTINCT FROM ?) ORDER BY d.ingested_at DESC"
    )
    params: List[Any] = [version is None, config_hash]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    enriched = 0
    for document_id, title, content, input_hash in rows:
        result = analyzer({"document_id": document_id, "title": title, "content": content})
        conn.execute("BEGIN")
        try:
            # An injected/network analyzer may have yielded while the source changed.
            current = conn.execute(f"SELECT {input_sql} FROM documents d WHERE document_id=?", [document_id]).fetchone()
            if current is None or current[0] != input_hash:
                conn.execute("ROLLBACK")
                continue
            store.upsert(
                document_id,
                sentiment_score=result.get("sentiment_score"),
                sentiment_label=result.get("sentiment_label"),
                topics=result.get("topics"),
            )
            versions.record(document_id, "enrichment", input_hash, config_hash)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        enriched += 1
    return enriched
