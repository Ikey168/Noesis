"""Offline Weather harness: fictional native documents through the real adapter and the runtime projector.

Each stage runs :class:`WeatherSourceAdapter` with the fixture transport and hands
every page to :class:`WeatherProjector` exactly as the source-pack runtime does,
with the page's acquisition time as the document ingestion time. Nothing here
is live coverage.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.source_packs import _digest
from src.ingestion.weather_sources import WeatherSourceAdapter, fixture_transport
from src.kb.weather_store import WeatherProjector
from tests.unit.weather import fixture_builder as fb

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/weather.json"
NS = "weather"
ENV_NS = "environment"
PRINCIPAL = "analyst"
REVIEWER = "reviewer"
SCOPES = {
    "knowledge:weather:read",
    "knowledge:weather:write",
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:environment:read",
    "knowledge:environment:write",
    f"namespace:{ENV_NS}:read",
    f"namespace:{ENV_NS}:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:calculate",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
}
REVIEWER_SCOPES = SCOPES | {"knowledge:weather:review"}
READ_ONLY = {"knowledge:weather:read", f"namespace:{NS}:read"}
DECLARED_US = {"provider": "aviationweather", "native_id": fb.US_ICAO}
DECLARED_DE = {"provider": "dwd", "native_id": fb.DWD}


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def source(
    source_id: str, provider: str, selection: dict[str, Any], endpoint: str
) -> dict[str, Any]:
    body = {
        "source_id": source_id,
        "connector": "weather",
        "endpoint": endpoint,
        "operations": ["observe"],
        "mapping": {"target_schema": "noesis-weather-record-v1", "version": "1.0.0"},
        "extractor_versions": ["weather-sources:1.0.0"],
        "budgets": {
            "timeout_ms": 30000,
            "max_results": 5000,
            "max_bytes": 5_000_000,
            "max_pages": 24,
        },
        "weather": {
            "provider": provider,
            "namespace": NS,
            "environment_namespace": ENV_NS,
            "user_agent": "noesis-weather-pack/1.0 (fixture)",
            "selection": selection,
        },
    }
    body["source_hash"] = _digest(body)
    return body


def page(
    url: str, body: str | bytes, params: dict[str, str] | None = None
) -> dict[str, Any]:
    item: dict[str, Any] = {"url": url, "params": params or {}, "status": 200}
    if isinstance(body, bytes):
        import base64

        item["body_base64"] = base64.b64encode(body).decode()
    else:
        item["body"] = body
    return item


DWD_10 = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/"
DWD_RR = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/precipitation/"
MOSMIX = f"https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/{fb.MOSMIX}/kml/"
CATALOGUE = (
    "https://www.dwd.de/DE/leistungen/met_verfahren_mosmix/mosmix_stationskatalog.cfg"
)
CAP = (
    "https://opendata.dwd.de/weather/alerts/cap/DISTRICT_DWD_STAT/"
    "Z_CAP_C_EDZW_LATEST_PVW_STATUS_PREMIUMDWD_DISTRICT_DE.zip"
)
AWC = "https://aviationweather.gov/api/data/"
NWS = "https://api.weather.gov/"
OM_META = "https://api.open-meteo.com/data/dwd_icon_d2/static/meta.json"
OM = "https://api.open-meteo.com/v1/forecast"
OM_PARAMS = {
    "latitude": "52.38",
    "longitude": "13.53",
    "hourly": "temperature_2m",
    "models": "icon_d2",
    "timezone": "GMT",
    "forecast_days": "1",
}


def dwd_10min(period: str) -> tuple[dict, list]:
    selection = {
        "stations": [fb.DWD],
        "resolution": "10_minutes",
        "period": period,
        "columns": ["TT_10", "RF_10"],
    }
    if period == "historical":
        selection["historical_files"] = {fb.DWD: fb.HISTORICAL_FILE}
        product = fb.dwd_10min(
            fb.HISTORICAL_10MIN, name="produkt_zehn_min_tu_20200101_20260630_99901.txt"
        )
        name = fb.HISTORICAL_FILE
    else:
        product, name = (
            fb.dwd_10min(fb.RECENT_10MIN),
            f"10minutenwerte_TU_{fb.DWD}_akt.zip",
        )
    base = DWD_10 + period + "/"
    return (
        source(f"dwd-10min-{period}", "dwd-cdc", selection, "https://opendata.dwd.de/"),
        [
            page(base + "zehn_min_tu_Beschreibung_Stationen.txt", fb.dwd_description()),
            page(base + name, product),
        ],
    )


def dwd_rr() -> tuple[dict, list]:
    selection = {
        "stations": [fb.DWD],
        "resolution": "hourly",
        "period": "recent",
        "parameter": "precipitation",
    }
    base = DWD_RR + "recent/"
    return (
        source("dwd-hourly-rr", "dwd-cdc", selection, "https://opendata.dwd.de/"),
        [
            page(
                base + "RR_Stundenwerte_Beschreibung_Stationen.txt",
                fb.dwd_description(),
            ),
            page(base + f"stundenwerte_RR_{fb.DWD}_akt.zip", fb.dwd_hourly_rr()),
        ],
    )


def mosmix(issue: str) -> tuple[dict, list]:
    selection = {
        "stations": [fb.MOSMIX],
        "elements": ["TTT", "R101", "RR1c"],
        "catalogue": True,
    }
    return (
        source("dwd-mosmix-l", "dwd-mosmix", selection, "https://opendata.dwd.de/"),
        [
            page(CATALOGUE, fb.mosmix_catalogue()),
            page(MOSMIX + f"MOSMIX_L_LATEST_{fb.MOSMIX}.kmz", fb.mosmix_kmz(issue)),
        ],
    )


def dwd_cap(stage: str) -> tuple[dict, list]:
    return (
        source(
            "dwd-cap-warnings",
            "dwd-cap",
            {"warncells": [fb.WARNCELL]},
            "https://opendata.dwd.de/",
        ),
        [page(CAP, fb.cap_zip(stage))],
    )


def awc(stage: str) -> tuple[dict, list]:
    products = ["stationinfo", "metar", "taf"] if stage == "first" else ["metar"]
    selection = {"stations": [fb.ICAO, fb.US_ICAO], "products": products, "hours": 3}
    ids = f"{fb.ICAO},{fb.US_ICAO}"
    pages = [
        page(AWC + "stationinfo", fb.stationinfo(), {"ids": ids, "format": "json"}),
        page(
            AWC + "metar", fb.metar(stage), {"ids": ids, "format": "json", "hours": "3"}
        ),
        page(AWC + "taf", fb.taf(), {"ids": ids, "format": "json"}),
    ]
    return source(
        f"awc-{stage}", "aviationweather", selection, "https://aviationweather.gov/"
    ), pages


def nws_forecast(update: str) -> tuple[dict, list]:
    point = {
        "latitude": fb.POINT[0],
        "longitude": fb.POINT[1],
        "grid": fb.GRID,
        "declared_station": DECLARED_US,
    }
    grid = f"{fb.GRID['wfo']}/{fb.GRID['x']},{fb.GRID['y']}"
    return (
        source("nws-gridpoint", "nws", {"points": [point]}, NWS),
        [
            page(NWS + f"points/{fb.POINT[0]},{fb.POINT[1]}", fb.nws_points()),
            page(NWS + f"gridpoints/{grid}/forecast/hourly", fb.nws_forecast(update)),
        ],
    )


def nws_alerts(stage: str) -> tuple[dict, list]:
    return (
        source("nws-alerts", "nws", {"alert_zones": [fb.UGC]}, NWS),
        [page(NWS + "alerts", fb.nws_alerts(stage), {"zone": fb.UGC})],
    )


def open_meteo(init: int) -> tuple[dict, list]:
    selection = {
        "latitude": "52.38",
        "longitude": "13.53",
        "model": "icon_d2",
        "hourly": ["temperature_2m"],
        "forecast_days": 1,
        "declared_station": DECLARED_DE,
    }
    return (
        source(
            "open-meteo-icon-d2", "open-meteo", selection, "https://api.open-meteo.com/"
        ),
        [
            page(OM_META, fb.open_meteo_meta(init)),
            page(OM, fb.open_meteo_forecast(init), OM_PARAMS),
        ],
    )


# Simulated acquisition times; each stage's documents were published before it ran.
STAGES = [
    ("2026-06-10T04:30:00Z", lambda: mosmix("2026-06-10T03:00:00.000Z")),
    ("2026-06-10T06:30:00Z", lambda: dwd_cap("alert")),
    ("2026-06-10T09:30:00Z", lambda: nws_forecast("2026-06-10T09:00:00+00:00")),
    ("2026-06-10T09:40:00Z", lambda: open_meteo(1781049600)),
    ("2026-06-10T10:30:00Z", lambda: mosmix("2026-06-10T09:00:00.000Z")),
    ("2026-06-10T10:40:00Z", lambda: open_meteo(1781060400)),
    ("2026-06-10T11:30:00Z", lambda: nws_forecast("2026-06-10T11:00:00+00:00")),
    ("2026-06-10T12:05:00Z", lambda: awc("first")),
    ("2026-06-10T12:30:00Z", lambda: dwd_cap("update")),
    ("2026-06-10T12:40:00Z", lambda: awc("second")),
    ("2026-06-10T13:30:00Z", lambda: mosmix("2026-06-10T12:00:00.000Z")),
    ("2026-06-10T14:00:00Z", lambda: dwd_10min("recent")),
    ("2026-06-10T14:05:00Z", dwd_rr),
    ("2026-06-10T19:30:00Z", lambda: nws_alerts("first")),
    ("2026-06-10T22:30:00Z", lambda: nws_alerts("second")),
    ("2026-06-11T02:30:00Z", lambda: dwd_cap("cancel")),
    ("2026-07-15T08:00:00Z", lambda: dwd_10min("historical")),
    (
        "2026-07-16T08:00:00Z",
        lambda: dwd_10min("recent"),
    ),  # a late 'recent' copy after the historical release
]


def acquire(
    conn: Any, at: str, stage: Any, *, run_id: str | None = None
) -> list[dict[str, Any]]:
    src, pages = stage()
    adapter = WeatherSourceAdapter(src, transport=fixture_transport(pages))
    projector = WeatherProjector(conn)
    projector.store.now = lambda: ms(at)
    receipts, outcomes, cursor, number = [], [], None, 0
    run = run_id or f"run:{src['source_id']}:{at}"
    while True:
        result = adapter.fetch_page(
            {"operation": "observe", "parameters": {}}, cursor=cursor
        )
        number += 1
        outcomes.append(
            projector.project_page(
                run_id=run,
                manifest={"pack_id": "weather-operational", "version": "1.0.0"},
                source=src,
                records=result.records,
                documents=[{"document_id": f"{run}:{number}", "ingested_at": ms(at)}],
                page_receipt=dict(result.receipt),
                principal_id=PRINCIPAL,
            )
        )
        receipts.append(dict(result.receipt))
        cursor = result.next_cursor
        if cursor is None:
            break
    projector.finish_source(
        run_id=run, manifest={}, source=src, status="complete", principal_id=PRINCIPAL
    )
    return [
        {"receipt": r, "outcome": o} for r, o in zip(receipts, outcomes, strict=True)
    ]


def acquire_all(
    conn: Any, *, until: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    results = {}
    for at, stage in STAGES:
        if until is None or ms(at) <= ms(until):
            results[at] = acquire(conn, at, stage)
    return results


def connection() -> Any:
    return duckdb.connect(":memory:")


# ------------------------------------------------------------------ production fixtures


def production_pages(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Authored bodies for the production URLs: fictional items only, so production stations stay out of scope."""

    from src.ingestion.weather_sources import plan

    spec = entry["weather"]
    pages = []
    for step in plan(spec["provider"], spec["selection"]):
        parse, url, params = step["parse"], step["url"], dict(step["params"])
        if parse == "dwd_station":
            body: str | bytes = fb.dwd_description()
        elif parse in {"dwd_10min", "dwd_hourly", "dwd_hourly_rr"}:
            head = {
                "dwd_10min": "STATIONS_ID;MESS_DATUM;  QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor\n",
                "dwd_hourly": "STATIONS_ID;MESS_DATUM;QN_9;TT_TU;RF_TU;eor\n",
                "dwd_hourly_rr": "STATIONS_ID;MESS_DATUM;QN_8;  R1;RS_IND;WRTR;eor\n",
            }[parse]
            body = fb.zipped(
                [("produkt_authored_fixture_header_only.txt", head.encode())]
            )
        elif parse == "mosmix_catalogue":
            body = fb.mosmix_catalogue()
        elif parse == "mosmix_kmz":
            body = fb.mosmix_kmz("2026-06-10T03:00:00.000Z")
        elif parse == "dwd_cap":
            body = fb.cap_zip("alert")
        elif parse == "awc_stationinfo":
            body = fb.stationinfo()
        elif parse == "awc_metar":
            body = fb.metar("first")
        elif parse == "awc_taf":
            body = fb.taf()
        elif parse == "nws_points":
            grid = step["context"]["grid"]
            body = json.dumps(
                {
                    "properties": {
                        "gridId": grid["wfo"],
                        "gridX": int(grid["x"]),
                        "gridY": int(grid["y"]),
                    }
                }
            )
        elif parse == "nws_forecast":
            body = json.dumps(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-71.0, 42.3], [-71.0, 42.4], [-70.9, 42.4], [-71.0, 42.3]]
                        ],
                    },
                    "properties": {
                        "updateTime": "2026-06-10T09:00:00+00:00",
                        "periods": [],
                    },
                }
            )
        elif parse == "nws_alerts":
            body = json.dumps({"type": "FeatureCollection", "features": []})
        elif parse == "open_meteo_meta":
            body = fb.open_meteo_meta(1781049600)
        else:  # open_meteo: an authored run with no hourly values
            body = json.dumps(
                {
                    "latitude": 52.47,
                    "longitude": 13.4,
                    "utc_offset_seconds": 0,
                    "hourly_units": {"time": "iso8601"},
                    "hourly": {"time": []},
                }
            )
            body = json.dumps(
                {
                    **json.loads(body),
                    "hourly": {
                        "time": [],
                        **{v: [] for v in spec["selection"]["hourly"]},
                    },
                    "hourly_units": {
                        "time": "iso8601",
                        **{v: "°C" for v in spec["selection"]["hourly"]},
                    },
                }
            )
        pages.append({**page(url, body, params), "authored": True})
    return pages


