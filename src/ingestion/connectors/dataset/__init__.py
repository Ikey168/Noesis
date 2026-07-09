"""
Dataset connectors: official statistics providers as ``dataset-series-v1``.

Track A of the beyond-text expansion. Series are evidence for checking
quantitative claims and are *not* documents; see
``docs/architecture/EVIDENCE_DATASETS_PLAN.md``.
"""

from __future__ import annotations

from src.ingestion.connectors.dataset.base import (
    DatasetConnector,
    RawSeries,
    SeriesRef,
)
from src.ingestion.connectors.dataset.eurostat import EurostatConnector
from src.ingestion.connectors.dataset.fred import FredConnector
from src.ingestion.connectors.dataset.store import ObservationStore
from src.ingestion.connectors.dataset.worldbank import WorldBankConnector

__all__ = [
    "DatasetConnector",
    "RawSeries",
    "SeriesRef",
    "ObservationStore",
    "WorldBankConnector",
    "FredConnector",
    "EurostatConnector",
]
