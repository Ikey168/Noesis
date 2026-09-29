"""Authored native weather documents for fictional stations (WX03-WX06, WX13).

Nothing here is captured data. The stations are fictional with realistic
identifier shapes: DWD station ``99901`` "Musterstadt-Flugfeld" (relocated on
2026-06-01), MOSMIX/WMO-style id ``10999``, ICAO ``EDXM`` (the same site as
stated by the MOSMIX catalogue), US ICAO ``KXQM``, NWS office grid ``ZZX/12,34``,
DWD warncell ``199901000`` and NWS zone ``MAZ999``. Bodies follow the documented
formats in ``docs/development/weather-evidence/source-audit.md``. ZIP members
carry a fixed timestamp, so the bytes and their hashes are reproducible.
"""

from __future__ import annotations

import io
import json
import zipfile

DWD = "99901"
MOSMIX = "10999"
ICAO = "EDXM"
US_ICAO = "KXQM"
WARNCELL = "199901000"
UGC = "MAZ999"
GRID = {"wfo": "ZZX", "x": "12", "y": "34"}
POINT = ["41.0000", "-70.0000"]
SENDER = "opendata@dwd.de"
NWS_SENDER = "w-nws.webmaster@noaa.gov"
WARNCELL_RING = [
    [13.45, 52.33],
    [13.62, 52.33],
    [13.62, 52.43],
    [13.45, 52.43],
    [13.45, 52.33],
]
NWS_RING = [[-70.1, 40.9], [-69.9, 40.9], [-69.9, 41.1], [-70.1, 41.1], [-70.1, 40.9]]


