"""
Structured filings connector (candidate track #784).

Filings (company registries, XBRL financial reports, procurement notices) are
hybrid structured+text sources: entities, officers and ownership feed the
knowledge graph; reported figures feed the Track A observation store. A filing's
reported revenue thus becomes checkable evidence against a claim about that
company — and against the official series.

This module maps a normalized filing into three products: a narrative
``Document``, ``dataset-series-v1`` ``SeriesRecord``s (``provider = "filing"``)
for the reported facts, and lightweight filer/officer entity relations for the
KG. Stdlib only.

See ``docs/architecture/BEYOND_TEXT_ROADMAP.md`` §4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.ingest.common.document_model import Document
from services.ingest.common.series_model import Observation, SeriesRecord


@dataclass
class FilingFact:
    """One reported numeric fact (an XBRL-style concept over a period)."""

    concept: str        # e.g. "Revenue", "NetIncome"
    value: float
    period: str         # "2023" or "2023-Q4"
    unit: Optional[str] = None


@dataclass
class Filing:
    filer: str
    filing_id: str
    facts: List[FilingFact] = field(default_factory=list)
    narrative: Optional[str] = None
    officers: List[str] = field(default_factory=list)
    cik: Optional[str] = None
    filed_at: Optional[int] = None
    source_url: Optional[str] = None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def _group_facts(facts: List[FilingFact]) -> Dict[str, List[FilingFact]]:
    by_concept: Dict[str, List[FilingFact]] = {}
    for f in facts:
        by_concept.setdefault(f.concept, []).append(f)
    return by_concept


def filing_to_series(filing: Filing, as_of: int = 0) -> List[SeriesRecord]:
    """One series per reported concept (revenue, net income, ...) for the filer."""
    series: List[SeriesRecord] = []
    for concept, facts in _group_facts(filing.facts).items():
        obs = [Observation(period=f.period, value=f.value) for f in sorted(facts, key=lambda x: x.period)]
        unit = next((f.unit for f in facts if f.unit), None)
        freq = "quarterly" if any("-Q" in f.period for f in facts) else "annual"
        series.append(SeriesRecord(
            series_id=f"filing:{_slug(filing.filer)}:{_slug(concept)}",
            provider="filing",
            title=f"{concept} — {filing.filer}",
            frequency=freq,
            as_of=as_of or (filing.filed_at or 0),
            observations=obs,
            unit=unit,
            geography=None,
            license="public filing",
            source_url=filing.source_url,
            metadata={"filer": filing.filer, "cik": filing.cik, "concept": concept},
        ))
    return series


def filing_to_document(filing: Filing, ingested_at: int) -> Document:
    """The filing's narrative as a document-ingest-v1 record."""
    return Document(
        document_id=f"filing:{filing.filing_id}",
        source_type="note",  # a filing is a structured note; no new enum needed
        language="en",
        ingested_at=ingested_at,
        source_id=filing.cik or filing.filer,
        url=filing.source_url,
        title=f"Filing: {filing.filer}",
        content=filing.narrative,
        authors=list(filing.officers),
        created_at=filing.filed_at,
        metadata={"filer": filing.filer, "cik": filing.cik, "officers": list(filing.officers)},
    )


@dataclass
class EntityRelation:
    subject: str
    predicate: str
    object: str


def filing_entities(filing: Filing) -> List[EntityRelation]:
    """Filer + officer relations for the KG (filer FILED_BY officers)."""
    relations: List[EntityRelation] = []
    for officer in filing.officers:
        relations.append(EntityRelation(subject=officer, predicate="OFFICER_OF", object=filing.filer))
    return relations


def ingest_filing(filing: Filing, ingested_at: int = 0) -> Dict[str, Any]:
    """Map a filing to its three products in one call."""
    return {
        "document": filing_to_document(filing, ingested_at),
        "series": filing_to_series(filing, as_of=ingested_at),
        "entities": filing_entities(filing),
    }
