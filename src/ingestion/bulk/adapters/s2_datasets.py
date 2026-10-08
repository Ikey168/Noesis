"""Semantic Scholar Datasets API (FA15). Needs ``SEMANTIC_SCHOLAR_API_KEY``.

Release ids are public (``/datasets/v1/release/``); the shard links of a dataset
(``/datasets/v1/release/<id>/dataset/<name>``) need the API key and point to
pre-signed, expiring S3 URLs (``ai2-s2ag.s3.amazonaws.com``) with gzipped JSONL.
With ``since_release`` the adapter asks for the diffs
(``/datasets/v1/diffs/<from>/to/<to>/<name>``) and processes their update files,
emitting the records of delete files to a ``deletions`` table.

The key is sent only to ``api.semanticscholar.org``, never to S3. Records are
read defensively: ``corpusid`` and a DOI from ``externalids`` or
``openaccessinfo.externalids``. **Not yet verified live** (no key configured when
written); the S3 host is allowlisted exactly, so a different host fails loudly.

Parameters: ``dataset`` ("abstracts" | "papers"), ``corpus_ids`` and/or ``dois``
(or ``all_records=true``), optional ``release`` (default latest) and
``since_release``. Licence: Semantic Scholar dataset licence (ODC-BY for most
datasets; abstracts carry their own licence field).
"""
from __future__ import annotations

import gzip
import json
import os
from typing import Any, Dict, Iterator, List, Mapping, Optional

from src.ingestion.bulk.adapters._common import text_set
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile, TableSpec
from src.ingestion.connectors.scholarly.abstracts import normalise_doi

API = "https://api.semanticscholar.org/datasets/v1/"


def _doi(record: Mapping[str, Any]) -> Optional[str]:
    ids = record.get("externalids") or (record.get("openaccessinfo") or {}).get("externalids") or {}
    return normalise_doi(ids.get("DOI") or ids.get("doi"))


class SemanticScholarDatasets(BulkAdapter):
    name = "s2-datasets"
    publisher = "Allen Institute for AI (Semantic Scholar)"
    title = "Semantic Scholar Academic Graph datasets"
    description = "Abstracts or paper metadata from S2AG dataset shards, filtered by corpus id or DOI."
    allowed_hosts = ("api.semanticscholar.org", "ai2-s2ag.s3.amazonaws.com")
    incremental = "release"
    tables = {
        "abstracts": TableSpec("abstracts", [{"name": "corpusid", "type": "integer"}, {"name": "doi", "type": "string"},
                                             {"name": "abstract", "type": "string"}, {"name": "licence", "type": "string"}],
                               primary_key=("corpusid",)),
        "papers": TableSpec("papers", [{"name": "corpusid", "type": "integer"}, {"name": "doi", "type": "string"},
                                       {"name": "title", "type": "string"}, {"name": "year", "type": "integer"},
                                       {"name": "venue", "type": "string"}, {"name": "authors", "type": "json"},
                                       {"name": "externalids", "type": "json"}], primary_key=("corpusid",)),
    }

    def _headers(self) -> Dict[str, str]:
        key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
        if not key:
            raise PermissionError("s2-datasets needs SEMANTIC_SCHOLAR_API_KEY for dataset links")
        return {"x-api-key": key}

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        dataset = params.get("dataset", "abstracts")
        if dataset not in self.tables:
            raise ValueError("dataset must be abstracts or papers")
        out = {"dataset": dataset, "corpus_ids": sorted({int(c) for c in params.get("corpus_ids") or []}),
               "dois": sorted({d for d in (normalise_doi(v) for v in params.get("dois") or []) if d}),
               "all_records": bool(params.get("all_records")), "release": params.get("release") or "latest",
               "since_release": params.get("since_release")}
        if not (out["corpus_ids"] or out["dois"] or out["all_records"]):
            raise ValueError("give corpus_ids, dois or all_records=true")
        return out

    def list_release(self, http, params) -> Release:
        headers = self._headers()
        release = json.loads(http.get_text(API + f"release/{params['release']}"))["release_id"]
        files: List[ReleaseFile] = []
        if params["since_release"]:
            diffs = json.loads(http.get_text(API + f"diffs/{params['since_release']}/to/{release}/{params['dataset']}",
                                             headers=headers))
            for d, diff in enumerate(diffs.get("diffs") or []):
                for kind in ("update_files", "delete_files"):
                    for i, url in enumerate(diff.get(kind) or []):
                        files.append(ReleaseFile(f"{diff.get('to_release', d)}-{kind}-{i:04d}.jsonl.gz", url, mode="stream",
                                                 metadata={"kind": kind}))
        else:
            info = json.loads(http.get_text(API + f"release/{release}/dataset/{params['dataset']}", headers=headers))
            files = [ReleaseFile(f"{params['dataset']}-{i:04d}.jsonl.gz", url, mode="stream", metadata={"kind": "full"})
                     for i, url in enumerate(info.get("files") or [])]
        if not files:
            raise ValueError("the dataset listed no files")
        suffix = f"-diff-from-{params['since_release']}" if params["since_release"] else ""
        return Release("semantic-scholar", f"s2ag-{release}-{params['dataset']}{suffix}", files, published_at=release,
                       licence={"id": "s2ag-dataset-licence", "terms_url": "https://www.semanticscholar.org/product/api/license",
                                "note": "most datasets ODC-BY; abstracts carry a per-record licence"})

    def process(self, source: FileSource, params) -> Iterator[Any]:
        dataset, deleting = params["dataset"], source.file.metadata.get("kind") == "delete_files"
        with source.open_stream() as raw, gzip.GzipFile(fileobj=raw) as lines:
            for line in lines:
                if not line.strip():
                    continue
                record = json.loads(line)
                corpusid = record.get("corpusid")
                doi = _doi(record)
                if not (params["all_records"] or (corpusid in params["corpus_ids"]) or (doi and doi in params["dois"])):
                    continue
                if deleting:
                    yield "deletions", {"corpusid": corpusid, "doi": doi, "dataset": dataset}
                elif dataset == "abstracts":
                    yield "abstracts", {"corpusid": corpusid, "doi": doi, "abstract": record.get("abstract"),
                                        "licence": (record.get("openaccessinfo") or {}).get("license")}
                else:
                    yield "papers", {"corpusid": corpusid, "doi": doi, "title": record.get("title"),
                                     "year": record.get("year"), "venue": record.get("venue"),
                                     "authors": [a.get("name") for a in record.get("authors") or [] if a.get("name")],
                                     "externalids": record.get("externalids")}
