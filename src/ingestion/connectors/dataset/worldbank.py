"""
World Bank dataset connector (Track A, first provider).

Harvests World Bank Open Data indicator series (no API key required, global
coverage) into ``dataset-series-v1`` records. The World Bank v2 API returns a
two-element JSON array: ``[pagination, observations]``.

The HTTP getter is injectable so ``fetch`` can be exercised without the network
in tests; ``parse`` is pure (raw JSON text -> ``SeriesRecord``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from services.ingest.common.series_model import Observation, SeriesRecord
from src.ingestion.connectors.dataset.base import DatasetConnector, RawSeries, SeriesRef
from src.ingestion.connectors.dataset.normalize import (
    normalize_geography,
    normalize_unit,
)

_API_BASE = "https://api.worldbank.org/v2"
_LICENSE = "CC-BY-4.0"

# discover() accepts a single spec or an iterable of them. A spec is either a
# (indicator, geography) pair or a {"indicator", "geography"} mapping.
IndicatorSpec = Union[Tuple[str, str], Dict[str, str]]


def _default_http_get(url: str) -> str:
    import urllib.request

    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed API host
        return resp.read().decode("utf-8")


def _iso_date_to_millis(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def _spec_to_pair(spec: IndicatorSpec) -> Tuple[str, str]:
    if isinstance(spec, dict):
        return str(spec["indicator"]), str(spec["geography"])
    indicator, geography = spec
    return str(indicator), str(geography)


class WorldBankConnector(DatasetConnector):
    """Harvest World Bank indicator series (annual, no key)."""

    provider = "worldbank"

    def __init__(self, http_get: Optional[Callable[[str], str]] = None, per_page: int = 20000):
        self._http_get = http_get or _default_http_get
        self._per_page = per_page

    def discover(self, query: Optional[Union[IndicatorSpec, Iterable[IndicatorSpec]]] = None) -> Iterable[SeriesRef]:
        """Yield a SeriesRef per (indicator, geography) spec."""
        if query is None:
            return
        specs: Iterable[IndicatorSpec]
        if isinstance(query, dict) or (isinstance(query, tuple) and len(query) == 2 and all(isinstance(x, str) for x in query)):
            specs = [query]  # a single spec
        else:
            specs = list(query)
        for spec in specs:
            indicator, geography = _spec_to_pair(spec)
            yield SeriesRef(
                locator=f"{indicator}/{geography}",
                metadata={"indicator": indicator, "geography": geography},
            )

    def _url(self, indicator: str, geography: str) -> str:
        return (
            f"{_API_BASE}/country/{geography}/indicator/{indicator}"
            f"?format=json&per_page={self._per_page}"
        )

    def fetch(self, ref: SeriesRef) -> RawSeries:
        indicator = ref.metadata["indicator"]
        geography = ref.metadata["geography"]
        url = self._url(indicator, geography)
        return RawSeries(
            ref=ref,
            content=self._http_get(url),
            content_type="application/json",
            source_url=url,
        )

    def parse(self, raw: RawSeries) -> List[SeriesRecord]:
        payload = json.loads(raw.content if isinstance(raw.content, str) else raw.content.decode("utf-8"))
        if not isinstance(payload, list) or len(payload) < 2 or payload[1] is None:
            # World Bank signals "no data" with a message page and null body.
            return []
        page_meta: Dict[str, Any] = payload[0] if isinstance(payload[0], dict) else {}
        rows: List[Dict[str, Any]] = payload[1]
        if not rows:
            return []

        first = rows[0]
        indicator = (first.get("indicator") or {}).get("id") or raw.ref.metadata.get("indicator")
        indicator_name = (first.get("indicator") or {}).get("value") or indicator
        country_name = (first.get("country") or {}).get("value")
        geo_raw = (first.get("country") or {}).get("id") or first.get("countryiso3code") or raw.ref.metadata.get("geography")
        geography = normalize_geography(geo_raw)

        title = f"{indicator_name} - {country_name}" if country_name else str(indicator_name)
        unit = normalize_unit("percent" if "%" in str(indicator_name) else first.get("unit") or None)
        as_of = _iso_date_to_millis(page_meta.get("lastupdated")) or raw.fetched_at

        observations: List[Observation] = []
        for row in rows:
            period = row.get("date")
            if not period:
                continue
            value = row.get("value")
            observations.append(Observation(period=str(period), value=value))
        # World Bank returns most-recent-first; store ascending by period.
        observations.sort(key=lambda o: o.period)

        series_id = f"wb:{indicator}:{geography}" if geography else f"wb:{indicator}"
        return [
            SeriesRecord(
                series_id=series_id,
                provider=self.provider,
                title=title,
                frequency="annual",
                as_of=as_of,
                observations=observations,
                unit=unit,
                geography=geography,
                license=_LICENSE,
                source_url=raw.source_url,
                metadata={"indicator": indicator},
            )
        ]
