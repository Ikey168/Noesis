"""
Data models for the papers connector: paper metadata and references.

These are the connector's internal representation of an academic paper and its
citations, independent of the document-ingest contract and the knowledge graph
(both of which are produced from these).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


def paper_id(arxiv_id: Optional[str] = None, doi: Optional[str] = None, title: Optional[str] = None) -> str:
    """Stable document id for a paper, preferring arXiv id, then DOI, then title.

    The id is reused for both the document-ingest record and the knowledge-graph
    Document node so a paper and the papers that cite it line up.
    """
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    if doi:
        return f"doi:{doi.lower()}"
    digest = hashlib.md5((title or "").strip().lower().encode()).hexdigest()[:12]
    return f"paper:{digest}"


@dataclass
class Reference:
    """A work cited by a paper."""

    title: Optional[str] = None
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    year: Optional[int] = None

    @property
    def document_id(self) -> str:
        return paper_id(self.arxiv_id, self.doi, self.title)


@dataclass
class PaperMetadata:
    """Normalized metadata for a single academic paper."""

    title: str
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    abstract: str = ""
    categories: List[str] = field(default_factory=list)
    primary_category: Optional[str] = None
    published: Optional[datetime] = None
    pdf_url: Optional[str] = None
    references: List[Reference] = field(default_factory=list)
    version_id: Optional[str] = None
    updated: Optional[datetime] = None

    @property
    def document_id(self) -> str:
        return paper_id(self.arxiv_id, self.doi, self.title)
