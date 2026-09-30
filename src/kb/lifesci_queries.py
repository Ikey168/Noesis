"""As-of answers: an entry at a release with its cross-reference graph, and published activity against a target.

LS09 (#2696) and LS10 (#2701) over the life-science records:

* :func:`entry_as_of` - given an accession (or a Gene ID, Tax ID, PDB ID or
  ChEMBL ID), the revision in force at a published release or on record at a
  date. Obsolete, merged, replaced and superseded identifiers are resolved to
  the successors their source names, with every step of the history shown; a
  UniProt secondary accession resolves to the entry that lists it. The UniSave
  version history answers which entry and sequence version was in force at a
  release even when that version was never acquired in full (reported as such).
  Cross-references are labelled with the source whose record asserts them
  (outgoing and incoming), with the reviewed identity state of each pair and
  the cross-pack links of the record. Every entry version used is cited.
* :func:`target_activities` - given a ChEMBL target (or a UniProt accession a
  ChEMBL target component names), the compounds and activities ChEMBL
  published for one release: published and ChEMBL-standardised type, relation,
  value and unit as strings, data-validity and activity comments, each row
  citing its activity revision and document. Values are never converted,
  compared or aggregated across assays or assay types.
* :func:`export_bundle` - either answer as a ``noesis-evidence-bundle-v1``
  citing every item with source, record revision and as-of time.

No answer infers function, interactions, disease relevance or activity, and no
answer carries a person name (the LS01 minimisation decision).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from src.kb.lifesci_records import (
    ACCESSION,
    ACTIVITY_ANSWER_CONTRACT,
    ENTRY_ANSWER_CONTRACT,
    MINIMISATION,
    NEVER_SENTENCE,
    READ_SCOPE,
    SUBJECT_PREFIX,
    LifeSciError,
    authorize,
    digest,
    forbidden_keys,
    release_order,
)
from src.kb.lifesci_store import LifeSciStore

ENTRY_TYPES = ("protein", "gene", "taxon", "structure", "target", "compound")
XREF_PREFIX = {"pdb": "pdb", "geneid": "ncbigene", "chembl": "chembl-target", "uniprotkb/swiss-prot": "uniprot",
               "uniprotkb/trembl": "uniprot", "uniprot": "uniprot"}
MAX_HOPS = 5


def as_of_ms(value: Any) -> int | None:
    if value is None or isinstance(value, int):
        return value
    text = str(value)
    if len(text) == 10:
        text += "T23:59:59.999+00:00"
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _date(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat()


def cite(record: Mapping[str, Any], revision: Mapping[str, Any]) -> dict[str, Any]:
    statement = revision["statement"]
    return {"record_id": record["record_id"], "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"], "provider": record["provider"],
            "record_type": record["record_type"], "record_key": record["record_key"], "release": revision["release"],
            "event": revision["event"], "url": statement["source"]["url"],
            "api_url": statement["source"].get("api_url"), "licence": statement["source"].get("licence"),
            "attribution": statement["source"].get("attribution"), "retrieved_at_ms": revision["observed_at_ms"],
            "retrieved_on": revision["retrieved_on"], "evidence_origin": revision["evidence_origin"]}


def successors(record_type: str, published: Mapping[str, Any]) -> list[str]:
    """Subject keys the source names as successors of an obsolete record."""
    if record_type == "protein":
        return [f"uniprot:{a}" for a in (published.get("inactive_reason") or {}).get("successors") or []]
    if record_type == "gene" and published.get("replaced_by"):
        return [f"ncbigene:{published['replaced_by']}"]
    if record_type == "taxon" and published.get("merged_into"):
        return [f"ncbitaxon:{published['merged_into']}"]
    if record_type == "structure":
        return [f"pdb:{p}" for p in published.get("superseded_by") or []]
    return []


def assertions(record_type: str, published: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Cross-references one record publishes: (database, id, field) and the covered subject key when known."""
    out: list[dict[str, Any]] = []

    def add(database: str, value: Any, field: str) -> None:
        prefix = XREF_PREFIX.get(str(database).casefold())
        out.append({"database": database, "id": str(value), "field": field,
                    "subject_key": f"{prefix}:{value}" if prefix else None})

    if record_type == "protein":
        for ref in published.get("cross_references") or []:
            add(ref["database"], ref["id"], "cross_references")
        taxon = (published.get("organism") or {}).get("taxon_id")
        if taxon:
            out.append({"database": "NCBI Taxonomy", "id": str(taxon), "field": "organism.taxon_id",
                        "subject_key": f"ncbitaxon:{taxon}"})
    elif record_type == "gene":
        for ref in published.get("cross_references") or []:
            add(ref["database"], ref["id"], "cross_references")
        out.append({"database": "NCBI Taxonomy", "id": published["tax_id"], "field": "tax_id",
                    "subject_key": f"ncbitaxon:{published['tax_id']}"})
    elif record_type == "structure":
        for entity in published.get("entities") or []:
            for accession in entity["uniprot_accessions"]:
                add("UniProt", accession, f"entities[{entity['entity_id']}].uniprot_accessions")
    elif record_type == "target":
        for component in published.get("components") or []:
            if component.get("accession"):
                add("UniProt", component["accession"], "components.accession")
        if published.get("tax_id"):
            out.append({"database": "NCBI Taxonomy", "id": str(published["tax_id"]), "field": "tax_id",
                        "subject_key": f"ncbitaxon:{published['tax_id']}"})
    elif record_type == "compound" and published.get("standard_inchikey"):
        out.append({"database": "InChIKey", "id": published["standard_inchikey"], "field": "standard_inchikey",
                    "subject_key": None})
    return out


