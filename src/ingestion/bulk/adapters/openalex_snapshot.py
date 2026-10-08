"""OpenAlex snapshot via remote DuckDB queries (FA13).

The snapshot is published on public S3 as Parquet, partitioned by
``updated_date``: ``https://openalex.s3.amazonaws.com/data/parquet/works/manifest.json``
lists every part file with its size and record count (2026-09-23: 476 million
works, 2,040 files, ~707 GB). Nothing is downloaded wholesale: DuckDB reads each
part remotely and transfers only the needed columns and the row groups that can
match, so a filtered extract fits on a small server. CC0.

Works become paper ``Document``s with the same ``document_id`` as the
``openalex`` connector (DOI-based), abstracts rebuilt from
``abstract_inverted_index``. Snapshot filtering differs from the API's relevance
``search``: report a snapshot extract as its own source with its filter and
snapshot date.

Parameters (at least one of the first group): ``dois``, ``ids`` (W…),
``primary_topic_ids``, ``source_ids``, ``title_contains``; optional
``publication_year_from`` / ``_to``, ``types``, ``languages``, and
``updated_since`` (YYYY-MM-DD: skip partitions last updated before it — the
incremental mode for repeated runs).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List, Mapping, Optional

from services.ingest.common.document_model import Document
from src.ingestion.bulk.adapters._common import text_set
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile
from src.ingestion.connectors.scholarly.abstracts import normalise_doi, rebuild_inverted_abstract
from src.ingestion.connectors.scholarly.base import _document_id, _to_millis

HOST = "openalex.s3.amazonaws.com"
MANIFEST = f"https://{HOST}/data/parquet/works/manifest.json"
_PART = re.compile(r"updated_date=(\d{4}-\d{2}-\d{2})/")
COLUMNS = ("id, doi, display_name, publication_date, publication_year, language, type, "
           "list_transform(authorships, a -> a.author.display_name) AS authors, "
           "primary_location.source.display_name AS venue, open_access.oa_url AS oa_url, "
           "abstract_inverted_index, primary_topic.id AS primary_topic_id, cited_by_count, is_retracted, updated_date")


def _https(url: str) -> str:
    return url.replace("s3://openalex/", f"https://{HOST}/")


class OpenAlexSnapshot(BulkAdapter):
    name = "openalex-snapshot"
    publisher = "OurResearch (OpenAlex)"
    title = "OpenAlex works snapshot (Parquet)"
    description = "Works filtered remotely from the OpenAlex Parquet snapshot; CC0."
    allowed_hosts = (HOST,)
    output = "documents"
    incremental = "file"

    def __init__(self) -> None:
        self._db = None

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        dois = sorted({d for d in (normalise_doi(v) for v in params.get("dois") or []) if d})
        ids = sorted({str(i).strip().rsplit("/", 1)[-1].upper() for i in params.get("ids") or [] if str(i).strip()})
        out = {"dois": dois, "ids": ids, "primary_topic_ids": text_set(params.get("primary_topic_ids")),
               "source_ids": text_set(params.get("source_ids")),
               "title_contains": str(params.get("title_contains") or "").strip(),
               "publication_year_from": int(params["publication_year_from"]) if params.get("publication_year_from") else None,
               "publication_year_to": int(params["publication_year_to"]) if params.get("publication_year_to") else None,
               "types": text_set(params.get("types")), "languages": text_set(params.get("languages")),
               "updated_since": params.get("updated_since")}
        if not any(out[k] for k in ("dois", "ids", "primary_topic_ids", "source_ids", "title_contains")):
            raise ValueError("give dois, ids, primary_topic_ids, source_ids or title_contains")
        if out["updated_since"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(out["updated_since"])):
            raise ValueError("updated_since must be YYYY-MM-DD")
        return out

    def list_release(self, http, params) -> Release:
        manifest = json.loads(http.get_text(MANIFEST))
        files: List[ReleaseFile] = []
        for entry in manifest["files"]:
            url = _https(entry["url"])
            match = _PART.search(url)
            partition = match.group(1) if match else ""
            if params["updated_since"] and partition and partition < params["updated_since"]:
                continue
            meta = entry.get("meta") or {}
            files.append(ReleaseFile(name=url.split("/works/", 1)[1], url=url, size=meta.get("content_length"),
                                     etag=f"{meta.get('content_length')}:{meta.get('record_count')}", mode="remote",
                                     metadata={"partition": partition, "records": meta.get("record_count")}))
        return Release("openalex", f"openalex-works-{manifest['date']}", files, published_at=manifest["date"],
                       licence={"id": "CC0-1.0", "attribution": "OpenAlex", "terms_url": "https://openalex.org/about"},
                       metadata={"record_count": manifest.get("record_count"), "content_length": manifest.get("content_length")})

    def _connection(self):
        if self._db is None:
            import duckdb
            self._db = duckdb.connect()
            self._db.execute("INSTALL httpfs; LOAD httpfs; SET threads=2; SET memory_limit='2GB'")
        return self._db

    def _where(self, params) -> tuple[str, list]:
        clauses, args = [], []
        group = []
        if params["dois"]:
            group.append("lower(doi) IN (SELECT unnest(?))")
            args.append(["https://doi.org/" + d for d in params["dois"]])
        if params["ids"]:
            group.append("id IN (SELECT unnest(?))")
            args.append(["https://openalex.org/" + i for i in params["ids"]])
        if params["primary_topic_ids"]:
            group.append("primary_topic.id IN (SELECT unnest(?))")
            args.append([t if t.startswith("http") else "https://openalex.org/" + t for t in params["primary_topic_ids"]])
        if params["source_ids"]:
            group.append("primary_location.source.id IN (SELECT unnest(?))")
            args.append([s if s.startswith("http") else "https://openalex.org/" + s for s in params["source_ids"]])
        if params["title_contains"]:
            group.append("display_name ILIKE ?")
            args.append("%" + params["title_contains"] + "%")
        clauses.append("(" + " OR ".join(group) + ")")
        if params["publication_year_from"]:
            clauses.append("publication_year >= ?"); args.append(params["publication_year_from"])
        if params["publication_year_to"]:
            clauses.append("publication_year <= ?"); args.append(params["publication_year_to"])
        if params["types"]:
            clauses.append("type IN (SELECT unnest(?))"); args.append(params["types"])
        if params["languages"]:
            clauses.append("language IN (SELECT unnest(?))"); args.append(params["languages"])
        return " AND ".join(clauses), args

    def process(self, source: FileSource, params) -> Iterator[Any]:
        source.http.check(source.file.url)
        where, args = self._where(params)
        sql = f"SELECT {COLUMNS} FROM read_parquet(?) WHERE {where}"
        cursor = self._connection().execute(sql, [source.file.url, *args])
        names = [d[0] for d in cursor.description]
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for values in rows:
                yield self._document(dict(zip(names, values)), source.file)

    @staticmethod
    def _document(row: Mapping[str, Any], file: ReleaseFile) -> Document:
        doi = normalise_doi(row.get("doi"))
        abstract: Optional[str] = None
        if row.get("abstract_inverted_index"):
            try:
                abstract = rebuild_inverted_abstract(json.loads(row["abstract_inverted_index"])) or None
            except (ValueError, TypeError):
                abstract = None
        published = row.get("publication_date")
        metadata = {"source_api": "openalex-snapshot", "work_identifier": f"doi:{doi}" if doi else f"openalex:{row['id']}",
                    "doi": doi, "venue": row.get("venue"), "external_id": row["id"],
                    "content_coverage": "abstract-only" if abstract else "metadata-only",
                    "snapshot_partition": file.metadata.get("partition"), "primary_topic": row.get("primary_topic_id"),
                    "type": row.get("type"), "cited_by_count": row.get("cited_by_count"),
                    "is_retracted": row.get("is_retracted")}
        return Document(document_id=_document_id("openalex", row["id"], doi), source_type="paper",
                        language=(row.get("language") or "en")[:8], ingested_at=_to_millis(str(row.get("updated_date"))) or 0,
                        source_id="openalex", url=f"https://doi.org/{doi}" if doi else row["id"],
                        title=row.get("display_name") or "", content=abstract,
                        content_ref=row.get("oa_url"), authors=[a for a in row.get("authors") or [] if a],
                        created_at=_to_millis(str(published)) if published else None,
                        metadata={k: v for k, v in metadata.items() if v not in (None, "", [])})
