"""As-of answers over research-entity records (#2579, RE09 #2624 and RE10 #2629).

* :meth:`ResearchEntitiesQueries.researcher` - given an ORCID iD, the employments and works asserted in the ORCID
  record version in force at a date, each labelled ``orcid-asserted`` (never verified authorship), with only the
  minimisation-allowed fields, the record version cited, the papers the asserted DOIs link to and the DataCite
  datasets that name the iD. Readable only with the researchers scope.
* :meth:`ResearchEntitiesQueries.organisation` - given a ROR ID, the ROR record of the release in force at a date with
  its relationships as published, the lineage along predecessor and successor relationships (each hop cited), the
  CORDIS projects of participants an accepted match ties to it (contributions grouped per currency, never summed
  across currencies), the DataCite datasets whose creators state it as an affiliation identifier, ownership links
  through accepted matches, and pending or unmatched identity.
* :meth:`ResearchEntitiesQueries.datasets_for_paper` - DataCite datasets whose related identifiers name a DOI, with
  the relation type as published.
* :meth:`ResearchEntitiesQueries.record_history` - every revision of one record, each cited.

Every answer cites each record version with its source, revision and as-of time, reports records not held as
``none_on_record`` and declares the exclusions: no rankings or metrics, no affiliation inferred from co-authorship,
no author matching by name, no personal data beyond the minimisation decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from src.ingestion.research_entities_sources import (
    EXCLUSIONS,
    NEVER_SENTENCE,
    REMOVAL_STATUSES,
)
from src.kb.research_entities_records import (
    READ_SCOPE,
    RESEARCHER_SCOPE,
    ResearchEntitiesError,
    ResearchEntitiesStore,
    as_of_ms,
    authorize,
    iso_from_ms,
    native_for,
    require_scope,
    researcher_view,
    table_exists,
)

ANSWER_CONTRACT = "noesis-research-entity-answer-v1"
LINEAGE_TYPES = ("predecessor", "successor")
MAX_LINEAGE = 20


class ResearchEntitiesQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = ResearchEntitiesStore(conn, initialize=False, now=now)

    # ------------------------------------------------------------------ helpers

    def _entry(self, namespace: str, kind: str, native: str, cutoff: int | None) -> dict[str, Any] | None:
        """The revision of a record in force at the cutoff, its statement, citation and known history."""
        record_id = self.store.find(namespace, kind, native)
        if record_id is None:
            return None
        revision, known = self.store.revision_as_of(namespace, record_id, cutoff)
        if revision is None:
            return {"record_id": record_id, "revision": None, "statement": None, "citation": None,
                    "history": [], "reason": "no revision of this record was in force at the as-of date"}
        return {"record_id": record_id, "revision": revision,
                "statement": self.store.statement(namespace, revision["revision_id"]),
                "citation": self.store.citation(namespace, revision, cutoff=cutoff),
                "history": [{k: r[k] for k in ("revision_id", "revision", "marker", "effective_at", "status")}
                            for r in known]}

    def _envelope(self, query: Mapping[str, Any], cutoff: int | None, status: str, citations, **body) -> dict:
        unique = {c["revision_id"]: c for c in citations if c}
        return {"contract": ANSWER_CONTRACT, "query": dict(query),
                "as_of": iso_from_ms(cutoff) if cutoff is not None else "latest", "as_of_ms": cutoff,
                "status": status, **body, "citations": [unique[k] for k in sorted(unique)],
                "never": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    def _datasets(self, namespace: str, cutoff: int | None, *, orcid: str | None = None, ror: str | None = None,
                  related: str | None = None) -> list[dict[str, Any]]:
        out = []
        for record in self.store.records(namespace, kind="dataset"):
            entry = self._entry(namespace, "dataset", record["native_id"], cutoff)
            if not entry or not entry["statement"] or not entry["statement"]["body"]:
                continue
            body = entry["statement"]["body"]
            basis = None
            if orcid and any(c.get("orcid") == orcid for c in body["creators"]):
                basis = {"method": "creator-orcid-as-published", "orcid": orcid}
            if ror and any(a["identifier"] == ror for c in body["creators"] for a in c["affiliation_identifiers"]):
                basis = {"method": "affiliation-identifier-as-published", "ror_id": ror}
            relations = []
            if related:
                from src.ingestion.research_entities_sources import (
                    ResearchEntitiesFormatError,
                    doi,
                )

                for item in body["related_identifiers"]:
                    try:
                        same = str(item.get("relatedIdentifierType")).upper() == "DOI" and \
                            doi(item.get("relatedIdentifier")) == related
                    except ResearchEntitiesFormatError:
                        same = False
                    if same:
                        relations.append(item)
                if relations:
                    basis = {"method": "published-related-identifier", "doi": related,
                             "relation_types": sorted({r["relationType"] for r in relations}),
                             "as_published": relations}
            if basis is None:
                continue
            out.append({"doi": body["doi"], "titles": body["titles"], "publisher": body["publisher"],
                        "publication_year": body["publication_year"], "version": body["version"],
                        "metadata_version": body["metadata_version"], "related_identifiers": body["related_identifiers"],
                        "basis": basis, "citation": entry["citation"]})
        return out

    def _links(self, namespace: str, revision_id: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "rentity_links"):
            return []
        from src.kb.research_entities_links import ResearchEntitiesLinks

        return ResearchEntitiesLinks(self.conn, initialize=False).links(namespace, scopes={"operator"},
                                                                        revision_id=revision_id)

    # ------------------------------------------------------------------ researcher (RE09)

    def researcher(self, namespace: str, orcid: str, *, scopes: Iterable[str], as_of: Any = None) -> dict[str, Any]:
        """Employments and works asserted in the ORCID record version in force at ``as_of``."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, RESEARCHER_SCOPE)
        cutoff = as_of_ms(as_of)
        native = native_for("researcher", orcid)
        query = {"orcid": native}
        entry = self._entry(namespace, "researcher", native, cutoff)
        if entry is None:
            return self._envelope(query, cutoff, "none_on_record", [], researcher=None,
                                  reason="no ORCID record for this iD is held; nothing is inferred from names")
        if entry["statement"] is None:
            return self._envelope(query, cutoff, "none_on_record", [], researcher=None, reason=entry["reason"])
        latest, _ = self.store.revision_as_of(namespace, entry["record_id"], None)
        withhold = latest is not None and latest["status"] in REMOVAL_STATUSES
        statement = entry["statement"]
        if statement["status"] in REMOVAL_STATUSES:
            return self._envelope(query, cutoff, "removed", [entry["citation"]], researcher=None,
                                  record_status=statement["status"], history=entry["history"],
                                  reason="ORCID reported the record as " + statement["status"])
        view = researcher_view(statement, withhold_name=withhold)
        links = self._links(namespace, entry["revision"]["revision_id"])
        papers = [{"doi": link["basis"]["identifier"]["value"], "target_status": link["target_status"],
                   "target": link["target"], "basis": link["basis"]["method"],
                   "assertion": "orcid-asserted, not verified authorship"}
                  for link in links if link["target_kind"] == "scholarly_work"]
        datasets = self._datasets(namespace, cutoff, orcid=native)
        return self._envelope(
            query, cutoff, "answered", [entry["citation"]] + [d["citation"] for d in datasets],
            researcher=view, record_status=statement["status"], record_revision=entry["citation"],
            history=entry["history"], linked_papers=papers, datasets=datasets,
            notice="employments and works are what the researcher asserts in ORCID; they are not verified "
            "authorship or affiliation, and nothing is inferred from co-authorship or names")

    # ------------------------------------------------------------------ organisation (RE10)

    def _lineage(self, namespace: str, ror: str, cutoff: int | None) -> list[dict[str, Any]]:
        hops, seen, frontier = [], {ror}, [ror]
        while frontier and len(hops) < MAX_LINEAGE:
            current = frontier.pop(0)
            entry = self._entry(namespace, "organisation", current, cutoff)
            if not entry or not entry["statement"] or not entry["statement"]["body"]:
                continue
            for relation in entry["statement"]["body"]["relationships"]:
                if relation["type"] not in LINEAGE_TYPES:
                    continue
                target = self._entry(namespace, "organisation", relation["id"], cutoff)
                held = bool(target and target["statement"] and target["statement"]["body"])
                hops.append({"from": current, "type": relation["type"], "to": relation["id"],
                             "label_as_published": relation.get("label"),
                             "stated_in": entry["citation"],
                             "to_record": {"status": target["statement"]["status"],
                                           "display_name": target["statement"]["body"]["display_name"],
                                           "citation": target["citation"]} if held else None,
                             "to_status": "held" if held else "not_held"})
                if relation["id"] not in seen:
                    seen.add(relation["id"])
                    frontier.append(relation["id"])
        return hops

    def _projects(self, namespace: str, ror: str, cutoff: int | None) -> tuple[list[dict[str, Any]], dict]:
        from src.kb.research_entities_identity import ResearchEntitiesIdentity

        if not table_exists(self.conn, "rentity_identity_matches"):
            return [], {}
        accepted = {a["pic"]: a for a in ResearchEntitiesIdentity(self.conn, initialize=False).accepted_pics(
            namespace, ror)}
        projects, totals = [], {}
        for record in self.store.records(namespace, kind="project"):
            entry = self._entry(namespace, "project", record["native_id"], cutoff)
            if not entry or not entry["statement"] or not entry["statement"]["body"]:
                continue
            body = entry["statement"]["body"]
            mine = [p for p in body["participants"] if p["pic"] in accepted]
            if not mine:
                continue
            for participant in mine:
                for field in ("ec_contribution", "net_ec_contribution"):
                    money = participant.get(field)
                    if money:
                        bucket = totals.setdefault(field, {})
                        bucket[money["currency"]] = str(Decimal(bucket.get(money["currency"], "0"))
                                                        + Decimal(money["amount"]))
            projects.append({
                "project": {k: body.get(k) for k in ("project_id", "programme", "acronym", "title", "status",
                                                     "start_date", "end_date", "total_cost", "ec_max_contribution",
                                                     "grant_doi")},
                "participation": [{k: p.get(k) for k in ("pic", "name", "role", "activity_type", "country",
                                                          "ec_contribution", "net_ec_contribution", "total_cost")}
                                  | {"identity": accepted[p["pic"]]} for p in mine],
                "citation": entry["citation"],
                "funding_links": [link for link in self._links(namespace, entry["revision"]["revision_id"])
                                  if link["target_kind"] == "funding_record"],
            })
        return projects, totals

    def organisation(self, namespace: str, ror: str, *, scopes: Iterable[str], as_of: Any = None) -> dict[str, Any]:
        """A ROR organisation as in the release in force at ``as_of`` with lineage, projects and datasets."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        cutoff = as_of_ms(as_of)
        native = native_for("organisation", ror)
        query = {"ror": native}
        entry = self._entry(namespace, "organisation", native, cutoff)
        if entry is None or entry["statement"] is None:
            return self._envelope(query, cutoff, "none_on_record", [], organisation=None,
                                  reason=(entry or {}).get("reason") or "no ROR record for this ID is held")
        statement = entry["statement"]
        releases = [m for m in self.store.memberships(namespace, entry["record_id"])
                    if cutoff is None or (m["published_on"] or "9999") <= iso_from_ms(cutoff)[:10]]
        if statement["status"] in REMOVAL_STATUSES:
            return self._envelope(query, cutoff, "removed", [entry["citation"]], organisation=None,
                                  record_status=statement["status"], history=entry["history"],
                                  reason="the ROR release in force does not contain this ID")
        body = statement["body"]
        lineage = self._lineage(namespace, native, cutoff)
        projects, totals = self._projects(namespace, native, cutoff)
        datasets = self._datasets(namespace, cutoff, ror=native)
        identity = self._identity(namespace, native)
        links = [link for link in self._links(namespace, entry["revision"]["revision_id"])
                 if link["target_kind"] == "ownership_entity"]
        citations = [entry["citation"]] + [h["stated_in"] for h in lineage] + \
            [h["to_record"]["citation"] for h in lineage if h["to_record"]] + [p["citation"] for p in projects] + \
            [d["citation"] for d in datasets]
        return self._envelope(
            query, cutoff, "answered", citations,
            organisation={"ror_id": body["ror_id"], "display_name": body["display_name"], "names": body["names"],
                          "types": body["types"], "status": statement["status"], "established": body["established"],
                          "locations": body["locations"], "external_ids": body["external_ids"],
                          "relationships": body["relationships"], "links": body["links"]},
            record_revision=entry["citation"], history=entry["history"],
            release_vintages=releases[-5:], lineage=lineage, projects=projects,
            contributions_by_currency=totals,
            contribution_note="contributions are grouped per currency as published; amounts in different currencies "
            "are never summed or converted",
            datasets=datasets, ownership_links=links, identity=identity)

    def _identity(self, namespace: str, ror: str) -> dict[str, Any]:
        if not table_exists(self.conn, "rentity_identity_matches") and \
                not table_exists(self.conn, "ownership_identity_candidates"):
            return {"status": "not_proposed", "matches": []}
        from src.kb.research_entities_identity import ResearchEntitiesIdentity

        identity = ResearchEntitiesIdentity(self.conn, initialize=False)
        matches = identity.matches(namespace, scopes={"operator"}, ror=ror)
        return {"status": "matched" if any(m["state"] == "accepted" for m in matches) else
                "candidates_pending" if any(m["state"] == "proposed" for m in matches) else "unmatched",
                "matches": [{k: m[k] for k in ("match_id", "kind", "method", "confidence", "state", "low_evidence")}
                            | {"right": m["right"]} for m in matches],
                "note": "only accepted matches are used; proposals, rejections and reverts are listed, never used"}

    # ------------------------------------------------------------------ datasets and history

    def datasets_for_paper(self, namespace: str, paper_doi: str, *, scopes: Iterable[str], as_of: Any = None
                           ) -> dict[str, Any]:
        """DataCite datasets whose related identifiers name a paper DOI, relation types as published."""
        authorize(namespace, set(scopes), READ_SCOPE)
        cutoff = as_of_ms(as_of)
        native = native_for("dataset", paper_doi)
        datasets = self._datasets(namespace, cutoff, related=native)
        return self._envelope({"paper_doi": native}, cutoff, "answered" if datasets else "none_on_record",
                              [d["citation"] for d in datasets], datasets=datasets,
                              **({} if datasets else {"reason": "no held dataset states this DOI among its related "
                                                                "identifiers"}))

    def record_history(self, namespace: str, kind: str, identifier: str, *, scopes: Iterable[str],
                       programme: str | None = None) -> dict[str, Any]:
        """Every revision of one record, oldest first, each cited; removals and corrections included."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if kind == "researcher":
            require_scope(scopes, RESEARCHER_SCOPE)
        native = native_for(kind, identifier, programme)
        record_id = self.store.find(namespace, kind, native)
        if record_id is None:
            return self._envelope({"kind": kind, "identifier": native}, None, "none_on_record", [], revisions=[])
        citations, revisions = [], []
        latest, _ = self.store.revision_as_of(namespace, record_id, None)
        withhold = latest is not None and latest["status"] in REMOVAL_STATUSES
        for revision in self.store.revisions(namespace, record_id):
            citation = self.store.citation(namespace, revision)
            statement = self.store.statement(namespace, revision["revision_id"])
            body = researcher_view(statement, withhold_name=withhold) if kind == "researcher" and statement["body"] \
                else statement["body"]
            revisions.append({"revision": revision["revision"], "status": revision["status"],
                              "marker": revision["marker"], "effective_at": revision["effective_at"],
                              "previous_revision_id": revision["previous_revision_id"], "record": body,
                              "citation": citation})
            citations.append(citation)
        return self._envelope({"kind": kind, "identifier": native}, None, "answered", citations, revisions=revisions,
                              memberships=self.store.memberships(namespace, record_id))

    # ------------------------------------------------------------------ evidence bundle

    @staticmethod
    def export_bundle(answer: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
        """A noesis-evidence-bundle-v1 citing every record revision the answer used with its source, revision and
        as-of time; unresolved links and records not held are omissions."""
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        if answer.get("contract") != ANSWER_CONTRACT:
            raise ResearchEntitiesError("invalid_request", "export an answer of the research-entities queries")
        builder = EvidenceBundleBuilder("answer", {"operation": "research-entities", "query": answer["query"]},
                                        created_at_ms=created_at_ms, as_of_ms=answer.get("as_of_ms"))
        refs = []
        for citation in answer.get("citations") or []:
            object_id = f"rentity-revision:{citation['revision_id'].rsplit(':', 1)[-1]}"
            builder.add_object("evidence", {
                "kind": "research-entity-revision", "locator": {"cited": True, "record_id": citation["record_id"],
                                                                "revision_id": citation["revision_id"]},
                "source": citation["provider"], "record_kind": citation["record_kind"],
                "native_id": citation["native_id"], "status": citation["status"],
                "revision": {k: citation[k] for k in ("revision", "revision_marker", "revision_basis",
                                                      "effective_at", "retrieved_at")},
                "as_of": citation["as_of"], "release": {k: citation[k] for k in ("release_id", "release_label",
                                                                                 "published_on")},
                "licence": citation["licence"], "attribution": citation["attribution"],
                "evidence_origin": citation["evidence_origin"], "live_verification": citation["live_verification"],
            }, object_id=object_id)
            refs.append(object_id)
            if citation.get("record_url"):
                builder.add_external_reference(f"record:{citation['record_id']}", citation["record_url"],
                                               required=False)
        for key in ("linked_papers",):
            for item in answer.get(key) or []:
                if item["target_status"] != "resolved":
                    builder.add_omission(f"asserted work {item['doi']}: {item['target_status']}")
        for hop in answer.get("lineage") or []:
            if hop["to_status"] != "held":
                builder.add_omission(f"lineage {hop['type']} {hop['to']} is not held")
        if answer.get("status") != "answered":
            builder.add_omission(str(answer.get("reason") or answer.get("status")))
        root = {k: answer.get(k) for k in ("contract", "query", "as_of", "status", "never")}
        builder.add_object("answer", {"kind": "research-entities", **root}, object_id="rentity-answer:root",
                           references=sorted(set(refs)), root=True)
        return builder.build()


__all__ = ["ANSWER_CONTRACT", "ResearchEntitiesQueries"]
