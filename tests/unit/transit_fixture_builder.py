"""Deterministic GTFS Schedule ZIPs and GTFS-Realtime protobufs for transit tests.

Run as a script to regenerate ``tests/fixtures/source_packs/transit-gtfs.json``.
All agencies, routes, stops and trips are fictional.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/source_packs/transit-gtfs.json"
SCHEDULE_URL = "https://www.vbb.de/vbbgtfs"
REALTIME_URL = "https://transit.example.org/gtfs-rt/fixture.pb"
# 2026-09-28 08:05 Europe/Berlin (CEST, UTC+2)
HEADER_TIMESTAMP = int(datetime(2026, 9, 28, 6, 5, tzinfo=UTC).timestamp())

STOPS = {"S-A": ("Fixture Platz", 52.5200, 13.4050), "S-B": ("Fixture Brücke", 52.5300, 13.4100),
         "S-C": ("Fixture Endhalt", 52.5400, 13.4200), "S-Z": ("Unserved stop", 52.6000, 13.5000)}


def _csv(header: str, rows: Sequence[str]) -> str:
    return "\n".join([header, *rows]) + "\n"


def schedule_files(*, relocate_b: bool = False, feed_version: str = "2026-09-20") -> dict[str, str]:
    stops = dict(STOPS)
    if relocate_b:
        stops["S-B"] = ("Fixture Brücke", 52.5310, 13.4115)
    return {
        "agency.txt": _csv("agency_id,agency_name,agency_url,agency_timezone",
                           ["FX,Fictional Transit Berlin,https://transit.example.org,Europe/Berlin"]),
        "routes.txt": _csv("route_id,agency_id,route_short_name,route_type",
                           ["R1,FX,X1,3", "R2,FX,X2,3", "R9,FX,X9,3"]),
        "calendar.txt": _csv("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date",
                             ["WK,1,1,1,1,1,0,0,20260901,20261231", "SA,0,0,0,0,0,1,0,20260901,20261231"]),
        "calendar_dates.txt": _csv("service_id,date,exception_type", ["WK,20260930,2", "SA,20260930,1"]),
        "trips.txt": _csv("route_id,service_id,trip_id,trip_headsign,shape_id",
                          ["R1,WK,T1,Nord,SH1", "R1,WK,T2,Nord (Nacht),SH1", "R2,WK,T3,Ost,", "R1,SA,T4,Nord,SH1",
                           "R9,WK,T9,Unpinned,"]),
        "stop_times.txt": _csv("trip_id,arrival_time,departure_time,stop_id,stop_sequence",
                               ["T1,08:00:00,08:00:00,S-A,1", "T1,08:10:00,08:10:00,S-B,2",
                                "T2,24:30:00,24:30:00,S-A,1", "T2,25:05:00,25:05:00,S-B,2",
                                "T3,09:00:00,09:00:00,S-B,1", "T3,09:15:00,09:15:00,S-C,2",
                                "T4,10:00:00,10:00:00,S-A,1", "T9,11:00:00,11:00:00,S-A,1"]),
        "stops.txt": _csv("stop_id,stop_name,stop_lat,stop_lon",
                          [f"{k},{v[0]},{v[1]},{v[2]}" for k, v in stops.items()]),
        "shapes.txt": _csv("shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence",
                           ["SH1,52.5200,13.4050,1", "SH1,52.5250,13.4080,2", "SH1,52.5300,13.4100,3"]),
        "feed_info.txt": _csv("feed_publisher_name,feed_publisher_url,feed_lang,feed_version",
                              [f"Fictional Transit Berlin,https://transit.example.org,de,{feed_version}"]),
    }


def build_zip(files: Mapping[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content.encode() if isinstance(content, str) else content)
    return buffer.getvalue()


# ------------------------------------------------------------ protobuf encoder


def _varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, value: Any) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    raw = value.encode() if isinstance(value, str) else bytes(value)
    return _varint(number << 3 | 2) + _varint(len(raw)) + raw


def message(*fields: tuple[int, Any]) -> bytes:
    return b"".join(_field(number, value) for number, value in fields if value is not None)


def trip(trip_id: str, route_id: str, *, relationship: int = 0) -> bytes:
    return message((1, trip_id), (3, "20260928"), (4, relationship or None), (5, route_id))


def feed_message(entities: Sequence[bytes], *, timestamp: int = HEADER_TIMESTAMP) -> bytes:
    header = message((1, "2.0"), (2, 0), (3, timestamp))
    return message((1, header), *((2, entity) for entity in entities))


def realtime_entities() -> list[bytes]:
    delayed = message((1, "tu-1"), (3, message(
        (1, trip("T1", "R1")),
        (2, message((1, 2), (3, message((1, 120))), (4, "S-B"))))))
    cancelled = message((1, "tu-3"), (3, message((1, trip("T3", "R2", relationship=3)))))
    unpinned = message((1, "tu-9"), (3, message((1, trip("T9", "R9")),
                                                (2, message((1, 1), (3, message((1, 600))), (4, "S-A"))))))
    detour = message((1, "alert-1"), (5, message(
        (1, message((1, HEADER_TIMESTAMP - 3600), (2, HEADER_TIMESTAMP + 7200))),
        (5, message((2, "R2"))), (7, 4),
        (10, message((1, message((1, "Umleitung Linie X2"), (2, "de"))))))))
    unknown_field = message((1, "future"), (99, "ignored by decoders"))
    return [delayed, cancelled, unpinned, detour, unknown_field]


def fixture() -> dict[str, Any]:
    return {
        "authored": True,
        "provider": "GTFS Schedule + GTFS Realtime (fictional agency)",
        "note": "Authored GTFS ZIP and GTFS-RT FeedMessage for a fictional Berlin-area agency; not a capture of "
                "any operator feed. Regenerate with python tests/unit/transit_fixture_builder.py.",
        "scenarios": ["calendar-exception", "realtime-cancellation", "realtime-delay", "realtime-detour-alert",
                      "route-shape", "service-after-midnight", "unknown-protobuf-field", "unpinned-route-filtered"],
        "native_pages": [
            {"url": SCHEDULE_URL, "status": 200,
             "body_base64": base64.b64encode(build_zip(schedule_files())).decode()},
            {"url": REALTIME_URL, "status": 200,
             "body_base64": base64.b64encode(feed_message(realtime_entities())).decode()},
        ],
    }


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(fixture(), indent=1, ensure_ascii=False) + "\n")
    print(FIXTURE)
