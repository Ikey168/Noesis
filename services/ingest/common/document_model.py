"""
Generalized Document data model and Article<->Document adapter.

Part of M0 (keystone) of the knowledge-engine pivot. See
``docs/architecture/knowledge-engine-pivot.md``.

The ``Document`` record generalizes the news-specific ``ArticleIngest`` so the
pipeline can ingest news, blogs, books, papers, transcripts, and arbitrary
uploads through one contract (``document-ingest-v1``). ``news`` becomes one
``source_type`` among many.

Enrichments (sentiment, topics, ...) are intentionally *not* part of the core
record. They are produced downstream and carried separately so they are one
analyzer among many rather than intrinsic to a document.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# Valid source types, mirrored from the document-ingest-v1 contract.
SOURCE_TYPES = ("news", "blog", "book", "paper", "transcript", "web", "note")


@dataclass
class DocumentEnrichments:
    """Downstream-produced enrichments, stored separately from the core record."""

    sentiment_score: Optional[float] = None
    topics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"sentiment_score": self.sentiment_score, "topics": list(self.topics)}


@dataclass
class Document:
    """Generalized raw document captured from any source (document-ingest-v1)."""

    document_id: str
    source_type: str
    language: str
    ingested_at: int  # milliseconds since epoch
    source_id: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    content_ref: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    created_at: Optional[int] = None  # milliseconds since epoch
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(
                f"Invalid source_type {self.source_type!r}; expected one of {SOURCE_TYPES}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a document-ingest-v1 payload dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Document":
        """Build a Document from a document-ingest-v1 payload dict."""
        known = {
            "document_id",
            "source_type",
            "language",
            "ingested_at",
            "source_id",
            "url",
            "title",
            "content",
            "content_ref",
            "authors",
            "created_at",
            "metadata",
        }
        return cls(**{k: v for k, v in payload.items() if k in known})


def article_to_document(article: Dict[str, Any]) -> Dict[str, Any]:
    """Map an ArticleIngest payload to a document-ingest-v1 payload (core only).

    News-specific fields with no generic home (``country``) move into
    ``metadata``. Enrichments (``sentiment_score``, ``topics``) are *not*
    included here; use :func:`article_enrichments` to extract them.
    """
    metadata: Dict[str, Any] = {}
    if article.get("country") is not None:
        metadata["country"] = article["country"]

    return Document(
        document_id=article["article_id"],
        source_type="news",
        language=article["language"],
        ingested_at=article["ingested_at"],
        source_id=article.get("source_id"),
        url=article.get("url"),
        title=article.get("title"),
        content=article.get("body"),
        content_ref=None,
        authors=[],
        created_at=article.get("published_at"),
        metadata=metadata,
    ).to_dict()


def article_enrichments(article: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the enrichment fields that left the core record."""
    return DocumentEnrichments(
        sentiment_score=article.get("sentiment_score"),
        topics=list(article.get("topics", [])),
    ).to_dict()


def document_to_article(
    document: Dict[str, Any],
    enrichments: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Reverse adapter: reconstruct an ArticleIngest payload from a news Document.

    Together with :func:`article_to_document` / :func:`article_enrichments` this
    round-trips news fields losslessly, keeping the legacy news pipeline green.
    """
    metadata = document.get("metadata") or {}
    enrichments = enrichments or {}
    return {
        "article_id": document["document_id"],
        "source_id": document.get("source_id"),
        "url": document.get("url"),
        "title": document.get("title"),
        "body": document.get("content"),
        "language": document["language"],
        "country": metadata.get("country"),
        "published_at": document.get("created_at"),
        "ingested_at": document["ingested_at"],
        "sentiment_score": enrichments.get("sentiment_score"),
        "topics": list(enrichments.get("topics", [])),
    }
