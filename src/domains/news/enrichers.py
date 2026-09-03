"""
News-domain enrichers: thin wrappers around the NLP/ML modules so they can be
called through the generic :class:`~src.domains.base.Enricher` interface.

All heavy imports (torch, transformers, psycopg2, …) are deferred to function
bodies so the enrichers can be instantiated without triggering those imports.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# --------------------------------------------------------------------------- #
# Fake-news / trustworthiness detection
# --------------------------------------------------------------------------- #

def fake_news_enricher(document: Any) -> Optional[Dict[str, Any]]:
    """Run the FakeNewsDetector on the document's content.

    Returns a dict with keys ``trustworthiness_score``, ``classification``,
    and ``trust_level``, or ``None`` if the detector is unavailable or the
    document has no content.
    """
    try:
        from src.nlp.fake_news_detector import FakeNewsDetector  # type: ignore
    except ImportError:
        return None

    content = _get_content(document)
    if not content:
        return None

    detector = FakeNewsDetector(use_pretrained=True)
    result = detector.predict_trustworthiness(content)
    return {
        "trustworthiness_score": result.get("trustworthiness_score"),
        "classification": result.get("classification"),
        "trust_level": result.get("trust_level"),
        "enricher": "fake_news_detector",
    }


# --------------------------------------------------------------------------- #
# Sentiment analysis
# --------------------------------------------------------------------------- #

def sentiment_enricher(document: Any) -> Optional[Dict[str, Any]]:
    """Run sentiment analysis on the document's content.

    Returns a dict with keys ``sentiment``, ``score``, and ``label``,
    or ``None`` if the analyzer is unavailable or there is no content.
    """
    try:
        from src.nlp.sentiment_analysis import create_analyzer  # type: ignore
    except ImportError:
        return None

    content = _get_content(document)
    if not content:
        return None

    analyzer = create_analyzer()
    result = analyzer.analyze(content)
    return {
        "sentiment": result.get("label") or result.get("sentiment"),
        "score": result.get("score"),
        "enricher": "sentiment_analysis",
    }


# --------------------------------------------------------------------------- #
# Event clustering
# --------------------------------------------------------------------------- #

def event_clusterer_enricher(document: Any) -> Optional[Dict[str, Any]]:
    """Tag the document with the event-cluster it belongs to (if any).

    The EventClusterer operates asynchronously on batches; this thin wrapper
    returns minimal metadata so the enrichment layer can call the clusterer
    with a full batch later. Here we just mark the document as eligible.
    """
    try:
        import src.nlp.event_clusterer  # noqa: F401  # type: ignore
    except ImportError:
        return None

    content = _get_content(document)
    if not content:
        return None

    # A full async batch-clustering run is done by the pipeline DAG; here we
    # return a sentinel so the enrichment layer knows to queue this document.
    return {
        "event_clustering_eligible": True,
        "enricher": "event_clusterer",
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _get_content(document: Any) -> Optional[str]:
    """Extract text content from a Document dataclass or dict."""
    if isinstance(document, dict):
        return document.get("content") or document.get("text")
    return getattr(document, "content", None) or getattr(document, "text", None)


def _get_field(document: Any, field: str) -> Any:
    if isinstance(document, dict):
        return document.get(field)
    return getattr(document, field, None)
