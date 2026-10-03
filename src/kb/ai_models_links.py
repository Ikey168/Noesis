"""AI model and dataset records linked to Literature, dataset DOIs and OSS packages by citation (#2780, AI07).

Track #2742. A link exists only where a source states the identifier, and every link records its **basis** and pins
**specific revisions** on both sides (the record revision that states the identifier and the target's document or
revision):

* ``literature`` - arXiv ids in Hub tags and DOIs in cards, Epoch links or OpenML ``citation`` resolve to papers held
  by the existing paper connector (:mod:`src.ingestion.connectors.paper`, document id ``arxiv:<id>`` or
  ``doi:<doi>``) and scholarly sources (:mod:`src.ingestion.connectors.scholarly.sources`, document id derived from
  the DOI) in the shared ``documents`` table (provider ``science.literature``);
* ``dataset-doi`` - DOIs resolve through the DataCite path of :mod:`src.ingestion.research_entities_sources`
  (``research_entity_records`` keyed ``research-entities:doi:<doi>``, provider ``science.research-entities``);
* ``oss-package`` - a package link is proposed only where a source names a registry package (a package URL such as
  ``pkg:pypi/<name>``); the target is the OSS ecosystems record of that canonical coordinate (provider
  ``oss.registries``). The Hub ``library_name`` stays stated text (state ``stated_text``) and is never turned into a
  package link by itself.

Bases: ``stated-identifier`` (the record's own revision states it) or ``accepted-match`` (a record tied to it by an
accepted AI06 identity match states it; the match is cited). When a provider is not installed the link is recorded
``provider_absent``; a held provider without the target gives ``target_not_held`` - reported, never dropped. Nothing is
merged and no paper, dataset or package content is copied.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from src.kb.ai_models_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    AiModelsError,
    authorize,
    canonical,
    digest,
    load,
    table_exists,
)
from src.kb.ai_models_store import AiModelsStore

CONTRACT = "noesis-ai-model-link-v1"
KINDS = ("literature", "dataset-doi", "oss-package", "library-name")
BASES = ("stated-identifier", "accepted-match")
STATES = ("linked", "target_not_held", "provider_absent", "stated_text")
PROVIDERS = {"literature": ("science.literature", "documents", "knowledge:read"),
             "dataset-doi": ("science.research-entities", "research_entity_records",
                             "knowledge:science:research-entities:read"),
             "oss-package": ("oss.registries", "oss_records", "knowledge:oss:read")}
_DDL = """
CREATE TABLE IF NOT EXISTS ai_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  kind TEXT NOT NULL, basis TEXT NOT NULL, identifier TEXT NOT NULL, target_json TEXT, state TEXT NOT NULL,
  evidence_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def literature_document_ids(scheme: str, identifier: str) -> list[str]:
    """Document ids the paper connector and the scholarly sources give a paper with this arXiv id or DOI."""
    from src.ingestion.connectors.paper.models import paper_id
    from src.ingestion.connectors.scholarly.base import _document_id

    if scheme == "arxiv":
        return [paper_id(arxiv_id=identifier)]
    return [paper_id(doi=identifier), _document_id("", "", identifier)]


class AiModelsLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = AiModelsStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _identity(self):
        from src.kb.ai_models_identity import AiModelsIdentity

        return AiModelsIdentity(self.conn, initialize=False, now=self.now)

    def _current(self, namespace: str, record_id: str) -> dict[str, Any] | None:
        published = [r for r in self.store.revision_rows(namespace, record_id) if r["state"] == "published"]
        return published[-1] if published else None

    # ------------------------------------------------------------------ targets

    def _literature(self, scheme: str, identifier: str) -> dict[str, Any] | None:
        for document_id in literature_document_ids(scheme, identifier):
            row = self.conn.execute("SELECT document_id, source_type, source_id, url, content_hash FROM documents "
                                    "WHERE document_id=?", [document_id]).fetchone()
            if row:
                return {"kind": "document", "provider_id": "science.literature", "document_id": row[0],
                        "source_type": row[1], "source_id": row[2], "url": row[3], "content_hash": row[4],
                        "resolved_by": "src.ingestion.connectors.paper" if not row[0].startswith("paper:") else
                        "src.ingestion.connectors.scholarly.sources"}
        return None

    def _dataset_doi(self, namespace: str, doi: str) -> dict[str, Any] | None:
        from src.ingestion.research_entities_sources import dataset_key, normalize_doi

        normal = normalize_doi(doi)
        if normal is None:
            return None
        row = self.conn.execute(
            "SELECT source_id, record_key, provider, current_revision_id FROM research_entity_records WHERE "
            "namespace IN (?, 'global') AND record_key=? ORDER BY namespace, source_id LIMIT 1",
            [namespace, dataset_key(normal)]).fetchone()
        if row is None:
            return None
        return {"kind": "research-entity", "provider_id": "science.research-entities", "source_id": row[0],
                "record_key": row[1], "provider": row[2], "revision_id": row[3],
                "resolved_by": "src.ingestion.research_entities_sources (DataCite)"}

    def _package(self, namespace: str, purl: str) -> tuple[dict[str, Any] | None, str | None]:
        from src.kb.oss_ecosystem_records import OssRecordError, coordinate

        match = re.fullmatch(r"pkg:(pypi|npm|cargo|maven)/(.+)", purl)
        if match is None:
            return None, "not a supported registry package URL"
        ecosystem, name = match.group(1), match.group(2)
        if ecosystem == "maven":
            name = name.replace("/", ":", 1)
        try:
            coord = coordinate(ecosystem, name)
        except OssRecordError as exc:
            return None, str(exc)
        row = self.conn.execute(
            "SELECT r.record_id, r.record_type, v.revision_id FROM oss_records r JOIN oss_revisions v ON "
            "v.record_id=r.record_id WHERE r.namespace=? AND r.coordinate=? ORDER BY v.order_ms DESC, v.seq DESC "
            "LIMIT 1", [namespace, coord]).fetchone()
        if row is None:
            return None, f"no OSS ecosystems record holds {coord}"
        return {"kind": "oss-record", "provider_id": "oss.registries", "coordinate": coord, "record_id": row[0],
                "record_type": row[1], "revision_id": row[2]}, None

    # ------------------------------------------------------------------ writes

    def _put(self, namespace, record_id, revision_id, kind, basis, identifier, target, state, evidence, principal_id):
        link_id = "ai-link:" + digest([namespace, record_id, revision_id, kind, basis, identifier, target,
                                       state])[:24]
        if self.conn.execute("SELECT 1 FROM ai_links WHERE namespace=? AND link_id=?",
                             [namespace, link_id]).fetchone():
            return link_id, False
        self.conn.execute("INSERT INTO ai_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, record_id, revision_id, kind, basis, identifier,
                           None if target is None else canonical(target), state, canonical(evidence), principal_id,
                           self.now()])
        return link_id, True

    def _stated(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        """Identifiers stated by the record itself and by records an accepted match ties to it."""
        out = []
        sources = [(record_id, "stated-identifier", None)]
        if table_exists(self.conn, "ai_identity_matches"):
            sources += [(c["record_id"], "accepted-match", c) for c in self._identity().counterparts(namespace,
                                                                                                    record_id)]
        for stating_id, basis, match in sources:
            revision = self._current(namespace, stating_id)
            if revision is None:
                continue
            statement = revision["statement"]
            identifiers = dict(statement.get("identifiers") or {})
            common = {"stated_by_record_id": stating_id, "stated_by_revision_id": revision["revision_id"],
                      **({"match_id": match["match_id"], "match_method": match["method"]} if match else {})}
            for arxiv in identifiers.get("arxiv") or []:
                out.append({"kind": "literature", "scheme": "arxiv", "identifier": arxiv, "basis": basis, **common})
            for doi in identifiers.get("doi") or []:
                out.append({"kind": "literature", "scheme": "doi", "identifier": doi, "basis": basis, **common})
                out.append({"kind": "dataset-doi", "scheme": "doi", "identifier": doi, "basis": basis, **common})
            for purl in identifiers.get("packages") or []:
                out.append({"kind": "oss-package", "scheme": "purl", "identifier": purl, "basis": basis, **common})
            library = dict(statement.get("declared") or {}).get("library_name")
            if library and basis == "stated-identifier":
                out.append({"kind": "library-name", "scheme": "text", "identifier": library, "basis": basis,
                            **common})
        return out

    def link(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
             record_id: str | None = None) -> dict[str, Any]:
        """Link every record (or one) by the identifiers its sources state; idempotent; missing targets reported."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        out: dict[str, list[str]] = {state: [] for state in STATES}
        records = [self.store.record(namespace, record_id)] if record_id else self.store.records(namespace)
        for record in records:
            if record["record_kind"] == "hub-refs":
                continue
            own = self._current(namespace, record["record_id"])
            if own is None:
                continue
            for item in self._stated(namespace, record["record_id"]):
                kind, identifier = item["kind"], item["identifier"]
                evidence = {k: v for k, v in item.items() if k not in {"kind", "identifier", "basis"}}
                target, state = None, "linked"
                if kind == "library-name":
                    state = "stated_text"
                    evidence["note"] = ("library_name is kept as stated text; a package link needs a source that "
                                        "names a registry package")
                else:
                    provider_id, table, scope = PROVIDERS[kind]
                    if not table_exists(self.conn, table):
                        state = "provider_absent"
                        evidence["reason"] = f"{provider_id} is not installed ({table})"
                    else:
                        if scope not in scopes and "operator" not in scopes:
                            raise AiModelsError("unauthorized", f"{scope} is required to read {provider_id}")
                        reason = None
                        if kind == "literature":
                            target = self._literature(item["scheme"], identifier)
                        elif kind == "dataset-doi":
                            target = self._dataset_doi(namespace, identifier)
                        else:
                            target, reason = self._package(namespace, identifier)
                        if target is None:
                            state = "target_not_held"
                            evidence["reason"] = reason or f"{provider_id} holds no record for this identifier"
                link_id, new = self._put(namespace, record["record_id"], own["revision_id"], kind, item["basis"],
                                         identifier, target, state, evidence, principal_id)
                if new:
                    out[state].append(link_id)
        return {**out, "note": "links rest on stated identifiers or accepted matches only; nothing is merged"}

    # ------------------------------------------------------------------ reads

    def get(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, record_id, revision_id, kind, basis, identifier, target_json, state, evidence_json, "
            "created_by FROM ai_links WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        if row is None:
            raise AiModelsError("not_found", "no such link")
        return {"contract": CONTRACT, "link_id": row[0], "record_id": row[1], "revision_id": row[2], "kind": row[3],
                "basis": row[4], "identifier": row[5], "target": load(row[6], None), "state": row[7],
                "evidence": json.loads(row[8]), "created_by": row[9]}

    def links(self, namespace: str, *, scopes: Iterable[str], record_id: str | None = None, kind: str | None = None,
              state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "ai_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM ai_links WHERE namespace=? AND (? IS NULL OR record_id=?) AND (? IS NULL OR kind=?) "
            "AND (? IS NULL OR state=?) ORDER BY record_id, kind, identifier, created_at_ms, link_id",
            [namespace, record_id, record_id, kind, kind, state, state]).fetchall()
        return [self.get(namespace, r[0]) for r in rows]

    def current_links(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        """The latest link per kind, basis and identifier for a record (earlier links stay in the history)."""
        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in self.links(namespace, scopes={"operator"}, record_id=record_id):
            latest[(item["kind"], item["basis"], item["identifier"])] = item
        return list(latest.values())


__all__ = ["BASES", "CONTRACT", "KINDS", "PROVIDERS", "STATES", "AiModelsLinks", "literature_document_ids"]
