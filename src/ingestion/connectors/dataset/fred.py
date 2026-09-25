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
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Union
from urllib.parse import urlencode

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


def _date_millis(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return int(datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc).timestamp() * 1000)


def _seasonal_adjustment(value: Any, short_value: Any) -> str:
    token = f"{value or ''} {short_value or ''}".casefold()
    short = str(short_value or "").strip().upper()
    if "not seasonally adjusted" in token or short in {"NSA", "NSA-S"}:
        return "not_adjusted"
    if "seasonally adjusted" in token or short in {"SA", "SAAR"}:
        return "adjusted"
    return "unknown"


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
                unknown = set(spec) - {
                    "series",
                    "geography",
                    "release_id",
                    "vintage_date",
                    "observation_start",
                    "observation_end",
                }
                if unknown or not spec.get("series"):
                    raise ValueError("FRED specs require series and supported optional fields")
                code = str(spec["series"])
                geo = spec.get("geography")
                release_id = spec.get("release_id")
                vintage_date = spec.get("vintage_date")
                observation_start = spec.get("observation_start")
                observation_end = spec.get("observation_end")
            else:
                code, geo = str(spec), None
                release_id = None
                vintage_date = observation_start = observation_end = None
            if release_id is not None:
                if type(release_id) is bool or not isinstance(release_id, (str, int)):
                    raise ValueError("release_id must be a positive integer")
                try:
                    release_id = int(release_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError("release_id must be a positive integer") from exc
                if release_id < 1:
                    raise ValueError("release_id must be a positive integer")
            if vintage_date is not None and _date_millis(str(vintage_date)) is None:
                raise ValueError("vintage_date must be a real YYYY-MM-DD date")
            for name, value in (
                ("observation_start", observation_start),
                ("observation_end", observation_end),
            ):
                if value is not None and _date_millis(str(value)) is None:
                    raise ValueError(f"{name} must be a real YYYY-MM-DD date")
            yield SeriesRef(
                locator=code,
                metadata={
                    "series": code,
                    "geography": geo,
                    "release_id": str(release_id) if release_id is not None else None,
                    "vintage_date": str(vintage_date) if vintage_date else None,
                    "observation_start": str(observation_start) if observation_start else None,
                    "observation_end": str(observation_end) if observation_end else None,
                },
            )

    def _params(self, code: str, ref: SeriesRef) -> Dict[str, str]:
        params = {"series_id": code, "api_key": str(self._api_key), "file_type": "json"}
        vintage_date = ref.metadata.get("vintage_date")
        if vintage_date:
            params.update({"realtime_start": vintage_date, "realtime_end": vintage_date})
        return params

    def _series_url(self, code: str, ref: SeriesRef) -> str:
        return f"{_API_BASE}/series?{urlencode(self._params(code, ref))}"

    def _obs_url(
        self, code: str, ref: SeriesRef, *, include_api_key: bool = True
    ) -> str:
        params = self._params(code, ref)
        params["output_type"] = "1"
        if not include_api_key:
            params.pop("api_key", None)
        if ref.metadata.get("observation_start"):
            params["observation_start"] = ref.metadata["observation_start"]
        if ref.metadata.get("observation_end"):
            params["observation_end"] = ref.metadata["observation_end"]
        return f"{_API_BASE}/series/observations?{urlencode(params)}"

    def release_dates(
        self,
        release_id: Union[str, int],
        *,
        realtime_start: str = "1776-07-04",
        realtime_end: str = "9999-12-31",
        include_release_dates_with_no_data: bool = False,
        limit: int = 10000,
    ) -> Dict[str, Any]:
        """Fetch FRED's source-published dates for one release.

        The provider supplies calendar dates, not time-of-day availability.
        This endpoint can omit future dates unless
        ``include_release_dates_with_no_data`` is enabled, so the returned
        coverage label is explicit and must not be read as a market-ready
        release timestamp.
        """

        if not self.configured:
            raise ValueError("FRED release calendar requires FRED_API_KEY")
        if type(release_id) is bool or not isinstance(release_id, (str, int)):
            raise ValueError("release_id must be a positive integer")
        try:
            release_number = int(release_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("release_id must be a positive integer") from exc
        if release_number < 1 or type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("release_id and limit are outside supported bounds")
        for name, value in (("realtime_start", realtime_start), ("realtime_end", realtime_end)):
            if _date_millis(value) is None:
                raise ValueError(f"{name} must be a real YYYY-MM-DD date")
        if realtime_start > realtime_end:
            raise ValueError("realtime_start must not be after realtime_end")
        params = {
            "release_id": str(release_number),
            "api_key": str(self._api_key),
            "file_type": "json",
            "realtime_start": realtime_start,
            "realtime_end": realtime_end,
            "include_release_dates_with_no_data": str(
                bool(include_release_dates_with_no_data)
            ).lower(),
            "limit": str(limit),
        }
        endpoint = f"{_API_BASE}/release/dates"
        payload = json.loads(self._http_get(f"{endpoint}?{urlencode(params)}"))
        raw_dates = payload.get("release_dates") or []
        dates = []
        for item in raw_dates[:limit]:
            date_value = str(item.get("date") or "")
            date_ms = _date_millis(date_value)
            if date_ms is None:
                continue
            dates.append(
                {
                    "date": date_value,
                    "date_ms": date_ms,
                    "precision": "day",
                    "source_release_id": str(item.get("release_id") or release_number),
                }
            )
        expected_count = int(payload.get("count", len(raw_dates)))
        truncated = expected_count > len(dates) or len(raw_dates) > limit
        return {
            "provider": "fred",
            "release_id": str(release_number),
            "dates": dates,
            "retrieved_at_ms": _now_millis(),
            "source_endpoint": endpoint,
            "coverage": "partial" if truncated else "provider_returned_dates",
            "truncated": truncated,
            "include_release_dates_with_no_data": include_release_dates_with_no_data,
            "release_time_precision": "date_only",
            "limitations": [
                "Provider release dates do not specify time-of-day availability",
                "Future calendar dates may be excluded unless requested and available from the provider",
            ],
        }

    def series_releases(
        self,
        series_id: str,
        *,
        realtime_start: Optional[str] = None,
        realtime_end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve the FRED releases associated with a series ID."""

        if not self.configured:
            raise ValueError("FRED series release lookup requires FRED_API_KEY")
        if not isinstance(series_id, str) or not series_id.strip() or len(series_id) > 100:
            raise ValueError("series_id must be nonempty bounded text")
        params = {
            "series_id": series_id.strip(),
            "api_key": str(self._api_key),
            "file_type": "json",
        }
        for name, value in (
            ("realtime_start", realtime_start),
            ("realtime_end", realtime_end),
        ):
            if value is not None:
                if _date_millis(value) is None:
                    raise ValueError(f"{name} must be a real YYYY-MM-DD date")
                params[name] = value
        if (
            realtime_start is not None
            and realtime_end is not None
            and realtime_start > realtime_end
        ):
            raise ValueError("realtime_start must not be after realtime_end")
        endpoint = f"{_API_BASE}/series/release"
        payload = json.loads(self._http_get(f"{endpoint}?{urlencode(params)}"))
        releases = [
            {
                "release_id": str(item.get("id")),
                "name": item.get("name"),
                "press_release": item.get("press_release"),
                "link": item.get("link"),
                "realtime_start": item.get("realtime_start"),
                "realtime_end": item.get("realtime_end"),
            }
            for item in payload.get("releases", [])
            if item.get("id") is not None
        ]
        return {
            "provider": "fred",
            "series_id": series_id.strip(),
            "releases": releases,
            "retrieved_at_ms": _now_millis(),
            "source_endpoint": endpoint,
            "coverage": "provider_returned_releases",
            "limitations": [
                "Release membership does not establish an exact value publication time",
                "Use release_dates() for source-published dates and keep its day precision",
            ],
        }

    def fetch(self, ref: SeriesRef) -> RawSeries:
        code = ref.metadata["series"]
        header = self._http_get(self._series_url(code, ref))
        observations = self._http_get(self._obs_url(code, ref))
        # Bundle both payloads; parse() splits them.
        payload = json.dumps({"header": json.loads(header), "observations": json.loads(observations)})
        return RawSeries(
            ref=ref,
            content=payload,
            content_type="application/json",
            source_url=self._obs_url(code, ref, include_api_key=False),
        )

    def _on_error(self, stage: str, ref: SeriesRef) -> None:
        # urllib exceptions can include the credential-bearing request URL.
        logger.warning("FredConnector: %s stage failed for %s", stage, ref.locator)

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
        requested_vintage = raw.ref.metadata.get("vintage_date")
        provider_realtime_start = requested_vintage or meta.get("realtime_start")
        as_of = (
            _date_millis(str(provider_realtime_start))
            or _parse_updated(last_updated)
            or raw.fetched_at
        )

        observations: List[Observation] = []
        observation_realtime_periods: Dict[str, Dict[str, Optional[str]]] = {}
        for row in bundle.get("observations", {}).get("observations", []):
            date = row.get("date")
            if not date:
                continue
            raw_val = row.get("value")
            # FRED marks missing values as ".".
            value = None if raw_val in (None, ".", "") else _to_float(raw_val)
            observations.append(Observation(period=_period_for(date, frequency), value=value))
            observation_realtime_periods[date] = {
                "start": row.get("realtime_start"),
                "end": row.get("realtime_end"),
            }
        observations.sort(key=lambda o: o.period)

        series_id = f"fred:{code}:{geography}" if geography else f"fred:{code}"
        vintage_id = (
            f"{code}@{provider_realtime_start}"
            if provider_realtime_start
            else f"{code}@{as_of}"
        )
        provider_release_id = meta.get("release_id") or raw.ref.metadata.get("release_id")
        release_id_provenance = (
            "provider_response"
            if meta.get("release_id") is not None
            else "caller_supplied"
            if raw.ref.metadata.get("release_id") is not None
            else "unavailable"
        )
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
                metadata={
                    "fred_id": code,
                    "provider_code": code,
                    "provider_release_id": str(provider_release_id)
                    if provider_release_id is not None
                    else None,
                    "provider_release_id_provenance": release_id_provenance,
                    "provider_release_at_ms": None,
                    "provider_release_time_status": "release timestamp not returned by series endpoints",
                    "provider_vintage_ms": as_of,
                    "vintage_id": vintage_id,
                    "vintage_basis": "explicit_alfred_realtime_period"
                    if requested_vintage
                    else "fred_current_realtime_period"
                    if provider_realtime_start
                    else "series_last_updated_fallback",
                    "vintage_date": provider_realtime_start,
                    "coverage_freshness_applicable": requested_vintage is None
                    and raw.ref.metadata.get("observation_end") is None,
                    "acquired_at_ms": raw.fetched_at,
                    "provider_last_updated_ms": _parse_updated(last_updated),
                    "observation_start": meta.get("observation_start"),
                    "observation_end": meta.get("observation_end"),
                    "frequency_short": meta.get("frequency_short"),
                    "units_short": meta.get("units_short"),
                    "seasonal_adjustment": _seasonal_adjustment(
                        meta.get("seasonal_adjustment"),
                        meta.get("seasonal_adjustment_short"),
                    ),
                    "seasonal_adjustment_source": meta.get("seasonal_adjustment"),
                    "notes": meta.get("notes"),
                    "observation_realtime_periods": observation_realtime_periods,
                },
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
