"""
Document summarization over the unified corpus.

Two summarizers behind one ``summarizer(text) -> str`` interface, following the
repo's heuristic-first / model-optional discipline (cf. ``frames.py``):

* :func:`extractive_summary` - the default. Dependency-light, deterministic,
  offline: score sentences by normalized term frequency with a lead bias, keep
  the top-N in reading order. Runs (and is gate-tested) with no ML stack.
* :func:`abstractive_summary` - optional. Lazily loads a transformers
  summarization pipeline (BART/DistilBART) for a fluent abstractive summary;
  falls back to the extractive summary if the model stack is unavailable.

:func:`summarize_documents` is the idempotent batch pass (writes
``document_summaries``); :func:`summarize_topic` composes a short brief for a
topic from its most recent documents. The heavy summarizer already in the repo
(``src/nlp/ai_summarizer.py``) is async, article-bound and eager-imports torch;
this is the corpus-oriented, import-light replacement.
"""

from __future__ import annotations

import re
from typing import Any, Callable, List, Optional

from src.database.news_articles_compat import corpus_table, ensure_corpus_documents_view
from src.ingestion.summary_store import SummaryStore

Summarizer = Callable[[str], str]

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z']+")
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "it",
    "its", "this", "that", "these", "those", "has", "have", "had", "will", "would",
    "could", "should", "after", "over", "into", "about", "than", "then", "he",
    "she", "they", "we", "you", "his", "her", "their", "our", "said",
})

DEFAULT_MAX_SENTENCES = 3


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENTENCE_RE.split((text or "").strip()) if s.strip()]


def extractive_summary(text: str, max_sentences: int = DEFAULT_MAX_SENTENCES) -> str:
    """Top-``max_sentences`` sentences by normalized term frequency + lead bias,
    returned in original reading order. Deterministic and dependency-free."""
    sentences = _sentences(text)
    if len(sentences) <= max_sentences:
        return " ".join(sentences)

    freq: dict = {}
    for w in _WORD_RE.findall(text.lower()):
        if len(w) > 2 and w not in _STOPWORDS:
            freq[w] = freq.get(w, 0) + 1
    if not freq:
        return " ".join(sentences[:max_sentences])
    top = max(freq.values())

    scored = []
    for i, sent in enumerate(sentences):
        words = [w for w in _WORD_RE.findall(sent.lower()) if w in freq]
        if not words:
            score = 0.0
        else:
            score = sum(freq[w] / top for w in words) / len(words)
        score += 0.15 if i == 0 else (0.05 if i == 1 else 0.0)  # lead bias
        scored.append((score, i, sent))

    keep = sorted((i for _, i, _ in sorted(scored, reverse=True)[:max_sentences]))
    return " ".join(sentences[i] for i in keep)


def abstractive_summary(
    text: str, max_length: int = 130, min_length: int = 30,
    model_name: str = "sshleifer/distilbart-cnn-12-6",
) -> str:
    """A fluent abstractive summary via a lazily-loaded transformers pipeline.

    Falls back to :func:`extractive_summary` if transformers/torch are
    unavailable or the model errors, so callers get a summary either way."""
    try:
        from transformers import pipeline

        summarizer = pipeline("summarization", model=model_name)
        out = summarizer(text[:3000], max_length=max_length, min_length=min_length,
                         do_sample=False)
        return out[0]["summary_text"].strip()
    except Exception:
        return extractive_summary(text)


def default_summarizer(text: str) -> str:
    """The offline default (extractive)."""
    return extractive_summary(text)


def summarize_documents(
    conn,
    summarizer: Optional[Summarizer] = None,
    limit: Optional[int] = None,
    method: str = "extractive",
) -> int:
    """Summarize documents that have no summary yet; return how many were done.

    Reads ``corpus_documents`` LEFT JOIN ``document_summaries`` for the
    un-summarized rows, runs ``summarizer`` (default: extractive) over
    ``title + content``, and upserts into ``document_summaries``. Idempotent.
    """
    summarizer = summarizer or default_summarizer
    ensure_corpus_documents_view(conn)
    store = SummaryStore(conn)

    query = (
        "SELECT d.id, d.title, d.content FROM corpus_documents d "
        "LEFT JOIN document_summaries s ON s.document_id = d.id "
        "WHERE s.document_id IS NULL ORDER BY d.publish_date DESC NULLS LAST"
    )
    params: List[Any] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    done = 0
    for document_id, title, content in rows:
        text = f"{title or ''}. {content or ''}".strip()
        store.upsert(document_id, summarizer(text), method=method)
        done += 1
    return done


def document_summary(conn, document_id: str, max_sentences: int = DEFAULT_MAX_SENTENCES) -> dict:
    """A single document's summary — the stored one if present, else computed
    extractively from its corpus content on the fly. Read-only (never writes)."""
    try:
        has_store = bool(conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'document_summaries'"
        ).fetchone())
    except Exception:
        has_store = False
    if has_store:
        row = conn.execute(
            "SELECT summary, method FROM document_summaries WHERE document_id = ?",
            [document_id],
        ).fetchone()
        if row is not None:
            return {"document_id": document_id, "summary": row[0], "method": row[1],
                    "stored": True}

    tbl = corpus_table(conn)
    row = conn.execute(
        f"SELECT title, content FROM {tbl} WHERE id = ?", [document_id]
    ).fetchone()
    if row is None:
        return {"error": f"document {document_id!r} not found", "code": "not_found",
                "document_id": document_id}
    text = f"{row[0] or ''}. {row[1] or ''}".strip()
    return {"document_id": document_id, "method": "extractive", "stored": False,
            "summary": extractive_summary(text, max_sentences=max_sentences)}


def summarize_topic(
    conn, topic: str, summarizer: Optional[Summarizer] = None, max_docs: int = 20,
    max_sentences: int = 5,
) -> dict:
    """A short brief for a topic (category), summarizing its most recent
    documents. Read-only; returns the summary and the documents it drew on."""
    summarizer = summarizer or (lambda t: extractive_summary(t, max_sentences=max_sentences))
    tbl = corpus_table(conn)
    rows = conn.execute(
        f"SELECT id, title, content FROM {tbl} WHERE category = ? "
        f"ORDER BY publish_date DESC NULLS LAST LIMIT ?",
        [topic, max_docs],
    ).fetchall()
    if not rows:
        return {"topic": topic, "summary": "", "document_count": 0,
                "note": "no documents for this topic"}
    corpus = " ".join(f"{r[1] or ''}. {r[2] or ''}" for r in rows)
    return {
        "topic": topic,
        "summary": summarizer(corpus),
        "document_count": len(rows),
        "document_ids": [r[0] for r in rows],
    }
