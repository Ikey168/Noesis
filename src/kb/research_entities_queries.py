"""Research-entity answers: a researcher's ORCID-asserted works and affiliations as of a date, and an organisation's
lineage, projects and related datasets (#2579, RE09 and RE10).

* :meth:`ResearchEntityQueries.researcher` takes an ORCID identifier and a date and answers from the record version in force
  then (the latest revision whose ORCID last-modified time is on or before the date): the public name, employments
  and works exactly as asserted in that version, each labelled **ORCID-asserted** (never verified authorship or a
  verified affiliation) with the asserting source kind and the links of that revision. Only the fields allowed by the
  RE01 minimisation decision are returned, and only to principals holding the researcher scope.
* :meth:`ResearchEntityQueries.organisation` takes a ROR id and a date and answers from the ROR release in force then:
  status, relationships as published in that release (parent, child, related, predecessor, successor) with the
  related records' own revisions, the successor and predecessor chains, CORDIS projects reached through accepted
  participant matches with each participation's role and contributions as published (totals are derived per currency,
  list their inputs and are never summed across currencies), datasets whose metadata cites the organisation's ROR id or
  a project it participates in, and accepted Corporate Ownership matches.
* :meth:`ResearchEntityQueries.datasets_for_paper` answers the datasets whose metadata relates a paper's DOI, with the
  relation type as published.

Every answer cites each record version (source, record revision, as-of time and observation time); a subject without
records is ``none_on_record`` and is never read as a clean result. No ranking, metric, inferred affiliation or name
disambiguation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from src.ingestion.research_entities_sources import (
    EXCLUSIONS,
    normalize_doi,
    ror_id,
    ror_url,
    valid_orcid,
)
from src.kb.research_entities_links import ResearchEntityLinks
from src.kb.research_entities_records import (
    READ_SCOPE,
    RESEARCHER_SCOPE,
    ResearchEntityError,
    ResearchEntityStore,
    authorize,
    forbidden_keys,
    may_read_researchers,
    table_exists,
)

CONTRACT = "noesis-research-entity-answer-v1"
ASSERTED = "ORCID-asserted: stated in the researcher's public ORCID record; not verified authorship or affiliation"
MAX_CHAIN = 10


def _date_in(start: str | None, end: str | None, day: str | None) -> str:
    """Whether a published (possibly partial) date range covers a day: 'yes', 'no' or 'unknown'."""
    if day is None:
        return "unknown"
    if start is None:
        return "unknown"
    if str(start) > day[:len(str(start))]:
        return "no"
    if end is not None and str(end) < day[:len(str(end))]:
        return "no"
    return "yes"


class ResearchEntityQueries:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = ResearchEntityStore(conn, initialize=False)
        self.links = ResearchEntityLinks(conn, initialize=False)

    def _links(self, namespace, scopes, revision) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "research_entity_links"):
            return []
        return self.links.links(namespace, scopes=scopes, source_key=revision["record_key"],
                                source_revision_id=revision["revision_id"])

    @staticmethod
    def _link_view(link: Mapping[str, Any]) -> dict[str, Any]:
        return {"kind": link["kind"], "status": link["status"], "target_side": link["target_side"],
                "target_key": link["target_key"], "target_revision": link["target_revision"],
                "basis": link["basis"], "link_id": link["link_id"]}

    @staticmethod
    def _versions(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"revision_id": v["revision_id"], "revision_no": v["revision_no"], "change": v["change"],
                 "native_revision": v["native_revision"], "source_as_of": v["source_as_of"], "status": v["status"],
                 "observed_at_ms": v["observed_at_ms"]} for v in history]

    # ------------------------------------------------------------------ RE09

    def researcher(self, namespace: str, orcid: str, *, scopes: Iterable[str], as_of: str | None = None
                   ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not may_read_researchers(scopes):
            raise ResearchEntityError("unauthorized", f"{RESEARCHER_SCOPE} is required to read researcher records "
                                      "(RE01 minimisation decision)")
        identifier = valid_orcid(orcid)
        if identifier is None:
            raise ResearchEntityError("invalid_request", "an ORCID identifier with a valid checksum is required")
        key = f"research-entities:orcid:{identifier}"
        history = self.store.history(namespace, key, scopes=scopes)
        base = {"contract": CONTRACT, "query": "researcher", "namespace": namespace, "orcid": identifier, "as_of": as_of,
                "exclusions": list(EXCLUSIONS)}
        if not history:
            return {**base, "status": "none_on_record",
                    "note": "no ORCID record of this identifier is on record; this says nothing about the researcher"}
        revision = self.store.as_of(namespace, key, as_of, scopes=scopes)
        if revision is None:
            return {**base, "status": "not_yet_published", "record_versions": self._versions(history),
                    "note": "no version of the record was in force on that date"}
        fields = revision["record"]["fields"]
        links = self._links(namespace, scopes, revision)
        day = str(as_of)[:10] if as_of else None
        employments = []
        for item in fields.get("employments") or []:
            rid = ((item.get("organisation") or {}).get("disambiguated") or {}).get("ror_id")
            employments.append({
                "assertion": ASSERTED, "put_code": item["put_code"],
                "organisation_as_asserted": item["organisation"], "department": item["department"],
                "role": item["role"], "start_date": item["start_date"], "end_date": item["end_date"],
                "covers_as_of_date": _date_in(item["start_date"], item["end_date"], day),
                "asserted_by": item["asserted_by"], "last_modified": item["last_modified"],
                "organisation_record": next((self._link_view(link) for link in links
                                             if link["kind"] == "researcher-asserted-employment"
                                             and link["reference"] == f"ror:{rid}"), None) if rid else None,
            })
        works = []
        for item in fields.get("works") or []:
            dois = [e["value"] for e in item.get("external_ids") or [] if e.get("type") == "doi"]
            works.append({
                "assertion": ASSERTED, "put_code": item["put_code"], "type": item["type"], "title": item["title"],
                "publication_year": item["publication_year"], "external_ids": item["external_ids"],
                "asserted_by": item["asserted_by"], "last_modified": item["last_modified"],
                "linked_records": [self._link_view(link) for link in links if link["kind"] ==
                                   "researcher-asserted-work" and link["reference"] in {f"doi:{d}" for d in dois}],
            })
        answer = {**base, "status": "answered", "record_status": revision["status"],
                  "record_version": {"revision_id": revision["revision_id"], "last_modified": fields["last_modified"],
                                     "native_revision": revision["native_revision"]},
                  "name": fields.get("name"), "name_status": fields.get("name_status"),
                  "employments": employments, "works": works,
                  "withheld_sections": fields.get("withheld_sections") or [],
                  "record_versions": self._versions(history), "citation": revision["citation"],
                  "labels": {"works": ASSERTED, "employments": ASSERTED},
                  "minimisation": "only the public fields allowed by research-entities-minimisation-v1 are returned"}
        if forbidden_keys(answer):
            raise ResearchEntityError("exclusion_violation", "an answer carries a ranking or metric key")
        return answer

    # ------------------------------------------------------------------ RE10

    def _org_at(self, namespace, rid, as_of, scopes) -> dict[str, Any] | None:
        return self.store.as_of(namespace, f"research-entities:ror:{ror_id(rid)}", as_of, scopes=scopes)

    def _chain(self, namespace, revision, as_of, scopes, direction) -> list[dict[str, Any]]:
        chain, seen, current = [], {revision["record_key"]}, revision
        while len(chain) < MAX_CHAIN:
            nxt = [r["id"] for r in current["record"]["fields"]["relationships"] if r["type"] == direction]
            if not nxt:
                break
            target = self._org_at(namespace, nxt[0], as_of, scopes)
            chain.append({"ror_id": nxt[0], "relationship": direction, "several_published": len(nxt) > 1,
                          "status": target["status"] if target else "not_on_record",
                          "citation": target["citation"] if target else None})
            if target is None or target["record_key"] in seen:
                break
            seen.add(target["record_key"])
            current = target
        return chain

    def organisation(self, namespace: str, ror: str, *, scopes: Iterable[str], as_of: str | None = None
                     ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        rid = ror_id(ror)
        if rid is None:
            raise ResearchEntityError("invalid_request", "a ROR id is required")
        key = f"research-entities:ror:{rid}"
        history = self.store.history(namespace, key, scopes=scopes)
        base = {"contract": CONTRACT, "query": "organisation", "namespace": namespace, "ror_id": ror_url(rid),
                "as_of": as_of, "exclusions": list(EXCLUSIONS)}
        if not history:
            return {**base, "status": "none_on_record",
                    "note": "no ROR record of this id is on record in the acquired releases"}
        revision = self.store.as_of(namespace, key, as_of, scopes=scopes)
        if revision is None:
            return {**base, "status": "not_yet_published", "record_versions": self._versions(history)}
        fields = revision["record"]["fields"]
        relationships = []
        for relation in fields["relationships"]:
            related = self._org_at(namespace, relation["id"], as_of, scopes)
            relationships.append({**relation, "related_status": related["status"] if related else "not_on_record",
                                  "related_citation": related["citation"] if related else None})
        projects, pending = self._projects(namespace, key, as_of, scopes)
        answer = {
            **base, "status": "answered", "record_status": fields["status"], "display_name": fields["display_name"],
            "names": fields["names"], "types": fields["types"], "external_ids": fields["external_ids"],
            "release": revision["record"].get("release"), "citation": revision["citation"],
            "record_versions": self._versions(history),
            "lineage": {"basis": "ROR relationships as published in the release in force", "relationships":
                        relationships, "successors": self._chain(namespace, revision, as_of, scopes, "successor"),
                        "predecessors": self._chain(namespace, revision, as_of, scopes, "predecessor")},
            "projects": projects, "participation_candidates_pending_review": pending,
            "contribution_totals": self._totals(projects),
            "datasets": self._datasets(namespace, key, [p["project_key"] for p in projects], scopes),
            "ownership": self._ownership(namespace, key, scopes),
        }
        answer["asserted_employments"] = self._employments(namespace, ror_url(rid), scopes)
        if forbidden_keys(answer):
            raise ResearchEntityError("exclusion_violation", "an answer carries a ranking or metric key")
        return answer

    def _projects(self, namespace, key, as_of, scopes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from src.kb.research_entities_identity import ResearchEntityIdentity

        if not table_exists(self.conn, "ownership_identity_candidates"):
            return [], []
        identity = ResearchEntityIdentity(self.conn, initialize=False)
        views = [v for v in identity.candidates(namespace, scopes=scopes) if v["target_key"] == key
                 and ":cordis-participant:" in v["subject_key"]]
        pending = [{"participant_key": v["subject_key"], "candidate_id": v["candidate_id"], "method": v["method"],
                    "low_evidence": v["low_evidence"],
                    "note": "a proposed match is not used until a reviewer accepts it"}
                   for v in views if v["state"] == "proposed"]
        out = []
        for match in [v for v in views if v["state"] == "accepted"]:
            pic = match["subject_key"].rsplit(":", 1)[1]
            for head in self.store.records(namespace, scopes=scopes, kinds=["project"]):
                revision = self.store.as_of(namespace, head["record_key"], as_of, scopes=scopes)
                if revision is None:
                    continue
                fields = revision["record"]["fields"]
                for participant in fields["participants"]:
                    if participant.get("pic") != pic:
                        continue
                    out.append({
                        "project_key": revision["record_key"], "programme": fields["programme"],
                        "project_id": fields["project_id"], "acronym": fields["acronym"], "title": fields["title"],
                        "status": fields["status"], "start_date": fields["start_date"], "end_date": fields["end_date"],
                        "participant": {"pic": pic, "name_as_published": participant["name"],
                                        "role": participant["role"], "order": participant["order"],
                                        "ec_contribution": participant["ec_contribution"],
                                        "net_ec_contribution": participant["net_ec_contribution"],
                                        "total_cost": participant["total_cost"]},
                        "match": {"candidate_id": match["candidate_id"], "decision_id": match["decision_id"],
                                  "method": match["method"], "reviewer": match["reviewer"]},
                        "citation": revision["citation"],
                    })
        out.sort(key=lambda p: (p["programme"], p["project_id"]))
        return out, pending

    @staticmethod
    def _totals(projects: list[dict[str, Any]]) -> dict[str, Any]:
        """EU contributions as published, summed per currency (derived, listing the inputs); never across currencies."""
        groups: dict[str, dict[str, Any]] = {}
        for project in projects:
            money = project["participant"].get("ec_contribution") or {}
            if money.get("amount") is None:
                continue
            group = groups.setdefault(money["currency"], {"sum": Decimal(0), "inputs": []})
            group["sum"] += Decimal(money["amount"])
            group["inputs"].append({"project_key": project["project_key"], "amount": money["amount"],
                                    "revision_id": project["citation"]["revision_id"]})
        return {"basis": "derived from the listed participations' EU contributions as published; per currency, "
                         "never converted or summed across currencies; not stored",
                "per_currency": {currency: {"sum": format(g["sum"], "f"), "inputs": g["inputs"]}
                                 for currency, g in sorted(groups.items())},
                "currencies": sorted(groups)}

    def _datasets(self, namespace, key, project_keys, scopes) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "research_entity_links"):
            return []
        found: dict[str, dict[str, Any]] = {}
        for link in self.links.links(namespace, scopes=scopes, status="resolved"):
            via = None
            if link["kind"] == "dataset-creator-affiliation" and link["target_key"] == key:
                via = {"basis": "the dataset metadata cites the organisation's ROR id", "link_id": link["link_id"]}
            elif link["kind"] == "dataset-funded-by-project" and link["target_key"] in project_keys:
                via = {"basis": "the dataset metadata names a project the organisation participates in as its award",
                       "project_key": link["target_key"], "link_id": link["link_id"]}
            if via is None:
                continue
            head = self.store.records(namespace, scopes=scopes, record_keys=[link["source_key"]])
            if not head or head[0]["revision_id"] != link["source_revision_id"]:
                continue  # only links of the dataset's current revision
            fields = head[0]["record"]["fields"]
            entry = found.setdefault(link["source_key"], {
                "dataset_key": link["source_key"], "doi": fields["doi"], "title": head[0]["record"]["title"],
                "record_status": head[0]["status"], "metadata_version": fields.get("metadata_version"),
                "via": [], "citation": head[0]["citation"]})
            entry["via"].append(via)
        return sorted(found.values(), key=lambda d: d["dataset_key"])

    def _ownership(self, namespace, key, scopes) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "research_entity_links"):
            return []
        return [self._link_view(link) for link in self.links.links(
            namespace, scopes=scopes, source_key=key, kind="organisation-ownership-entity")]

    def _employments(self, namespace, rid, scopes) -> dict[str, Any]:
        """ORCID iDs whose current public record asserts an employment at the organisation (researcher scope only)."""
        if not may_read_researchers(scopes):
            return {"withheld": True, "note": f"{RESEARCHER_SCOPE} is required to list researcher records"}
        if not table_exists(self.conn, "research_entity_links"):
            return {"withheld": False, "assertions": []}
        rows = []
        for link in self.links.links(namespace, scopes=scopes, kind="researcher-asserted-employment",
                                     target_key=f"research-entities:ror:{ror_id(rid)}"):
            head = self.store.records(namespace, scopes=scopes, record_keys=[link["source_key"]])
            if head and head[0]["revision_id"] == link["source_revision_id"]:
                rows.append({"orcid": head[0]["record"]["fields"]["orcid"], "assertion": ASSERTED,
                             "put_code": link["basis"]["put_code"], "start_date": link["basis"]["start_date"],
                             "end_date": link["basis"]["end_date"], "citation": head[0]["citation"]})
        return {"withheld": False, "assertions": rows,
                "note": "affiliations are only what each researcher's public ORCID record asserts; none is inferred"}

    def datasets_for_paper(self, namespace: str, doi: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        target = normalize_doi(doi)
        if target is None:
            raise ResearchEntityError("invalid_request", "a DOI is required")
        datasets = []
        for head in self.store.records(namespace, scopes=scopes, kinds=["dataset"]):
            relations = [r for r in head["record"]["fields"].get("related_identifiers") or [] if r.get("doi") == target]
            if relations:
                datasets.append({"dataset_key": head["record_key"], "doi": head["record"]["fields"]["doi"],
                                 "title": head["record"]["title"], "record_status": head["status"],
                                 "relations_as_published": [r["relation_type"] for r in relations],
                                 "citation": head["citation"]})
        return {"contract": CONTRACT, "query": "datasets_for_paper", "namespace": namespace, "doi": target,
                "status": "answered" if datasets else "none_on_record", "datasets": datasets,
                "basis": "DataCite related identifiers as published; no dataset is inferred",
                "exclusions": list(EXCLUSIONS)}

    # ------------------------------------------------------------------ evidence

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing the record revision behind it: source, record revision, as-of and observation time."""
        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []

        def add(identifier: str, text: str, citation: Mapping[str, Any] | None) -> None:
            if not citation:
                return
            bibliography.setdefault(citation["revision_id"], {
                "id": citation["revision_id"],
                "text": f"{citation['provider']} {citation['record_key']} (source {citation['source_id']}, revision "
                        f"{citation['revision_no']}, native revision {citation['native_revision']}, as of "
                        f"{citation['source_as_of'] or 'not stated'}, observed {citation['observed_at_ms']}, "
                        f"{citation['evidence_origin']} evidence), {citation['locator']}"})
            assertions.append({"id": identifier, "text": text, "kind": "sourced",
                               "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                                 "id": citation["record_key"], "revision": citation["revision_id"],
                                                 "locator": {"section": citation["record_key"]}}],
                               "citations": [citation["revision_id"]]})

        if answer.get("query") == "researcher" and answer.get("status") == "answered":
            add("record", f"ORCID record {answer['orcid']} version {answer['record_version']['last_modified']} "
                f"({answer['record_status']})", answer["citation"])
            for item in answer["employments"]:
                add(f"employment-{item['put_code']}", f"ORCID-asserted employment at "
                    f"{item['organisation_as_asserted'].get('name')} ({item['start_date']} to "
                    f"{item['end_date'] or 'open'})", answer["citation"])
            for item in answer["works"]:
                add(f"work-{item['put_code']}", f"ORCID-asserted work {item['title']} "
                    f"{[e['value'] for e in item['external_ids']]}", answer["citation"])
        elif answer.get("query") == "organisation" and answer.get("status") == "answered":
            add("record", f"ROR {answer['ror_id']} {answer['display_name']} ({answer['record_status']}) in release "
                f"{(answer.get('release') or {}).get('label')}", answer["citation"])
            for relation in answer["lineage"]["relationships"]:
                add(f"relationship-{relation['type']}-{relation['id']}", f"{relation['type']} {relation['id']} "
                    f"({relation['related_status']})", relation["related_citation"] or answer["citation"])
            for project in answer["projects"]:
                add(f"project-{project['project_key']}", f"{project['programme']} {project['project_id']} "
                    f"{project['acronym']}: {project['participant']['role']}, EU contribution "
                    f"{(project['participant']['ec_contribution'] or {}).get('amount')} "
                    f"{(project['participant']['ec_contribution'] or {}).get('currency')} as published",
                    project["citation"])
            for dataset in answer["datasets"]:
                add(f"dataset-{dataset['dataset_key']}", f"dataset {dataset['doi']} {dataset['title']}",
                    dataset["citation"])
        elif answer.get("query") == "datasets_for_paper":
            for dataset in answer.get("datasets") or []:
                add(f"dataset-{dataset['dataset_key']}", f"dataset {dataset['doi']} "
                    f"{'/'.join(dataset['relations_as_published'])} {answer['doi']}", dataset["citation"])
        title = answer.get("orcid") or answer.get("ror_id") or answer.get("doi")
        return {"sections": [{"id": answer.get("query", "answer"), "title": f"{title} as of {answer.get('as_of')}",
                              "assertions": assertions}],
                "bibliography": list(bibliography.values()), "exclusions": list(EXCLUSIONS)}
