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
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Union

from services.ingest.common.document_model import Document

logger = logging.getLogger(__name__)


class PermanentFetchError(Exception):
    """A fetch failure that will not succeed on retry (404, gone, unauthorized).

    ``harvest_run`` skips a source immediately when ``fetch`` raises this,
    instead of burning retries. Any other exception is treated as transient
    and retried with backoff.
    """


@dataclass
class SourceRef:
    """A discoverable location/handle to fetch (a feed URL, paper id, file path)."""

    locator: str
    title: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def source_id(self) -> str:
        """Stable key for health tracking (explicit ``source_id`` or the locator)."""
        return str(self.metadata.get("source_id") or self.locator)


@dataclass
class RawDocument:
    """Raw payload fetched for a :class:`SourceRef`, prior to normalization."""

    ref: SourceRef
    content: Union[bytes, str]
    content_type: Optional[str] = None
    fetched_at: int = field(default_factory=lambda: int(time.time() * 1000))
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HarvestSummary:
    """Outcome of one :meth:`Connector.harvest_run` (#897).

    Source-stage counts satisfy ``discovered >= fetched >= parsed`` (some
    discovered sources are skipped by scheduling or error before fetch/parse).
    Store-stage counts satisfy ``inserted + duplicate + invalid == documents``
    when a store is attached — every produced document lands in exactly one
    outcome. ``per_source`` breaks the same counts down by ``source_id`` so a
    single degraded source is visible against a healthy run.
    """

    unchanged: int = 0
    discovered: int = 0     # sources enumerated by discover()
    skipped: int = 0        # sources not due / quarantined (not fetched)
    fetched: int = 0        # sources fetched successfully
    fetch_errors: int = 0   # sources that failed to fetch (retries exhausted / permanent)
    parsed: int = 0         # sources parsed successfully
    parse_errors: int = 0   # sources that failed to parse
    documents: int = 0      # Document records produced by parse
    inserted: int = 0       # documents persisted by the store
    duplicate: int = 0      # documents dedup-skipped by the store
    invalid: int = 0        # documents contract-rejected by the store
    per_source: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def _source(self, source_id: str) -> Dict[str, int]:
        return self.per_source.setdefault(source_id, {
            "discovered": 0, "skipped": 0, "fetched": 0, "fetch_errors": 0,
            "parsed": 0, "parse_errors": 0, "documents": 0,
            "inserted": 0, "duplicate": 0, "invalid": 0,
        })

    def as_dict(self) -> Dict[str, Any]:
        return {
            "discovered": self.discovered, "skipped": self.skipped,
            "fetched": self.fetched, "fetch_errors": self.fetch_errors,
            "parsed": self.parsed, "parse_errors": self.parse_errors,
            "documents": self.documents, "inserted": self.inserted,
            "duplicate": self.duplicate, "invalid": self.invalid,
            "per_source": self.per_source,
        }