class LifeSciQueries:
    def __init__(self, conn: Any) -> None:
        from src.kb.lifesci_identity import LifeSciIdentity
        from src.kb.lifesci_links import LifeSciLinks

        self.conn = conn
        self.store = LifeSciStore(conn, initialize=False)
        self.identity = LifeSciIdentity(conn, initialize=False)
        self.links = LifeSciLinks(conn, initialize=False)

    # ------------------------------------------------------------------ resolution

    def _by_subject(self, namespace: str) -> dict[str, dict[str, Any]]:
        return {r["subject_key"]: r for r in self.store.records(namespace) if r["record_type"] in ENTRY_TYPES}

    def _find(self, namespace: str, query: str) -> tuple[str, list[str]]:
        text = str(query or "").strip()
        if not text:
            raise LifeSciError("invalid_request", "give an accession, Gene ID, Tax ID, PDB ID or ChEMBL ID")
        subjects = self._by_subject(namespace)
        if text in subjects:
            return "subject_key", [text]
        keys = sorted(k for k, r in subjects.items() if r["record_key"] in {text, text.upper()})
        if keys:
            return "native_key", keys
        if ACCESSION.fullmatch(text.upper()):
            listing = []
            for key, record in subjects.items():
                if record["record_type"] != "protein":
                    continue
                revision = self.store.current(namespace, record["record_id"])
                if text.upper() in (revision["statement"]["as_published"].get("secondary_accessions") or []):
                    listing.append(key)
            if listing:
                return "secondary_accession", sorted(listing)
        return "not_found", []

    def _revision(self, namespace, record, *, release, as_of):
        if release is not None and record["provider"] in {"uniprot", "chembl"}:
            return self.store.at_release(namespace, record["record_id"], release, as_of_ms=as_of), "release"
        return self.store.current(namespace, record["record_id"], as_of_ms=as_of), "retrieval"

    # ------------------------------------------------------------------ LS09

    def entry_as_of(self, namespace: str, query: str, *, scopes: Iterable[str], release: str | None = None,
                    as_of: Any = None) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        at = as_of_ms(as_of)
        interpreted, keys = self._find(namespace, query)
        subjects = self._by_subject(namespace)
        base = {"contract": ENTRY_ANSWER_CONTRACT, "namespace": namespace, "query": query,
                "interpreted_as": interpreted, "release": release, "as_of": _date(at),
                "as_of_basis": ("the revision published in the named release (or the latest earlier one) and "
                                "retrieved by the date" if release else
                                "the latest revision retrieved on or before the date"),
                "boundary": NEVER_SENTENCE, "minimisation": MINIMISATION["decision"]}
        if not keys:
            return {**base, "status": "not_on_record", "entries": [], "resolution": [], "citations": [],
                    "unknowns": [{"kind": "not_on_record", "reason": f"{query!r} is not in the acquired, bounded "
                                  "selection; this is not a statement about the source"}]}
        resolution: list[dict[str, Any]] = []
        if interpreted == "secondary_accession":
            resolution.append({"from": query, "to": keys, "reason": "secondary accession listed by the entry",
                               "asserted_by": "uniprot"})
        entries, citations, unknowns = [], [], []
        frontier, seen = list(keys), set()
        hops = 0
        while frontier and hops <= MAX_HOPS:
            hops += 1
            nxt = []
            for key in frontier:
                if key in seen:
                    continue
                seen.add(key)
                record = subjects.get(key)
                if record is None:
                    unknowns.append({"kind": "successor_not_acquired", "subject_key": key,
                                     "reason": "the source names this successor; it is not in the acquired selection"})
                    continue
                revision, basis = self._revision(namespace, record, release=release, as_of=at)
                if revision is None:
                    versions, in_force = (self._versions(namespace, record["record_key"], release)
                                          if record["record_type"] == "protein" and release else ([], None))
                    if in_force:
                        entries.append({"subject_key": key, "record_type": "protein", "provider": "uniprot",
                                        "record_key": record["record_key"], "event": "version-only",
                                        "selected_by": "unisave-history", "version_in_force": in_force,
                                        "unisave_versions": versions,
                                        "note": "UniSave names the entry and sequence version in force at this "
                                                "release; the full entry at that version was not acquired"})
                        citations.append(in_force["citation"])
                        unknowns.append({"kind": "full_entry_not_acquired", "subject_key": key,
                                         "reason": f"entry version {in_force['entry_version']} is known from UniSave "
                                                   "only"})
                        continue
                    unknowns.append({"kind": "no_revision_at", "subject_key": key,
                                     "reason": "no revision of this record was published by the release or retrieved "
                                               "by the date"})
                    continue
                published = revision["statement"]["as_published"]
                follow = successors(record["record_type"], published) if revision["event"] == "obsoleted" else []
                if follow:
                    resolution.append({"from": key, "to": follow, "reason": revision["statement"]["effective"].get(
                        "date_basis") or "obsoleted by the source", "asserted_by": record["provider"],
                        "revision_id": revision["revision_id"], "release": revision["release"]})
                    nxt += follow
                entries.append(self._entry(namespace, record, revision, basis, subjects, release=release, at=at))
                citations.append(cite(record, revision))
            frontier = nxt
        current = [e for e in entries if e["event"] in {"published", "version-only"}]
        status = ("answered" if current else "obsolete_without_held_successor" if entries
                  else "not_published_by_release_or_date")
        answer = {**base, "status": status,
                  "entries": entries, "resolution": resolution, "citations": citations, "unknowns": unknowns}
        leaks = forbidden_keys(answer)
        if leaks:
            raise LifeSciError("boundary", f"answer carries excluded fields: {leaks}")
        return answer

    def _versions(self, namespace, accession, release):
        rows = []
        for record in self.store.records(namespace, record_type="entry_version"):
            if not record["record_key"].startswith(accession + ":"):
                continue
            revision = self.store.current(namespace, record["record_id"])
            published = revision["statement"]["as_published"]
            rows.append({**{k: published.get(k) for k in ("entry_version", "sequence_version", "database",
                                                          "first_release", "last_release", "first_release_date",
                                                          "last_release_date")},
                         "citation": cite(record, revision)})
        rows.sort(key=lambda r: r["entry_version"])
        in_force = None
        if release:
            wanted = release_order(release)
            in_force = next((r for r in rows if release_order(r["first_release"]) <= wanted
                             <= release_order(r["last_release"])), None)
        return rows, in_force

    def _entry(self, namespace, record, revision, basis, subjects, *, release, at) -> dict[str, Any]:
        published = revision["statement"]["as_published"]
        kind = record["record_type"]
        entry: dict[str, Any] = {
            "subject_key": record["subject_key"], "record_type": kind, "provider": record["provider"],
            "record_key": record["record_key"], "name": record["subject_name"], "event": revision["event"],
            "selected_by": basis, "revision_id": revision["revision_id"], "release": revision["release"],
            "as_published": published, "citation": cite(record, revision),
            "history": [{"revision_no": r["revision_no"], "revision_id": r["revision_id"], "release": r["release"],
                         "event": r["event"], "retrieved_on": r["retrieved_on"],
                         **({"entry_version": r["statement"]["as_published"].get("entry_version"),
                             "sequence_version": r["statement"]["as_published"].get("sequence_version")}
                            if kind == "protein" else {}),
                         **({"revision": r["statement"]["as_published"].get("revision")} if kind == "structure"
                            else {})}
                        for r in self.store.revisions(namespace, record["record_id"], as_of_ms=at)],
        }
        if kind == "protein":
            entry["label"] = "reviewed (Swiss-Prot)" if published.get("reviewed") else (
                "unreviewed (TrEMBL)" if published.get("reviewed") is False else published["entry_type"])
            versions, in_force = self._versions(namespace, record["record_key"], release)
            entry["unisave_versions"] = versions
            if release:
                entry["version_in_force"] = in_force or {"status": "not in the acquired UniSave history"}
                if in_force and published.get("entry_version") not in (None, in_force["entry_version"]):
                    entry["version_in_force"] = {**in_force, "note": "UniSave names this version for the release; "
                                                 "the held full entry is a different version, shown as acquired"}
        if kind == "structure":
            entry["structure_note"] = "experimental method and resolution as published; no model quality judgement"
        entry["cross_references"] = self._graph(namespace, record, published, subjects)
        entry["links"] = [{k: link[k] for k in ("target_kind", "target_id", "target_revision", "relation", "basis",
                                                "revision_id")}
                          for link in self.links.links(namespace, scopes={"operator"}, record_id=record["record_id"])]
        return entry

    def _graph(self, namespace, record, published, subjects) -> dict[str, Any]:
        outgoing = []
        matches = {frozenset((m["left_key"], m["right_key"])): m
                   for m in self.identity.matches(namespace, scopes={"operator"}, subject_key=record["subject_key"])}
        for ref in assertions(record["record_type"], published):
            key = ref["subject_key"]
            target = subjects.get(key) if key else None
            current = self.store.current(namespace, target["record_id"]) if target else None
            match = matches.get(frozenset((record["subject_key"], key))) if key else None
            outgoing.append({**ref, "asserted_by": record["provider"], "held": target is not None,
                             "target_revision_id": current["revision_id"] if current else None,
                             "identity": {"match_id": match["match_id"], "state": match["state"],
                                          "method": match["method"], "confidence": match["confidence"]}
                             if match else None})
        incoming = []
        for key, other in subjects.items():
            if key == record["subject_key"]:
                continue
            revision = self.store.current(namespace, other["record_id"])
            for ref in assertions(other["record_type"], revision["statement"]["as_published"]):
                if ref["subject_key"] == record["subject_key"]:
                    match = matches.get(frozenset((record["subject_key"], key)))
                    incoming.append({"subject_key": key, "record_type": other["record_type"],
                                     "asserted_by": other["provider"], "field": ref["field"],
                                     "revision_id": revision["revision_id"], "release": revision["release"],
                                     "identity": {"match_id": match["match_id"], "state": match["state"],
                                                  "method": match["method"]} if match else None})
        others = [{"match_id": m["match_id"], "with": m["right_key"] if m["left_key"] == record["subject_key"]
                   else m["left_key"], "state": m["state"], "method": m["method"], "confidence": m["confidence"]}
                  for pair, m in sorted(matches.items(), key=lambda i: i[1]["match_id"])
                  if not any(o["identity"] and o["identity"]["match_id"] == m["match_id"] for o in outgoing + incoming)]
        return {"outgoing": outgoing, "incoming": sorted(incoming, key=lambda i: i["subject_key"]),
                "other_identity_matches": others,
                "labelling": "each cross-reference names the source whose record asserts it; identity states are "
                             "reviewer decisions, never automatic merges"}

    # ------------------------------------------------------------------ LS10

    def target_activities(self, namespace: str, target: str, *, scopes: Iterable[str], release: str | None = None,
                          as_of: Any = None) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        at = as_of_ms(as_of)
        text = str(target or "").strip()
        targets = [r for r in self.store.records(namespace, record_type="target") if r["record_key"] == text.upper()]
        via = "chembl_id"
        if not targets and ACCESSION.fullmatch(text.upper()):
            via = "published target component accession"
            for record in self.store.records(namespace, record_type="target"):
                revision = self.store.current(namespace, record["record_id"], as_of_ms=at)
                if revision and text.upper() in {c.get("accession") for c in
                                                 revision["statement"]["as_published"].get("components") or []}:
                    targets.append(record)
        base = {"contract": ACTIVITY_ANSWER_CONTRACT, "namespace": namespace, "query": target, "resolved_via": via,
                "as_of": _date(at), "boundary": NEVER_SENTENCE,
                "values_policy": "values, relations and units exactly as ChEMBL published them (published and "
                                 "ChEMBL-standardised side by side); never converted, compared or aggregated across "
                                 "assays or assay types"}
        if not targets:
            return {**base, "status": "not_on_record", "release": release, "targets": [], "activities": [],
                    "removed_in_release": [], "citations": [],
                    "unknowns": [{"kind": "not_on_record", "reason": f"no ChEMBL target on record for {target!r}"}]}
        keys = {r["record_key"] for r in targets}
        activities = [r for r in self.store.records(namespace, record_type="activity")]
        releases = sorted({rev["release"] for r in activities for rev in self.store.revisions(
            namespace, r["record_id"], as_of_ms=at)
            if rev["statement"]["as_published"]["target_chembl_id"] in keys and rev["release"]}, key=release_order)
        chosen = release or (releases[-1] if releases else None)
        rows, removed, citations = [], [], []
        target_rows = []
        for record in targets:
            revision = self.store.at_release(namespace, record["record_id"], chosen, as_of_ms=at) if chosen else None
            if revision:
                target_rows.append({"target_chembl_id": record["record_key"],
                                    **{k: revision["statement"]["as_published"].get(k) for k in (
                                        "pref_name", "target_type", "organism", "tax_id", "components")},
                                    "citation": cite(record, revision)})
                citations.append(cite(record, revision))
        compounds, documents = {}, {}
        for record in activities:
            revision = self.store.at_release(namespace, record["record_id"], chosen, as_of_ms=at) if chosen else None
            if revision is None:
                continue
            published = revision["statement"]["as_published"]
            if published["target_chembl_id"] not in keys:
                continue
            if revision["event"] == "removed":
                removed.append({"activity_id": published["activity_id"], "citation": cite(record, revision),
                                "basis": revision["statement"]["effective"]["date_basis"]})
                citations.append(cite(record, revision))
                continue
            compound = self._related(namespace, "compound", published["molecule_chembl_id"], chosen, at, compounds)
            document = self._related(namespace, "document", published["document_chembl_id"], chosen, at, documents)
            rows.append({
                "activity_id": published["activity_id"],
                "compound": {"molecule_chembl_id": published["molecule_chembl_id"], **(
                    {k: compound["published"].get(k) for k in ("pref_name", "standard_inchikey")}
                    if compound else {"status": "compound record not on record"})},
                "assay": {"assay_chembl_id": published["assay_chembl_id"], "assay_type": published.get("assay_type"),
                          "description": published.get("assay_description")},
                "published": published.get("published"),
                "standard": {**(published.get("standard") or {}), "label": "standardised by ChEMBL as published"},
                "data_validity_comment": published.get("data_validity_comment"),
                "data_validity_description": published.get("data_validity_description"),
                "activity_comment": published.get("activity_comment"),
                "document": ({"document_chembl_id": published["document_chembl_id"],
                              **{k: document["published"].get(k) for k in ("doi", "pubmed_id", "title", "journal",
                                                                           "year")},
                              "citation": document["citation"]} if document else
                             {"document_chembl_id": published["document_chembl_id"],
                              "status": "document record not on record", **(published.get("document_citation") or {})}),
                "citation": cite(record, revision)})
            citations.append(cite(record, revision))
            for item in (compound, document):
                if item:
                    citations.append(item["citation"])
        rows.sort(key=lambda r: (str(r["assay"]["assay_type"]), str((r["published"] or {}).get("type")),
                                 r["compound"]["molecule_chembl_id"], r["activity_id"]))
        unique = {c["revision_id"]: c for c in citations}
        answer = {**base, "status": "answered" if rows else "no_published_activity_on_record", "release": chosen,
                  "releases_on_record": releases, "targets": target_rows, "activities": rows,
                  "activity_types": sorted({str((r["published"] or {}).get("type")) for r in rows}),
                  "aggregation": "none", "removed_in_release": removed, "citations": list(unique.values()),
                  "unknowns": [] if rows else [{"kind": "no_activity", "reason": f"no activity on record for the "
                                                f"target in release {chosen}"}]}
        leaks = forbidden_keys(answer)
        if leaks:
            raise LifeSciError("boundary", f"answer carries excluded fields: {leaks}")
        return answer

    def _related(self, namespace, record_type, key, release, at, cache):
        if key not in cache:
            record = self.store.find(namespace, record_type, key)
            revision = self.store.at_release(namespace, record["record_id"], release, as_of_ms=at) if record else None
            cache[key] = ({"published": revision["statement"]["as_published"], "citation": cite(record, revision)}
                          if revision else None)
        return cache[key]


