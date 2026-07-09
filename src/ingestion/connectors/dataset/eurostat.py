"""
Eurostat dataset connector (Track A / A6).

Harvests Eurostat series into ``dataset-series-v1`` records over the Eurostat
dissemination API, which returns **JSON-stat 2.0** — a dimensioned cube with a
flattened ``value`` map. This connector extracts a single time series from the
cube for a chosen geography, computing the flat index over the dimension order.

No API key is required. The geography codes Eurostat uses at country level are
already ISO 3166 alpha-2 (``DE``, ``FR``), so a claim about "Germany" (which the
quantity extractor codes ``DE``) resolves against them directly; NUTS region
codes (``DE1``) pass through unchanged.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from services.ingest.common.series_model import Observation, SeriesRecord
from src.ingestion.connectors.dataset.base import DatasetConnector, RawSeries, SeriesRef
from src.ingestion.connectors.dataset.normalize import normalize_frequency, normalize_geography, normalize_unit

_API_BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
_LICENSE = "Eurostat (reuse permitted with attribution)"

# A Eurostat spec is a dataset code plus a geography (and optional extra filters).
EurostatSpec = Union[Dict[str, Any]]

_QUARTER_RE = re.compile(r"^(\d{4})[-_]?Q([1-4])$", re.I)
_MONTH_RE = re.compile(r"^(\d{4})[-_]?M?(\d{2})$")


def _default_http_get(url: str) -> str:
    import urllib.request

    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed API host
        return resp.read().decode("utf-8")


def _normalize_period(raw: str, frequency: str) -> str:
    """Normalize a Eurostat time code to the contract period form."""
    m = _QUARTER_RE.match(raw)
    if m:
        return f"{m.group(1)}-Q{m.group(2)}"
    if frequency == "monthly":
        m = _MONTH_RE.match(raw)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return raw


def _strides(size: List[int]) -> List[int]:
    """Row-major strides for a JSON-stat ``size`` vector."""
    strides = [1] * len(size)
    for i in range(len(size) - 2, -1, -1):
        strides[i] = strides[i + 1] * size[i + 1]
    return strides


class EurostatConnector(DatasetConnector):
    """Harvest Eurostat series from JSON-stat 2.0 (no key)."""

    provider = "eurostat"

    def __init__(self, http_get: Optional[Callable[[str], str]] = None):
        self._http_get = http_get or _default_http_get

    def discover(self, query: Optional[Union[EurostatSpec, Iterable[EurostatSpec]]] = None) -> Iterable[SeriesRef]:
        if query is None:
            return
        specs: Iterable[EurostatSpec]
        if isinstance(query, dict):
            specs = [query]
        else:
            specs = list(query)
        for spec in specs:
            dataset = str(spec["dataset"])
            geography = str(spec["geography"])
            filters = {k: v for k, v in spec.items() if k not in ("dataset", "geography")}
            yield SeriesRef(
                locator=f"{dataset}/{geography}",
                metadata={"dataset": dataset, "geography": geography, "filters": filters},
            )

    def _url(self, dataset: str, geography: str, filters: Dict[str, Any]) -> str:
        params = [f"format=JSON", f"geo={geography}"]
        for k, v in filters.items():
            params.append(f"{k}={v}")
        return f"{_API_BASE}/{dataset}?{'&'.join(params)}"

    def fetch(self, ref: SeriesRef) -> RawSeries:
        url = self._url(ref.metadata["dataset"], ref.metadata["geography"], ref.metadata.get("filters", {}))
        return RawSeries(ref=ref, content=self._http_get(url), content_type="application/json", source_url=url)

    def parse(self, raw: RawSeries) -> List[SeriesRecord]:
        cube = json.loads(raw.content if isinstance(raw.content, str) else raw.content.decode("utf-8"))
        dim_ids: List[str] = cube.get("id") or []
        size: List[int] = cube.get("size") or []
        dimension: Dict[str, Any] = cube.get("dimension") or {}
        values: Dict[str, Any] = cube.get("value") or {}
        if not dim_ids or not size or "time" not in dim_ids:
            return []

        strides = _strides(size)
        # Fixed index per non-time dimension (singletons; else index 0 with a note).
        fixed: Dict[str, int] = {}
        picked_labels: Dict[str, str] = {}
        multi_dims: List[str] = []
        for d in dim_ids:
            if d == "time":
                continue
            cats = (dimension.get(d, {}).get("category", {}) or {})
            index_map = cats.get("index", {}) or {}
            if not index_map:
                fixed[d] = 0
                continue
            if len(index_map) > 1:
                multi_dims.append(d)
            # Pick the lowest index (the first category).
            code, idx = min(index_map.items(), key=lambda kv: kv[1])
            fixed[d] = idx
            label = (cats.get("label", {}) or {}).get(code, code)
            picked_labels[d] = label

        time_cats = dimension.get("time", {}).get("category", {}) or {}
        time_index = time_cats.get("index", {}) or {}
        time_labels = time_cats.get("label", {}) or {}
        # Frequency from the freq dimension if present, else inferred from labels.
        freq_code = None
        if "freq" in picked_labels:
            freq_code = picked_labels["freq"]
        frequency = normalize_frequency(freq_code) if freq_code else _infer_frequency(list(time_index))

        unit = normalize_unit(picked_labels.get("unit"))
        geography = normalize_geography(raw.ref.metadata.get("geography"))
        title = cube.get("label") or raw.ref.metadata["dataset"]
        as_of = _parse_updated(cube.get("updated")) or raw.fetched_at

        time_pos = dim_ids.index("time")
        observations: List[Observation] = []
        # Iterate time categories in index order.
        for period_code, t_idx in sorted(time_index.items(), key=lambda kv: kv[1]):
            flat = t_idx * strides[time_pos]
            for d in dim_ids:
                if d == "time":
                    continue
                flat += fixed[d] * strides[dim_ids.index(d)]
            value = values.get(str(flat))
            observations.append(
                Observation(period=_normalize_period(period_code, frequency), value=value if value is not None else None)
            )
        observations.sort(key=lambda o: o.period)

        dataset = raw.ref.metadata["dataset"]
        series_id = f"estat:{dataset}:{geography}" if geography else f"estat:{dataset}"
        metadata: Dict[str, Any] = {"dataset": dataset}
        if multi_dims:
            metadata["collapsed_dimensions"] = {d: picked_labels.get(d) for d in multi_dims}
        return [
            SeriesRecord(
                series_id=series_id,
                provider=self.provider,
                title=str(title),
                frequency=frequency,
                as_of=as_of,
                observations=observations,
                unit=unit,
                geography=geography,
                license=_LICENSE,
                source_url=raw.source_url,
                metadata=metadata,
            )
        ]


def _infer_frequency(time_codes: List[str]) -> str:
    for code in time_codes:
        if _QUARTER_RE.match(code):
            return "quarterly"
        if _MONTH_RE.match(code) and len(code.replace("M", "").replace("-", "")) == 6:
            return "monthly"
    return "annual"


def _parse_updated(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None
