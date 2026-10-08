"""OpenSanctions bulk data (FA08): free for non-commercial use, no key.

``https://data.opensanctions.org/datasets/latest/<collection>/index.json``
redirects to the current artifact version and lists its resources with size
and SHA-1 checksum. ``targets.simple.csv`` (~460 MB for ``default``) has one row
per target: id, schema, name, aliases, birth_date, countries, addresses,
identifiers, sanctions, phones, emails, dataset, first_seen, last_seen,
last_change. Licence: CC BY-NC 4.0 — business use requires a data licence;
every record carries the licence.

Parameters: ``collection`` (default "default", or e.g. "sanctions"), and at
least one of ``ids``, ``names`` (case-insensitive substring of name or alias),
``countries`` (ISO2, lower case as published), ``datasets``, ``schemata``, or
``all_targets=true``. Contact fields (phones, emails) are not kept.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, Mapping

from src.ingestion.bulk.adapters._common import csv_rows, text_set
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile, TableSpec

INDEX = "https://data.opensanctions.org/datasets/latest/{collection}/index.json"
LICENCE = {"id": "CC-BY-NC-4.0", "attribution": "OpenSanctions",
           "terms_url": "https://www.opensanctions.org/licensing/", "commercial_use": "requires a data licence"}


def _split(value: str):
    return [v for v in (value or "").split(";") if v]


class OpenSanctionsTargets(BulkAdapter):
    name = "opensanctions-targets"
    publisher = "OpenSanctions"
    title = "OpenSanctions targets (simplified CSV)"
    description = "Sanctioned and other targets from an OpenSanctions collection; non-commercial use only."
    allowed_hosts = ("data.opensanctions.org",)
    incremental = "release"
    tables = {"targets": TableSpec("targets", [
        {"name": "id", "type": "string"}, {"name": "schema", "type": "string"}, {"name": "name", "type": "string"},
        {"name": "aliases", "type": "json"}, {"name": "birth_date", "type": "string"}, {"name": "countries", "type": "json"},
        {"name": "identifiers", "type": "json"}, {"name": "sanctions", "type": "json"}, {"name": "datasets", "type": "json"},
        {"name": "first_seen", "type": "string"}, {"name": "last_seen", "type": "string"},
        {"name": "last_change", "type": "string"}, {"name": "licence", "type": "string"}], primary_key=("id",))}

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        collection = str(params.get("collection") or "default")
        if not re.fullmatch(r"[a-z0-9_]+", collection):
            raise ValueError("collection must be an OpenSanctions dataset name")
        out = {"collection": collection, "ids": text_set(params.get("ids")),
               "names": [n.casefold() for n in text_set(params.get("names"))],
               "countries": [c.lower() for c in text_set(params.get("countries"))],
               "datasets": text_set(params.get("datasets")), "schemata": text_set(params.get("schemata")),
               "all_targets": bool(params.get("all_targets"))}
        if not any(out[k] for k in ("ids", "names", "countries", "datasets", "schemata", "all_targets")):
            raise ValueError("give ids, names, countries, datasets, schemata or all_targets=true")
        return out

    def list_release(self, http, params) -> Release:
        index = json.loads(http.get_text(INDEX.format(collection=params["collection"])))
        resource = next(r for r in index["resources"] if r["name"] == "targets.simple.csv")
        files = [ReleaseFile(name="targets.simple.csv", url=resource["url"], size=resource.get("size"),
                             checksum=("sha1", resource["checksum"]) if resource.get("checksum") else None,
                             mode="download")]
        return Release("opensanctions", f"{params['collection']}-{index['version']}", files,
                       published_at=(index.get("updated_at") or "")[:19] + "Z", licence=LICENCE,
                       metadata={"collection": params["collection"], "version": index["version"]})

    def _match(self, row: Mapping[str, str], params) -> bool:
        if params["all_targets"]:
            return True
        if params["ids"] and row.get("id") in params["ids"]:
            return True
        if params["schemata"] and row.get("schema") not in params["schemata"]:
            return False
        if params["datasets"] and not set(_split(row.get("dataset", ""))) & set(params["datasets"]):
            return False
        if params["countries"] and not set(_split(row.get("countries", ""))) & set(params["countries"]):
            return False
        if params["names"]:
            hay = (row.get("name", "") + ";" + row.get("aliases", "")).casefold()
            if not any(n in hay for n in params["names"]):
                return False
        return bool(params["schemata"] or params["datasets"] or params["countries"] or params["names"])

    def process(self, source: FileSource, params) -> Iterator[Any]:
        with open(source.path, "rb") as raw:
            for row in csv_rows(raw):
                if not self._match(row, params):
                    continue
                yield "targets", {
                    "id": row.get("id"), "schema": row.get("schema"), "name": row.get("name"),
                    "aliases": _split(row.get("aliases", "")), "birth_date": row.get("birth_date") or None,
                    "countries": _split(row.get("countries", "")), "identifiers": _split(row.get("identifiers", "")),
                    "sanctions": _split(row.get("sanctions", "")), "datasets": _split(row.get("dataset", "")),
                    "first_seen": row.get("first_seen") or None, "last_seen": row.get("last_seen") or None,
                    "last_change": row.get("last_change") or None, "licence": LICENCE["id"]}