def zipped(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue()


# ---------------------------------------------------------------------- DWD CDC

GEOGRAPHY = (
    "Stations_id;Stationshoehe;Geogr.Breite;Geogr.Laenge;von_datum;bis_datum;Stationsname\n"
    f"{DWD};38;52.3700;13.5100;19900101;20260531;Musterstadt-Flugfeld\n"
    f"{DWD};41;52.3810;13.5290;20260601;;Musterstadt-Flugfeld\n"
)
DESCRIPTION = (
    "Stations_id von_datum bis_datum Stationshoehe geoBreite geoLaenge Stationsname Bundesland Abgabe\n"
    "----------- --------- --------- ------------- --------- --------- ----------------------------------------- "
    "---------- --------\n"
    f"{DWD} 19900101 20260609             41     52.3810   13.5290 Musterstadt-Flugfeld"
    "                     Brandenburg                                                                       Frei\n"
)


def dwd_description() -> bytes:
    return DESCRIPTION.encode("latin-1")


def dwd_10min(
    rows: list[tuple[str, str, str, str]],
    *,
    name: str = "produkt_zehn_min_tu_20260531_20260610_99901.txt",
) -> bytes:
    """rows: (MESS_DATUM YYYYMMDDHHMM, QN, TT_10, RF_10)."""

    lines = ["STATIONS_ID;MESS_DATUM;  QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor"]
    lines += [
        f"{int(DWD)};{stamp};{qn:>4};-999;{tt:>5};-999;{rf:>5};-999;eor"
        for stamp, qn, tt, rf in rows
    ]
    return zipped(
        [
            (name, ("\n".join(lines) + "\n").encode("latin-1")),
            (f"Metadaten_Geographie_{DWD}.txt", GEOGRAPHY.encode("latin-1")),
        ]
    )


RECENT_10MIN = [
    ("202605311200", "1", "14.1", "70.0"),  # before the relocation (old site)
    ("202606101200", "1", "18.4", "55.0"),
    ("202606101300", "1", "19.5", "52.0"),
    ("202606101310", "1", "-999", "51.0"),  # published missing temperature
]
# The later quality-controlled release revises 12:00 (18.4 -> 18.6) and sets QN 10.
HISTORICAL_10MIN = [
    ("202605311200", "10", "14.1", "70.0"),
    ("202606101200", "10", "18.6", "55.0"),
    ("202606101300", "10", "19.5", "52.0"),
    ("202606101310", "10", "-999", "51.0"),
]
HISTORICAL_FILE = f"10minutenwerte_TU_{DWD}_20200101_20260630_hist.zip"


def dwd_hourly_rr() -> bytes:
    lines = [
        "STATIONS_ID;MESS_DATUM;QN_8;  R1;RS_IND;WRTR;eor",
        f"{int(DWD)};2026061012;    3;   0.0;     0;   -999;eor",
        f"{int(DWD)};2026061013;    3;   0.4;     1;   -999;eor",
    ]
    return zipped(
        [
            (
                "produkt_rr_stunde_20260531_20260610_99901.txt",
                ("\n".join(lines) + "\n").encode("latin-1"),
            ),
            (f"Metadaten_Geographie_{DWD}.txt", GEOGRAPHY.encode("latin-1")),
        ]
    )


# ------------------------------------------------------------------------ MOSMIX

MOSMIX_RUNS = {
    # issue time -> {element: [values at 12:00Z, 13:00Z]}
    "2026-06-10T03:00:00.000Z": {
        "TTT": ["291.35", "292.15"],
        "R101": ["10.00", "70.00"],
        "RR1c": ["0.00", "0.30"],
    },
    "2026-06-10T09:00:00.000Z": {
        "TTT": ["291.75", "292.45"],
        "R101": ["5.00", "80.00"],
        "RR1c": ["0.00", "0.50"],
    },
    "2026-06-10T12:00:00.000Z": {
        "TTT": ["291.95", "292.65"],
        "R101": ["2.00", "90.00"],
        "RR1c": ["-", "0.60"],
    },
}
STEPS = ["2026-06-10T12:00:00.000Z", "2026-06-10T13:00:00.000Z"]


def mosmix_kmz(issue: str) -> bytes:
    values = MOSMIX_RUNS[issue]
    forecasts = "".join(
        f'<dwd:Forecast dwd:elementName="{name}"><dwd:value>     {"     ".join(vals)}</dwd:value></dwd:Forecast>'
        for name, vals in values.items()
    )
    kml = (
        '<?xml version="1.0" encoding="ISO-8859-1" standalone="yes"?>'
        '<kml:kml xmlns:dwd="https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd" '
        'xmlns:kml="http://www.opengis.net/kml/2.2"><kml:Document><kml:ExtendedData><dwd:ProductDefinition>'
        "<dwd:Issuer>Deutscher Wetterdienst</dwd:Issuer><dwd:ProductID>MOSMIX</dwd:ProductID>"
        f"<dwd:GeneratingProcess>DWD MOSMIX hourly, Version 1.0</dwd:GeneratingProcess><dwd:IssueTime>{issue}"
        "</dwd:IssueTime><dwd:ForecastTimeSteps>"
        + "".join(f"<dwd:TimeStep>{s}</dwd:TimeStep>" for s in STEPS)
        + "</dwd:ForecastTimeSteps></dwd:ProductDefinition></kml:ExtendedData>"
        f"<kml:Placemark><kml:name>{MOSMIX}</kml:name><kml:description>MUSTERSTADT/FLUGFELD</kml:description>"
        f"<kml:ExtendedData>{forecasts}</kml:ExtendedData><kml:Point><kml:coordinates>13.5288,52.3812,41.0"
        "</kml:coordinates></kml:Point></kml:Placemark></kml:Document></kml:kml>"
    )
    stamp = issue[:13].replace("-", "").replace("T", "")
    return zipped([(f"MOSMIX_L_{stamp}_{MOSMIX}.kml", kml.encode("latin-1"))])


def mosmix_catalogue() -> bytes:
    return (
        "ID    ICAO NAME                 LAT    LON     ELEV\n"
        "----- ---- -------------------- -----  ------- -----\n"
        f"{MOSMIX} {ICAO} MUSTERSTADT/FLUGFELD 52.23   13.32    41\n"
        "10998 ---- BEISPIELHEIM         52.10   13.10    60\n"
    ).encode("latin-1")


# ---------------------------------------------------------------------------- CAP


def cap(
    identifier: str,
    sent: str,
    msg_type: str,
    *,
    references: str = "",
    severity: str = "Minor",
    expires: str = "2026-06-11T08:00:00+00:00",
    warncell: str = WARNCELL,
    headline: str = "Amtliche WARNUNG vor FROST",
    area: str = "Musterkreis",
) -> bytes:
    ring = " ".join(f"{lat},{lon}" for lon, lat in WARNCELL_RING)
    refs = f"<references>{references}</references>" if references else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier><sender>{SENDER}</sender><sent>{sent}</sent>"
        f"<status>Actual</status><msgType>{msg_type}</msgType><source>PVW</source><scope>Public</scope>{refs}"
        "<info><language>de-DE</language><category>Met</category><event>FROST</event>"
        "<responseType>Prepare</responseType>"
        f"<urgency>Immediate</urgency><severity>{severity}</severity><certainty>Likely</certainty>"
        "<eventCode><valueName>II</valueName><value>22</value></eventCode>"
        f"<effective>{sent}</effective><onset>2026-06-10T20:00:00+00:00</onset><expires>{expires}</expires>"
        f"<senderName>Deutscher Wetterdienst</senderName><headline>{headline}</headline>"
        "<description>Es tritt leichter Frost um -1 °C auf.</description>"
        "<instruction>Text des Herausgebers (fiktiv).</instruction>"
        f"<area><areaDesc>{area}</areaDesc><polygon>{ring}</polygon>"
        f"<geocode><valueName>WARNCELLID</valueName><value>{warncell}</value></geocode></area>"
        "</info></alert>"
    ).encode()


