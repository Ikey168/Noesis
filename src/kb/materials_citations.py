"""Link property values to cited papers and test-method standards by citation only (MT10, #2088).

A reference a source prints with a value (WebBook row, COD publication) is a
*value-level* citation; a database's own reference paper (Materials Project,
JARVIS, OQMD, WebBook, COD) is a *dataset-level* citation. Both are kept and
labelled. Resolution is by identifier only:

* a DOI resolves to the Science pack's literature record - a ``documents``
  row with ``source_type='paper'`` whose DOI is equal after normalisation -
  pinned to its committed revision;
* a test-method designation (ASTM, ISO, DIN) resolves to a
  ``technology.standards`` catalogue edition by exact reference
  (whitespace-collapsed, case-folded), and only when the caller names the
  standards namespace (checked at call time with ``knowledge:standards:read``);
* everything else stays the reference string the source printed.

No link is made by topic, title or text similarity, and no copy of a paper
or standard is stored.
"""

from __future__ import annotations

import json
from typing import Any

from src.kb.materials_store import MaterialsError, table_exists

STANDARDS_READ = "knowledge:standards:read"


def standard_key(value: str) -> str:
    return " ".join(str(value).split()).casefold()


def _papers(conn) -> dict[str, dict[str, str]]:
    from src.ingestion.connectors.paper import trial_registry

    if not table_exists(conn, "documents") or not table_exists(
        conn, "document_revision_records"
    ):
        return {}
    found: dict[str, dict[str, str]] = {}
    for document_id, source_id, metadata in conn.execute(
        "SELECT document_id, source_id, metadata FROM documents WHERE source_type='paper' ORDER BY document_id"
    ).fetchall():
        ids = trial_registry.identifiers(
            {
                "document_id": document_id,
                "source_id": source_id,
                "metadata": json.loads(metadata) if metadata else {},
            }
        )
        if not ids["doi"]:
            continue
        revision = conn.execute(
            "SELECT revision_id FROM document_revision_records WHERE document_id=? AND "
            "committed_watermark IS NOT NULL ORDER BY revision DESC LIMIT 1",
            [document_id],
        ).fetchone()
        if revision:
            found.setdefault(
                ids["doi"], {"document_id": document_id, "revision_id": revision[0]}
            )
    return found


def _standards(conn, namespace, scopes) -> dict[str, list[str]]:
    scopes = set(scopes or ())
    if "operator" not in scopes and (
        STANDARDS_READ not in scopes
        or not {f"namespace:{namespace}:read", f"namespace:{namespace}:write"} & scopes
    ):
        raise MaterialsError(
            "unauthorized",
            f"{STANDARDS_READ} and access to namespace {namespace} are required to "
            "resolve test-method standards",
        )
    if not table_exists(conn, "standard_revisions"):
        return {}
    catalogue: dict[str, list[str]] = {}
    for native_id, reference in conn.execute(
        "SELECT r.native_id, r.reference FROM standard_current c JOIN standard_revisions r USING(revision_id) "
        "WHERE c.namespace=? ORDER BY r.native_id",
        [namespace],
    ).fetchall():
        catalogue.setdefault(standard_key(reference), []).append(native_id)
    return catalogue


class CitationResolver:
    """Resolves references for one request; the paper and standard indexes are read once."""

    def __init__(self, conn, *, standards_namespace=None, scopes=()):
        self.papers = _papers(conn)
        self.standards_namespace = standards_namespace
        self.standards = (
            None
            if standards_namespace is None
            else _standards(conn, standards_namespace, scopes)
        )

    def resolve(self, reference: dict[str, Any]) -> dict[str, Any]:
        result = {k: v for k, v in reference.items() if v is not None}
        if reference.get("standard"):
            if self.standards is None:
                result["resolution"] = {
                    "kind": "standard",
                    "status": "unresolved",
                    "reason": "no standards namespace was requested",
                }
            else:
                hits = self.standards.get(standard_key(reference["standard"])) or []
                result["resolution"] = (
                    {
                        "kind": "standard",
                        "status": "linked",
                        "basis": "exact reference",
                        "namespace": self.standards_namespace,
                        "technical_object_id": f"standard:iso:{hits[0]}",
                    }
                    if len(hits) == 1
                    else {
                        "kind": "standard",
                        "status": "unresolved",
                        "reason": "ambiguous reference in the catalogue"
                        if hits
                        else "designation not in the acquired standards catalogue",
                    }
                )
        elif reference.get("doi"):
            paper = self.papers.get(reference["doi"])
            result["resolution"] = (
                {"kind": "paper", "status": "linked", "basis": "equal DOI", **paper}
                if paper
                else {
                    "kind": "paper",
                    "status": "unresolved",
                    "reason": "no Science literature record with this DOI",
                }
            )
        else:
            result["resolution"] = {
                "kind": "reference-string",
                "status": "unresolved",
                "reason": "no DOI or standard designation printed by the source",
            }
        return result

    def split(self, value_references, dataset_references):
        return {
            "value_level": [
                self.resolve(r) for r in value_references if r.get("level") == "value"
            ],
            "dataset_level": [
                self.resolve(r)
                for r in [*value_references, *dataset_references]
                if r.get("level") == "dataset"
            ],
        }
