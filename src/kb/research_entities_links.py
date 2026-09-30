"""Research-entity records linked to other packs by citation and accepted matches (#2579, RE08 #2618).

Links are built from what the registries publish, never inferred:

* a **researcher** links to a Scholarly-literature work (document store, ``source_type`` paper) only through a DOI
  the researcher asserts among the public works of the ORCID revision (``orcid-asserted-identifier``); the link is an
  assertion, not verified authorship, and no name is ever compared;
* a **dataset** links to a work through a DOI among its DataCite related identifiers, with the relation type as
  published (``published-related-identifier``);
* a **project** links to a Funding & grants record (``src.kb.funding_opportunities``) whose provider id is the CORDIS
  project id or whose record states the project id or grant DOI (``shared-identifier``);
* an **organisation** or a **participant** links to an ownership entity only through an accepted RE07 match
  (``accepted-match``).

Each link records its basis and points at a specific revision on both sides (the subject's record revision; the
target's content hash, funding revision or ownership revision). A missing store is ``provider_absent`` and a missing
record ``target_missing``: both are kept and reported, never dropped. No collaboration, co-authorship or influence
link is ever derived.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.research_entities_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    ResearchEntitiesStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-research-entity-link-v1"
BASES = ("orcid-asserted-identifier", "published-related-identifier", "shared-identifier", "accepted-match")
TARGETS = {
    "scholarly_work": ("science.literature", "documents"),
    "funding_record": ("funding", "funding_opportunity_revisions"),
    "ownership_entity": ("ownership", "ownership_records"),
}
NOTICE = ("Linked by a published identifier or an accepted identity match only; ORCID works are the researcher's "
          "assertions, not verified authorship; no collaboration or influence link is derived.")
_DDL = """
CREATE TABLE IF NOT EXISTS rentity_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, subject_record_id TEXT NOT NULL, subject_revision_id TEXT NOT NULL,
  subject_json TEXT NOT NULL, target_kind TEXT NOT NULL, target_json TEXT NOT NULL, basis_json TEXT NOT NULL,
  target_status TEXT NOT NULL, history_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return [str(value)] if isinstance(value, (str, int)) and not isinstance(value, bool) else []


def _doi_key(value: Any) -> str | None:
    from src.ingestion.research_entities_sources import ResearchEntitiesFormatError, doi

    try:
        return doi(value)
    except ResearchEntitiesFormatError:
        return None


class ResearchEntitiesLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = ResearchEntitiesStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ targets

    def _work(self, identifier: str) -> tuple[str, dict[str, Any] | None]:
        if not table_exists(self.conn, "documents"):
            return "provider_absent", None
        rows = self.conn.execute(
            "SELECT document_id, content_hash, url, metadata FROM documents WHERE source_type='paper' ORDER BY "
            "document_id").fetchall()
        for document_id, content_hash, url, metadata in rows:
            stated = {_doi_key(json.loads(metadata or "{}").get("doi")), _doi_key(url)}
            if identifier in stated:
                return "resolved", {"record_id": document_id, "revision": content_hash, "url": url}
        return "target_missing", None

    def _funding(self, project: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        if not table_exists(self.conn, "funding_opportunity_revisions"):
            return "provider_absent", []
        needles = {project["project_id"]} | ({project["grant_doi"]} if project.get("grant_doi") else set())
        out = []
        rows = self.conn.execute(
            "SELECT o.opportunity_id, o.namespace, o.provider, o.provider_id, o.revision, r.content_json FROM "
            "funding_opportunities o JOIN funding_opportunity_revisions r ON r.opportunity_id=o.opportunity_id AND "
            "r.revision=o.revision ORDER BY o.opportunity_id").fetchall()
        for opportunity_id, namespace, provider, provider_id, revision, content in rows:
            values = _strings(json.loads(content))
            stated = [n for n in sorted(needles) if n == str(provider_id)
                      or any(re.search(rf"(?<![\w.]){re.escape(n)}(?![\w])", v) for v in values)]
            if stated:
                out.append({"record_id": opportunity_id, "namespace": namespace, "provider": provider,
                            "revision": int(revision), "identifier": stated[0],
                            "record_sha256": digest(json.loads(content))})
        return ("resolved" if out else "target_missing"), out

    def _ownership(self, ownership_key: str, ownership_namespace: str | None) -> tuple[str, dict[str, Any] | None]:
        if not table_exists(self.conn, "ownership_records"):
            return "provider_absent", None
        row = self.conn.execute(
            "SELECT r.namespace, r.record_id, r.current_revision, v.revision_id FROM ownership_records r JOIN "
            "ownership_record_revisions v ON v.namespace=r.namespace AND v.record_id=r.record_id AND "
            "v.revision=r.current_revision WHERE r.record_key=? AND (? IS NULL OR r.namespace=?) ORDER BY "
            "r.namespace LIMIT 1", [ownership_key, ownership_namespace, ownership_namespace]).fetchone()
        if row is None:
            return "target_missing", None
        return "resolved", {"namespace": row[0], "record_id": row[1], "record_key": ownership_key,
                            "revision": int(row[2]), "revision_id": row[3]}

    # ------------------------------------------------------------------ build

    def _put(self, namespace, subject, target_kind, target, basis, status, created):
        link_id = "rentity-link:" + digest([namespace, subject["revision_id"], target_kind, basis,
                                           target.get("record_id") if status == "resolved" else None])[:24]
        row = self.conn.execute("SELECT target_status, history_json FROM rentity_links WHERE namespace=? AND "
                                "link_id=?", [namespace, link_id]).fetchone()
        now = self.now()
        if row is None:
            self.conn.execute(
                "INSERT INTO rentity_links VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, link_id, subject["record_id"], subject["revision_id"], canonical(subject), target_kind,
                 canonical(target), canonical(basis), status, canonical([{"status": status, "at_ms": now}]), now])
            created.append(link_id)
        elif row[0] != status:
            history = json.loads(row[1]) + [{"status": status, "at_ms": now}]
            self.conn.execute("UPDATE rentity_links SET target_status=?, target_json=?, history_json=? WHERE "
                              "namespace=? AND link_id=?", [status, canonical(target), canonical(history), namespace,
                                                           link_id])
        return link_id

    def build(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
              ownership_namespace: str | None = None) -> dict[str, Any]:
        """Link every revision of every held record by published identifier or accepted match; idempotent.
        Missing providers and targets are reported with the link that could not resolve."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        del principal_id
        self.conn.execute(_DDL)
        created: list[str] = []
        revisions = [(record, revision) for record in self.store.records(namespace)
                     for revision in self.store.revisions(namespace, record["record_id"])]
        for record, revision in revisions:
            statement = self.store.statement(namespace, revision["revision_id"])
            body = statement["body"]
            if not body:
                continue
            subject = {"record_kind": record["record_kind"], "provider": record["provider"],
                       "native_id": record["native_id"], "record_id": record["record_id"],
                       "revision_id": revision["revision_id"], "revision": revision["revision"]}
            if record["record_kind"] == "researcher":
                for work in body.get("works") or []:
                    for identifier in work.get("identifiers") or []:
                        if identifier["type"] != "doi":
                            continue
                        status, target = self._work(identifier["value"])
                        basis = {"method": "orcid-asserted-identifier", "identifier": identifier,
                                 "put_code": work.get("put_code"),
                                 "assertion": "orcid-asserted, not verified authorship"}
                        self._put(namespace, subject, "scholarly_work", target or {}, basis, status, created)
            elif record["record_kind"] == "dataset":
                for related in body.get("related_identifiers") or []:
                    key = _doi_key(related.get("relatedIdentifier"))
                    if str(related.get("relatedIdentifierType")).upper() != "DOI" or key is None:
                        continue
                    status, target = self._work(key)
                    basis = {"method": "published-related-identifier", "identifier": {"type": "doi", "value": key},
                             "relation_type": related.get("relationType"), "as_published": related}
                    self._put(namespace, subject, "scholarly_work", target or {}, basis, status, created)
            elif record["record_kind"] == "project":
                status, targets = self._funding(body)
                for target in targets or [{}]:
                    basis = {"method": "shared-identifier",
                             "identifier": {"type": "cordis-project", "value": body["project_id"]},
                             "stated_identifier": target.get("identifier")}
                    self._put(namespace, subject, "funding_record", target, basis, status, created)
                keys = [("research-entities:pic:" + p["pic"], p["pic"]) for p in body.get("participants") or []]
                self._ownership_links(namespace, subject, keys, ownership_namespace, created)
            elif record["record_kind"] == "organisation":
                keys = [("research-entities:ror:" + body["ror_id"].rsplit("/", 1)[-1], body["ror_id"])]
                self._ownership_links(namespace, subject, keys, ownership_namespace, created)
        return {"created": created, "links": self.links(namespace, scopes={"operator"}),
                "missing": [v for v in self.links(namespace, scopes={"operator"})
                            if v["target_status"] != "resolved"]}

    def _ownership_links(self, namespace, subject, keys, ownership_namespace, created) -> list[str]:
        from src.kb.research_entities_identity import ResearchEntitiesIdentity

        identity = ResearchEntitiesIdentity(self.conn, initialize=False, now=self.now)
        out = []
        for key, label in keys:
            for match in identity.accepted_ownership(namespace, key):
                status, target = self._ownership(match["ownership_key"], ownership_namespace)
                out.append(self._put(namespace, subject, "ownership_entity",
                                     target or {"record_key": match["ownership_key"]},
                                     {"method": "accepted-match", "match_id": match["match_id"],
                                      "match_method": match["method"], "decision_id": match["decision_id"],
                                      "subject_key": key, "subject_identifier": label}, status, created))
        return out

    # ------------------------------------------------------------------ reads

    def _view(self, row) -> dict[str, Any]:
        keys = ("link_id", "subject_record_id", "subject_revision_id", "subject", "target_kind", "target", "basis",
                "target_status", "history", "created_at_ms")
        value = dict(zip(keys, row))
        for key in ("subject", "target", "basis", "history"):
            value[key] = json.loads(value[key])
        value["target_pack"] = TARGETS[value["target_kind"]][0]
        return {"contract": CONTRACT, **value, "notice": NOTICE}

    def links(self, namespace: str, *, scopes: Iterable[str], record_id: str | None = None,
              revision_id: str | None = None, target_kind: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "rentity_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id, subject_record_id, subject_revision_id, subject_json, target_kind, target_json, "
            "basis_json, target_status, history_json, created_at_ms FROM rentity_links WHERE namespace=? AND "
            "(? IS NULL OR subject_record_id=?) AND (? IS NULL OR subject_revision_id=?) AND (? IS NULL OR "
            "target_kind=?) ORDER BY subject_record_id, target_kind, link_id",
            [namespace, record_id, record_id, revision_id, revision_id, target_kind, target_kind]).fetchall()
        return [self._view(r) for r in rows]

__all__ = ["BASES", "TARGETS", "ResearchEntitiesLinks"]
