"""Life-science answers: an entry as of a release with its cross-reference graph, and compounds with published
activity against a target (#2652, LS09 #2696, LS10 #2701).

* :meth:`LifeSciQueries.entry` takes an accession (``X9EXA1``, ``9EXA``, ``CHEMBL...``, ``source:native``) and an
  optional release label and/or date. It returns the entry version in force then, every earlier and later version
  with its release, and - for an obsolete, merged, replaced or deleted accession - the successor chain resolved to
  the successors' versions in force, with the history shown. The cross-reference graph lists the entry's own
  published cross-references (labelled with the source that asserts them and resolved to acquired records where
  possible), the cross-references other sources publish that name this entry, the accepted and proposed identity
  matches and the cross-pack links. Every entry version used is cited.
* :meth:`LifeSciQueries.compounds_for_target` takes a ChEMBL target ID (or a UniProt accession, resolved through the
  target components ChEMBL publishes) and a ChEMBL release and lists every activity as ChEMBL published it for that
  release, grouped by assay type and activity type side by side: the published and standard type, relation, value
  and unit, the data-validity comment, the compound (with its InChIKey) and the source document. Values are never
  converted, normalised, averaged or aggregated across assays, and nothing is ranked.

A subject with no records is ``none_on_record``; that is never evidence of absence in the source. No biological or
clinical inference and no activity prediction is made. :meth:`LifeSciQueries.export_bundle` turns an answer into an
evidence bundle whose every item cites source, record revision and as-of time.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.lifesci_records import (
    ANSWER_CONTRACT,
    BUNDLE_CONTRACT,
    EXCLUSIONS,
    INACTIVE,
    READ_SCOPE,
    SOURCES,
    XREF_TARGETS,
    LifeSciError,
    authorize,
    detect_reference,
    digest,
    minimised,
    table_exists,
)
from src.kb.lifesci_store import LifeSciStore, as_of_day, iso_from_ms

# Databases other sources use to name a record of each (source, record type).
NAMED_BY = {}
for _database, _target in XREF_TARGETS.items():
    NAMED_BY.setdefault(_target, set()).add(_database)
NONE_NOTE = ("no record of this subject is held for the requested release or date; this is not evidence that the "
             "source has none")


class LifeSciQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.store = LifeSciStore(conn, initialize=False, now=now)

    # ------------------------------------------------------------------ helpers

    def _cite(self, namespace, revision, release, as_of) -> dict[str, Any]:
        return self.store.citation(namespace, revision, as_of=as_of, release=release)

    def _release_for(self, source: str, release: str | None) -> str | None:
        """A release label only applies to the source that uses it (UniProt 2099_01, ChEMBL CHEMBL_99)."""
        if not release:
            return None
        text = str(release)
        if text.upper().startswith("CHEMBL"):
            return text if source == "chembl" else None
        if re.fullmatch(r"\d{4}_\d{2}", text):
            return text if source == "uniprot" else None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text if source in {"ncbi-gene", "ncbi-taxonomy", "rcsb-pdb"} else None
        return text

    def _version(self, namespace, record_id, release, as_of) -> dict[str, Any] | None:
        head = self.store.record(namespace, record_id)
        return self.store.in_force(namespace, record_id, release=self._release_for(head["source"], release),
                                   as_of=as_of)

    def _history(self, namespace, record_id, release, as_of) -> list[dict[str, Any]]:
        out = []
        for revision in self.store.revisions(namespace, record_id):
            releases = [m["release_label"] for m in self.store.memberships(namespace, record_id)
                        if m["revision_id"] == revision["revision_id"]]
            out.append({"revision_id": revision["revision_id"], "version_marker": revision["marker"],
                        "version_basis": revision["basis"], "version_date": revision["version_date"],
                        "status": revision["status"], "successors": revision["successors"],
                        "first_release": revision["release_label"], "releases": releases,
                        "citation": self._cite(namespace, revision, release, as_of)})
        return out

    def _resolve(self, namespace: str, reference: str) -> tuple[list[str], list[tuple[str, str]]]:
        candidates = detect_reference(reference)
        if str(reference).startswith("lifesci-record:"):
            return self.store.resolve(namespace, reference), candidates
        found = []
        for source, native in candidates:
            found += self.store.find(namespace, source, native)
        return found, candidates

    # ------------------------------------------------------------------ LS09

    def entry(self, namespace: str, reference: str, *, scopes: Iterable[str], release: str | None = None,
              as_of: Any = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        day = as_of_day(as_of)
        base = {"contract": ANSWER_CONTRACT, "kind": "entry", "namespace": namespace, "reference": reference,
                "release": release, "as_of": day, "exclusions": list(EXCLUSIONS)}
        if not self.store.ready():
            return {**base, "status": "none_on_record", "note": NONE_NOTE}
        found, candidates = self._resolve(namespace, reference)
        if not candidates and not found:
            raise LifeSciError("invalid_reference", "name a UniProt accession, PDB ID, ChEMBL ID, NCBI Gene/Tax ID "
                                                    f"(source:id for numeric ids; sources {SOURCES}) or a record id")
        if not found:
            return {**base, "status": "none_on_record", "candidates": [f"{s}:{n}" for s, n in candidates],
                    "note": NONE_NOTE}
        if len(found) > 1:
            return {**base, "status": "ambiguous",
                    "records": [self.store.record(namespace, r) for r in found],
                    "note": "the identifier names records of several sources; ask with source:id"}
        (record_id,) = found
        head = self.store.record(namespace, record_id)
        version = self._version(namespace, record_id, release, day)
        if version is None:
            return {**base, "status": "none_on_record", "record": head,
                    "history": self._history(namespace, record_id, release, day),
                    "note": "the record was first published after the requested release or date"}
        statement = self.store.statement(namespace, version["revision_id"])
        entry = {"record": head, "version": {"revision_id": version["revision_id"], "marker": version["marker"],
                                             "basis": version["basis"], "status": version["status"],
                                             "release": version["release_label"]},
                 "label": statement["label"], "attributes": statement["attributes"],
                 "citations": statement["citations"], "licence": statement["licence"],
                 "citation": self._cite(namespace, version, release, day)}
        successors = self._successors(namespace, head, version, release, day)
        return minimised({
            **base, "status": "answered", "entry": entry,
            "history": self._history(namespace, record_id, release, day),
            "releases": self.store.releases(namespace, record_id),
            "resolved_to": successors,
            "xref_graph": self._graph(namespace, head, version, release, day, scopes),
            "note": "the version in force at the requested release or date; merged or obsolete accessions are "
                    "resolved to their successors with the history shown",
        })

    def _successors(self, namespace, head, version, release, day, depth: int = 0) -> list[dict[str, Any]]:
        if version["status"] not in INACTIVE or not version["successors"] or depth > 5:
            return []
        out = []
        for successor in version["successors"]:
            ids = self.store.find(namespace, head["source"], successor, head["record_type"])
            if not ids:
                out.append({"source": head["source"], "native_id": successor, "status": "not_acquired",
                            "via": {"from": head["native_id"], "kind": version["status"],
                                    "revision_id": version["revision_id"]}})
                continue
            succ_version = self._version(namespace, ids[0], release, day)
            if succ_version is None:
                out.append({"record_id": ids[0], "native_id": successor, "status": "none_on_record"})
                continue
            succ_head = self.store.record(namespace, ids[0])
            out.append({"record_id": ids[0], "native_id": successor, "status": succ_version["status"],
                        "version_marker": succ_version["marker"],
                        "via": {"from": head["native_id"], "kind": version["status"],
                                "revision_id": version["revision_id"]},
                        "citation": self._cite(namespace, succ_version, release, day),
                        "then": self._successors(namespace, succ_head, succ_version, release, day, depth + 1)})
        return out

    def _graph(self, namespace, head, version, release, day, scopes) -> dict[str, Any]:
        outgoing = []
        for xref in self.store.xrefs(namespace, version["revision_id"]):
            target = XREF_TARGETS.get(xref["database"])
            item = {**xref, "asserted_by": head["source"], "asserting_revision_id": version["revision_id"]}
            if target:
                ids = self.store.find(namespace, target[0], xref["id"], target[1])
                other = self._version(namespace, ids[0], release, day) if ids else None
                item["resolved"] = ({"record_id": ids[0], "citation": self._cite(namespace, other, release, day)}
                                    if other else None)
                item["resolution"] = "acquired" if other else "not_acquired"
            else:
                item["resolution"] = "external"
            outgoing.append(item)
        incoming = []
        databases = NAMED_BY.get((head["source"], head["record_type"]), set())
        for row in self.store.xrefs_naming(namespace, databases, head["native_id"]):
            if row["record_id"] == head["record_id"]:
                continue
            other_head = self.store.record(namespace, row["record_id"])
            in_force = self._version(namespace, row["record_id"], release, day)
            if in_force is None or in_force["revision_id"] != row["revision_id"]:
                continue  # only the version in force at the requested release asserts
            incoming.append({"record_id": row["record_id"], "source": other_head["source"],
                             "record_type": other_head["record_type"], "native_id": other_head["native_id"],
                             "database": row["database"], "relation": row["relation"],
                             "asserted_by": other_head["source"],
                             "citation": self._cite(namespace, in_force, release, day)})
        identity, links = [], []
        if table_exists(self.conn, "lifesci_matches"):
            from src.kb.lifesci_identity import LifeSciIdentity

            identity = [{k: m[k] for k in ("match_id", "left_id", "right_kind", "right_id", "method", "confidence",
                                            "state", "decision_id")}
                        for m in LifeSciIdentity(self.conn, initialize=False).matches(
                            namespace, scopes=scopes, record_id=head["record_id"])]
        if table_exists(self.conn, "lifesci_links"):
            from src.kb.lifesci_links import LifeSciLinks

            links = LifeSciLinks(self.conn, initialize=False).links(namespace, scopes=scopes,
                                                                     record_id=head["record_id"])
        return {"outgoing": outgoing, "incoming": incoming, "identity_matches": identity, "links": links,
                "note": "each cross-reference is labelled with the source that asserts it; identity matches are "
                        "reviewable and only accepted ones carry links"}

    # ------------------------------------------------------------------ LS10

    def compounds_for_target(self, namespace: str, target: str, *, scopes: Iterable[str],
                             release: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        base = {"contract": ANSWER_CONTRACT, "kind": "compounds_for_target", "namespace": namespace,
                "target": target, "release": release, "exclusions": list(EXCLUSIONS)}
        targets, basis = self._targets(namespace, target, release)
        if not targets:
            return {**base, "status": "none_on_record", "note": NONE_NOTE}
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        target_views = []
        for target_id, target_version in targets:
            target_views.append({"record_id": target_id, "native_id": self.store.record(namespace,
                                                                                          target_id)["native_id"],
                                 "citation": self._cite(namespace, target_version, release, None)})
            chembl_id = self.store.record(namespace, target_id)["native_id"]
            for row in self.store.xrefs_naming(namespace, {"ChEMBL"}, chembl_id):
                head = self.store.record(namespace, row["record_id"])
                if head["record_type"] != "activity":
                    continue
                version = self._version(namespace, row["record_id"], release, None)
                if version is None or version["revision_id"] != row["revision_id"]:
                    continue
                attributes = self.store.statement(namespace, version["revision_id"])["attributes"]
                item = {
                    "activity_id": attributes["activity_id"], "assay_chembl_id": attributes["assay_chembl_id"],
                    "assay_type": attributes["assay_type"], "assay_description": attributes["assay_description"],
                    "published": attributes["published"], "standard": attributes["standard"],
                    "data_validity_comment": attributes["data_validity_comment"],
                    "activity_comment": attributes["activity_comment"],
                    "compound": self._related(namespace, "compound", attributes["molecule_chembl_id"], release),
                    "document": self._related(namespace, "document", attributes["document_chembl_id"], release),
                    "citation": self._cite(namespace, version, release, None),
                }
                key = (str(attributes["assay_type"]), str(attributes["standard"]["type"]
                                                          or attributes["published"]["type"]))
                groups.setdefault(key, []).append(item)
        if not groups:
            return {**base, "status": "none_on_record", "targets": target_views, "target_basis": basis,
                    "note": "no activity against this target is held for the requested release; " + NONE_NOTE}
        return minimised({
            **base, "status": "answered", "targets": target_views, "target_basis": basis,
            "groups": [{"assay_type": assay_type, "activity_type": activity_type,
                        "activities": sorted(items, key=lambda i: str(i["activity_id"]))}
                       for (assay_type, activity_type), items in sorted(groups.items())],
            "note": "activities as ChEMBL published them for the release, grouped side by side by assay type and "
                    "activity type; values, relations and units are never converted or aggregated, validity "
                    "comments are shown, and nothing is ranked or predicted",
        })

    def _related(self, namespace, record_type, native_id, release) -> dict[str, Any]:
        ids = self.store.find(namespace, "chembl", native_id, record_type)
        version = self._version(namespace, ids[0], release, None) if ids else None
        if version is None:
            return {"native_id": native_id, "status": "not_acquired"}
        statement = self.store.statement(namespace, version["revision_id"])
        view = {"native_id": native_id, "record_id": ids[0], "label": statement["label"],
                "citation": self._cite(namespace, version, release, None)}
        if record_type == "compound":
            view["standard_inchi_key"] = dict(statement["attributes"].get("structures") or {}).get(
                "standard_inchi_key")
        else:
            view["cites"] = statement["citations"]
        return view

    def _targets(self, namespace, target, release) -> tuple[list[tuple[str, dict]], str]:
        candidates = detect_reference(target)
        out = []
        for source, native in candidates:
            if source == "chembl":
                for record_id in self.store.find(namespace, "chembl", native, "target"):
                    version = self._version(namespace, record_id, release, None)
                    if version is not None:
                        out.append((record_id, version))
                return out, "chembl-target-id"
            if source == "uniprot":
                for row in self.store.xrefs_naming(namespace, {"UniProt"}, native):
                    head = self.store.record(namespace, row["record_id"])
                    if head["record_type"] != "target":
                        continue
                    version = self._version(namespace, row["record_id"], release, None)
                    if version is not None and version["revision_id"] == row["revision_id"]:
                        out.append((row["record_id"], version))
                return out, "chembl-target-component (the UniProt accession as ChEMBL publishes it)"
        if not candidates:
            raise LifeSciError("invalid_reference", "name a ChEMBL target ID or a UniProt accession")
        return out, "unsupported"

    # ------------------------------------------------------------------ evidence bundle

    def export_bundle(self, answer: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
        """Every cited item of an answer with source, record revision and as-of time."""
        citations: dict[str, dict[str, Any]] = {}

        def walk(value: Any) -> None:
            if isinstance(value, Mapping):
                cite = value.get("citation")
                if isinstance(cite, Mapping) and cite.get("revision_id"):
                    citations[cite["revision_id"]] = dict(cite)
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(answer)
        items = [{"source": c["source"], "record_type": c["record_type"], "native_id": c["native_id"],
                  "record_revision": c["revision_id"], "version_marker": c["version_marker"],
                  "release": c["release"], "released_on": c["released_on"], "retrieved_at": c["retrieved_at"],
                  "as_of": c["as_of"], "url": c["url"], "licence": c["licence"],
                  "evidence_origin": c["evidence_origin"]} for _, c in sorted(citations.items())]
        created = iso_from_ms(created_at_ms if created_at_ms is not None else self.store.now())
        return {"contract": BUNDLE_CONTRACT, "bundle_id": "lifesci-bundle:" + digest([answer.get("kind"),
                                                                                       items])[:24],
                "answer_kind": answer.get("kind"), "status": answer.get("status"), "created_at": created,
                "items": items, "exclusions": list(EXCLUSIONS),
                "note": "each item cites its source, record revision and as-of time; redistribution follows each "
                        "source's licence"}


__all__ = ["LifeSciQueries"]