A1 = "2.49.0.0.276.0.DWD.PVW.1781071200000.fixture-a1"
U1 = "2.49.0.0.276.0.DWD.PVW.1781092800000.fixture-u1"
C1 = "2.49.0.0.276.0.DWD.PVW.1781143200000.fixture-c1"
O1 = "2.49.0.0.276.0.DWD.PVW.1781071200000.fixture-o1"
CAP_STAGES = {
    "alert": [
        (f"{A1}.xml", cap(A1, "2026-06-10T08:00:00+02:00", "Alert")),
        (
            f"{O1}.xml",
            cap(
                O1,
                "2026-06-10T08:00:00+02:00",
                "Alert",
                warncell="199999000",
                area="Anderswo",
            ),
        ),
    ],
    "update": [
        (
            f"{U1}.xml",
            cap(
                U1,
                "2026-06-10T14:00:00+02:00",
                "Update",
                severity="Moderate",
                expires="2026-06-11T09:00:00+00:00",
                references=f"{SENDER},{A1},2026-06-10T08:00:00+02:00",
            ),
        )
    ],
    "cancel": [
        (
            f"{C1}.xml",
            cap(
                C1,
                "2026-06-11T04:00:00+02:00",
                "Cancel",
                references=f"{SENDER},{A1},2026-06-10T08:00:00+02:00 "
                f"{SENDER},{U1},2026-06-10T14:00:00+02:00",
            ),
        )
    ],
}


def cap_zip(stage: str) -> bytes:
    return zipped(CAP_STAGES[stage])


# ------------------------------------------------------------------ aviationweather


def stationinfo() -> str:
    return json.dumps(
        [
            {
                "icaoId": ICAO,
                "iataId": "",
                "faaId": "",
                "wmoId": MOSMIX,
                "lat": 52.3805,
                "lon": 13.53,
                "elev": 41,
                "site": "Musterstadt/Flugfeld",
                "state": "",
                "country": "DE",
                "priority": 2,
            },
            {
                "icaoId": US_ICAO,
                "iataId": "",
                "faaId": "XQM",
                "wmoId": "",
                "lat": 41.01,
                "lon": -70.01,
                "elev": 5,
                "site": "Fictional Harbor",
                "state": "MA",
                "country": "US",
                "priority": 3,
            },
        ]
    )


