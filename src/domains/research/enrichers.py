"""
Research-domain enrichers (R7 / Track N1).

Lightweight, dependency-free enrichers over paper metadata: normalize the
publication venue, count citations/references, and tag key concepts from the
title and abstract. They run only for ``source_type = 'paper'`` documents and
follow the generic :class:`~src.domains.base.Enricher` interface (return a
dict of results, or ``None`` to skip).

Stdlib-only, so importing the pack never pulls a heavy dependency.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _field(document: Any, key: str, default=None):
    """Read a field from a Document dataclass or a plain dict."""
    if isinstance(document, dict):
        return document.get(key, default)
    return getattr(document, key, default)


def _metadata(document: Any) -> Dict[str, Any]:
    meta = _field(document, "metadata", {})
    return meta if isinstance(meta, dict) else {}


def venue_enricher(document: Any) -> Optional[Dict[str, Any]]:
    """Normalize the publication venue from metadata (venue / journal /
    booktitle), title-casing and trimming."""
    meta = _metadata(document)
    raw = meta.get("venue") or meta.get("journal") or meta.get("booktitle")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return {"venue": raw.strip(), "enricher": "venue"}


def citation_enricher(document: Any) -> Optional[Dict[str, Any]]:
    """Count a paper's references and its incoming citation count from
    metadata, tolerating either a list or an integer."""
    meta = _metadata(document)
    refs = meta.get("references")
    ref_ids: List[str] = []
    if isinstance(refs, list):
        ref_ids = [str(r) for r in refs if r]
    citations = meta.get("citations") or meta.get("cited_by")
    if not isinstance(citations, int):
        citations = len(ref_ids) if ref_ids else 0
    if not ref_ids and not citations:
        return None
    return {
        "citations": int(citations),
        "refs": ",".join(ref_ids),
        "reference_count": len(ref_ids),
        "enricher": "citation",
    }


_CONCEPT_STOP = frozenset(
    "study analysis approach method methods results paper using based novel "
    "towards toward new model models framework via case learning".split()
)


def concept_enricher(document: Any) -> Optional[Dict[str, Any]]:
    """Tag the paper's dominant concept from its title/abstract (the most
    frequent salient noun-ish token), used to bucket a venue's topic mix."""
    from src.analytics.text import tokenize

    title = _field(document, "title", "") or ""
    abstract = _metadata(document).get("abstract", "") or ""
    tokens = [t for t in tokenize(f"{title} {title} {abstract}") if t not in _CONCEPT_STOP]
    if not tokens:
        return None
    counts: Dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    concept = max(counts, key=lambda k: (counts[k], k))
    return {"concept": concept, "enricher": "concept"}
