"""EIA bulk download files (FA04): keyless alternative to the EIA API.

``https://www.eia.gov/opendata/bulk/manifest.txt`` lists datasets (ELEC, EBA,
NG, PET, TOTAL, SEDS, INTL, AEO.<year>, …) with ``last_updated`` and an
``accessURL`` to a ZIP (3 MB–700 MB) holding one JSONL file: one series per line
(``series_id``, ``name``, ``units``, ``f``, ``data`` = [[period, value], …];
category lines carry ``category_id`` and are skipped). No key is needed.

Parameters: ``datasets`` (required, e.g. ["TOTAL", "ELEC"]), and ``series``
(ids) and/or ``series_prefix``, or ``all_series=true``; ``since_period``
(e.g. "2015" or "201501"). EIA data are U.S. government works (public domain).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Iterator, Mapping

from src.ingestion.bulk.adapters._common import number, text_set, zip_members
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile, TableSpec

MANIFEST = "https://www.eia.gov/opendata/bulk/manifest.txt"


class EiaBulk(BulkAdapter):
    name = "eia-bulk"
    publisher = "U.S. Energy Information Administration"
    title = "EIA bulk download files"
    description = "Series metadata and observations from EIA bulk ZIP files, filtered to declared series."
    allowed_hosts = ("www.eia.gov", "api.eia.gov")
    tables = {
        "series": TableSpec("series", [{"name": "dataset", "type": "string"}, {"name": "series_id", "type": "string"},
                                       {"name": "name", "type": "string"}, {"name": "units", "type": "string"},
                                       {"name": "frequency", "type": "string"}, {"name": "last_updated", "type": "string"}],
                            primary_key=("series_id",)),
        "observations": TableSpec("observations", [{"name": "series_id", "type": "string"}, {"name": "period", "type": "string"},
                                                   {"name": "value", "type": "number"}],
                                  primary_key=("series_id", "period")),
    }

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        datasets = text_set(params.get("datasets"), upper=True)
        if not datasets:
            raise ValueError("datasets is required, e.g. [\"TOTAL\"]")
        series = text_set(params.get("series"), upper=True)
        prefix = str(params.get("series_prefix") or "").upper()
        if not (series or prefix or params.get("all_series")):
            raise ValueError("give series, series_prefix or all_series=true")
        return {"datasets": datasets, "series": series, "series_prefix": prefix,
                "all_series": bool(params.get("all_series")),
                "since_period": str(params["since_period"]) if params.get("since_period") else None}

    def list_release(self, http, params) -> Release:
        manifest = json.loads(http.get_text(MANIFEST))["dataset"]
        files, latest = [], []
        for name in params["datasets"]:
            entry = manifest.get(name)
            if not entry:
                raise ValueError(f"EIA dataset {name!r} is not in the bulk manifest")
            url = entry["accessURL"].replace("http://", "https://")
            head = http.head(url)
            files.append(ReleaseFile(name=f"{name}.zip", url=url, mode="download",
                                     size=int(head["content-length"]) if head.get("content-length") else None,
                                     etag=head.get("etag"), last_modified=head.get("last-modified"),
                                     metadata={"dataset": name, "last_updated": entry.get("last_updated")}))
            latest.append(entry.get("last_updated") or "")
        newest = max(latest)
        release_id = "eia-" + "-".join(params["datasets"]) + "-" + newest[:19].replace(":", "").replace("-", "")
        return Release("eia", release_id[:120], files, published_at=newest[:19] + "Z" if newest else None,
                       licence={"id": "public-domain-us-gov", "attribution": "U.S. Energy Information Administration",
                                "terms_url": "https://www.eia.gov/about/copyrights_reuse.php"})

    def _wanted(self, sid: str, params) -> bool:
        return params["all_series"] or sid in params["series"] or bool(params["series_prefix"]) and sid.startswith(params["series_prefix"])

    def process(self, source: FileSource, params) -> Iterator[Any]:
        dataset = source.file.metadata.get("dataset")
        since = params["since_period"]
        for handle in zip_members(source.path, (".txt", ".json", ".jsonl")):
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                sid = str(item.get("series_id") or "").upper()
                if not sid or not self._wanted(sid, params):
                    continue
                yield "series", {"dataset": dataset, "series_id": sid, "name": item.get("name"),
                                 "units": item.get("units"), "frequency": item.get("f"),
                                 "last_updated": item.get("last_updated")}
                for period, value in item.get("data") or []:
                    period = str(period)
                    if since and period[:len(since)] < since:
                        continue
                    yield "observations", {"series_id": sid, "period": period, "value": number(value)}
