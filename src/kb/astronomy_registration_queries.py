"""An object's registration, operators, catalogue status and re-entry record as of a date (#2224, SO10).

Extends the Astronomy as-of queries (:mod:`src.kb.astronomy_queries`, #2157)
with the registration records of :mod:`src.kb.astronomy_registration`. Given
an object (COSPAR designator, NORAD number, ``discos:<id>`` or a name) and an
``as_of`` (a date counts as its end, UTC) plus an optional
``acquired_by_ms``, the answer reads only what was published by the cutoff:

* **registration** - the UNOOSA index entry with its status revisions and
  retrieval dates, and the dated chain of registration-document entries
  (registration, transfers of supervision, changes of status, re-entry
  notices), each with symbol, locator, language and verbatim quotation. The
  registering State, supervising State and status are resolved from that chain
  as of the date, each citing the entry it comes from. Without a registration
  the answer says "none on record" (and, when the index says so, "no UN
  registration on record") with the sources consulted - never "unregistered".
* **operators** - operator/owner assertions as published, with the party
  identity decisions (accepted links, open candidates) used.
* **catalogue** - the GCAT/SATCAT ``orbital_object`` records' status and decay
  date (linked by exact identifier or an accepted candidate).
* **re-entry** - per publisher, every published prediction and the confirmed
  report, kept distinct. Noesis adds no prediction.

Answers carry pins, a generation and an answer hash, and export as evidence
bundles (:meth:`RegistrationQueries.export_bundle`) with the identity matches
used; account-restricted DISCOS values are cited, never exported.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.astronomy_records import READ_SCOPE, AstronomyError, authorize, cutoffs, digest, observed_day
from src.kb.astronomy_registration import (
    NO_UN_REGISTRATION,
    NOTICE,
    PROVIDERS,
    RESTRICTED_PROVIDERS,
    RegistrationCitations,
    RegistrationStore,
    identifier_keys,
)

CONTRACT = "noesis-astronomy-registration-answer-v1"
NONE_ON_RECORD = "none on record"
SEMANTICS = {
    "cutoff": "only records published by the as-of cutoff (a date counts as its end, UTC) and, when given, acquired "
    "by acquired_by_ms; a record without a stated date counts from its first observation",
    "none_on_record": "no registration is on record in the sources consulted; this is not 'unregistered'",
    "reentry": "predictions and the confirmed report are the publisher's; Noesis adds no prediction",
    "attribution": "registering State, supervising State and operators only as published",
}


def citation(view: Mapping[str, Any], revision: Mapping[str, Any] | None = None) -> dict[str, Any]:
    record = (revision or view)["record"]
    source = record["source"]
    out = {k: source[k] for k in ("provider", "source_record_id", "url", "locator", "attribution", "published_at",
                                  "retrieved_at_ms") if k in source}
    return {
        **out,
        "record_id": view["record_id"],
        "revision_id": (revision or view)["revision_id"],
        "revision": int((revision or view)["revision"]),
        "public_at_ms": int((revision or view)["public_at_ms"]),
        **({"restricted": True} if record.get("restricted") else {}),
    }


def _entry(view: Mapping[str, Any]) -> dict[str, Any]:
    record = view["record"]
    return {
        **{k: record[k] for k in ("entry_kind", "un_document", "document_date", "document_locator", "language",
                                  "quotation", "registering_state", "registrant_kind", "object_name", "cospar",
                                  "norad", "launch_date", "function", "status", "registered_orbit", "supervision",
                                  "status_change", "reentry", "instruments", "references")
           if k in record},
        "citation": citation(view),
    }


def _restricted(item: Any) -> bool:
    return isinstance(item, Mapping) and (
        bool((item.get("citation") or {}).get("restricted"))
        or (item.get("provider") in RESTRICTED_PROVIDERS and "predictions" in item)
    )


def _public(value: Any) -> Any:
    """The answer without account-restricted values: a restricted item is replaced by its citation only."""
    if isinstance(value, Mapping):
        return {k: _public(v) for k, v in value.items()}
    if isinstance(value, list):
        return [
            {"restricted": True, "citation": item.get("citation"), "provider": item.get("provider")}
            if _restricted(item) else _public(item)
            for item in value
        ]
    return value


class RegistrationQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        from src.kb.astronomy_store import AstronomyStore

        self.conn = conn
        self.store = RegistrationStore(conn, initialize=False, now=now)
        self.objects = AstronomyStore(conn, initialize=False, now=now)

    # -------------------------------------------------------------- plumbing

    def _collect(self, namespace, identifier, as_of, scopes, acquired_by_ms):
        from src.kb.astronomy_identity import orbital_group
        from src.kb.astronomy_registration_identity import object_key, registration_key

        authorize(namespace, set(scopes), READ_SCOPE)
        self.store.require_ready()
        cut = cutoffs(as_of, acquired_by_ms)
        cutoff = cut["published_by_ms"]
        keys = set(identifier_keys(identifier))
        catalogue_views = []
        if self.objects.ready():
            catalogue_views = self.objects.visible(
                namespace, kinds=["orbital_object"], public_cutoff_ms=cutoff, acquired_by_ms=acquired_by_ms
            )["records"]
        # One expansion through the identifiers the registration records state, then the catalogue's pairs.
        first = self.store.visible(namespace, keys=keys, public_cutoff_ms=cutoff, acquired_by_ms=acquired_by_ms)
        for view in first["records"] + first["pending"]:
            record = view["record"]
            keys |= {f"{k}:{record[k]}" for k in ("cospar", "norad") if record.get(k)}
        ids = [k.split(":", 1)[1] for k in keys if k.startswith(("cospar:", "norad:"))]
        group: set[str] = set()
        conflicts = []
        for value in ids:
            found = orbital_group([v["record"] for v in catalogue_views], value)
            group |= {k for k in found["keys"] if k.startswith(("cospar:", "norad:"))}
            conflicts += found["conflicts"]
        # Accepted name-only / conflict candidates connect a registration to a catalogue object.
        from src.kb.astronomy_identity import AstronomyIdentity

        accepted = AstronomyIdentity(self.conn, initialize=False).accepted_pairs(namespace)
        visible = self.store.visible(namespace, keys=keys | group, public_cutoff_ms=cutoff,
                                     acquired_by_ms=acquired_by_ms)
        by_reg_key = {registration_key(v["record"]): v for v in visible["records"]}
        by_obj_key = {object_key(v["record"]): v for v in catalogue_views}
        used_candidates = []
        for left, right in accepted:
            for reg, obj in ((left, right), (right, left)):
                if reg in by_reg_key and obj in by_obj_key:
                    other = by_obj_key[obj]["record"]
                    group |= {f"{k}:{other[k]}" for k in ("cospar", "norad") if other.get(k)}
                    used_candidates.append({"registration": reg, "object": obj, "state": "accepted"})
        catalogue = [v for v in catalogue_views
                     if {f"{k}:{v['record'][k]}" for k in ("cospar", "norad") if v["record"].get(k)} & (keys | group)
                     or object_key(v["record"]) in {c["object"] for c in used_candidates}]
        return cut, visible, catalogue, sorted(keys | group), conflicts, used_candidates

    def _answer(self, query, namespace, cut, body, views, unreadable, *, status):
        answer = {
            "contract": CONTRACT,
            "query": query,
            "namespace": namespace,
            "status": status,
            "knowledge_cutoff": dict(cut),
            **body,
            "unreadable": unreadable,
            "semantics": SEMANTICS,
            "notice": NOTICE,
            "pins": dict(sorted({v["record_id"]: v["revision_id"] for v in views}.items())),
            "generation": self.store.generation(namespace),
        }
        answer["answer_hash"] = digest({k: v for k, v in answer.items() if k != "generation"})
        return answer

    def _consulted(self, namespace, acquired_by_ms):
        ran = {p["provider"]: p for p in self.store.providers_consulted(namespace, acquired_by_ms=acquired_by_ms)}
        return [ran.get(p, {"provider": p, "documents": 0, "state": "not run in this namespace"})
                for p in sorted(PROVIDERS)]

    @staticmethod
    def _reentry(views) -> list[dict[str, Any]]:
        out = []
        for view in sorted(views, key=lambda v: (v["record"]["source"]["provider"], v["record_id"])):
            revisions = [r for r in view["revisions"] if r["change"] != "history"] or view["revisions"]
            reports = [
                {
                    **{k: r["record"][k] for k in ("report_kind", "issued_at", "reported_time", "reported_time_text",
                                                   "uncertainty", "location") if k in r["record"]},
                    "citation": citation(view, r),
                }
                for r in sorted(view["revisions"], key=lambda r: r["record"]["issued_at"])
            ]
            out.append({
                "provider": view["record"]["source"]["provider"],
                "predictions": [r for r in reports if r["report_kind"] == "prediction"],
                "confirmed": [r for r in reports if r["report_kind"] == "post_event"],
                "current": reports[-1] if reports else None,
                "revisions_superseded": len(revisions) - 1,
                "later": view["later"],
                "policy": "the publisher's reports as published; the confirmed report supersedes predictions "
                "without deleting them; Noesis adds no prediction",
            })
        return out

    # -------------------------------------------------------------- answers

    def object_registration_as_of(
        self,
        namespace: str,
        identifier: str,
        as_of: Any,
        *,
        scopes: Iterable[str],
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        from src.kb.astronomy_registration_identity import RegistrationIdentity

        cut, visible, catalogue, keys, conflicts, used = self._collect(
            namespace, identifier, as_of, scopes, acquired_by_ms
        )
        views = visible["records"]
        entries = [v for v in views if v["kind"] == "registration_entry"]
        index = [v for v in entries if v["record"]["entry_kind"] == "index_entry"]
        documents = sorted((v for v in entries if v["record"]["entry_kind"] != "index_entry"),
                           key=lambda v: (v["record"]["document_date"], v["record_id"]))
        registrations = [v for v in documents if v["record"]["entry_kind"] == "registration"]
        current: dict[str, Any] = {}
        if registrations:
            first = registrations[0]["record"]
            current["registering_state"] = {"value": first.get("registering_state"),
                                            "registrant_kind": first.get("registrant_kind"),
                                            "citation": citation(registrations[0])}
            current["supervising_state"] = dict(current["registering_state"])
        for view in documents:
            record = view["record"]
            if record["entry_kind"] == "transfer_of_supervision":
                current["supervising_state"] = {"value": record["supervision"]["to"],
                                                "effective_date": record["supervision"].get("effective_date"),
                                                "citation": citation(view)}
            elif record["entry_kind"] == "change_of_status":
                current["status"] = {"value": record["status_change"]["status"],
                                     "effective_date": record["status_change"].get("effective_date"),
                                     "citation": citation(view)}
            elif record["entry_kind"] == "re_entry_notice":
                current["reentry_notice"] = {**record.get("reentry", {}), "citation": citation(view)}
        index_view = []
        for view in index:
            record = view["record"]
            index_view.append({
                **{k: record[k] for k in ("cospar", "object_name", "registering_state", "un_registered",
                                          "un_document", "status", "function", "decay_date", "index_retrieved_on")
                   if k in record},
                "status_history": [
                    {"status": r["record"].get("status"), "index_retrieved_on": r["record"].get("index_retrieved_on"),
                     "revision_id": r["revision_id"]}
                    for r in view["revisions"] if r["change"] != "history"
                ],
                "citation": citation(view),
            })
            if "status" not in current and record.get("status"):
                current["index_status"] = {"value": record["status"],
                                           "index_retrieved_on": record.get("index_retrieved_on"),
                                           "citation": citation(view)}
        on_record = bool(registrations) or any(e.get("un_registered") for e in index_view)
        un_registration = (
            {"state": "on record", "documents": sorted({v["record"]["un_document"] for v in documents}
                                                       | {e["un_document"] for e in index_view if e.get("un_document")})}
            if on_record
            else {"state": NO_UN_REGISTRATION if index_view else NONE_ON_RECORD,
                  "note": "not a statement that the State failed to register; a national register may exist",
                  "sources_consulted": self._consulted(namespace, acquired_by_ms)}
        )
        identity = RegistrationIdentity(self.conn, initialize=False)
        operators = []
        for view in sorted((v for v in views if v["kind"] == "operator_assertion"),
                           key=lambda v: (v["record"].get("asserted_on") or "", v["record_id"])):
            record = view["record"]
            operators.append({
                **{k: record[k] for k in ("operator_name", "role", "asserted_on", "identifier", "un_document",
                                          "document_locator") if k in record},
                "identity": identity.party_identity(namespace, "operator", record["operator_name"]),
                "citation": citation(view),
            })
        parties = {}
        for key in ("registering_state", "supervising_state"):
            if current.get(key, {}).get("value"):
                kind = ("organisation" if current[key].get("registrant_kind") == "intergovernmental_organisation"
                        and key == "registering_state" else "state")
                parties[key] = identity.party_identity(namespace, kind, current[key]["value"])
        catalogue_body = [
            {"provider": v["record"]["source"]["provider"],
             **{k: v["record"][k] for k in ("name", "cospar", "norad", "status", "decay_date", "owner")
                if k in v["record"]},
             "record_id": v["record_id"], "revision_id": v["revision_id"]}
            for v in sorted(catalogue, key=lambda v: (v["record"]["source"]["provider"], v["record_id"]))
        ]
        record_ids = [v["record_id"] for v in views]
        links = identity.object_links(namespace, record_ids)
        body = {
            "identifier": identifier,
            "linked_identifiers": keys,
            "registration": {
                "un_registration": un_registration,
                "index": index_view,
                "documents": [_entry(v) for v in documents],
                "current": current,
                "state": "answered" if (documents or index_view) else NONE_ON_RECORD,
            },
            "operators": operators,
            "catalogue": catalogue_body,
            "reentry": self._reentry([v for v in views if v["kind"] == "reentry_report"]),
            "discos": [{"discos_id": v["record"]["discos_id"], "citation": citation(v)}
                       for v in views if v["kind"] in {"discos_object", "discos_citation"}],
            "identity_matches": {
                "object_links": links,
                "accepted_candidates": used,
                "catalogue_conflicts": conflicts,
                "parties": parties,
            },
            "citations": [
                {k: link[k] for k in ("target_kind", "citation_kind", "citation_value", "state", "target_id",
                                      "target_revision_id", "link_id", "stated") if k in link}
                for link in RegistrationCitations(self.conn, initialize=False).for_records(namespace, record_ids)
                if link["state"] != "reverted"
            ],
            "sources_consulted": self._consulted(namespace, acquired_by_ms),
        }
        if views:
            status = "answered"
        elif visible["pending"]:
            status = "not_yet_published"
            body["first_published_on"] = observed_day(min(p["first_public_at_ms"] for p in visible["pending"]))
        else:
            status = NONE_ON_RECORD.replace(" ", "_")
            body["statement"] = (f"{NONE_ON_RECORD}: no registration, operator or re-entry record for "
                                 f"{identifier} in the sources consulted")
        return self._answer("object_registration_as_of", namespace, cut, body, views + catalogue,
                            visible["unreadable"], status=status)

    def reentry_record(
        self,
        namespace: str,
        identifier: str,
        *,
        scopes: Iterable[str],
        as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        cut, visible, _, keys, _, _ = self._collect(
            namespace, identifier, as_of if as_of is not None else "9999-12-31", scopes, acquired_by_ms
        )
        views = [v for v in visible["records"] if v["kind"] == "reentry_report"]
        notices = [_entry(v) for v in visible["records"] if v["kind"] == "registration_entry"
                   and v["record"]["entry_kind"] == "re_entry_notice"]
        body = {"identifier": identifier, "linked_identifiers": keys, "reentry": self._reentry(views),
                "un_notices": notices, "sources_consulted": self._consulted(namespace, acquired_by_ms)}
        status = "answered" if views or notices else NONE_ON_RECORD.replace(" ", "_")
        if status != "answered":
            body["statement"] = f"{NONE_ON_RECORD}: no re-entry report for {identifier} in the sources consulted"
        return self._answer("reentry_record", namespace, cut, body, views, visible["unreadable"], status=status)

    # -------------------------------------------------------------- evidence bundles

    def export_bundle(self, namespace: str, answer: Mapping[str, Any], *, scopes: Iterable[str]) -> dict[str, Any]:
        """An evidence bundle citing every record revision and identity match the answer used.

        Restricted (ESA DISCOS) revisions are cited by provider, record and revision only; their values are omitted.
        """
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        authorize(namespace, set(scopes), READ_SCOPE)
        if answer.get("contract") != CONTRACT:
            raise AstronomyError("invalid_request", "export an answer of this provider")
        builder = EvidenceBundleBuilder(
            "receipt",
            {"operation": answer["query"], "namespace": namespace, "answer_hash": answer.get("answer_hash")},
            created_at_ms=0,
        )
        refs = []
        for record_id, revision_id in sorted((answer.get("pins") or {}).items()):
            if not record_id.startswith("astro-reg:"):
                refs.append(builder.add_object("evidence", {"kind": "astronomy-catalogue-record",
                                                            "record_id": record_id, "revision_id": revision_id},
                                               object_id=revision_id))
                continue
            revision = self.store.revision(namespace, revision_id)
            record = revision["record"]
            source = record["source"]
            payload: dict[str, Any] = {
                "kind": "astronomy-registration-record",
                "record_id": record_id,
                "revision_id": revision_id,
                "locator": {k: source[k] for k in ("provider", "source_record_id", "url", "locator") if k in source},
            }
            if record.get("restricted"):
                payload["restricted"] = True
                builder.add_omission(f"{revision_id}: account-restricted DISCOS values are not exported",
                                     object_id=revision_id)
            else:
                payload["statement"] = {k: v for k, v in record.items() if k not in {"source", "unknowns"}}
            refs.append(builder.add_object("evidence", payload, object_id=revision_id))
            if source.get("url") and not record.get("restricted"):
                builder.add_external_reference(f"source:{revision_id}", source["url"], required=False)
        for link in (answer.get("identity_matches") or {}).get("object_links", []):
            refs.append(builder.add_object("evidence", {"kind": "identity-match", **link}, object_id=link["link_id"]))
        for index, candidate in enumerate((answer.get("identity_matches") or {}).get("accepted_candidates", [])):
            refs.append(builder.add_object("evidence", {"kind": "identity-decision", **candidate},
                                           object_id=f"accepted-candidate:{index}"))
        builder.add_object("receipt", {"kind": "astronomy-registration-answer", **_public(answer)},
                           object_id=f"astronomy-registration:{answer.get('answer_hash')}", references=refs, root=True)
        if answer.get("statement"):
            builder.add_omission(answer["statement"])
        return {"bundle": builder.build(), "boundary": [SEMANTICS["reentry"], SEMANTICS["attribution"]]}
