"""ICIJ Offshore Leaks Database bulk release (#2888): a bounded subgraph, never the whole database.

The download page offers one ZIP of CSV files: one per node type
(``nodes-entities.csv``, ``nodes-officers.csv``, ``nodes-intermediaries.csv``,
``nodes-addresses.csv``, ``nodes-others.csv``) and ``relationships.csv``. The
archive URL, the member names and the columns are recorded as *to verify* in
``docs/development/offshore-leaks-evidence/source-audit.md``; a missing
required column fails the file as schema drift.

Parameters (validated by :func:`src.ingestion.offshore_leaks.validate_selection`):
``node_ids`` (1-200 seed node ids), or ``datasets`` (``sourceID`` values,
verbatim) and/or ``jurisdictions`` (ICIJ jurisdiction codes) with an explicit
``max_nodes`` (<= 1000); ``hops`` 0 or 1 (default 1); ``node_kinds`` overrides
(``{node_id: "person"|"legal_entity"}``). The kept subset holds the selected
node rows and relationship rows verbatim, each with the licence and ICIJ's
caveat; project it into Corporate Ownership with
:func:`src.kb.offshore_leaks.project_bulk_subset`. Licence: ODbL (database) and
CC BY-SA (contents), citing ICIJ.
"""
from __future__ import annotations

import zipfile
from collections.abc import Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any, ClassVar

from src.ingestion.bulk.adapters._common import head_file, newest, stamp
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, TableSpec
from src.ingestion.offshore_leaks import (
    BULK_URL,
    CAVEAT,
    LICENSE,
    NODE_FILES,
    RELATIONSHIPS_FILE,
    iter_csv,
    select_subgraph,
    validate_selection,
)

LICENCE = {"id": "ODbL-1.0 (database); CC-BY-SA (contents)", "attribution": "International Consortium of "
           "Investigative Journalists (ICIJ)", "terms_url": "https://offshoreleaks.icij.org/pages/database",
           "note": LICENSE, "caveat": CAVEAT}


class IcijOffshoreLeaks(BulkAdapter):
    name = "icij-offshore-leaks"
    publisher = "International Consortium of Investigative Journalists"
    title = "ICIJ Offshore Leaks Database (bounded subgraph)"
    description = ("Selected entity, officer, intermediary and address nodes of the ICIJ Offshore Leaks Database "
                   "and the relationships touching them, verbatim. Inclusion does not imply wrongdoing.")
    allowed_hosts = ("offshoreleaks-data.icij.org",)
    incremental = "release"
    tables: ClassVar[dict[str, TableSpec]] = {
        "nodes": TableSpec("nodes", [
            {"name": "node_id", "type": "string"}, {"name": "node_type", "type": "string"},
            {"name": "name", "type": "string"}, {"name": "sourceID", "type": "string"},
            {"name": "row", "type": "json"}, {"name": "licence", "type": "string"}], primary_key=("node_id",)),
        "relationships": TableSpec("relationships", [
            {"name": "node_id_start", "type": "string"}, {"name": "node_id_end", "type": "string"},
            {"name": "rel_type", "type": "string"}, {"name": "link", "type": "string"},
            {"name": "sourceID", "type": "string"}, {"name": "row", "type": "json"},
            {"name": "licence", "type": "string"}]),
        "selection": TableSpec("selection", [
            {"name": "seeds", "type": "json"}, {"name": "missing_nodes", "type": "json"},
            {"name": "truncated", "type": "boolean"}, {"name": "selection", "type": "json"}]),
    }

    def validate_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return validate_selection(params)
        except Exception as exc:  # SourcePackError carries the reason; the runner expects ValueError
            raise ValueError(str(exc)) from exc

    def list_release(self, http, params) -> Release:
        archive = head_file(http, "full-oldb.zip", BULK_URL, mode="download")
        return Release("icij-offshore-leaks", "full-oldb-" + stamp(newest([archive]), archive.etag or "unknown"),
                       [archive], licence=LICENCE, published_at=None,
                       metadata={"caveat": CAVEAT, "selection": dict(params)})

    @staticmethod
    def _members(archive: zipfile.ZipFile) -> dict[str, str]:
        found: dict[str, str] = {}
        for member in archive.namelist():
            base = PurePosixPath(member).name
            if base in NODE_FILES or base == RELATIONSHIPS_FILE:
                found.setdefault(base, member)
        return found

    def process(self, source: FileSource, params) -> Iterator[Any]:
        with zipfile.ZipFile(source.path) as archive:
            members = self._members(archive)
            for required in ("nodes-entities.csv", RELATIONSHIPS_FILE):
                if required not in members:
                    raise ValueError(f"the archive lacks {required} (schema drift)")

            def rows(name: str):
                if name not in members:
                    return iter(())
                kind = NODE_FILES.get(name, "relationship")

                def generate():
                    with archive.open(members[name]) as handle:
                        yield from iter_csv(handle, kind, name)
                return generate()

            subgraph = select_subgraph(rows, params)
        for node_id, row in sorted(subgraph["nodes"].items()):
            clean = {k: v for k, v in row.items() if not k.startswith("_")}
            yield "nodes", {"node_id": node_id, "node_type": row["_type"], "name": row.get("name") or None,
                            "sourceID": row.get("sourceID") or None, "file": row["_file"], "line": row["_line"],
                            "row": clean, "licence": LICENCE["id"]}
        for edge in subgraph["relationships"]:
            clean = {k: v for k, v in edge.items() if not k.startswith("_")}
            yield "relationships", {"node_id_start": edge.get("node_id_start"), "node_id_end": edge.get("node_id_end"),
                                    "rel_type": edge.get("rel_type"), "link": edge.get("link") or None,
                                    "sourceID": edge.get("sourceID") or None, "line": edge["_line"], "row": clean,
                                    "licence": LICENCE["id"]}
        yield "selection", {"seeds": subgraph["seeds"], "missing_nodes": subgraph["missing_nodes"],
                            "truncated": subgraph["truncated"], "selection": subgraph["selection"]}
