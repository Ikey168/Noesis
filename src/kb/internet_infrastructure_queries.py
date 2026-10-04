"""An ASN's, prefix's or domain's records as of a date, each source separately and cited (#2743, II09).

Given a *declared* ASN, prefix or domain and an instant, :meth:`InfrastructureQueries.records_as_of` returns the
routing observations (RIPEstat), interconnection records (PeeringDB, the network's self-declaration), registrations
(RDAP), certificates (crt.sh) and the CT logs those certificates state, each as the revision or observation current at
that instant, side by side and cited with source, record revision and as-of time. Accepted identity matches (II07)
and links (II08) are listed next to the statements; nothing is merged into one record.

Refused in code, before any read: IP-keyed lookups (a bare address or a host route), person-keyed lookups (e-mail
addresses, registry contact handles, names), wildcard or pattern lookups, and any resource the source declarations do
not name. There is no reverse or "what else" query: no "other networks of this organisation", no "domains on this
address", no subdomain listing, no port or banner data, no verdict and no ranking.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.internet_infrastructure_sources import (
    EXCLUSIONS,
    NEVER_SENTENCE,
    InfrastructureError,
    classify_resource,
)
from src.kb.internet_infrastructure_records import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    InfrastructureRecordError,
    authorize,
    digest,
    iso,
    to_ms,
)
from src.kb.internet_infrastructure_store import InternetInfrastructureStore

SECTIONS = {"ripestat": "routing", "peeringdb": "interconnection", "rdap": "registration", "crtsh": "certificates",
            "ct-log-list": "ct_logs"}


def resolve_resource(value: Any) -> dict[str, str]:
    """A declared-resource reference, or :class:`InfrastructureRecordError` for IP-keyed, person-keyed or wildcard
    input (codes ``ip_lookup_refused``, ``person_identifier_refused``, ``wildcard_refused``)."""
    try:
        return classify_resource(value)
    except InfrastructureError as exc:
        raise InfrastructureRecordError(exc.code, exc.message) from None


class InfrastructureQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = InternetInfrastructureStore(conn, initialize=False, now=now)
        self.now = now or (lambda: int(time.time() * 1000))

    def _declared(self, namespace: str, resource: Mapping[str, str]) -> None:
        if not self.store.ready() or not self.store.is_declared(namespace, resource):
            raise InfrastructureRecordError("undeclared_resource", "only resources the source declarations name are "
                                                                   "answered; undeclared or reverse lookups go "
                                                                   "through the OSINT review gate first")

    def _statement(self, namespace: str, obj: Mapping[str, Any], at_ms: int) -> dict[str, Any] | None:
        if obj["shape"] == "observations":
            record = self.store.observation_as_of(namespace, obj["object_id"], at_ms)
            if record is None:
                return None
            return {"provider": obj["provider"], "object_id": obj["object_id"], "object_kind": obj["object_kind"],
                    "data_call": record["data_call"], "data_call_version": record["data_call_version"],
                    "observed_at": record["stated_time"], "time_basis": record["time_basis"],
                    "statement": record["content"], "label": obj["label"],
                    "citation": self.store.cite(namespace, obj, record)}
        record = self.store.revision_as_of(namespace, obj["object_id"], at_ms)
        if record is None:
            return None
        return {"provider": obj["provider"], "object_id": obj["object_id"], "object_kind": obj["object_kind"],
                "native_id": obj["native_id"], "state": record["state"], "revision_no": record["revision_no"],
                "source_revision": record["source_revision"], "valid_from": record["valid_from"],
                "statement": record["content"], "label": obj["label"],
                "citation": self.store.cite(namespace, obj, record)}

    def records_as_of(self, namespace: str, resource: Any, *, scopes: Iterable[str],
                      as_of: str | int | None = None) -> dict[str, Any]:
        """Every source's statement about one declared resource current at the date, separately and cited."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = resolve_resource(resource)
        self._declared(namespace, wanted)
        at_ms = to_ms(as_of) if as_of not in (None, "") else self.now()
        sections: dict[str, list[dict[str, Any]]] = {name: [] for name in SECTIONS.values()}
        not_yet, object_ids = [], []
        for obj in self.store.objects(namespace, resource=wanted):
            statement = self._statement(namespace, obj, at_ms)
            object_ids.append(obj["object_id"])
            if statement is None:
                not_yet.append({"provider": obj["provider"], "object_kind": obj["object_kind"],
                                "native_id": obj["native_id"], "note": "first recorded after the requested date"})
                continue
            sections[SECTIONS[obj["provider"]]].append(statement)
        logs = {log_id for s in sections["certificates"] for log_id in s["statement"].get("log_ids") or []}
        for obj in self.store.objects(namespace, provider="ct-log-list"):
            if obj["native_id"] in logs:
                statement = self._statement(namespace, obj, at_ms)
                if statement:
                    sections["ct_logs"].append(statement)
                    object_ids.append(obj["object_id"])
        from src.kb.internet_infrastructure_identity import InfrastructureIdentity
        from src.kb.internet_infrastructure_links import InfrastructureLinks

        matches = InfrastructureIdentity(self.conn, initialize=False).accepted_for(namespace, object_ids) \
            if self._has("ii_identity_assertions") else []
        links = InfrastructureLinks(self.conn, initialize=False).links_for(namespace, object_ids) \
            if self._has("ii_links") else []
        declared_by = next((d["providers"] for d in self.store.declared(namespace)
                            if (d["kind"], d["value"]) == (wanted["kind"], wanted["value"])), [])
        coverage = {provider: ("statement" if any(s["provider"] == provider for v in sections.values() for s in v)
                               else "none_on_record") for provider in declared_by}
        answer = {
            "contract": ANSWER_CONTRACT, "query": "records_as_of", "namespace": namespace, "resource": wanted,
            "as_of": iso(at_ms), "sections": sections, "not_yet_recorded": not_yet,
            "status": "answered" if any(sections.values()) else "none_on_record",
            "coverage": coverage, "identity_matches": matches, "links": links,
            "semantics": "each source's statement is shown separately as the revision or observation current at the "
                         "date; accepted identity matches relate records, they never merge them",
            "never": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS),
        }
        answer["answer_hash"] = digest({k: v for k, v in answer.items() if k != "answer_hash"})
        return answer

    def history(self, namespace: str, resource: Any, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Every revision and observation of every source about one declared resource, cited."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = resolve_resource(resource)
        self._declared(namespace, wanted)
        objects = [self.store.history(namespace, o["object_id"], scopes=scopes)
                   for o in self.store.objects(namespace, resource=wanted)]
        return {"contract": ANSWER_CONTRACT, "query": "history", "namespace": namespace, "resource": wanted,
                "objects": objects, "exclusions": list(EXCLUSIONS)}

    def declared_resources(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        return {"declared": self.store.declared(namespace) if self.store.ready() else [],
                "note": "only these resources are answered; there is no reverse, IP-keyed or person-keyed lookup"}

    def _has(self, table: str) -> bool:
        from src.kb.internet_infrastructure_records import table_exists

        return table_exists(self.conn, table)

    # ------------------------------------------------------------------ evidence bundle

    def export_bundle(self, namespace: str, answer: Mapping[str, Any], *, scopes: Iterable[str]) -> dict[str, Any]:
        """A ``noesis-evidence-bundle-v1`` citing every statement with source, record revision and as-of time."""
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        authorize(namespace, set(scopes), READ_SCOPE)
        if answer.get("contract") != ANSWER_CONTRACT or answer.get("query") != "records_as_of":
            raise InfrastructureRecordError("invalid_request", "export a records_as_of answer of this provider")
        builder = EvidenceBundleBuilder("receipt", {"operation": "records_as_of", "namespace": namespace,
                                                    "resource": dict(answer["resource"]), "as_of": answer["as_of"],
                                                    "answer_hash": answer.get("answer_hash")}, created_at_ms=0)
        refs = []
        for section, statements in sorted(answer["sections"].items()):
            for statement in statements:
                citation = statement["citation"]
                refs.append(builder.add_object("evidence", {
                    "kind": "internet-infrastructure-statement", "section": section,
                    "provider": citation["provider"], "source_id": citation["source_id"],
                    "record_id": citation["record_id"], "revision_no": citation["revision_no"],
                    "source_revision": citation["source_revision"], "revision_basis": citation["revision_basis"],
                    "as_of": citation["as_of"], "retrieved_at": citation["retrieved_at"],
                    "evidence_origin": citation["evidence_origin"], "live_verification": citation["live_verification"],
                    "redistribution": citation["redistribution"], "url": citation["url"],
                    "statement": statement["statement"]}, object_id=citation["record_id"]))
                if citation.get("url"):
                    builder.add_external_reference(f"source:{citation['record_id']}", citation["url"],
                                                   required=False)
        for match in answer.get("identity_matches") or []:
            refs.append(builder.add_object("evidence", {"kind": "identity-match", **match},
                                           object_id=match["assertion_id"]))
        for link in answer.get("links") or []:
            refs.append(builder.add_object("evidence", {"kind": "link", **link}, object_id=link["link_id"]))
        for missing in answer.get("not_yet_recorded") or []:
            builder.add_omission(f"{missing['provider']} {missing['object_kind']} {missing['native_id']}: first "
                                 f"recorded after {answer['as_of']}")
        for provider, state in sorted((answer.get("coverage") or {}).items()):
            if state == "none_on_record":
                builder.add_omission(f"{provider}: none on record for the resource as of {answer['as_of']}")
        builder.add_object("receipt", {"kind": "internet-infrastructure-answer", "resource": dict(answer["resource"]),
                                       "as_of": answer["as_of"], "status": answer["status"],
                                       "exclusions": list(EXCLUSIONS)},
                           object_id=f"internet-infrastructure:{answer.get('answer_hash')}", references=refs,
                           root=True)
        return {"bundle": builder.build(), "exclusions": list(EXCLUSIONS)}


__all__ = ["SECTIONS", "InfrastructureQueries", "resolve_resource"]
