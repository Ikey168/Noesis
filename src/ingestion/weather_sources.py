"""Bounded acquisition of operational weather records (WX03-WX06, #2166-#2169).

One ``weather`` native connector serves every Weather source under the
source-pack runtime (``src/ingestion/source_pack_runtime.py``), one plan step
per runtime page:

* **DWD CDC** (WX03): station descriptions, 10-minute and hourly
  ``air_temperature`` products with the ``QN`` quality level kept verbatim, and
  the ``Metadaten_Geographie`` location history inside each product ZIP. The
  DWD contract, the host allow-list (``environment_providers.PROVIDER_HOSTS``),
  the hourly request plan and the hourly/station parsers are the Climate &
  Environment ones (:func:`environment_providers.plan`,
  :func:`environment_providers.parse_dwd_product`,
  :func:`environment_providers.parse_dwd_stations`). There is no second DWD
  client. Only the 10-minute product layout is parsed here.
* **DWD MOSMIX_L and CAP warnings** (WX04): one ``forecast_issuance`` per
  station and run from the single-station KMZ, and one ``warning`` per CAP
  message from the latest-state ZIP, bounded to declared warncells.
* **aviationweather.gov and the NWS API** (WX05): METAR with the raw text kept
  verbatim and ``COR`` corrections as revisions, TAF issuances (raw text and
  validity period only), station info as source-stated cross-identifiers, NWS
  ``/points`` grid mapping, gridpoint hourly forecasts and alerts. Every
  request sends the declared ``User-Agent``.
* **Open-Meteo** (WX06): a pinned-model forecast run as an issuance vintage,
  through the Climate adapter's ``open-meteo-forecast`` plan and parsers.

Parsers never invent values. A missing value is absent, with the source's
marker. A response whose shape differs raises ``ProviderError('schema_drift')``.
Each item in a document (a placemark, a CAP message, a METAR) is parsed on its
own and never inherits a value from the item before it. Items outside the
declared selection are counted in the page receipt, not stored.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from src.ingestion import environment_providers as ep
from src.ingestion.provider_execution import ProviderError, canonical
from src.kb import weather_records as wr

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_SCHEMA = "noesis-weather-record-v1"
IDENTIFIER_CONTRACT = "noesis-weather-station-identifier-v1"
CONNECTOR = "weather"
FIXTURE_SECRET = None
DEFAULT_USER_AGENT = (
    "noesis-weather-pack/1.0 (operator contact declared in the source pack)"
)
MAX_STEPS = 24
PROVIDER_HOSTS = {
    # DWD reuses the Climate & Environment allow-list; the MOSMIX catalogue is on www.dwd.de (verify).
    "dwd-cdc": set(ep.PROVIDER_HOSTS["dwd"]),
    "dwd-mosmix": set(ep.PROVIDER_HOSTS["dwd"]) | {"www.dwd.de"},
    "dwd-cap": set(ep.PROVIDER_HOSTS["dwd"]),
    "aviationweather": {"aviationweather.gov"},
    "nws": {"api.weather.gov"},
    "open-meteo": set(ep.PROVIDER_HOSTS["open-meteo-forecast"]),
}
_AUDIT = "docs/development/weather-evidence/source-audit.md"
PROVIDER_CONTRACTS = {
    "dwd-cdc": {
        "reuses": "environment_providers.PROVIDER_CONTRACTS['dwd']",
        "decision": "implement",
        "attribution": wr.ATTRIBUTION["dwd-cdc"],
        "audit": _AUDIT,
        "corrections": "historical release outranks recent; a revised value appends a revision",
    },
    "dwd-mosmix": {
        "decision": "implement (MOSMIX_L single stations; MOSMIX_S not implemented)",
        "attribution": wr.ATTRIBUTION["dwd-mosmix"],
        "audit": _AUDIT,
        "issuance": "IssueTime per run; each run is a new issuance, never an overwrite",
    },
    "dwd-cap": {
        "decision": "implement",
        "attribution": wr.ATTRIBUTION["dwd-cap"],
        "audit": _AUDIT,
        "corrections": "msgType Update/Cancel threads to the referenced messages at read time",
    },
    "aviationweather": {
        "decision": "implement (METAR, TAF issuance, station info; TAF change groups not implemented)",
        "attribution": wr.ATTRIBUTION["aviationweather"],
        "audit": _AUDIT,
        "user_agent": "required by policy (verify); always sent",
        "corrections": "METAR COR appended as a revision ordered by receiptTime",
    },
    "nws": {
        "decision": "implement (points, gridpoint hourly forecast, alerts; station observations link-only)",
        "attribution": wr.ATTRIBUTION["nws"],
        "audit": _AUDIT,
        "user_agent": "required (verify); always sent",
        "issuance": "updateTime is the issuance time; generatedAt recorded separately",
    },
    "open-meteo": {
        "reuses": "environment_providers.PROVIDER_CONTRACTS['open-meteo-forecast']",
        "decision": "implement behind the default-off weather-open-meteo feature (non-commercial terms)",
        "attribution": wr.ATTRIBUTION["open-meteo"],
        "audit": _AUDIT,
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": "unverified-live",
        "report": None,
        "note": "no dated live run yet (WX14); offline authored fixtures only",
    }
    for provider in PROVIDER_CONTRACTS
}
DWD_10MIN_COLUMNS = {
    "TT_10": "°C",
    "RF_10": "%",
    "TD_10": "°C",
    "PP_10": "hPa",
    "TM5_10": "°C",
}
# MOSMIX element units as documented by DWD (verify); probabilities are published percentages.
MOSMIX_ELEMENTS = {
    "TTT": ("K", "value"),
    "Td": ("K", "value"),
    "FF": ("m/s", "value"),
    "FX1": ("m/s", "value"),
    "DD": ("°", "value"),
    "PPPP": ("Pa", "value"),
    "RR1c": ("kg/m2", "value"),
    "N": ("%", "value"),
    "R101": ("%", "probability"),
    "wwP": ("%", "probability"),
}
METAR_FIELDS = {
    "temp": "°C",
    "dewp": "°C",
    "wspd": "kt",
    "wdir": "°",
    "wgst": "kt",
    "visib": "SM",
    "altim": "hPa",
    "slp": "hPa",
}
NWS_UNITS = {"F": "°F", "C": "°C"}
_ICAO = re.compile(r"^[A-Z][A-Z0-9]{3}$")
_DWD_ID = re.compile(r"^\d{5}$")
_MOSMIX_ID = re.compile(r"^[A-Z0-9]{4,5}$")
_WARNCELL = re.compile(r"^\d{9}$")
_UGC = re.compile(r"^[A-Z]{2}[CZ]\d{3}$")


# --------------------------------------------------------------------- helpers


def _json(content: bytes) -> Any:
    try:
        return json.loads(content, parse_float=str, parse_int=str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError(
            "schema_drift", "provider response is not valid JSON"
        ) from exc


def _decimal(value: Any, field: str) -> str | None:
    """Exact decimal text, or ``None`` for a published missing value (never a float, never 'None')."""

    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "-", "null", "NaN", "-999", "-999.0", "-999.00"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ProviderError(
            "schema_drift", f"{field} is not numeric: {text!r}"
        ) from exc
    if not number.is_finite():
        raise ProviderError("schema_drift", f"{field} is not finite")
    return text


def _utc(value: Any, field: str, *, documented_utc: bool = False) -> str:
    """A source time as UTC; ``documented_utc`` marks a provider whose offset-free times are documented as UTC."""

    text = str(value or "").strip()
    if documented_utc and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?", text
    ):
        text = text.replace(" ", "T") + "Z"
    try:
        return wr.utc(int(text) if re.fullmatch(r"\d{9,11}", text) else text)
    except wr.WeatherRecordError as exc:
        raise ProviderError("schema_drift", f"{field}: {exc}") from exc


def _build(
    factory: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any
) -> dict[str, Any]:
    try:
        return factory(*args, **kwargs)
    except wr.WeatherRecordError as exc:
        raise ProviderError("schema_drift", f"record failed validation: {exc}") from exc


def _bounded(
    values: Any, field: str, limit: int, pattern: re.Pattern[str]
) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= limit:
        raise ProviderError(
            "unbounded_selection", f"{field} must list 1-{limit} explicit items"
        )
    items = [str(v) for v in values]
    if any(not pattern.fullmatch(v) for v in items):
        raise ProviderError(
            "unbounded_selection", f"{field} has an identifier of the wrong shape"
        )
    return items


def _zip_members(content: bytes, what: str) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            if len(names) > 5000:
                raise ProviderError(
                    "schema_drift", f"{what} archive has too many members"
                )
            return {name: archive.read(name) for name in names}
    except zipfile.BadZipFile as exc:
        raise ProviderError("schema_drift", f"{what} is not a ZIP archive") from exc


def identifier_statement(
    station: dict[str, str],
    scheme: str,
    value: str,
    *,
    stated_by: str,
    locator: dict[str, Any],
) -> dict[str, Any]:
    """A source-stated identifier of a station (the basis of deterministic cross-identifier links)."""

    return {
        "contract": IDENTIFIER_CONTRACT,
        "station": wr.station_ref(station["provider"], station["native_id"]),
        "scheme": scheme,
        "value": str(value).strip().upper(),
        "stated_by": stated_by,
        "locator": {k: v for k, v in locator.items() if v is not None},
    }


def _day(text: str, field: str) -> str | None:
    text = str(text or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"\d{8}", text):
        raise ProviderError("schema_drift", f"{field} is not YYYYMMDD")
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


# ------------------------------------------------------------------ selections


def plan(provider: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Ordered, bounded request steps for one explicit selection."""

    if provider not in PROVIDER_CONTRACTS:
        raise ProviderError("not_implemented", f"{provider} has no weather adapter")
    selection = dict(selection or {})
    steps: list[dict[str, Any]] = []
    if provider == "dwd-cdc":
        stations = _bounded(selection.get("stations"), "stations", 5, _DWD_ID)
        resolution, period = selection.get("resolution"), selection.get("period")
        if resolution not in {"10_minutes", "hourly"} or period not in {
            "recent",
            "historical",
        }:
            raise ProviderError(
                "unbounded_selection",
                "DWD selections pin resolution (10_minutes|hourly) and period",
            )
        group = selection.get("parameter", "air_temperature")
        if group not in {"air_temperature", "precipitation"} or (
            group == "precipitation" and resolution != "hourly"
        ):
            raise ProviderError(
                "unbounded_selection",
                "v1 covers air_temperature (10-minute, hourly) and hourly "
                "precipitation only",
            )
        files = dict(selection.get("historical_files") or {})
        precedence = 1 if period == "historical" else 0
        for station in stations:
            if group == "precipitation":
                base = (
                    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/"
                    f"precipitation/{period}/"
                )
                name = (
                    f"stundenwerte_RR_{station}_akt.zip"
                    if period == "recent"
                    else str(files.get(station) or "")
                )
                if not re.fullmatch(rf"stundenwerte_RR_{station}_[\w]+\.zip", name):
                    raise ProviderError(
                        "unbounded_selection",
                        "historical DWD selections pin the product file name",
                    )
                steps.append(
                    {
                        "parse": "dwd_station",
                        "url": base + "RR_Stundenwerte_Beschreibung_Stationen.txt",
                        "params": {},
                        "context": {"station": station},
                    }
                )
                steps.append(
                    {
                        "parse": "dwd_hourly_rr",
                        "url": base + name,
                        "params": {},
                        "context": {
                            "station": station,
                            "period": period,
                            "precedence": precedence,
                        },
                    }
                )
                continue
            if resolution == "hourly":
                # The Climate & Environment plan and host policy, unchanged.
                for step in ep.plan(
                    "dwd",
                    {
                        "parameter": "air_temperature",
                        "station": station,
                        "period": period,
                        "historical_file": files.get(station),
                    },
                ):
                    parse = (
                        "dwd_station"
                        if step["parse"] == "dwd_stations"
                        else "dwd_hourly"
                    )
                    steps.append(
                        {
                            **step,
                            "parse": parse,
                            "context": {
                                **step["context"],
                                "precedence": precedence,
                                "period": period,
                            },
                        }
                    )
                continue
            base = (
                "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/"
                f"air_temperature/{period}/"
            )
            name = (
                f"10minutenwerte_TU_{station}_akt.zip"
                if period == "recent"
                else str(files.get(station) or "")
            )
            if not re.fullmatch(rf"10minutenwerte_TU_{station}_[\w]+\.zip", name):
                raise ProviderError(
                    "unbounded_selection",
                    "historical DWD selections pin the product file name",
                )
            columns = [str(c) for c in selection.get("columns") or ["TT_10", "RF_10"]]
            if not columns or set(columns) - set(DWD_10MIN_COLUMNS):
                raise ProviderError(
                    "unbounded_selection",
                    f"10-minute columns are a subset of {sorted(DWD_10MIN_COLUMNS)}",
                )
            steps.append(
                {
                    "parse": "dwd_station",
                    "url": base + "zehn_min_tu_Beschreibung_Stationen.txt",
                    "params": {},
                    "context": {"station": station},
                }
            )
            steps.append(
                {
                    "parse": "dwd_10min",
                    "url": base + name,
                    "params": {},
                    "context": {
                        "station": station,
                        "period": period,
                        "precedence": precedence,
                        "columns": sorted(columns),
                    },
                }
            )
    elif provider == "dwd-mosmix":
        stations = _bounded(selection.get("stations"), "stations", 5, _MOSMIX_ID)
        elements = [str(e) for e in selection.get("elements") or []]
        if not elements or set(elements) - set(MOSMIX_ELEMENTS):
            raise ProviderError(
                "unbounded_selection",
                f"MOSMIX elements are a subset of {sorted(MOSMIX_ELEMENTS)}",
            )
        if selection.get("catalogue"):
            steps.append(
                {
                    "parse": "mosmix_catalogue",
                    "params": {},
                    "context": {"stations": stations},
                    "url": "https://www.dwd.de/DE/leistungen/met_verfahren_mosmix/mosmix_stationskatalog.cfg",
                }
            )
        for station in stations:
            steps.append(
                {
                    "parse": "mosmix_kmz",
                    "params": {},
                    "context": {"station": station, "elements": sorted(elements)},
                    "url": (
                        "https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/"
                        f"{station}/kml/MOSMIX_L_LATEST_{station}.kmz"
                    ),
                }
            )
    elif provider == "dwd-cap":
        cells = _bounded(selection.get("warncells"), "warncells", 20, _WARNCELL)
        archive = str(selection.get("archive") or "DISTRICT_DWD_STAT")
        if archive not in {"DISTRICT_DWD_STAT", "COMMUNEUNION_DWD_STAT"}:
            raise ProviderError(
                "unbounded_selection",
                "CAP archive is DISTRICT_DWD_STAT or COMMUNEUNION_DWD_STAT",
            )
        flavour = "DISTRICT" if archive.startswith("DISTRICT") else "COMMUNEUNION"
        steps.append(
            {
                "parse": "dwd_cap",
                "params": {},
                "context": {
                    "warncells": sorted(cells),
                    "language": selection.get("language", "de-DE"),
                },
                "url": (
                    f"https://opendata.dwd.de/weather/alerts/cap/{archive}/"
                    f"Z_CAP_C_EDZW_LATEST_PVW_STATUS_PREMIUMDWD_{flavour}_DE.zip"
                ),
            }
        )
    elif provider == "aviationweather":
        stations = _bounded(selection.get("stations"), "stations", 10, _ICAO)
        products = [
            str(p) for p in selection.get("products") or ["stationinfo", "metar"]
        ]
        if not products or set(products) - {"stationinfo", "metar", "taf"}:
            raise ProviderError(
                "unbounded_selection",
                "aviationweather products are stationinfo, metar and taf",
            )
        hours = int(selection.get("hours", 3))
        if not 1 <= hours <= 24:
            raise ProviderError("unbounded_selection", "METAR history is 1-24 hours")
        ids = ",".join(stations)
        for product in ("stationinfo", "metar", "taf"):
            if product not in products:
                continue
            params = {
                "ids": ids,
                "format": "json",
                **({"hours": str(hours)} if product == "metar" else {}),
            }
            steps.append(
                {
                    "parse": f"awc_{product}",
                    "url": f"https://aviationweather.gov/api/data/{product}",
                    "params": params,
                    "context": {"stations": stations},
                }
            )
    elif provider == "nws":
        points = selection.get("points") or []
        zones = [str(z) for z in selection.get("alert_zones") or []]
        if (
            not isinstance(points, list)
            or len(points) > 3
            or (not points and not zones)
        ):
            raise ProviderError(
                "unbounded_selection",
                "NWS selections name 1-3 points and/or alert zones",
            )
        for point in points:
            grid = dict(point.get("grid") or {})
            lat, lon = str(point.get("latitude")), str(point.get("longitude"))
            if not re.fullmatch(r"[A-Z]{3}", str(grid.get("wfo") or "")) or not all(
                str(grid.get(k, "")).isdigit() for k in ("x", "y")
            ):
                raise ProviderError(
                    "unbounded_selection",
                    "NWS points pin their office grid (wfo, x, y)",
                )
            context = {
                "point": [lat, lon],
                "grid": {"wfo": grid["wfo"], "x": str(grid["x"]), "y": str(grid["y"])},
                "declared_station": point.get("declared_station"),
            }
            steps.append(
                {
                    "parse": "nws_points",
                    "url": f"https://api.weather.gov/points/{lat},{lon}",
                    "params": {},
                    "context": context,
                }
            )
            steps.append(
                {
                    "parse": "nws_forecast",
                    "params": {},
                    "context": context,
                    "url": (
                        f"https://api.weather.gov/gridpoints/{grid['wfo']}/{grid['x']},{grid['y']}"
                        "/forecast/hourly"
                    ),
                }
            )
        for zone in _bounded(zones, "alert_zones", 5, _UGC) if zones else []:
            steps.append(
                {
                    "parse": "nws_alerts",
                    "url": "https://api.weather.gov/alerts",
                    "params": {"zone": zone},
                    "context": {"zone": zone},
                }
            )
    elif provider == "open-meteo":
        declared = selection.get("declared_station")
        # The Climate & Environment open-meteo-forecast plan: meta.json, then the pinned-model forecast.
        for step in ep.plan(
            "open-meteo-forecast",
            {k: v for k, v in selection.items() if k != "declared_station"},
        ):
            steps.append(
                {**step, "context": {**step["context"], "declared_station": declared}}
            )
    if not steps or len(steps) > MAX_STEPS:
        raise ProviderError(
            "unbounded_selection", "selection compiles to no or too many requests"
        )
    return steps


