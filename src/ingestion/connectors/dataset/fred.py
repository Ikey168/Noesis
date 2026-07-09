"""
FRED dataset connector (Track A / A5).

Harvests Federal Reserve Economic Data (FRED) series into ``dataset-series-v1``
records. FRED is **key-gated**: it needs ``FRED_API_KEY``. Following the
skip-with-warning discipline, a harvest with no key configured yields nothing
(and logs a warning) rather than failing — so an unconfigured deployment stays
green.

Two endpoints are used: ``fred/series`` for the header (title, unit, frequency)
and ``fred/series/observations`` for the values. The HTTP getter is injectable
so both are exercised offline in tests.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from services.ingest.common.series_model import Observation, SeriesRecord
from src.ingestion.connectors.dataset.base import DatasetConnector, RawSeries, SeriesRef
from src.ingestion.connectors.dataset.normalize import normalize_frequency, normalize_geography, normalize_unit

logger = logging.getLogger(__name__)

_API_BASE = "https://api.stlouisfed.org/fred"
_LICENSE = "FRED terms of use (attribution required)"

# A FRED spec is a series code, optionally with an explicit geography.
FredSpec = Union[str, Dict[str, str]]


def _default_http_get(url: str) -> str:
    import urllib.request

    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed API host
        return resp.read().decode("utf-8")


def _period_for(date: str, frequency: str) -> str:
    """Normalize a FRED YYYY-MM-DD observation date to the contract period form."""
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return date
    if frequency == "annual":
        return f"{dt.year}"
    if frequency == "quarterly":
        return f"{dt.year}-Q{((dt.month - 1) // 3) + 1}"
    if frequency in ("monthly", "weekly", "daily"):
        return f"{dt.year}-{dt.month:02d}" if frequency == "monthly" else date
    return date


def _now_millis() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class FredConnector(DatasetConnector):
    """Harvest FRED series (key-gated)."""

    provider = "fred"

    def __init__(
        self,
        api_key: Optional[str] = None,
        http_get: Optional[Callable[[str], str]] = None,
    ):
        self._api_key = api_key if api_key is not None else os.environ.get("FRED_API_KEY")
        self._http_get = http_get or _default_http_get

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def discover(self, query: Optional[Union[FredSpec, Iterable[FredSpec]]] = None) -> Iterable[SeriesRef]:
        if not self.configured:
            logger.warning("FredConnector: no FRED_API_KEY configured — skipping harvest")
            return
        if query is None:
            return
        specs: Iterable[FredSpec]
        if isinstance(query, (str, dict)):
            specs = [query]
        else:
            specs = list(query)
        for spec in specs:
            if isinstance(spec, dict):
                code = str(spec["series"])
                geo = spec.get("geography")
            else:
                code, geo = str(spec), None
            yield SeriesRef(locator=code, metadata={"series": code, "geography": geo})

    def _series_url(self, code: str) -> str:
        return f"{_API_BASE}/series?series_id={code}&api_key={self._api_key}&file_type=json"

    def _obs_url(self, code: str) -> str:
        return f"{_API_BASE}/series/observations?series_id={code}&api_key={self._api_key}&file_type=json"

    def fetch(self, ref: SeriesRef) -> RawSeries:
        code = ref.metadata["series"]
        header = self._http_get(self._series_url(code))
        observations = self._http_get(self._obs_url(code))
        # Bundle both payloads; parse() splits them.
        payload = json.dumps({"header": json.loads(header), "observations": json.loads(observations)})
        return RawSeries(ref=ref, content=payload, content_type="application/json", source_url=self._obs_url(code))

    def parse(self, raw: RawSeries) -> List[SeriesRecord]:
        bundle = json.loads(raw.content if isinstance(raw.content, str) else raw.content.decode("utf-8"))
        header_payload = bundle.get("header", {})
        seriess = header_payload.get("seriess") or []
        if not seriess:
            return []
        meta: Dict[str, Any] = seriess[0]
        code = meta.get("id") or raw.ref.metadata.get("series")
        title = meta.get("title") or code
        frequency = normalize_frequency(meta.get("frequency") or meta.get("frequency_short"))
        unit = normalize_unit(meta.get("units") or meta.get("units_short"))
        geography = normalize_geography(raw.ref.metadata.get("geography"))
        last_updated = meta.get("last_updated")
        as_of = _parse_updated(last_updated) or raw.fetched_at

        observations: List[Observation] = []
        for row in bundle.get("observations", {}).get("observations", []):
            date = row.get("date")
            if not date:
                continue
            raw_val = row.get("value")
            # FRED marks missing values as ".".
            value = None if raw_val in (None, ".", "") else _to_float(raw_val)
            observations.append(Observation(period=_period_for(date, frequency), value=value))
        observations.sort(key=lambda o: o.period)

        series_id = f"fred:{code}:{geography}" if geography else f"fred:{code}"
        return [
            SeriesRecord(
                series_id=series_id,
                provider=self.provider,
                title=title,
                frequency=frequency,
                as_of=as_of,
                observations=observations,
                unit=unit,
                geography=geography,
                license=_LICENSE,
                source_url=raw.source_url,
                metadata={"fred_id": code},
            )
        ]


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_updated(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    text = value.strip()
    # FRED last_updated looks like "2025-01-10 07:31:02-06".
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None