# ------------------------------------------------------------------ export


def export_bundle(answer: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
    """A noesis-evidence-bundle-v1 citing every record revision (source, revision, release, as-of time) used."""
    from src.evidence_bundle.builder import EvidenceBundleBuilder

    builder = EvidenceBundleBuilder("answer", {"operation": "life-sciences", "contract": answer["contract"],
                                               "query": answer["query"], "release": answer.get("release")},
                                    created_at_ms=created_at_ms, as_of_ms=as_of_ms(answer.get("as_of")))
    refs = []
    for citation in answer.get("citations") or []:
        object_id = f"lifesci-revision:{citation['revision_id']}"
        builder.add_object("evidence", {
            "kind": "lifesci-record-revision",
            "locator": {"cited": True, "record_id": citation["record_id"], "revision_id": citation["revision_id"]},
            "source": citation["provider"], "record_type": citation["record_type"],
            "record_key": citation["record_key"], "revision_no": citation["revision_no"],
            "release": citation["release"], "event": citation["event"],
            "as_of": {"retrieved_at_ms": citation["retrieved_at_ms"], "retrieved_on": citation["retrieved_on"]},
            "licence": citation.get("licence"), "attribution": citation.get("attribution"), "url": citation["url"],
            "evidence_origin": citation.get("evidence_origin")}, object_id=object_id)
        refs.append(object_id)
        builder.add_external_reference(f"record:{citation['record_id']}:{citation['revision_no']}", citation["url"],
                                       required=False)
    for entry in answer.get("entries") or []:
        graph = entry.get("cross_references") or {}
        for item in list(graph.get("outgoing") or []) + list(graph.get("incoming") or []):
            identity = item.get("identity")
            if identity:
                object_id = f"lifesci-match:{identity['match_id']}"
                builder.add_object("evidence", {"kind": "lifesci-identity-match", "locator": {
                    "cited": True, "match_id": identity["match_id"]}, "state": identity["state"],
                    "method": identity["method"]}, object_id=object_id)
                refs.append(object_id)
    for unknown in answer.get("unknowns") or []:
        builder.add_omission(f"{unknown['kind']}: {unknown.get('reason') or ''}".strip())
    root = {k: answer.get(k) for k in ("contract", "query", "status", "release", "as_of", "boundary")}
    builder.add_object("answer", {"kind": "life-sciences", **root},
                       object_id=f"lifesci-answer:{digest([root, sorted(set(refs))])[:24]}",
                       references=sorted(set(refs)), root=True)
    return builder.build()


__all__ = ["SUBJECT_PREFIX", "LifeSciQueries", "as_of_ms", "assertions", "cite", "export_bundle", "successors"]