def metar(stage: str) -> str:
    base = {
        "lat": 52.3805,
        "lon": 13.53,
        "elev": 41,
        "name": "Musterstadt/Flugfeld, DE",
        "altim": 1015,
        "wdir": 240,
        "wspd": 8,
        "visib": "6+",
        "qcField": 0,
    }
    first = {
        **base,
        "icaoId": ICAO,
        "receiptTime": "2026-06-10 11:55:12",
        "obsTime": 1781092200,
        "reportTime": "2026-06-10 11:50:00",
        "temp": 18,
        "dewp": 9,
        "metarType": "METAR",
        "rawOb": "METAR EDXM 101150Z 24008KT 9999 FEW030 18/09 Q1015",
    }
    corrected = {
        **first,
        "receiptTime": "2026-06-10 12:08:40",
        "temp": 19,
        "rawOb": "METAR COR EDXM 101150Z 24008KT 9999 FEW030 19/09 Q1015",
    }
    suspect = {
        **base,
        "icaoId": ICAO,
        "receiptTime": "2026-06-10 12:24:00",
        "obsTime": 1781094000,
        "temp": 22,
        "dewp": 9,
        "metarType": "METAR",
        "wdir": "VRB",
        "rawOb": "METAR EDXM 101220Z VRB03KT 9999 FEW030 22/09 Q1015 $",
    }
    us = {
        "lat": 41.01,
        "lon": -70.01,
        "elev": 5,
        "name": "Fictional Harbor, MA, US",
        "altim": 1016,
        "qcField": 4,
        "visib": "10+",
        "wdir": 200,
        "wspd": 6,
        "dewp": 10,
        "metarType": "METAR",
    }
    us1 = {
        **us,
        "icaoId": US_ICAO,
        "receiptTime": "2026-06-10 12:57:00",
        "obsTime": 1781096040,
        "temp": 16.7,
        "rawOb": "METAR KXQM 101254Z AUTO 20006KT 10SM CLR 17/10 A3001",
    }
    us2 = {
        **us,
        "icaoId": US_ICAO,
        "receiptTime": "2026-06-10 13:57:00",
        "obsTime": 1781099640,
        "temp": 18.0,
        "rawOb": "METAR KXQM 101354Z AUTO 20006KT 10SM CLR 18/10 A3001",
    }
    other = {
        **base,
        "icaoId": "EDZZ",
        "receiptTime": "2026-06-10 11:55:00",
        "obsTime": 1781092200,
        "temp": 5,
        "rawOb": "METAR EDZZ 101150Z 00000KT CAVOK 05/01 Q1020",
    }
    return json.dumps(
        {"first": [first, other, us1], "second": [corrected, suspect, us1, us2]}[stage]
    )


def taf() -> str:
    return json.dumps(
        [
            {
                "icaoId": ICAO,
                "issueTime": "2026-06-10 11:00:00",
                "bulletinTime": "2026-06-10 11:00:00",
                "validTimeFrom": 1781096400,
                "validTimeTo": 1781200800,
                "rawTAF": "TAF EDXM 101100Z 1012/1118 24008KT 9999 FEW030 TEMPO 1014/1018 SHRA",
                "fcsts": [{"timeFrom": 1781096400, "timeTo": 1781200800, "wspd": 8}],
            }
        ]
    )


# ---------------------------------------------------------------------------- NWS


def nws_points() -> str:
    return json.dumps(
        {
            "type": "Feature",
            "properties": {
                "gridId": GRID["wfo"],
                "gridX": int(GRID["x"]),
                "gridY": int(GRID["y"]),
                "forecastHourly": f"https://api.weather.gov/gridpoints/ZZX/{GRID['x']},{GRID['y']}/forecast/hourly",
                "observationStations": "https://api.weather.gov/gridpoints/ZZX/12,34/stations",
            },
        }
    )


NWS_RUNS = {
    "2026-06-10T09:00:00+00:00": [61, 63],
    "2026-06-10T11:00:00+00:00": [62, 64],
}


def nws_forecast(update: str) -> str:
    temps = NWS_RUNS[update]
    periods = []
    for number, (start, end, temp) in enumerate(
        zip(
            ["2026-06-10T13:00:00+00:00", "2026-06-10T14:00:00+00:00"],
            ["2026-06-10T14:00:00+00:00", "2026-06-10T15:00:00+00:00"],
            temps,
            strict=True,
        ),
        start=1,
    ):
        periods.append(
            {
                "number": number,
                "startTime": start,
                "endTime": end,
                "isDaytime": True,
                "temperature": temp,
                "temperatureUnit": "F",
                "windSpeed": "10 mph" if number == 1 else "10 to 15 mph",
                "windDirection": "SW",
                "probabilityOfPrecipitation": {
                    "unitCode": "wmoUnit:percent",
                    "value": 20,
                },
                "shortForecast": "Sunny",
            }
        )
    return json.dumps(
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-70.01, 40.99],
                        [-69.99, 40.99],
                        [-69.99, 41.01],
                        [-70.01, 41.01],
                        [-70.01, 40.99],
                    ]
                ],
            },
            "properties": {
                "updateTime": update,
                "generatedAt": "2026-06-10T11:40:00+00:00",
                "units": "us",
                "periods": periods,
            },
        }
    )


