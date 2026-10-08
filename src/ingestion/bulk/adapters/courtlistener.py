"""CourtListener bulk data (FA11) — only the tables that are still published.

The public bucket ``com-courtlistener-storage`` (``bulk-data/``) holds
bz2-compressed CSV files named ``<table>-YYYY-MM-DD.csv.bz2``. Checked October
2026: **dockets, opinions, opinion clusters and citations have been published
as empty files since 2024-03-01**, so case law cannot come from bulk data; the
free API token (5 req/min, 125/day) or a free EDU membership remains the route
for those. Still published (latest 2025-01-31): ``courts``, the judges database
(``people-db-*``) and financial disclosures (``financial-disclosures*``).

Parameters: ``tables`` (required, e.g. ["courts", "people-db-people"]), optional
``where`` ({column: [values]}) applied to every table that has that column.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, Iterator, List, Mapping

from src.ingestion.bulk.adapters._common import csv_rows, decompressed
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile, TableSpec

BUCKET = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/"
EMPTY_SINCE = {"dockets", "opinions", "opinion-clusters", "citations"}
_KEY = re.compile(r"<Key>([^<]+)</Key><LastModified>([^<]+)</LastModified><ETag>&quot;([^&]+)&quot;</ETag><Size>(\d+)")
_NAME = re.compile(r"^bulk-data/([a-z0-9-]+?)-(\d{4}-\d{2}-\d{2})\.csv\.bz2$")


class CourtListenerBulk(BulkAdapter):
    name = "courtlistener-bulk"
    publisher = "Free Law Project (CourtListener)"
    title = "CourtListener bulk data (courts, judges, financial disclosures)"
    description = "Tables still published in CourtListener's bulk bucket; case-law tables are empty since 2024-03."
    allowed_hosts = ("com-courtlistener-storage.s3-us-west-2.amazonaws.com",)
    tables = {"records": TableSpec("records", [{"name": "table", "type": "string"}, {"name": "id", "type": "string"},
                                               {"name": "record", "type": "json"}], primary_key=("table", "id"))}

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        tables = sorted({str(t).strip() for t in params.get("tables") or [] if str(t).strip()})
        if not tables:
            raise ValueError("tables is required, e.g. [\"courts\"]")
        unavailable = sorted(set(tables) & EMPTY_SINCE)
        if unavailable:
            raise ValueError(f"{unavailable} are published empty since 2024-03-01; use the API or an EDU membership")
        where = {str(k): [str(v) for v in (vals if isinstance(vals, list) else [vals])]
                 for k, vals in (params.get("where") or {}).items()}
        return {"tables": tables, "where": where}

    def _listing(self, http) -> List[tuple]:
        keys, token = [], None
        while True:
            query = {"list-type": "2", "prefix": "bulk-data/", "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            body = http.get_text(BUCKET + "?" + urllib.parse.urlencode(query))
            keys += _KEY.findall(body)
            match = re.search(r"<NextContinuationToken>([^<]+)<", body)
            if not match:
                return keys
            token = match.group(1)

    def list_release(self, http, params) -> Release:
        latest: Dict[str, tuple] = {}
        for key, modified, etag, size in self._listing(http):
            match = _NAME.match(key)
            if match and match.group(1) in params["tables"] and int(size) > 64:
                table, date = match.groups()
                if table not in latest or date > latest[table][0]:
                    latest[table] = (date, key, etag, int(size), modified)
        missing = [t for t in params["tables"] if t not in latest]
        if missing:
            raise ValueError(f"no non-empty bulk file for {missing}")
        files = [ReleaseFile(name=f"{t}.csv.bz2", url=BUCKET + key, size=size, etag=etag, last_modified=modified,
                             mode="stream", metadata={"table": t, "date": date})
                 for t, (date, key, etag, size, modified) in sorted(latest.items())]
        newest = max(v[0] for v in latest.values())
        return Release("courtlistener", f"courtlistener-{newest}", files, published_at=newest,
                       licence={"id": "courtlistener-bulk", "attribution": "Free Law Project, CourtListener",
                                "terms_url": "https://www.courtlistener.com/terms/"})

    def process(self, source: FileSource, params) -> Iterator[Any]:
        table = source.file.metadata["table"]
        where = params["where"]
        with source.open_stream() as raw:
            for row in csv_rows(decompressed(raw, source.file.name)):
                if any(col in row and row[col] not in values for col, values in where.items()):
                    continue
                yield "records", {"table": table, "id": row.get("id") or row.get("pk") or "", "record": row}