# --------------------------------------------------------------------- DWD CDC


def parse_geography(
    text: str, station: str, *, url: str, file: str
) -> list[dict[str, Any]]:
    """``Metadaten_Geographie``: one location vintage per published row, dates as published."""

    reader = csv.reader(io.StringIO(text), delimiter=";")
    header = [h.strip() for h in next(reader, [])]
    needed = {
        "Stations_id",
        "Stationshoehe",
        "Geogr.Breite",
        "Geogr.Laenge",
        "von_datum",
        "bis_datum",
        "Stationsname",
    }
    if needed - set(header):
        raise ProviderError("schema_drift", "DWD geography metadata columns changed")
    at = {name: i for i, name in enumerate(header)}
    records = []
    for line, row in enumerate(reader, start=2):
        if (
            not row
            or not row[0].strip()
            or row[0].strip().startswith(("generiert", "Legende"))
        ):
            continue
        if row[at["Stations_id"]].strip().zfill(5) != station:
            raise ProviderError(
                "schema_drift", "geography metadata belongs to another station"
            )
        records.append(
            _build(
                wr.location_vintage,
                "dwd-cdc",
                {"provider": "dwd", "native_id": station},
                latitude=_decimal(row[at["Geogr.Breite"]], "Geogr.Breite"),
                longitude=_decimal(row[at["Geogr.Laenge"]], "Geogr.Laenge"),
                elevation_m=_decimal(row[at["Stationshoehe"]], "Stationshoehe"),
                valid_from=_day(row[at["von_datum"]], "von_datum"),
                valid_to=_day(row[at["bis_datum"]], "bis_datum"),
                name=row[at["Stationsname"]].strip() or None,
                locator={"url": url, "file": file, "line": line},
            )
        )
    return records


