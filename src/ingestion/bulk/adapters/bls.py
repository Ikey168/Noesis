"""BLS LABSTAT flat files (FA03): the keyless bulk alternative to the BLS API.

https://download.bls.gov/pub/time.series/<survey>/ lists one directory per
survey (``cu`` = CPI-U, ``ce`` = employment, …) with tab-separated files:
``<survey>.series`` (series metadata) and ``<survey>.data.*`` (observations:
series_id, year, period, value, footnote_codes; fields are space-padded, lines
end in CRLF). The server answers 403 without a User-Agent that names a contact,
and sends ETag/Last-Modified, so unchanged files are skipped between runs.

Parameters: ``survey`` (required), ``series`` (list of series ids) and/or
``series_prefix``, ``since_year``, ``files`` (file names; default the series file
and ``<survey>.data.0.Current``). BLS data are U.S. federal government works
(public domain); cite the Bureau of Labor Statistics.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterator, Mapping

from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile, TableSpec

BASE = "https://download.bls.gov/pub/time.series/"
_SURVEY = re.compile(r"^[a-z]{2}$")


class BlsFlatFiles(BulkAdapter):
    name = "bls-flat-files"
    publisher = "U.S. Bureau of Labor Statistics"
    title = "BLS LABSTAT time-series flat files"
    description = "Series metadata and observations from download.bls.gov/pub/time.series, filtered to declared series."
    allowed_hosts = ("download.bls.gov",)
    output = "rows"
    tables = {
        "series": TableSpec("series", [
            {"name": "survey", "type": "string"}, {"name": "series_id", "type": "string"},
            {"name": "series_title", "type": "string"}, {"name": "attributes", "type": "json"}],
            primary_key=("series_id",)),
        "observations": TableSpec("observations", [
            {"name": "survey", "type": "string"}, {"name": "series_id", "type": "string"},
            {"name": "year", "type": "integer"}, {"name": "period", "type": "string"},
            {"name": "value", "type": "number"}, {"name": "footnote_codes", "type": "string"}],
            primary_key=("series_id", "year", "period")),
    }

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        survey = str(params.get("survey") or "").lower()
        if not _SURVEY.match(survey):
            raise ValueError("survey must be a two-letter BLS survey code, e.g. 'cu'")
        series = sorted({str(s).strip().upper() for s in params.get("series") or [] if str(s).strip()})
        prefix = str(params.get("series_prefix") or "").strip().upper()
        if not series and not prefix and not params.get("all_series"):
            raise ValueError("give series, series_prefix, or all_series=true")
        files = list(params.get("files") or [f"{survey}.series", f"{survey}.data.0.Current"])
        bad = [f for f in files if not f.startswith(survey + ".")]
        if bad:
            raise ValueError(f"files must belong to survey {survey}: {bad}")
        since = params.get("since_year")
        return {"survey": survey, "series": series, "series_prefix": prefix,
                "all_series": bool(params.get("all_series")), "files": files,
                "since_year": int(since) if since is not None else None}

    def list_release(self, http, params) -> Release:
        survey = params["survey"]
        listing = http.get_text(f"{BASE}{survey}/")
        available = set(re.findall(rf">({survey}\.[A-Za-z0-9_.]+)<", listing))
        missing = [f for f in params["files"] if f not in available]
        if missing:
            raise ValueError(f"not in the BLS listing for {survey}: {missing}")
        files, newest = [], None
        for name in params["files"]:
            url = f"{BASE}{survey}/{name}"
            head = http.head(url)
            modified = head.get("last-modified")
            if modified:
                stamp = parsedate_to_datetime(modified)
                newest = stamp if newest is None or stamp > newest else newest
            files.append(ReleaseFile(name=name, url=url, size=int(head["content-length"]) if head.get("content-length") else None,
                                     etag=head.get("etag"), last_modified=modified, mode="stream"))
        published = newest.strftime("%Y-%m-%dT%H:%M:%SZ") if newest else None
        release_id = f"{survey}-" + (newest.strftime("%Y%m%dT%H%M%SZ") if newest else datetime.utcnow().strftime("%Y%m%d"))
        return Release(provider="bls", release_id=release_id, files=files, published_at=published,
                       licence={"id": "public-domain-us-gov", "attribution": "U.S. Bureau of Labor Statistics",
                                "terms_url": "https://www.bls.gov/opub/copyright-information.htm"},
                       metadata={"survey": survey})

    def _wanted(self, series_id: str, params: Mapping[str, Any]) -> bool:
        if params["all_series"]:
            return True
        return series_id in params["series"] or bool(params["series_prefix"]) and series_id.startswith(params["series_prefix"])

    def process(self, source: FileSource, params) -> Iterator[Any]:
        survey, name = params["survey"], source.file.name
        with source.open_stream() as raw:
            lines = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
            header = [h.strip() for h in next(lines).rstrip("\r\n").split("\t")]
            for line in lines:
                fields = [v.strip() for v in line.rstrip("\r\n").split("\t")]
                if len(fields) < 2:
                    continue
                row = dict(zip(header, fields))
                series_id = row.get("series_id", "")
                if not self._wanted(series_id, params):
                    continue
                if name.endswith(".series"):
                    yield "series", {"survey": survey, "series_id": series_id,
                                     "series_title": row.get("series_title"),
                                     "attributes": {k: v for k, v in row.items()
                                                    if k not in ("series_id", "series_title") and v != ""}}
                else:
                    year = int(row["year"])
                    if params["since_year"] and year < params["since_year"]:
                        continue
                    value = row.get("value", "")
                    yield "observations", {"survey": survey, "series_id": series_id, "year": year,
                                           "period": row.get("period"),
                                           "value": float(value) if value not in ("", "-") else None,
                                           "footnote_codes": row.get("footnote_codes") or None}
