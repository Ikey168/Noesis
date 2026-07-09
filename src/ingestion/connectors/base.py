"""
Ingestion connector framework.

A connector turns some external source (a news feed, a paper repository, a book
file, an audio transcript, ...) into normalized ``Document`` records
(document-ingest-v1). Every connector follows the same three-step contract:

    discover()  ->  iterable of SourceRef   (what is there to ingest)
    fetch(ref)  ->  RawDocument             (pull the raw bytes/text)
    parse(raw)  ->  list of Document        (normalize to the contract)

``harvest()`` chains the three so callers can iterate ``Document`` objects
without caring about the source. New media types (papers, books, transcripts,
uploads) plug in by subclassing :class:`Connector` and registering a
``source_type`` in :mod:`src.ingestion.connectors.registry`.

See ``docs/architecture/knowledge-engine-pivot.md``.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union

from services.ingest.common.document_model import Document


@dataclass
class SourceRef:
    """A discoverable location/handle to fetch (a feed URL, paper id, file path)."""

    locator: str
    title: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RawDocument:
    """Raw payload fetched for a :class:`SourceRef`, prior to normalization."""

    ref: SourceRef
    content: Union[bytes, str]
    content_type: Optional[str] = None
    fetched_at: int = field(default_factory=lambda: int(time.time() * 1000))


class Connector(abc.ABC):
    """Base class for all ingestion connectors.

    Subclasses set :attr:`source_type` (one of the document-ingest-v1 source
    types) and implement :meth:`discover`, :meth:`fetch`, and :meth:`parse`.
    """

    #: document-ingest-v1 source_type this connector produces.
    source_type: str = ""

    @abc.abstractmethod
    def discover(self, query: Optional[Any] = None) -> Iterable[SourceRef]:
        """Enumerate the sources to ingest (optionally narrowed by ``query``)."""

    @abc.abstractmethod
    def fetch(self, ref: SourceRef) -> RawDocument:
        """Pull the raw payload for a single :class:`SourceRef`."""

    @abc.abstractmethod
    def parse(self, raw: RawDocument) -> List[Document]:
        """Normalize a :class:`RawDocument` into one or more ``Document`` records."""

    def harvest(self, query: Optional[Any] = None) -> Iterator[Document]:
        """Run discover -> fetch -> parse, yielding normalized documents.

        Individual sources that fail to fetch/parse are skipped (logged by the
        connector) so one bad source does not abort the whole run.
        """
        for ref in self.discover(query):
            try:
                raw = self.fetch(ref)
            except Exception:  # noqa: BLE001 - resilience: skip unreachable sources
                self._on_error("fetch", ref)
                continue
            try:
                documents = self.parse(raw)
            except Exception:  # noqa: BLE001 - resilience: skip unparseable sources
                self._on_error("parse", ref)
                continue
            for document in documents:
                yield document

    def _on_error(self, stage: str, ref: SourceRef) -> None:
        import logging

        logging.getLogger(self.__class__.__module__).warning(
            "%s: %s stage failed for %s", self.__class__.__name__, stage, ref.locator,
            exc_info=True,
        )
