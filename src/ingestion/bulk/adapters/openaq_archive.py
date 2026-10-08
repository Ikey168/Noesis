"""OpenAQ open-data archive on AWS S3 (FA16): keyless alternative to the OpenAQ API.

``https://openaq-data-archive.s3.amazonaws.com/records/csv.gz/locationid=<id>/year=<y>/month=<mm>/``
holds one gzipped CSV per location and day (location_id, sensors_id, location,
datetime, lat, lon, parameter, units, value). Only the declared locations and
months are listed and fetched; files keep their ETag, so re-runs skip unchanged
days. Licences are per location as published by OpenAQ (most CC BY 4.0) — check
the location's licence before redistribution.

Parameters: ``location_ids`` (required), ``date_from`` / ``date_to``
(YYYY-MM-DD, inclusive; at most 24 months per run), ``parameters`` (e.g.
["pm25", "no2"]).
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import date
from typing import Any, Dict, Iterator, List, Mapping

from src.ingestion.bulk.adapters._common import csv_rows, decompressed, number
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile, TableSpec

BUCKET = "https://openaq-data-archive.s3.amazonaws.com/"
_KEY = re.compile(r"<Key>([^<]+)</Key><LastModified>([^<]+)</LastModified><ETag>&quot;([^&]+)&quot;</ETag><Size>(\d+)")
_DAY = re.compile(r"location-\d+-(\d{8})\.csv\.gz$")


class OpenAqArchive(BulkAdapter):
    name = "openaq-archive"
    publisher = "OpenAQ"
    title = "OpenAQ open-data archive"
    description = "Hourly air-quality measurements for declared locations from the OpenAQ S3 archive."
    allowed_hosts = ("openaq-data-archive.s3.amazonaws.com",)
    tables = {"measurements": TableSpec("measurements", [
        {"name": "location_id", "type": "integer"}, {"name": "sensor_id", "type": "integer"},
        {"name": "location", "type": "string"}, {"name": "datetime", "type": "string"},
        {"name": "lat", "type": "number"}, {"name": "lon", "type": "number"},
        {"name": "parameter", "type": "string"}, {"name": "units", "type": "string"}, {"name": "value", "type": "number"}],
        primary_key=("sensor_id", "datetime"))}

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        ids = sorted({int(i) for i in params.get("location_ids") or []})
        if not ids:
            raise ValueError("location_ids is required")
        start, end = date.fromisoformat(params["date_from"]), date.fromisoformat(params["date_to"])
        if end < start or (end.year - start.year) * 12 + end.month - start.month >= 24:
            raise ValueError("date range must be ordered and at most 24 months")
        return {"location_ids": ids, "date_from": start.isoformat(), "date_to": end.isoformat(),
                "parameters": sorted({str(p).lower() for p in params.get("parameters") or []})}

    def list_release(self, http, params) -> Release:
        start, end = date.fromisoformat(params["date_from"]), date.fromisoformat(params["date_to"])
        months, y, m = [], start.year, start.month
        while (y, m) <= (end.year, end.month):
            months.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        files: List[ReleaseFile] = []
        for location in params["location_ids"]:
            for y, m in months:
                prefix = f"records/csv.gz/locationid={location}/year={y}/month={m:02d}/"
                body = http.get_text(BUCKET + "?" + urllib.parse.urlencode({"list-type": "2", "prefix": prefix}))
                for key, modified, etag, size in _KEY.findall(body):
                    day = _DAY.search(key)
                    if not day:
                        continue
                    iso = f"{day.group(1)[:4]}-{day.group(1)[4:6]}-{day.group(1)[6:]}"
                    if params["date_from"] <= iso <= params["date_to"]:
                        files.append(ReleaseFile(key.rsplit("/", 1)[1], BUCKET + key, size=int(size), etag=etag,
                                                 last_modified=modified, mode="stream", metadata={"location_id": location}))
        if not files:
            raise ValueError("no archive files for those locations and dates")
        newest = max(f.last_modified or "" for f in files)[:10]
        return Release("openaq", f"openaq-{params['date_from']}-{params['date_to']}-{newest}", files,
                       licence={"id": "per-location", "attribution": "OpenAQ",
                                "terms_url": "https://docs.openaq.org/about/terms",
                                "note": "licence per location as published by OpenAQ; check before redistribution"})

    def process(self, source: FileSource, params) -> Iterator[Any]:
        wanted = params["parameters"]
        with source.open_stream() as raw:
            for row in csv_rows(decompressed(raw, source.file.name)):
                if wanted and row.get("parameter", "").lower() not in wanted:
                    continue
                yield "measurements", {"location_id": int(row["location_id"]), "sensor_id": int(row["sensors_id"]),
                                       "location": row.get("location"), "datetime": row.get("datetime"),
                                       "lat": number(row.get("lat")), "lon": number(row.get("lon")),
                                       "parameter": row.get("parameter"), "units": row.get("units"),
                                       "value": number(row.get("value"))}