N1 = "urn:oid:2.49.0.1.840.0.fixture.001.1"
N2 = "urn:oid:2.49.0.1.840.0.fixture.002.1"


def nws_alert(
    identifier: str, sent: str, message_type: str, references: list[dict]
) -> dict:
    return {
        "id": f"https://api.weather.gov/alerts/{identifier}",
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [NWS_RING]},
        "properties": {
            "id": identifier,
            "areaDesc": "Fictional Harbor County",
            "sender": NWS_SENDER,
            "senderName": "NWS Fictional Office",
            "sent": sent,
            "effective": sent,
            "onset": "2026-06-11T02:00:00-04:00",
            "expires": "2026-06-11T09:00:00-04:00",
            "status": "Actual",
            "messageType": message_type,
            "category": "Met",
            "severity": "Minor",
            "certainty": "Likely",
            "urgency": "Expected",
            "event": "Frost Advisory",
            "headline": "Frost Advisory issued (fictional)",
            "description": "Issuer text (fictional).",
            "instruction": "Issuer instruction (fictional).",
            "geocode": {"SAME": ["025999"], "UGC": [UGC, "MAZ998"]},
            "references": references,
        },
    }


def nws_alerts(stage: str) -> str:
    first = nws_alert(N1, "2026-06-10T15:00:00-04:00", "Alert", [])
    update = nws_alert(
        N2,
        "2026-06-10T18:00:00-04:00",
        "Update",
        [
            {
                "@id": f"https://api.weather.gov/alerts/{N1}",
                "identifier": N1,
                "sender": NWS_SENDER,
                "sent": "2026-06-10T15:00:00-04:00",
            }
        ],
    )
    features = {"first": [first], "second": [first, update]}[stage]
    return json.dumps({"type": "FeatureCollection", "features": features})


# --------------------------------------------------------------------- Open-Meteo

OPEN_METEO_RUNS = {1781049600: ["18.3", "19.2"], 1781060400: ["18.5", "19.4"]}


def open_meteo_meta(init: int) -> str:
    return json.dumps(
        {
            "last_run_initialisation_time": init,
            "last_run_availability_time": init + 5400,
            "last_run_modification_time": init + 5300,
        }
    )


def open_meteo_forecast(init: int) -> str:
    return json.dumps(
        {
            "latitude": 52.38,
            "longitude": 13.53,
            "generationtime_ms": 0.1,
            "utc_offset_seconds": 0,
            "timezone": "GMT",
            "timezone_abbreviation": "GMT",
            "elevation": 41.0,
            "hourly_units": {"time": "iso8601", "temperature_2m": "°C"},
            "hourly": {
                "time": ["2026-06-10T12:00", "2026-06-10T13:00"],
                "temperature_2m": OPEN_METEO_RUNS[init],
            },
        }
    )


def dwd_hourly_tu() -> bytes:
    """Hourly air_temperature in the Climate & Environment layout (the Climate pack reads the same file)."""

    lines = [
        "STATIONS_ID;MESS_DATUM;QN_9;TT_TU;RF_TU;eor",
        f"{int(DWD)};2026061012;    3;  18.3;  56.0;eor",
        f"{int(DWD)};2026061013;    3;  19.4;  52.0;eor",
    ]
    return zipped(
        [
            (
                "produkt_tu_stunde_20260531_20260610_99901.txt",
                ("\n".join(lines) + "\n").encode("latin-1"),
            ),
            (f"Metadaten_Geographie_{DWD}.txt", GEOGRAPHY.encode("latin-1")),
        ]
    )
