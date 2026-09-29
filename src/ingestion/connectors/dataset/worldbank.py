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
from urllib.parse import urlencode

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

    def __init__(
        self,
        http_get: Optional[Callable[[str], str]] = None,
        per_page: int = 20000,
        max_pages: int = 20,
        max_observations: int = 100000,
    ):
        if type(per_page) is not int or not 1 <= per_page <= 20000:
            raise ValueError("per_page must be between 1 and 20000")
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100")
        if type(max_observations) is not int or not 1 <= max_observations <= 1000000:
            raise ValueError("max_observations must be between 1 and 1000000")
        self._http_get = http_get or _default_http_get
        self._per_page = per_page
        self._max_pages = max_pages
        self._max_observations = max_observations

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

    def _url(self, indicator: str, geography: str, page: int = 1) -> str:
        query = urlencode(
            {"format": "json", "per_page": self._per_page, "page": page}
        )
        return f"{_API_BASE}/country/{geography}/indicator/{indicator}?{query}"

    def fetch(self, ref: SeriesRef) -> RawSeries:
        indicator = ref.metadata["indicator"]
        geography = ref.metadata["geography"]
        url = self._url(indicator, geography)
        first_content = self._http_get(url)
        first_payload = json.loads(first_content)
        if not isinstance(first_payload, list) or len(first_payload) < 2:
            return RawSeries(
                ref=ref,
                content=first_content,
                content_type="application/json",
                source_url=url,
            )

        page_meta = first_payload[0] if isinstance(first_payload[0], dict) else {}
        try:
            pages = int(page_meta.get("pages") or 1)
        except (TypeError, ValueError) as exc:
            raise ValueError("World Bank pagination metadata is invalid") from exc
        if pages < 1 or pages > self._max_pages:
            raise ValueError("World Bank response exceeds the configured page budget")
        rows = list(first_payload[1] or [])
        if len(rows) > self._max_observations:
            raise ValueError("World Bank response exceeds the observation budget")
        page_urls = [url]
        for page in range(2, pages + 1):
            page_url = self._url(indicator, geography, page=page)
            page_payload = json.loads(self._http_get(page_url))
            if not isinstance(page_payload, list) or len(page_payload) < 2:
                raise ValueError("World Bank returned an invalid subsequent page")
            if page_payload[1] is not None:
                if not isinstance(page_payload[1], list):
                    raise ValueError("World Bank returned malformed observations")
                rows.extend(page_payload[1])
                if len(rows) > self._max_observations:
                    raise ValueError("World Bank response exceeds the observation budget")
            page_urls.append(page_url)
        ref.metadata["pagination"] = {
            "page_count": pages,
            "pages_retrieved": len(page_urls),
            "total": page_meta.get("total"),
            "per_page": page_meta.get("per_page"),
            "complete": len(page_urls) == pages,
            "source_page_urls": page_urls,
        }
        payload = json.dumps([page_meta, rows])
        return RawSeries(
            ref=ref,
            content=payload,
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
        observation_metadata: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            period = row.get("date")
            if not period:
                continue
            value = row.get("value")
            observations.append(Observation(period=str(period), value=value))
            observation_metadata[str(period)] = {
                "decimal": row.get("decimal"),
                "obs_status": row.get("obs_status"),
                "unit": row.get("unit"),
            }
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
                metadata={
                    "indicator": indicator,
                    "provider_code": indicator,
                    "provider_release_at_ms": None,
                    "provider_release_time_status": "World Bank indicator response has no release timestamp",
                    "provider_vintage_ms": as_of,
                    "vintage_id": f"{indicator}:{geography}@{as_of}",
                    "vintage_basis": "world_bank_lastupdated"
                    if page_meta.get("lastupdated")
                    else "retrieval_time_fallback",
                    "provider_updated_at_ms": _iso_date_to_millis(
                        page_meta.get("lastupdated")
                    ),
                    "acquired_at_ms": raw.fetched_at,
                    "pagination": raw.ref.metadata.get("pagination", {}),
                    "observation_metadata": observation_metadata,
                    "seasonal_adjustment": "unknown",
                },
            )
        ]
