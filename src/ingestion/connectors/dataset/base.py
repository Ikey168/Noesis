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
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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


_PERIOD_RE = re.compile(r"^(\d{4})(?:-(Q[1-4]|W\d{2}|\d{2})(?:-(\d{2}))?)?$")


def _period_lag(period: str, frequency: str, today: date) -> Optional[int]:
    """Whole periods between an observation period and ``today``.

    Periods use the ``dataset-series-v1`` forms (``2025``, ``2025-Q3``,
    ``2025-07``, ``2025-07-31``); unrecognized forms return None.
    """

    match = _PERIOD_RE.match(period)
    if not match:
        return None
    year, middle, day = int(match.group(1)), match.group(2), match.group(3)
    if frequency == "annual" and middle is None:
        return today.year - year
    if frequency == "quarterly" and middle and middle.startswith("Q"):
        return (today.year - year) * 4 + (today.month - 1) // 3 - (int(middle[1]) - 1)
    if frequency == "monthly" and middle and middle.isdigit() and day is None:
        return (today.year - year) * 12 + today.month - int(middle)
    if frequency in {"daily", "weekly"} and middle and middle.isdigit() and day:
        try:
            observed = date(year, int(middle), int(day))
        except ValueError:
            return None
        days = (today - observed).days
        return days if frequency == "daily" else days // 7
    return None


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

    def harvest_with_report(self, query: Optional[Any] = None) -> Dict[str, Any]:
        """Harvest a bounded request and report skipped provider inputs.

        ``harvest`` stays backwards compatible and resilient for scheduled
        jobs.  Callers that need to make a readiness decision should use this
        method so missing credentials, provider errors, and empty responses
        are visible instead of being indistinguishable from complete coverage.
        Error messages and request URLs are deliberately excluded because they
        may contain credentials or other sensitive query parameters.
        """

        records: List[SeriesRecord] = []
        diagnostics: List[Dict[str, str]] = []
        discovered = 0
        configured = getattr(self, "configured", True)
        if configured is False:
            return {
                "provider": self.provider,
                "status": "blocked",
                "discovered": 0,
                "records": records,
                "diagnostics": [
                    {
                        "stage": "configuration",
                        "code": "missing_credentials",
                    }
                ],
            }

        try:
            refs = iter(self.discover(query))
        except Exception as exc:  # noqa: BLE001 - summarized in diagnostics
            diagnostics.append(
                {"stage": "discover", "code": self._diagnostic_code(exc)}
            )
            refs = iter(())

        def safe_refs():
            try:
                yield from refs
            except Exception as exc:  # noqa: BLE001 - generator discovery errors
                diagnostics.append(
                    {"stage": "discover", "code": self._diagnostic_code(exc)}
                )

        for ref in safe_refs():
            discovered += 1
            try:
                raw = self.fetch(ref)
            except Exception as exc:  # noqa: BLE001 - summarized in diagnostics
                diagnostics.append(
                    {
                        "stage": "fetch",
                        "locator": ref.locator,
                        "code": self._diagnostic_code(exc),
                    }
                )
                continue
            try:
                parsed = self.parse(raw)
            except Exception as exc:  # noqa: BLE001 - summarized in diagnostics
                diagnostics.append(
                    {
                        "stage": "parse",
                        "locator": ref.locator,
                        "code": self._diagnostic_code(exc),
                    }
                )
                continue
            if not parsed:
                diagnostics.append(
                    {
                        "stage": "parse",
                        "locator": ref.locator,
                        "code": "empty_provider_result",
                    }
                )
            for record in parsed:
                stale = self._staleness(record, raw.fetched_at)
                if stale is not None:
                    diagnostics.append(
                        {"stage": "coverage", "locator": ref.locator, **stale}
                    )
            records.extend(parsed)

        status = (
            "partial"
            if diagnostics and records
            else "unavailable"
            if diagnostics
            else "empty"
            if not records
            else "available"
        )
        return {
            "provider": self.provider,
            "status": status,
            "discovered": discovered,
            "records": records,
            "diagnostics": diagnostics,
        }

    #: Periods an observation may lag its acquisition before the series is
    #: reported stale. Official statistics publish with a lag; a series that
    #: stops (a discontinued or rebased dataset) must not look complete.
    STALE_AFTER_PERIODS = {
        "daily": 10,
        "weekly": 5,
        "monthly": 4,
        "quarterly": 3,
        "annual": 2,
    }

    @classmethod
    def _staleness(cls, record: SeriesRecord, fetched_at_ms: int) -> Optional[Dict[str, Any]]:
        """Return a ``stale_series`` diagnostic when the latest period lags."""

        if record.metadata.get("coverage_freshness_applicable", True) is False:
            return None
        allowed = cls.STALE_AFTER_PERIODS.get(str(record.frequency))
        periods = [obs.period for obs in record.observations if obs.value is not None]
        if allowed is None or not periods:
            return None
        acquired = record.metadata.get("acquired_at_ms") or fetched_at_ms
        try:
            today = datetime.fromtimestamp(int(acquired) / 1000, tz=timezone.utc).date()
        except (TypeError, ValueError, OverflowError, OSError):
            return None
        lag = _period_lag(max(periods), str(record.frequency), today)
        if lag is None or lag <= allowed:
            return None
        return {
            "code": "stale_series",
            "series_id": record.series_id,
            "last_period": max(periods),
            "lag_periods": lag,
            "allowed_lag_periods": allowed,
        }

    @staticmethod
    def _diagnostic_code(exc: Exception) -> str:
        """Return a safe stable error code without exposing provider messages."""

        candidate = getattr(exc, "code", None) or type(exc).__name__
        code = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(candidate))[:80]
        return code or "provider_error"

    def _on_error(self, stage: str, ref: SeriesRef) -> None:
        logging.getLogger(self.__class__.__module__).warning(
            "%s: %s stage failed for %s", self.__class__.__name__, stage, ref.locator,
            exc_info=True,
        )