class Connector(abc.ABC):
    """Base class for all ingestion connectors.

    Subclasses set :attr:`source_type` (one of the document-ingest-v1 source
    types) and implement :meth:`discover`, :meth:`fetch`, and :meth:`parse`.
    """

    #: document-ingest-v1 source_type this connector produces.
    source_type: str = ""

    #: Registry key for this connector. Defaults to ``source_type`` when unset,
    #: so a connector may declare a distinct ``name`` to coexist with another
    #: that emits the same ``source_type`` (e.g. ``upload`` and ``filings`` both
    #: produce ``source_type="note"``).
    name: str = ""

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

    def harvest_run(
        self,
        query: Optional[Any] = None,
        *,
        store: Optional[Any] = None,
        health: Optional[Any] = None,
        retries: int = 2,
        backoff_base: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        now_ms: Optional[int] = None,
        respect_schedule: bool = True,
    ) -> HarvestSummary:
        """Adaptive, observable harvest: retry, health tracking, scheduling (#896, #897).

        Unlike :meth:`harvest` (a bare generator), this drives the same
        discover -> fetch -> parse chain with the resilience the live RSS path
        already had, generalized to every connector:

        - transient ``fetch`` failures are retried with exponential backoff;
          a :class:`PermanentFetchError` skips the source immediately;
        - each source's yield and field-fill are recorded into ``health`` (a
          :class:`~src.ingestion.source_health.SourceHealthTracker`), so a
          source that silently stops yielding is flagged ``degraded``;
        - when ``respect_schedule`` and ``health`` are set, a source that is
          not :meth:`~src.ingestion.source_health.SourceHealthTracker.due`
          (backed off or quarantined) is skipped without fetching;
        - when a ``store`` (:class:`~src.ingestion.document_store.DocumentStore`)
          is passed, parsed documents are validated/deduped/persisted and the
          store outcome folds into the returned :class:`HarvestSummary`.

        ``health`` and ``store`` are duck-typed (only the methods used are
        required), so the run is offline-testable with fakes.
        """
        summary = HarvestSummary()

        for ref in self.discover(query):
            summary.discovered += 1
            sid = ref.source_id
            ps = summary._source(sid)
            ps["discovered"] += 1

            if (
                health is not None
                and respect_schedule
                and not health.due(sid, now_ms=now_ms)
            ):
                summary.skipped += 1
                ps["skipped"] += 1
                continue

            raw = self._fetch_with_retry(ref, retries, backoff_base, sleep, summary, ps)
            if raw is None:
                self._record_health(health, sid, 0, [], fetch_errors=1, now_ms=now_ms)
                continue
            summary.fetched += 1
            ps["fetched"] += 1
            if raw.metadata.get('outcome') == 'unchanged':
                summary.unchanged += 1
                ps['unchanged'] = ps.get('unchanged', 0) + 1
                if health is not None:
                    health.record_unchanged(sid, now_ms=now_ms)
                continue

            try:
                documents = self.parse(raw)
            except Exception:  # noqa: BLE001 - resilience: skip unparseable sources
                self._on_error("parse", ref)
                summary.parse_errors += 1
                ps["parse_errors"] += 1
                self._record_health(health, sid, 0, [], fetch_errors=0, now_ms=now_ms)
                continue
            summary.parsed += 1
            ps["parsed"] += 1
            summary.documents += len(documents)
            ps["documents"] += len(documents)

            self._record_health(health, sid, len(documents), documents,
                                 fetch_errors=0, now_ms=now_ms)

            if store is not None and documents:
                up = store.upsert(documents)
                summary.inserted += up.inserted
                summary.duplicate += up.duplicate
                summary.invalid += up.invalid
                ps["inserted"] += up.inserted
                ps["duplicate"] += up.duplicate
                ps["invalid"] += up.invalid

        return summary

    def _fetch_with_retry(self, ref, retries, backoff_base, sleep, summary, ps):
        """Fetch ``ref``, retrying transient errors; return the raw doc or None."""
        attempt = 0
        while True:
            try:
                return self.fetch(ref)
            except PermanentFetchError:
                self._on_error("fetch", ref)
                summary.fetch_errors += 1
                ps["fetch_errors"] += 1
                return None
            except Exception as exc:  # noqa: BLE001 - transient: retry with backoff
                attempt += 1
                if attempt > retries:
                    self._on_error("fetch", ref)
                    summary.fetch_errors += 1
                    ps["fetch_errors"] += 1
                    return None
                delay = max(backoff_base * (2 ** (attempt - 1)), getattr(exc, 'retry_after', None) or 0)
                if delay > 60:
                    summary.fetch_errors += 1
                    ps['fetch_errors'] += 1
                    return None  # Defer long Retry-After values to a later scheduled run.
                sleep(delay)

    @staticmethod
    def _record_health(health, source_id, articles, documents, *, fetch_errors, now_ms):
        """Record one source's outcome into a SourceHealthTracker, if provided."""
        if health is None:
            return
        from src.ingestion.source_health import field_fill_rates

        fill = field_fill_rates(
            [{"title": d.title, "content": d.content} for d in documents]
        )
        health.record_run(
            source_id, articles, field_fill=fill,
            fetch_errors=fetch_errors, now_ms=now_ms,
        )

    def _on_error(self, stage: str, ref: SourceRef) -> None:
        logger.warning(
            "%s: %s stage failed for %s", self.__class__.__name__, stage, ref.locator,
            exc_info=True,
        )
