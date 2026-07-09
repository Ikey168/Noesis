"""
Dataset connector framework (Track A of the beyond-text expansion).

A dataset connector turns an official statistics provider (World Bank, FRED,
Eurostat, ...) into normalized :class:`SeriesRecord` records
(``dataset-series-v1``). It mirrors the document ``Connector`` interface —
``discover`` -> ``fetch`` -> ``parse`` chained by ``harvest`` — but emits
statistical series rather than ``Document`` records, because series are
versioned numeric evidence, not text (see
``docs/architecture/EVIDENCE_DATASETS_PLAN.md``).

    discover(query) -> iterable of SeriesRef   (which series to harvest)
    fetch(ref)      -> RawSeries               (pull the raw provider payload)
    parse(raw)      -> list of SeriesRecord     (normalize to the contract)
    harvest(query)  -> iterator of SeriesRecord (discover -> fetch -> parse)

``harvest`` is resilient: a series that fails to fetch or parse is skipped and
logged so one bad series does not abort the run.
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union

from services.ingest.common.series_model import SeriesRecord


@dataclass
class SeriesRef:
    """A discoverable handle for a series to harvest (provider code + scope)."""

    locator: str
    title: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RawSeries:
    """Raw payload fetched for a :class:`SeriesRef`, prior to normalization."""

    ref: SeriesRef
    content: Union[bytes, str]
    content_type: Optional[str] = None
    source_url: Optional[str] = None
    fetched_at: int = field(default_factory=lambda: int(time.time() * 1000))


class DatasetConnector(abc.ABC):
    """Base class for statistics-provider connectors.

    Subclasses set :attr:`provider` and implement :meth:`discover`,
    :meth:`fetch`, and :meth:`parse`.
    """

    #: dataset-series-v1 provider short name this connector produces.
    provider: str = ""

    @abc.abstractmethod
    def discover(self, query: Optional[Any] = None) -> Iterable[SeriesRef]:
        """Enumerate the series to harvest (optionally narrowed by ``query``)."""

    @abc.abstractmethod
    def fetch(self, ref: SeriesRef) -> RawSeries:
        """Pull the raw payload for a single :class:`SeriesRef`."""

    @abc.abstractmethod
    def parse(self, raw: RawSeries) -> List[SeriesRecord]:
        """Normalize a :class:`RawSeries` into one or more series records."""

    def harvest(self, query: Optional[Any] = None) -> Iterator[SeriesRecord]:
        """Run discover -> fetch -> parse, yielding normalized series records.

        Series that fail to fetch/parse are skipped (and logged) so one bad
        series does not abort the whole run.
        """
        for ref in self.discover(query):
            try:
                raw = self.fetch(ref)
            except Exception:  # noqa: BLE001 - resilience: skip unreachable series
                self._on_error("fetch", ref)
                continue
            try:
                records = self.parse(raw)
            except Exception:  # noqa: BLE001 - resilience: skip unparseable series
                self._on_error("parse", ref)
                continue
            for record in records:
                yield record

    def _on_error(self, stage: str, ref: SeriesRef) -> None:
        logging.getLogger(self.__class__.__module__).warning(
            "%s: %s stage failed for %s", self.__class__.__name__, stage, ref.locator,
            exc_info=True,
        )
