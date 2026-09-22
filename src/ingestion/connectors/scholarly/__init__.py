"""Scholarly paper connectors (source_type="paper"), recency by publication date.

Importing this package registers every scholarly source connector (OpenAlex,
Crossref, Semantic Scholar, Europe PMC, PubMed, bioRxiv, medRxiv, DOAJ, CORE,
DBLP, HAL, PLOS, Zenodo). Resolve one with ``get_connector("<name>")``.
"""
from src.ingestion.connectors.scholarly.base import (  # noqa: F401
    ScholarlyConnector,
    ScholarlyQuery,
    ScholarlySource,
)
from src.ingestion.connectors.scholarly import sources  # noqa: F401,E402
from src.ingestion.connectors.scholarly.sources import SCHOLARLY_SOURCES  # noqa: F401

__all__ = ["ScholarlyConnector", "ScholarlyQuery", "ScholarlySource", "SCHOLARLY_SOURCES"]
