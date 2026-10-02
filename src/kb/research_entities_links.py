"""Research-entity records linked to other packs by citation, shared identifier and accepted matches (#2579, RE08).

Every link starts at a specific record revision and points at a specific target revision, and records its basis:

* ``citation`` - the source record cites the target: a DOI a researcher's public ORCID record asserts as one of their
  works (labelled ORCID-asserted, never verified authorship), a DOI a dataset's metadata relates with a published
  ``relationType`` (IsSupplementTo, Cites, IsVersionOf, ...);
* ``shared-identifier`` - both sides publish the same identifier: the ROR id of an ORCID-asserted employment, a ROR
  id in a dataset creator's affiliation or name identifiers, a CORDIS project id as a dataset funding reference's award
  number (European Commission funder), a CORDIS topic or call identifier stated by a Funding & grants record;
* ``accepted-match`` - an accepted RE07 identity decision (ROR organisation or CORDIS participant to a Corporate
  Ownership entity, CORDIS participant to a ROR organisation), with the candidate and decision ids.

Targets live in other packs: Scholarly literature papers (documents whose metadata states the DOI), Funding & grants
records (``funding_opportunities``), Corporate Ownership entities (``ownership_records``) and this provider's own
records. A missing provider is reported as ``provider_absent`` and a missing target as ``target_missing``; both stay
on record with the cited identifier and are re-resolved on later runs - nothing is dropped. There are no inferred
collaboration, co-authorship or influence links and no name joins.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.research_entities_sources import (
    canonical,
    digest,
    normalize_doi,
    ror_url,
)
from src.kb.research_entities_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    ResearchEntityStore,
    authorize,
    may_read_researchers,
    table_exists,
)

CONTRACT = "noesis-research-entity-link-v1"
EC_FUNDER_IDS = {"10.13039/501100000780"}
LINK_KINDS = ("researcher-asserted-work", "researcher-asserted-employment", "dataset-related-work",
              "dataset-creator-affiliation", "dataset-funded-by-project", "project-funding-record",
              "organisation-ownership-entity", "participant-ownership-entity", "participant-organisation")
NOTICE = ("a link records what a registry record cites or an accepted identity decision; it is not evidence of "
          "authorship, collaboration or influence")
_DDL = """
CREATE TABLE IF NOT EXISTS research_entity_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, kind TEXT NOT NULL, source_key TEXT NOT NULL,
  source_revision_id TEXT NOT NULL, reference TEXT NOT NULL, target_side TEXT NOT NULL, target_key TEXT,
  target_revision TEXT, status TEXT NOT NULL, basis_json TEXT NOT NULL, history_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


class ResearchEntityLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = ResearchEntityStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ targets

    def _papers(self) -> dict[str, dict[str, Any]] | None:
        """Scholarly literature papers by the DOI their metadata states; ``None`` without the literature store."""
        if not table_exists(self.conn, "documents"):
            return None
        revisions = table_exists(self.conn, "document_revision_records")
        out: dict[str, dict[str, Any]] = {}
        for document_id, title, metadata, content_hash in self.conn.execute(
                "SELECT document_id, title, metadata, content_hash FROM documents WHERE source_type='paper' "
                "ORDER BY document_id").fetchall():
            meta = json.loads(metadata) if isinstance(metadata, str) and metadata else dict(metadata or {})
            doi = normalize_doi(meta.get("doi"))
            if not doi:
                continue
            revision = None
            if revisions:
                row = self.conn.execute(
                    "SELECT revision_id FROM document_revision_records WHERE document_id=? AND committed_watermark "
                    "IS NOT NULL ORDER BY revision DESC LIMIT 1", [document_id]).fetchone()
                revision = row[0] if row else None
            out.setdefault(doi, {"document_id": document_id, "title": title,
                                 "revision": revision or content_hash or "sha256:" + digest([document_id, meta])})
        return out

    def _funding(self, identifier: str) -> tuple[str, dict[str, Any] | None]:
        if not table_exists(self.conn, "funding_opportunity_revisions"):
            return "provider_absent", None
        rows = self.conn.execute(
            "SELECT o.opportunity_id, o.provider, o.revision, r.content_json FROM funding_opportunities o JOIN "
            "funding_opportunity_revisions r ON r.opportunity_id=o.opportunity_id AND r.revision=o.revision "
            "WHERE o.provider_id=? OR o.round_id=? ORDER BY o.opportunity_id", [identifier, identifier]).fetchall()
        for opportunity_id, provider, revision, content in rows:
            if identifier in str(content):  # the held record must state the identifier
                return "resolved", {"key": opportunity_id, "provider": provider, "revision": f"revision:{revision}"}
        return "target_missing", None

    # ------------------------------------------------------------ writes

    def _put(self, namespace, kind, source, reference, target_side, status, target, basis, principal_id) -> str:
        link_id = "re-link:" + digest([namespace, kind, source["record_key"], source["revision_id"], reference])[:24]
        target = target or {}
        now = self.now()
        row = self.conn.execute("SELECT status, target_key, target_revision, history_json FROM research_entity_links "
                                "WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO research_entity_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, link_id, kind, source["record_key"], source["revision_id"], reference, target_side,
                 target.get("key"), target.get("revision"), status, canonical(basis),
                 canonical([{"status": status, "by": principal_id, "at_ms": now}]), principal_id, now])
            return "created"
        if (row[0], row[1], row[2]) == (status, target.get("key"), target.get("revision")):
            return "unchanged"
        history = json.loads(row[3]) + [{"status": status, "previous_status": row[0], "by": principal_id,
                                         "at_ms": now}]
        self.conn.execute("UPDATE research_entity_links SET status=?, target_key=?, target_revision=?, basis_json=?, "
                          "history_json=? WHERE namespace=? AND link_id=?",
                          [status, target.get("key"), target.get("revision"), canonical(basis), canonical(history),
                           namespace, link_id])
        return "re-resolved"

    def link(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
             ownership_namespace: str | None = None) -> dict[str, Any]:
        """Resolve every citation, shared identifier and accepted match of the current revisions; idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.conn.execute(_DDL)
        papers = self._papers()
        providers = {"literature": "absent" if papers is None else "present",
                     "funding": "present" if table_exists(self.conn, "funding_opportunity_revisions") else "absent",
                     "ownership": ("not_requested" if not ownership_namespace else
                                   "present" if table_exists(self.conn, "ownership_records") else "absent"),
                     "researchers": ("linked" if may_read_researchers(scopes) else
                                     "skipped: linking researcher records needs the researchers:read scope")}
        records = self.store.records(namespace, scopes=scopes)
        by_key = {r["record_key"]: r for r in records}
        organisations = {r["record"]["fields"]["ror_id"]: r for r in records if r["record_kind"] == "organisation"}
        datasets = {r["record"]["fields"]["doi"]: r for r in records if r["record_kind"] == "dataset"}
        projects = {r["record"]["fields"]["project_id"]: r for r in records if r["record_kind"] == "project"}
        outcome: dict[str, int] = {}

        def put(kind, source, reference, side, status, target, basis):
            change = self._put(namespace, kind, source, reference, side, status, target, basis, principal_id)
            outcome[change] = outcome.get(change, 0) + 1

        def paper_or_dataset(doi):
            if doi in datasets:
                row = datasets[doi]
                return "research-entities", "resolved", {"key": row["record_key"], "revision": row["revision_id"]}
            if papers is None:
                return "literature", "provider_absent", None
            paper = papers.get(doi)
            if paper is None:
                return "literature", "target_missing", None
            return "literature", "resolved", {"key": paper["document_id"], "revision": paper["revision"]}

        def organisation(rid):
            row = organisations.get(rid)
            if row is None:
                return "target_missing", None
            return "resolved", {"key": row["record_key"], "revision": row["revision_id"]}

        for row in records:
            fields = row["record"]["fields"]
            if row["record_kind"] == "researcher":
                for work in fields.get("works") or []:
                    for ext in work.get("external_ids") or []:
                        doi = normalize_doi(ext.get("value")) if ext.get("type") == "doi" else None
                        if not doi:
                            continue
                        side, status, target = paper_or_dataset(doi)
                        put("researcher-asserted-work", row, f"doi:{doi}", side, status, target,
                            {"kind": "citation", "method": "orcid-asserted-work-doi", "put_code": work["put_code"],
                             "asserted_by": work["asserted_by"]["kind"], "relationship": ext.get("relationship"),
                             "label": "ORCID-asserted work; not verified authorship"})
                for employment in fields.get("employments") or []:
                    rid = ((employment.get("organisation") or {}).get("disambiguated") or {}).get("ror_id")
                    if not rid:
                        continue
                    status, target = organisation(rid)
                    put("researcher-asserted-employment", row, f"ror:{rid}", "research-entities", status, target,
                        {"kind": "shared-identifier", "method": "orcid-asserted-employment-ror",
                         "put_code": employment["put_code"], "asserted_by": employment["asserted_by"]["kind"],
                         "start_date": employment["start_date"], "end_date": employment["end_date"],
                         "label": "ORCID-asserted employment; not a verified affiliation"})
            elif row["record_kind"] == "dataset":
                for related in fields.get("related_identifiers") or []:
                    if not related.get("doi"):
                        continue
                    side, status, target = paper_or_dataset(related["doi"])
                    put("dataset-related-work", row, f"{related['relation_type']}:doi:{related['doi']}", side, status,
                        target, {"kind": "citation", "method": "datacite-related-identifier",
                                 "relation_type": related["relation_type"]})
                for role in ("creators", "contributors"):
                    for person in fields.get(role) or []:
                        ids = set(person.get("affiliation_ids") or [])
                        ids |= {ror_url(i.get("value")) for i in person.get("identifiers") or []
                                if str(i.get("scheme") or "").upper() == "ROR" and ror_url(i.get("value"))}
                        for rid in sorted(ids):
                            status, target = organisation(rid)
                            put("dataset-creator-affiliation", row, f"{role}[{person['position']}]:ror:{rid}",
                                "research-entities", status, target,
                                {"kind": "shared-identifier", "method": "datacite-ror-identifier", "role": role,
                                 "name_type": person["name_type"]})
                for funding in fields.get("funding_references") or []:
                    funder = normalize_doi(funding.get("funder_identifier"))
                    award = str(funding.get("award_number") or "").strip()
                    if not award or funder not in EC_FUNDER_IDS:
                        continue
                    project = projects.get(award)
                    put("dataset-funded-by-project", row, f"award:{award}", "research-entities",
                        "resolved" if project else "target_missing",
                        {"key": project["record_key"], "revision": project["revision_id"]} if project else None,
                        {"kind": "shared-identifier", "method": "datacite-award-number",
                         "funder_identifier": funding.get("funder_identifier")})
            elif row["record_kind"] == "project":
                topics = sorted(set(fields.get("topics") or []))
                calls = sorted({fields.get("master_call"), fields.get("sub_call")} - {None, ""} - set(topics))
                for identifier in topics + calls:
                    status, target = self._funding(identifier)
                    if identifier in calls and status != "resolved":
                        continue  # a call id is linked when a Funding record states it; topics are always reported
                    put("project-funding-record", row, f"funding:{identifier}", "funding", status, target,
                        {"kind": "shared-identifier", "method": "cordis-topic-or-call-identifier",
                         "identifier": identifier, "identifier_kind": "topic" if identifier in topics else "call"})
        if providers["ownership"] == "present" and not self.conn.execute(
                "SELECT 1 FROM ownership_records WHERE namespace=? LIMIT 1", [ownership_namespace]).fetchone():
            providers["ownership"] = "absent"
        self._accepted(namespace, by_key, scopes, ownership_namespace, providers, put)
        return {"outcome": outcome, "providers": providers, "links": self.links(namespace, scopes=scopes)}

    def _accepted(self, namespace, by_key, scopes, ownership_namespace, providers, put) -> None:
        from src.kb.research_entities_identity import ResearchEntityIdentity

        if not table_exists(self.conn, "ownership_identity_candidates"):
            return
        identity = ResearchEntityIdentity(self.conn, initialize=False)
        entities = {}
        if providers["ownership"] == "present":
            entities = {e["record"]["record_key"]: e for e in identity.service._entities(
                ownership_namespace, "research-entities-links", scopes)}
        for match in identity.accepted(namespace, scopes=scopes):
            subject = match["subject_key"]
            basis = {"kind": "accepted-match", "candidate_id": match["candidate_id"],
                     "decision_id": match["decision_id"], "method": match["method"], "reviewer": match["reviewer"],
                     "low_evidence": match["low_evidence"]}
            if ":cordis-participant:" in subject:
                projects = [by_key[k] for k in sorted(by_key) if k.startswith("research-entities:cordis:") and any(
                    p.get("pic") == subject.rsplit(":", 1)[1] for p in by_key[k]["record"]["fields"]["participants"])]
                sources = [{"record_key": subject, "revision_id": p["revision_id"]} for p in projects]
            else:
                source = by_key.get(subject)
                sources = [{"record_key": subject, "revision_id": source["revision_id"]}] if source else []
            for source in sources:
                if match["target_kind"] == "ror_organisation":
                    target = by_key.get(match["target_key"])
                    put("participant-organisation", source, f"match:{match['candidate_id']}", "research-entities",
                        "resolved" if target else "target_missing",
                        {"key": target["record_key"], "revision": target["revision_id"]} if target else None, basis)
                    continue
                kind = ("participant-ownership-entity" if ":cordis-participant:" in subject
                        else "organisation-ownership-entity")
                if providers["ownership"] != "present":
                    status = "provider_absent" if providers["ownership"] == "absent" else "not_requested"
                    put(kind, source, f"match:{match['candidate_id']}", "ownership", status, None, basis)
                    continue
                entity = entities.get(match["target_key"])
                put(kind, source, f"match:{match['candidate_id']}", "ownership",
                    "resolved" if entity else "target_missing",
                    {"key": match["target_key"], "revision": str(entity["revision"])} if entity else None,
                    {**basis, "ownership_namespace": ownership_namespace})

    # ------------------------------------------------------------ reads

    def links(self, namespace: str, *, scopes: Iterable[str], source_key: str | None = None,
              kind: str | None = None, target_key: str | None = None, source_revision_id: str | None = None,
              status: str | None = None) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "research_entity_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id, kind, source_key, source_revision_id, reference, target_side, target_key, "
            "target_revision, status, basis_json, history_json, created_by, created_at_ms FROM research_entity_links "
            "WHERE namespace=? AND (? IS NULL OR source_key=?) AND (? IS NULL OR kind=?) AND (? IS NULL OR "
            "target_key=?) AND (? IS NULL OR source_revision_id=?) AND (? IS NULL OR status=?) "
            "ORDER BY kind, source_key, source_revision_id, reference",
            [namespace, source_key, source_key, kind, kind, target_key, target_key, source_revision_id,
             source_revision_id, status, status]).fetchall()
        out = []
        for row in rows:
            view = dict(zip(("link_id", "kind", "source_key", "source_revision_id", "reference", "target_side",
                             "target_key", "target_revision", "status"), row[:9], strict=True))
            if view["source_key"].startswith("research-entities:orcid:") and not may_read_researchers(scopes):
                continue
            out.append({"contract": CONTRACT, "namespace": namespace, **view, "basis": json.loads(row[9]),
                        "history": json.loads(row[10]), "created_by": row[11], "created_at_ms": row[12],
                        "notice": NOTICE})
        return out

    def current(self, namespace: str, *, scopes: Iterable[str], source_key: str, source_revision_id: str | None = None,
                kind: str | None = None) -> list[dict[str, Any]]:
        """Links of one record revision (its current revision by default)."""
        if source_revision_id is None:
            head = self.store.records(namespace, scopes=scopes, record_keys=[source_key])
            if not head:
                return []
            source_revision_id = head[0]["revision_id"]
        return self.links(namespace, scopes=scopes, source_key=source_key, source_revision_id=source_revision_id,
                          kind=kind)


def summarise(links: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for link in links:
        counts[link["status"]] = counts.get(link["status"], 0) + 1
    return counts