def write_production_fixtures() -> dict[str, Any]:
    from src.ingestion.source_packs import validate_source_pack
    from src.ingestion.weather_sources import replay_native_fixture

    manifest = json.loads(PACK.read_text())
    validated = {s["source_id"]: s for s in validate_source_pack(manifest)["sources"]}
    hashes = {}
    for entry in manifest["sources"]:
        fixture = {
            "captured": False,
            "provider": entry["weather"]["provider"],
            "note": (
                "Authored bodies served for the production URLs. They name fictional stations, warncells "
                "and alerts only, so the production selection is out of scope for every item; parsing is "
                "exercised with fictional selections in tests/unit/weather/harness.py."
            ),
            "native_pages": production_pages(entry),
            "scenarios": [
                "bounded-selection",
                "out-of-scope-items-counted",
                "user-agent-sent",
            ],
        }
        path = ROOT / f"tests/fixtures/source_packs/weather-{entry['source_id']}.json"
        path.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n")
        raw = path.read_bytes()
        output = replay_native_fixture(
            copy.deepcopy(validated[entry["source_id"]]), json.loads(raw)
        )
        entry["fixture"] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "expected_output_hash": _digest(output),
        }
        hashes[entry["source_id"]] = entry["fixture"]
    PACK.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return hashes


if __name__ == "__main__":
    print(json.dumps(write_production_fixtures(), indent=2))