def _geography_from_zip(
    members: dict[str, bytes], station: str, url: str
) -> list[dict[str, Any]]:
    names = [
        n
        for n in members
        if n.startswith("Metadaten_Geographie") and n.endswith(".txt")
    ]
    if len(names) > 1:
        raise ProviderError(
            "schema_drift", "DWD archive holds more than one geography file"
        )
    if not names:
        return []
    return parse_geography(
        members[names[0]].decode("latin-1"), station, url=url, file=names[0]
    )


def parse_dwd_station(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    """The DWD station description, parsed by the Climate parser into an environment ``station`` record."""

    records, carry = ep.parse_dwd_stations(
        content, {"station": context["station"]}, {}, url=url
    )
    return [ep._record(r) for r in records], {
        k: v for k, v in carry.items() if k.endswith("missing")
    }


def parse_dwd_10min(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    members = _zip_members(content, "DWD 10-minute product")
    products = [n for n in members if n.startswith("produkt_") and n.endswith(".txt")]
    if len(products) != 1:
        raise ProviderError(
            "schema_drift", "DWD archive must contain exactly one product file"
        )
    text = members[products[0]].decode("latin-1")
    reader = csv.reader(io.StringIO(text), delimiter=";")
    header = [h.strip() for h in next(reader, [])]
    columns = list(context["columns"])
    if not {"STATIONS_ID", "MESS_DATUM", "QN"} <= set(header) or set(columns) - set(
        header
    ):
        raise ProviderError("schema_drift", "DWD 10-minute product columns changed")
    at = {name: i for i, name in enumerate(header)}
    station = context["station"]
    records = []
    for line, row in enumerate(reader, start=2):
        if not row or not row[0].strip():
            continue
        if row[at["STATIONS_ID"]].strip().zfill(5) != station:
            raise ProviderError(
                "schema_drift", "DWD product belongs to another station"
            )
        stamp = row[at["MESS_DATUM"]].strip()
        try:
            observed = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ProviderError(
                "schema_drift", f"MESS_DATUM {stamp!r} is not YYYYMMDDHHMM"
            ) from exc
        qn = row[at["QN"]].strip()
        params = []
        for column in columns:
            value = _decimal(row[at[column]], column)
            item = {
                "parameter": column,
                "unit": DWD_10MIN_COLUMNS[column],
                "qc": {"scheme": "dwd-qn", "native": qn},
            }
            if value is None:
                item["missing"] = (
                    f"published missing marker {row[at[column]].strip() or '(empty)'}"
                )
            else:
                item["value"] = value
            params.append(item)
        records.append(
            _build(
                wr.observation_report,
                "dwd-cdc",
                {"provider": "dwd", "native_id": station},
                observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dwd-10min",
                parameters=params,
                precedence=context["precedence"],
                release={"period": context["period"], "product": products[0]},
                locator={"url": url, "file": products[0], "line": line},
            )
        )
    return records + _geography_from_zip(members, station, url), {}


def parse_dwd_hourly(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    """Hourly reports from the Climate parser's series: one report per hour with every column's QN_9 verbatim."""

    series, _ = ep.parse_dwd_product(
        content,
        {
            "station": context["station"],
            "parameter": "air_temperature",
            "period": context["period"],
        },
        {},
        url=url,
    )
    by_hour: dict[str, list[dict[str, Any]]] = {}
    product = None
    for item in series:
        column, unit = item["indicator"]["code"], item["unit"]
        product = item["release"].get("product")
        for value in item["values"]:
            param = {
                "parameter": column,
                "unit": unit,
                "qc": {"scheme": "dwd-qn", "native": value["quality"]},
            }
            if value["value"] is None:
                param["missing"] = "published missing marker -999"
            else:
                param["value"] = value["value"]
            by_hour.setdefault(value["start"], []).append(
                (param, value["flags"].get("line"))
            )
    records = []
    for start, params in sorted(by_hour.items()):
        records.append(
            _build(
                wr.observation_report,
                "dwd-cdc",
                {"provider": "dwd", "native_id": context["station"]},
                _utc(start, "MESS_DATUM"),
                "dwd-hourly",
                parameters=[p for p, _ in params],
                precedence=context["precedence"],
                release={"period": context["period"], "product": product},
                locator={"url": url, "file": product, "line": params[0][1]},
            )
        )
    members = _zip_members(content, "DWD hourly product")
    return records + _geography_from_zip(members, context["station"], url), {}


def parse_dwd_hourly_rr(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    """Hourly precipitation (``R1`` mm with ``QN_8``); the hour's time stamp as published (verify interval end)."""

    members = _zip_members(content, "DWD hourly precipitation product")
    products = [n for n in members if n.startswith("produkt_") and n.endswith(".txt")]
    if len(products) != 1:
        raise ProviderError(
            "schema_drift", "DWD archive must contain exactly one product file"
        )
    reader = csv.reader(
        io.StringIO(members[products[0]].decode("latin-1")), delimiter=";"
    )
    header = [h.strip() for h in next(reader, [])]
    if not {"STATIONS_ID", "MESS_DATUM", "QN_8", "R1"} <= set(header):
        raise ProviderError("schema_drift", "DWD hourly precipitation columns changed")
    at = {name: i for i, name in enumerate(header)}
    station, records = context["station"], []
    for line, row in enumerate(reader, start=2):
        if not row or not row[0].strip():
            continue
        if row[at["STATIONS_ID"]].strip().zfill(5) != station:
            raise ProviderError(
                "schema_drift", "DWD product belongs to another station"
            )
        stamp = row[at["MESS_DATUM"]].strip()
        try:
            observed = datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ProviderError(
                "schema_drift", f"MESS_DATUM {stamp!r} is not YYYYMMDDHH"
            ) from exc
        value = _decimal(row[at["R1"]], "R1")
        param = {
            "parameter": "R1",
            "unit": "mm",
            "qc": {"scheme": "dwd-qn", "native": row[at["QN_8"]].strip()},
        }
        if value is None:
            param["missing"] = f"published missing marker {row[at['R1']].strip()}"
        else:
            param["value"] = value
        records.append(
            _build(
                wr.observation_report,
                "dwd-cdc",
                {"provider": "dwd", "native_id": station},
                observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dwd-hourly",
                parameters=[param],
                precedence=context["precedence"],
                release={"period": context["period"], "product": products[0]},
                locator={"url": url, "file": products[0], "line": line},
            )
        )
    return records + _geography_from_zip(members, station, url), {}


# ----------------------------------------------------------------------- MOSMIX


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml(content: bytes, what: str) -> ET.Element:
    if b"<!DOCTYPE" in content[:2000] or b"<!ENTITY" in content[:4000]:
        raise ProviderError("schema_drift", f"{what} declares a DTD; refused")
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise ProviderError("schema_drift", f"{what} is not well-formed XML") from exc


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in node if _local(c.tag) == name]


def _first(node: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in node.iter() if _local(c.tag) == name), None)


def _attr(node: ET.Element, name: str) -> str | None:
    return next((v for k, v in node.attrib.items() if _local(k) == name), None)


def parse_mosmix_kmz(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    members = _zip_members(content, "MOSMIX KMZ")
    kml = [n for n in members if n.lower().endswith(".kml")]
    if len(kml) != 1:
        raise ProviderError(
            "schema_drift", "MOSMIX KMZ must hold exactly one KML document"
        )
    root = _xml(members[kml[0]], "MOSMIX KML")
    definition = _first(root, "ProductDefinition")
    if definition is None:
        raise ProviderError("schema_drift", "MOSMIX KML lacks dwd:ProductDefinition")
    issue = _first(definition, "IssueTime")
    steps = _first(definition, "ForecastTimeSteps")
    if issue is None or steps is None:
        raise ProviderError(
            "schema_drift", "MOSMIX KML lacks IssueTime or ForecastTimeSteps"
        )
    issued_at = _utc(issue.text, "IssueTime")
    times = [_utc(t.text, "TimeStep") for t in steps if _local(t.tag) == "TimeStep"]
    process = _first(definition, "GeneratingProcess")
    product_id = _first(definition, "ProductID")
    records, out_of_scope = [], 0
    wanted = set(context["elements"])
    for placemark in (n for n in root.iter() if _local(n.tag) == "Placemark"):
        # Each placemark is parsed on its own: nothing carries over from the previous station.
        name = _first(placemark, "name")
        station = (name.text or "").strip() if name is not None else ""
        if station != context["station"]:
            out_of_scope += 1
            continue
        description = _first(placemark, "description")
        coordinates = _first(placemark, "coordinates")
        if coordinates is None or not coordinates.text:
            raise ProviderError("schema_drift", "MOSMIX placemark lacks coordinates")
        parts = coordinates.text.strip().split(",")
        if len(parts) != 3:
            raise ProviderError("schema_drift", "MOSMIX coordinates are lon,lat,height")
        lon, lat, height = (_decimal(p, "coordinates") for p in parts)
        elements = []
        for forecast in (n for n in placemark.iter() if _local(n.tag) == "Forecast"):
            element = _attr(forecast, "elementName")
            if element not in wanted:
                continue
            value_node = _first(forecast, "value")
            values = (value_node.text or "").split() if value_node is not None else []
            if len(values) != len(times):
                raise ProviderError(
                    "schema_drift",
                    f"MOSMIX element {element} is misaligned with the time steps",
                )
            unit, kind = MOSMIX_ELEMENTS[element]
            for valid_time, raw in zip(times, values, strict=True):
                value = _decimal(raw, element)
                item = {
                    "parameter": element,
                    "valid_time": valid_time,
                    "unit": unit,
                    "kind": kind,
                }
                if value is None:
                    item["missing"] = f"published missing marker {raw}"
                else:
                    item["value"] = value
                elements.append(item)
        station_ref = {"provider": "dwd-mosmix", "native_id": station}
        title = (
            (description.text or "").strip()
            if description is not None and description.text
            else station
        )
        records.append(
            _build(
                wr.forecast_issuance,
                "dwd-mosmix",
                "MOSMIX_L",
                {
                    "kind": "station",
                    "ref": f"dwd-mosmix:{station}",
                    "station": station_ref,
                    "geometry": {
                        "type": "Point",
                        "coordinates": [float(lon), float(lat)],
                    },
                },
                issued_at,
                run_id=f"MOSMIX_L:{station}:{issued_at}",
                elements=elements,
                model=(process.text or "").strip()
                if process is not None and process.text
                else None,
                originator="Deutscher Wetterdienst",
                locator={
                    "url": url,
                    "file": kml[0],
                    "pointer": f"Placemark[name={station}]",
                },
            )
        )
        records.append(
            ep._record(
                wr.environment_station(
                    "dwd-mosmix",
                    station,
                    title,
                    source_url=url,
                    longitude=float(lon),
                    latitude=float(lat),
                    identifiers={"mosmix_id": station},
                    elevation_m=float(height) if height is not None else None,
                    network="DWD MOSMIX",
                )
            )
        )
        records.append(
            identifier_statement(
                station_ref,
                "mosmix_id",
                station,
                stated_by="dwd-mosmix",
                locator={"url": url, "file": kml[0]},
            )
        )
    receipt = {
        "out_of_scope_placemarks": out_of_scope,
        "product_id": (product_id.text or "").strip()
        if product_id is not None and product_id.text
        else None,
    }
    return records, receipt


def parse_mosmix_catalogue(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict], dict]:
    """Source-stated MOSMIX id <-> ICAO pairs from the catalogue (fixed columns; coordinates not used)."""

    text = content.decode("latin-1")
    lines = text.splitlines()
    if not lines or not re.match(r"^\s*ID\s+ICAO\s+NAME", lines[0]):
        raise ProviderError("schema_drift", "MOSMIX catalogue header changed")
    wanted, records, seen = set(context["stations"]), [], set()
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip() or set(line.strip()) <= {"-", " "}:
            continue
        head = line.split()
        if len(head) < 2:
            raise ProviderError(
                "schema_drift", f"catalogue line {number} has no ICAO column"
            )
        station, icao = head[0], head[1]
        if station not in wanted or station in seen:
            continue
        seen.add(station)
        if _ICAO.fullmatch(icao):
            records.append(
                identifier_statement(
                    {"provider": "dwd-mosmix", "native_id": station},
                    "icao",
                    icao,
                    stated_by="dwd-mosmix-catalogue",
                    locator={"url": url, "line": number},
                )
            )
    return records, {"catalogue_missing": sorted(wanted - seen)}


# -------------------------------------------------------------------------- CAP


def _cap_text(node: ET.Element, name: str) -> str | None:
    child = next((c for c in node if _local(c.tag) == name), None)
    return (
        (child.text or "").strip() or None if child is not None and child.text else None
    )


def _cap_polygon(text: str) -> list[list[float]]:
    ring = []
    for pair in text.split():
        lat, lon = pair.split(",")
        ring.append([float(lon), float(lat)])
    return ring


def parse_cap_message(
    content: bytes,
    *,
    url: str,
    file: str,
    provider: str,
    language: str,
    area_filter: Callable[[list[dict[str, Any]]], bool],
) -> dict[str, Any] | None:
    """One CAP 1.2 alert message; ``None`` when none of its areas is in the declared selection."""

    root = _xml(content, "CAP message")
    if _local(root.tag) != "alert":
        raise ProviderError("schema_drift", "CAP document root is not <alert>")
    identifier, sender, sent = (
        _cap_text(root, k) for k in ("identifier", "sender", "sent")
    )
    msg_type = _cap_text(root, "msgType")
    if not identifier or not sender or not sent or not msg_type:
        raise ProviderError(
            "schema_drift", "CAP message lacks identifier, sender, sent or msgType"
        )
    infos = _children(root, "info")
    info = next(
        (i for i in infos if _cap_text(i, "language") == language),
        infos[0] if infos else None,
    )
    areas = []
    fields: dict[str, Any] = {}
    if info is not None:
        for area in _children(info, "area"):
            codes = []
            for geocode in _children(area, "geocode"):
                scheme, value = (
                    _cap_text(geocode, "valueName"),
                    _cap_text(geocode, "value"),
                )
                if scheme in wr.CODE_SCHEMES and value:
                    codes.append({"scheme": scheme, "value": value})
            polygons = [
                _cap_polygon(p.text)
                for p in _children(area, "polygon")
                if p.text and p.text.strip()
            ]
            areas.append(
                {
                    "description": _cap_text(area, "areaDesc"),
                    "codes": codes,
                    "polygons": polygons,
                }
            )
        code = next(iter(_children(info, "eventCode")), None)
        fields = {
            k: _cap_text(info, k)
            for k in (
                "event",
                "severity",
                "urgency",
                "certainty",
                "headline",
                "description",
                "instruction",
                "language",
            )
        }
        fields["event_code"] = (
            f"{_cap_text(code, 'valueName')}:{_cap_text(code, 'value')}"
            if code is not None and _cap_text(code, "value")
            else None
        )
        for field in ("onset", "effective", "expires"):
            value = _cap_text(info, field)
            fields[field] = _utc(value, field) if value else None
    if not area_filter(areas):
        return None
    try:
        references = wr.parse_cap_references(_cap_text(root, "references"))
    except wr.WeatherRecordError as exc:
        raise ProviderError("schema_drift", str(exc)) from exc
    return _build(
        wr.warning,
        provider,
        identifier,
        sender,
        _utc(sent, "sent"),
        msg_type,
        status=_cap_text(root, "status"),
        scope=_cap_text(root, "scope"),
        references=references,
        areas=areas,
        locator={"url": url, "file": file},
        **fields,
    )


def parse_dwd_cap(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    members = _zip_members(content, "DWD CAP archive")
    wanted = set(context["warncells"])

    def in_scope(areas: list[dict[str, Any]]) -> bool:
        return any(
            c["scheme"] == "WARNCELLID" and c["value"] in wanted
            for a in areas
            for c in a["codes"]
        )

    records, out_of_scope = [], 0
    for name in sorted(members):
        if not name.lower().endswith(".xml"):
            continue
        record = parse_cap_message(
            members[name],
            url=url,
            file=name,
            provider="dwd-cap",
            language=context.get("language") or "de-DE",
            area_filter=in_scope,
        )
        if record is None:
            out_of_scope += 1
        else:
            records.append(record)
    return records, {"out_of_scope_messages": out_of_scope}


# ------------------------------------------------------------- aviationweather


def _awc_list(content: bytes, what: str) -> list[dict[str, Any]]:
    payload = _json(content)
    if not isinstance(payload, list) or any(not isinstance(i, dict) for i in payload):
        raise ProviderError(
            "schema_drift",
            f"aviationweather {what} response is not a JSON list of objects",
        )
    return payload


def _awc_time(value: Any, field: str) -> str:
    # aviationweather.gov documents its times as UTC (verify); offset-free text is read as UTC on that basis.
    return _utc(value, field, documented_utc=True)


def parse_awc_stationinfo(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict], dict]:
    wanted, records, seen = set(context["stations"]), [], set()
    for index, item in enumerate(_awc_list(content, "stationinfo")):
        icao = str(item.get("icaoId") or "").strip().upper()
        if icao not in wanted:
            continue
        seen.add(icao)
        lat, lon = _decimal(item.get("lat"), "lat"), _decimal(item.get("lon"), "lon")
        if lat is None or lon is None:
            raise ProviderError("schema_drift", f"station {icao} lacks coordinates")
        station = {"provider": "aviationweather", "native_id": icao}
        identifiers = {"icao": icao}
        locator = {"url": url, "pointer": f"/{index}"}
        records.append(
            identifier_statement(
                station, "icao", icao, stated_by="aviationweather", locator=locator
            )
        )
        wmo = str(item.get("wmoId") or "").strip()
        if re.fullmatch(r"\d{5}", wmo):
            identifiers["wmo"] = wmo
            records.append(
                identifier_statement(
                    station, "wmo", wmo, stated_by="aviationweather", locator=locator
                )
            )
        elevation = _decimal(item.get("elev"), "elev")
        records.append(
            ep._record(
                wr.environment_station(
                    "aviationweather",
                    icao,
                    str(item.get("site") or icao),
                    source_url=url,
                    longitude=float(lon),
                    latitude=float(lat),
                    identifiers=identifiers,
                    elevation_m=float(elevation) if elevation is not None else None,
                    network="aviationweather.gov",
                )
            )
        )
    return records, {"stationinfo_missing": sorted(wanted - seen)}


def parse_awc_metar(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    wanted, records, out_of_scope = set(context["stations"]), [], 0
    for index, item in enumerate(_awc_list(content, "metar")):
        # Each report stands alone: a field it does not state is missing, never taken from another report.
        icao = str(item.get("icaoId") or "").strip().upper()
        if icao not in wanted:
            out_of_scope += 1
            continue
        raw = str(item.get("rawOb") or "").strip()
        if not raw or item.get("obsTime") is None:
            raise ProviderError("schema_drift", f"METAR {index} lacks rawOb or obsTime")
        kind = str(item.get("metarType") or "METAR").strip().upper()
        if kind not in {"METAR", "SPECI"}:
            raise ProviderError("schema_drift", f"unknown metarType {kind}")
        qc = str(item.get("qcField")) if item.get("qcField") is not None else None
        params = []
        for field, unit in METAR_FIELDS.items():
            param = {
                "parameter": field,
                "unit": unit,
                "qc": {"scheme": "awc-qcfield", "native": qc},
            }
            published = item.get(field)
            text = str(published).strip() if published is not None else None
            if text is None or text == "":
                param["missing"] = "not reported"
            elif field == "wdir" and text.upper() == "VRB":
                param["missing"] = "published as VRB (variable direction)"
            elif field == "visib" and text.endswith("+"):
                param["missing"] = f"published as {text} (a lower bound, not a value)"
            else:
                param["value"] = _decimal(text, field)
                if param["value"] is None:
                    del param["value"]
                    param["missing"] = f"published missing marker {text}"
            if param["qc"]["native"] is None:
                param["qc"] = {"scheme": "awc-qcfield"}
            params.append(param)
        tokens = raw.split()
        correction = "COR" if "COR" in tokens[:4] else None
        receipt_time = item.get("receiptTime")
        records.append(
            _build(
                wr.observation_report,
                "aviationweather",
                {"provider": "aviationweather", "native_id": icao},
                _utc(item["obsTime"], "obsTime"),
                kind.lower(),
                parameters=params,
                raw_text=raw,
                correction=correction,
                source_time=_awc_time(receipt_time, "receiptTime")
                if receipt_time
                else None,
                locator={"url": url, "pointer": f"/{index}"},
            )
        )
    return records, {"out_of_scope_reports": out_of_scope}


def parse_awc_taf(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    wanted, records = set(context["stations"]), []
    for index, item in enumerate(_awc_list(content, "taf")):
        icao = str(item.get("icaoId") or "").strip().upper()
        if icao not in wanted:
            continue
        raw = str(item.get("rawTAF") or "").strip()
        if not raw or not item.get("issueTime"):
            raise ProviderError(
                "schema_drift", f"TAF {index} lacks rawTAF or issueTime"
            )
        issued = _awc_time(item["issueTime"], "issueTime")
        records.append(
            _build(
                wr.forecast_issuance,
                "aviationweather",
                "TAF",
                {
                    "kind": "station",
                    "ref": f"aviationweather:{icao}",
                    "station": {"provider": "aviationweather", "native_id": icao},
                },
                issued,
                run_id=f"TAF:{icao}:{issued}",
                elements=[],
                raw_text=raw,
                valid_from=_utc(item["validTimeFrom"], "validTimeFrom")
                if item.get("validTimeFrom")
                else None,
                valid_to=_utc(item["validTimeTo"], "validTimeTo")
                if item.get("validTimeTo")
                else None,
                originator="NOAA / National Weather Service, Aviation Weather Center",
                locator={"url": url, "pointer": f"/{index}"},
            )
        )
    return records, {
        "taf_change_groups": "not implemented: conditional period forecasts (see WX01)"
    }


# -------------------------------------------------------------------------- NWS


def _feature_polygon(geometry: Any) -> dict[str, Any] | None:
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
        return None
    rings = [
        [[float(p[0]), float(p[1])] for p in ring]
        for ring in geometry.get("coordinates") or []
    ]
    return {"type": "Polygon", "coordinates": rings} if rings else None


def parse_nws_points(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    payload = _json(content)
    properties = (
        (payload or {}).get("properties") if isinstance(payload, dict) else None
    )
    if not isinstance(properties, dict) or not properties.get("gridId"):
        raise ProviderError(
            "schema_drift", "NWS /points response lacks properties.gridId"
        )
    stated = {
        "wfo": str(properties["gridId"]),
        "x": str(properties.get("gridX")),
        "y": str(properties.get("gridY")),
    }
    if stated != context["grid"]:
        raise ProviderError(
            "schema_drift",
            f"NWS grid mapping changed: point maps to {stated}, selection pins "
            f"{context['grid']}; update the selection",
        )
    return [], {
        "grid_mapping": {
            "point": context["point"],
            "grid": stated,
            "source": url,
            "forecast_hourly": properties.get("forecastHourly"),
        }
    }


def _nws_number(value: Any) -> tuple[str | None, str | None]:
    """A numeric text like '10 mph' or a quantity object; ranges ('10 to 15 mph') stay missing."""

    if isinstance(value, dict):
        return _decimal(value.get("value"), "value"), value.get("unitCode")
    text = str(value or "").strip()
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([A-Za-z/%]+)?", text)
    return (match.group(1), match.group(2)) if match else (None, None)


def parse_nws_forecast(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    payload = _json(content)
    properties = (
        (payload or {}).get("properties") if isinstance(payload, dict) else None
    )
    if not isinstance(properties, dict) or not isinstance(
        properties.get("periods"), list
    ):
        raise ProviderError("schema_drift", "NWS forecast lacks properties.periods")
    if not properties.get("updateTime"):
        raise ProviderError(
            "schema_drift", "NWS forecast lacks updateTime (the issuance time)"
        )
    if not properties["periods"]:
        return [], {
            "no_periods": "the forecast published no periods; no issuance is recorded"
        }
    geometry = _feature_polygon(payload.get("geometry"))
    if geometry is None:
        raise ProviderError("schema_drift", "NWS forecast lacks the grid cell polygon")
    issued = _utc(properties["updateTime"], "updateTime")
    elements = []
    for index, period in enumerate(properties["periods"]):
        if not isinstance(period, dict) or not period.get("startTime"):
            raise ProviderError("schema_drift", f"NWS period {index} lacks startTime")
        start = _utc(period["startTime"], "startTime")
        end = _utc(period["endTime"], "endTime") if period.get("endTime") else None
        entries = []
        temperature_unit = NWS_UNITS.get(str(period.get("temperatureUnit") or ""))
        if period.get("temperature") is not None and temperature_unit:
            entries.append(
                (
                    "temperature",
                    _decimal(period["temperature"], "temperature"),
                    temperature_unit,
                    "value",
                )
            )
        speed, unit = _nws_number(period.get("windSpeed"))
        entries.append(
            ("windSpeed", speed, unit or "mph", "value")
            if speed is not None
            else ("windSpeed", None, "mph", "value")
        )
        for name, kind in (
            ("probabilityOfPrecipitation", "probability"),
            ("relativeHumidity", "value"),
            ("dewpoint", "value"),
        ):
            if isinstance(period.get(name), dict):
                value, code = _nws_number(period[name])
                entries.append((name, value, str(code or "unknown"), kind))
        for name, value, unit_text, kind in entries:
            item = {
                "parameter": name,
                "valid_time": start,
                "unit": unit_text,
                "kind": kind,
            }
            if end:
                item["valid_to"] = end
            if value is None:
                item["missing"] = "not published as a single number"
            else:
                item["value"] = value
            elements.append(item)
    grid = context["grid"]
    declared = context.get("declared_station")
    location = {
        "kind": "grid-cell",
        "ref": f"nws:{grid['wfo']}/{grid['x']},{grid['y']}",
        "geometry": geometry,
    }
    if declared:
        location.update(
            declared_station=dict(declared),
            declared_basis="declared in the source selection",
        )
    record = _build(
        wr.forecast_issuance,
        "nws",
        "nws-gridpoint-hourly",
        location,
        issued,
        run_id=f"nws:{grid['wfo']}/{grid['x']},{grid['y']}:{issued}",
        elements=elements,
        generated_at=_utc(properties["generatedAt"], "generatedAt")
        if properties.get("generatedAt")
        else None,
        originator="NOAA / National Weather Service",
        locator={"url": url, "pointer": "/properties/periods"},
    )
    return [record], {}


def parse_nws_alerts(
    content: bytes, context: Mapping[str, Any], *, url: str
) -> tuple[list[dict[str, Any]], dict]:
    payload = _json(content)
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        raise ProviderError(
            "schema_drift", "NWS alerts response is not a FeatureCollection"
        )
    records = []
    for index, feature in enumerate(features):
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            raise ProviderError("schema_drift", f"NWS alert {index} lacks properties")
        identifier = properties.get("id") or properties.get("identifier")
        if not identifier or not properties.get("sender") or not properties.get("sent"):
            raise ProviderError(
                "schema_drift", f"NWS alert {index} lacks id, sender or sent"
            )
        geocode = properties.get("geocode") or {}
        codes = [{"scheme": "UGC", "value": str(v)} for v in geocode.get("UGC") or []]
        codes += [
            {"scheme": "SAME", "value": str(v)} for v in geocode.get("SAME") or []
        ]
        polygon = _feature_polygon(feature.get("geometry"))
        area = {
            "description": properties.get("areaDesc"),
            "codes": codes,
            "polygons": polygon["coordinates"][:1] if polygon else [],
        }
        references = []
        for ref in properties.get("references") or []:
            references.append(
                {
                    "sender": ref.get("sender"),
                    "identifier": ref.get("identifier"),
                    "sent": _utc(ref["sent"], "references.sent")
                    if ref.get("sent")
                    else None,
                }
            )
        fields = {
            k: (str(properties[k]).strip() or None)
            if properties.get(k) is not None
            else None
            for k in (
                "event",
                "severity",
                "urgency",
                "certainty",
                "headline",
                "description",
                "instruction",
                "status",
                "scope",
            )
        }
        records.append(
            _build(
                wr.warning,
                "nws",
                str(identifier),
                str(properties["sender"]),
                _utc(properties["sent"], "sent"),
                str(properties.get("messageType") or ""),
                areas=[area],
                references=references,
                onset=_utc(properties["onset"], "onset")
                if properties.get("onset")
                else None,
                effective=_utc(properties["effective"], "effective")
                if properties.get("effective")
                else None,
                expires=_utc(properties["expires"], "expires")
                if properties.get("expires")
                else None,
                language="en-US",
                locator={"url": url, "pointer": f"/features/{index}"},
                **fields,
            )
        )
    return records, {}


# ------------------------------------------------------------------ Open-Meteo


def parse_open_meteo_meta(
    content: bytes, context: Mapping[str, Any], *, url: str, carry: dict
) -> tuple[list, dict]:
    _, meta = ep.parse_open_meteo_meta(content, context, {}, url=url)
    return [], {**carry, **meta}


def parse_open_meteo(
    content: bytes, context: Mapping[str, Any], *, url: str, carry: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict]:
    """One run of a pinned model as an issuance vintage keyed by the model's meta.json run time."""

    if not carry.get("issue_time"):
        raise ProviderError(
            "schema_drift",
            "Open-Meteo run time (meta.json) was not read before the forecast",
        )
    series, _ = ep.parse_open_meteo(
        content, {**context}, {"issue_time": carry["issue_time"]}, url=url
    )
    if not series:
        return [], {}
    first = series[0]
    location = first["location"]
    model = context["model"]
    elements = []
    for item in series:
        for value in item["values"]:
            element = {
                "parameter": item["indicator"]["code"],
                "valid_time": value["start"],
                "unit": item["unit"],
                "kind": "value",
            }
            if value["value"] is None:
                element["missing"] = "published as null"
            else:
                element["value"] = value["value"]
            elements.append(element)
    if not elements:
        return [], {
            "no_values": "the run published no hourly values; no issuance is recorded"
        }
    issued = carry["issue_time"]
    loc = {
        "kind": "grid-point",
        "ref": location["ref"],
        "geometry": location["geometry"],
    }
    if context.get("declared_station"):
        loc.update(
            declared_station=dict(context["declared_station"]),
            declared_basis="declared in the source selection",
        )
    record = _build(
        wr.forecast_issuance,
        "open-meteo",
        "open-meteo-forecast",
        loc,
        issued,
        run_id=f"open-meteo:{model}:{issued}",
        elements=elements,
        model=model,
        originator=ep.OPEN_METEO_MODELS[model]["dataset"],
        locator={"url": url, "pointer": "/hourly"},
    )
    return [record], {"grid_values": "model grid values, never station observations"}


PARSERS: dict[str, Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]]] = {
    "dwd_station": parse_dwd_station,
    "dwd_10min": parse_dwd_10min,
    "dwd_hourly": parse_dwd_hourly,
    "dwd_hourly_rr": parse_dwd_hourly_rr,
    "mosmix_kmz": parse_mosmix_kmz,
    "mosmix_catalogue": parse_mosmix_catalogue,
    "dwd_cap": parse_dwd_cap,
    "awc_stationinfo": parse_awc_stationinfo,
    "awc_metar": parse_awc_metar,
    "awc_taf": parse_awc_taf,
    "nws_points": parse_nws_points,
    "nws_forecast": parse_nws_forecast,
    "nws_alerts": parse_nws_alerts,
}


def parse_step(
    step: Mapping[str, Any], content: bytes, carry: Mapping[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Parse one step: (records, receipt notes, carry for the next step of the same plan)."""

    context = dict(step.get("context") or {})
    carry = dict(carry or {})
    if step["parse"] == "open_meteo_meta":
        _, carry = parse_open_meteo_meta(content, context, url=step["url"], carry={})
        return [], {"issue_time": carry.get("issue_time")}, carry
    if step["parse"] == "open_meteo":
        records, notes = parse_open_meteo(
            content, context, url=step["url"], carry=carry
        )
        return records, notes, {}
    records, notes = PARSERS[step["parse"]](content, context, url=step["url"])
    validated = []
    for record in records:
        if record.get("contract") == wr.CONTRACT:
            validated.append(wr.validate(record))
        else:
            validated.append(record)
    return validated, notes, {}


# ------------------------------------------------------- source-pack adapter


def _record_id(record: Mapping[str, Any]) -> str:
    if record.get("contract") == wr.CONTRACT:
        return f"{record['provider']}:{record['record_type']}:{record['record_key']}"
    if record.get("contract") == IDENTIFIER_CONTRACT:
        return f"identifier:{wr.station_key(record['station'])}:{record['scheme']}:{record['value']}"
    return f"environment-station:{record['provider']}:{record['native_id']}"


def runtime_record(record: Mapping[str, Any]) -> dict[str, Any]:
    title = (
        record.get("title")
        or record.get("headline")
        or record.get("record_key")
        or _record_id(record)
    )
    url = (record.get("locator") or {}).get("url") or record.get("source_url")
    language = "de" if str(record.get("provider", "")).startswith("dwd") else "en"
    return {
        "id": _record_id(record),
        "title": str(title)[:300],
        "url": url,
        "language": language,
        "content": canonical(record),
        "weather_records": [dict(record)],
    }


class WeatherSourceAdapter:
    """``weather`` native connector: one plan step per runtime page, the declared User-Agent on every request."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        del secret
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter
        from src.ingestion.source_packs import SourcePackError

        self.source = json.loads(json.dumps(source))
        spec = dict(self.source.get("weather") or {})
        self.provider = str(spec.get("provider") or "")
        try:
            self.steps = plan(self.provider, spec.get("selection"))
        except ProviderError as exc:
            raise SourcePackError("unbounded_source", str(exc)) from exc
        for step in self.steps:
            if urlsplit(step["url"]).hostname not in PROVIDER_HOSTS[self.provider]:
                raise SourcePackError(
                    "network_policy",
                    "weather requests stay on the provider's declared hosts",
                )
        self.user_agent = str(spec.get("user_agent") or DEFAULT_USER_AGENT)
        self.transport = transport or partial(
            HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
        )
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "weather": {
                "provider": self.provider,
                "steps": len(self.steps),
                "user_agent": self.user_agent,
                "live_verification": LIVE_VERIFICATION[self.provider]["status"],
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self) -> str:
        return hashlib.sha256(
            canonical(
                {"source_hash": self.source["source_hash"], "steps": self.steps}
            ).encode()
        ).hexdigest()

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms
        from src.ingestion.source_packs import SourcePackError

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if dict(request.get("parameters") or {}):
            raise SourcePackError(
                "parameter_forbidden", "weather runs use the pinned selection"
            )
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != self._scope():
            raise SourcePackError(
                "cursor_drift", "cursor belongs to a different selection"
            )
        index = int(state.get("i", 0))
        if index >= len(self.steps):
            return RuntimePage((), None, 0, receipt={"status": 200})
        step = self.steps[index]
        headers = {"Accept": "*/*", "User-Agent": self.user_agent}
        response = self.transport(
            url=step["url"],
            params=dict(step["params"]),
            headers=headers,
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        status = int(response.get("status", 200))
        response_headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "source response exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                f"{self.provider} rate limit reached",
                retry_after_ms=_retry_after_ms(response_headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed",
                f"{self.provider} refused the request (HTTP {status})",
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"{self.provider} returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError(
                "schema_drift", f"{self.provider} returned HTTP {status}"
            )
        response_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            records, notes, carry = parse_step(step, raw, state.get("carry") or {})
        except ProviderError as exc:
            raise SourcePackError("schema_drift", str(exc)) from exc
        more = index + 1 < len(self.steps)
        next_cursor = (
            json.dumps(
                {"i": index + 1, "scope": self._scope(), "carry": carry}, sort_keys=True
            )
            if more
            else None
        )
        receipt = {
            "status": status,
            "step": index,
            "steps": len(self.steps),
            "parse": step["parse"],
            "response_sha256": response_sha256,
            "provider": self.provider,
            "request": {
                "url": step["url"],
                "params": dict(step["params"]),
                "user_agent": self.user_agent,
            },
            "notes": notes,
            "records": len(records),
            "coverage": {"complete": False, "basis": "explicit bounded selection"},
        }
        return RuntimePage(
            tuple(runtime_record(r) for r in records),
            next_cursor,
            len(raw),
            receipt=receipt,
        )


ADAPTERS = {CONNECTOR: WeatherSourceAdapter}


def _fixture_body(page: Mapping[str, Any]) -> bytes:
    if "body_base64" in page:
        return base64.b64decode(page["body_base64"])
    body = page.get("body")
    return (
        json.dumps(body).encode()
        if isinstance(body, (dict, list))
        else (body or "").encode("utf-8")
    )


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Serve authored native pages keyed by URL + query; every request must carry a User-Agent."""

    by_key = {
        (page["url"], canonical(dict(page.get("params") or {}))): page for page in pages
    }

    def transport(*, url, params, headers, timeout, **_):
        del timeout
        if not dict(headers or {}).get("User-Agent"):
            return {"status": 403, "headers": {}, "content": b"User-Agent required"}
        page = by_key.get((url, canonical(dict(params or {}))))
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": _fixture_body(page),
        }

    return transport


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = WeatherSourceAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"]))
    )
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page(
            {"operation": min(source["operations"]), "parameters": {}}, cursor=cursor
        )
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
