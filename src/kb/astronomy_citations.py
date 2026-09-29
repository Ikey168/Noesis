"""Astronomy records linked to Science pack papers by bibcode and DOI (#2149, AS09).

The Astronomy pack stores the *reference*; the Science pack owns the paper.
A link points from an astronomy record revision to an exact paper document
revision (``documents`` plus ``document_revision_records``, the literature
records the Science providers harvest, read the way
:mod:`src.domains.research.analytics` and :mod:`src.kb.clinical_publications`
read them). Nothing is copied.

* References come only from what a source states: the archive's
  ``pl_refname`` / ``disc_refname`` (ADS bibcode or DOI in the link), the
  removed listing's reference, and MPC circulars (identification announcements
  and MPCORB orbit references). SWPC products state no literature references;
  their serial-number references are threaded by the queries instead.
* A reference resolves by exact, normalised identifier: a DOI to a paper whose
  metadata states that DOI, a bibcode to a paper whose metadata states that
  bibcode. A bibcode and a DOI are paired only because one paper's metadata
  states both; there is no title or fuzzy matching.
* An unresolved reference stays visible as ``unresolved`` (MPC circulars are
  not papers and always stay unresolved).
* Links are directional (record -> paper) and revertible. A revert is final
  for that paper revision; a new paper revision is new evidence and a new
  link, so no revert ever reactivates.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.astronomy_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    AstronomyError,
    authorize,
    canonical,
    digest,
    normalize_bibcode,
    normalize_circular,
    normalize_doi,
)
from src.kb.astronomy_store import AstronomyStore

CONTRACT = "noesis-astronomy-citation-link-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS astronomy_citation_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  citation_kind TEXT NOT NULL, citation_value TEXT NOT NULL, document_id TEXT, document_revision_id TEXT,
  state TEXT NOT NULL, basis TEXT NOT NULL, evidence_json TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def stated_citations(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """The references a record states, normalised (bibcode, doi, circular), deduplicated."""
    refs: list[Mapping[str, Any]] = []
    if isinstance(record.get("reference"), Mapping):
        refs.append(record["reference"])
    if isinstance((record.get("discovery") or {}).get("reference"), Mapping):
        refs.append(record["discovery"]["reference"])
    found: dict[tuple[str, str], dict[str, str]] = {}
    for ref in refs:
        for kind, normalise in (("bibcode", normalize_bibcode), ("doi", normalize_doi)):
            value = normalise(ref.get(kind))
            if value:
                found[(kind, value)] = {
                    "kind": kind,
                    "value": value,
                    "stated": ref.get("text", "")[:300],
                }
    circulars = []
    if record["kind"] == "identification":
        circulars.append(record.get("announced_in"))
    if record["kind"] == "orbit_solution" and record.get("publisher") == "MPC":
        circulars.append(record.get("reference"))
    for value in circulars:
        circular = normalize_circular(value)
        if circular:
            found[("circular", circular)] = {
                "kind": "circular",
                "value": circular,
                "stated": str(value),
            }
    return [found[k] for k in sorted(found)]


class AstronomyCitations:
    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], int] | None = None,
        initialize: bool = True,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = AstronomyStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name='astronomy_citation_links'"
            ).fetchone()
        )

    def _papers(self) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """Paper documents (Science literature records) by the DOI and bibcode their metadata states."""
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('documents', 'document_revision_records')"
            ).fetchall()
        }
        if len(tables) < 2:
            return {}
        index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        rows = self.conn.execute(
            "SELECT document_id, title, metadata FROM documents WHERE source_type='paper' "
            "ORDER BY document_id"
        ).fetchall()
        for document_id, title, metadata in rows:
            revision = self.conn.execute(
                "SELECT revision_id FROM document_revision_records WHERE document_id=? AND committed_watermark IS "
                "NOT NULL ORDER BY revision DESC LIMIT 1",
                [document_id],
            ).fetchone()
            if not revision:
                continue
            meta = (
                json.loads(metadata)
                if isinstance(metadata, str) and metadata
                else dict(metadata or {})
            )
            paper = {
                "document_id": document_id,
                "revision_id": revision[0],
                "title": title,
                "doi": normalize_doi(meta.get("doi")),
                "bibcode": normalize_bibcode(meta.get("bibcode")),
            }
            for kind in ("doi", "bibcode"):
                if paper[kind]:
                    index.setdefault((kind, paper[kind]), []).append(paper)
        return index

    def link(
        self, namespace: str, *, principal_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Resolve every stated reference of the current records; idempotent, never reactivates a revert."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        papers = self._papers()
        created, unresolved = [], 0
        for view in self.store.visible(namespace)["records"]:
            for cite in stated_citations(view["record"]):
                matches = (
                    papers.get((cite["kind"], cite["value"]), [])
                    if cite["kind"] != "circular"
                    else []
                )
                targets = [(p, f"stated-{cite['kind']}") for p in matches]
                if not targets:
                    unresolved += 1
                    reason = (
                        "an MPC circular is not a paper record"
                        if cite["kind"] == "circular"
                        else "no Science literature record states this identifier"
                    )
                    targets = [(None, reason)]
                for paper, basis in targets:
                    link_id = (
                        "astro-cite:"
                        + digest(
                            [
                                namespace,
                                view["record_id"],
                                cite["kind"],
                                cite["value"],
                                paper and paper["document_id"],
                                paper and paper["revision_id"],
                            ]
                        )[:24]
                    )
                    if self.conn.execute(
                        "SELECT 1 FROM astronomy_citation_links WHERE namespace=? AND link_id=?",
                        [namespace, link_id],
                    ).fetchone():
                        continue
                    now = self.now()
                    evidence = {"stated": cite["stated"]}
                    if paper:
                        evidence["paper_states"] = {
                            k: paper[k] for k in ("doi", "bibcode") if paper[k]
                        }
                    self.conn.execute(
                        "INSERT INTO astronomy_citation_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [
                            namespace,
                            link_id,
                            view["record_id"],
                            view["revision_id"],
                            cite["kind"],
                            cite["value"],
                            paper and paper["document_id"],
                            paper and paper["revision_id"],
                            "linked" if paper else "unresolved",
                            basis,
                            canonical(evidence),
                            principal_id,
                            now,
                            canonical(
                                [
                                    {
                                        "state": "linked" if paper else "unresolved",
                                        "by": principal_id,
                                        "at_ms": now,
                                    }
                                ]
                            ),
                        ],
                    )
                    created.append(link_id)
        return {
            "created": created,
            "unresolved_references": unresolved,
            "links": self.links(namespace, scopes=scopes),
        }

    def revert(
        self,
        namespace: str,
        link_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise AstronomyError("invalid_decision", "a revert needs a reason")
        row = self._row(namespace, link_id)
        if row["state"] != "linked":
            raise AstronomyError("invalid_state", f"link is {row['state']}")
        history = row["history"] + [
            {
                "state": "reverted",
                "by": principal_id,
                "reason": reason.strip(),
                "at_ms": self.now(),
            }
        ]
        self.conn.execute(
            "UPDATE astronomy_citation_links SET state='reverted', history_json=? WHERE namespace=? "
            "AND link_id=?",
            [canonical(history), namespace, link_id],
        )
        return self._row(namespace, link_id)

    def _row(self, namespace: str, link_id: str) -> dict[str, Any]:
        if not self._ready():
            raise AstronomyError("not_found", "no citation link is on record")
        row = self.conn.execute(
            "SELECT link_id, record_id, revision_id, citation_kind, citation_value, document_id, "
            "document_revision_id, state, basis, evidence_json, history_json FROM astronomy_citation_links "
            "WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            raise AstronomyError(
                "not_found", "citation link is not on record in this namespace"
            )
        out = dict(
            zip(
                (
                    "link_id",
                    "record_id",
                    "revision_id",
                    "citation_kind",
                    "citation_value",
                    "document_id",
                    "document_revision_id",
                    "state",
                    "basis",
                ),
                row[:9],
            )
        )
        return {
            "contract": CONTRACT,
            **{k: v for k, v in out.items() if v is not None},
            "evidence": json.loads(row[9]),
            "history": json.loads(row[10]),
            "direction": "astronomy record -> Science paper (the Science pack owns the paper)",
        }

    def links(
        self, namespace: str, *, scopes: Iterable[str], record_id: str | None = None
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        return self.for_records(namespace, None if record_id is None else [record_id])

    def for_records(
        self, namespace: str, record_ids: Iterable[str] | None
    ) -> list[dict[str, Any]]:
        """Links per stated citation: active links win; an unresolved one shows only while nothing resolves it."""
        if not self._ready():
            return []
        rows = [
            self._row(namespace, r[0])
            for r in self.conn.execute(
                "SELECT link_id FROM astronomy_citation_links WHERE namespace=? ORDER BY record_id, citation_kind, "
                "citation_value, link_id",
                [namespace],
            ).fetchall()
        ]
        wanted = None if record_ids is None else set(record_ids)
        rows = [r for r in rows if wanted is None or r["record_id"] in wanted]
        resolved = {
            (r["record_id"], r["citation_kind"], r["citation_value"])
            for r in rows
            if r["state"] == "linked"
        }
        return [
            r
            for r in rows
            if r["state"] != "unresolved"
            or (r["record_id"], r["citation_kind"], r["citation_value"]) not in resolved
        ]
