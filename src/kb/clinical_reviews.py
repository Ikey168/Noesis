"""PROSPERO review registrations as review-protocol records linked to the systematic-review store (H06).

PROSPERO offers no documented machine interface, so a registration enters only
as a record export the user supplies; it is stored as a ``review-registration``
record (provenance ``user-supplied-export``) and, when the user names one of
their systematic-review protocols, the protocol is linked to it through
:meth:`~src.kb.systematic_reviews.SystematicReviewStore.link_registration`.
A registration is a protocol, never review findings.
"""

from __future__ import annotations

from src.ingestion.clinical_providers import parse_prospero_export
from src.kb.clinical_records import ClinicalRecordStore, _require_write, record_id
from src.kb.systematic_reviews import SystematicReviewStore


def import_prospero_registration(conn, namespace, export, *, principal_id, scopes, protocol_id=None, now=None):
    _require_write(namespace, scopes)
    store = ClinicalRecordStore(conn, now=now)
    registration = parse_prospero_export(export, supplied_by=principal_id)
    link = None
    if protocol_id:
        reviews = SystematicReviewStore(conn, now=now)
        link = reviews.link_registration(namespace, protocol_id, "prospero", registration["identifier"],
                                         principal_id=principal_id, scopes=scopes,
                                         record={"source_url": registration["source_url"]})
        registration = {**registration, "protocol_link": {"store": "systematic_reviews", "protocol_id": protocol_id,
                                                          "protocol_revision": link["protocol_revision"]}}
    body = {k: v for k, v in registration.items() if k not in {"unknowns"}}
    # Write access was checked above; a user-supplied export is not a network acquisition.
    summary = store._ingest(namespace, "prospero", [body],
                            observation_id=f"prospero-import:{principal_id}:{registration['identifier']}",
                            observed_at_ms=store.now(),
                            evidence={"supplied_by": principal_id, "kind": "user-supplied-export"},
                            execution="user-supplied")
    rid = record_id(namespace, {**body, "record_kind": "review-registration"})
    return {"record_id": rid, "registration": body, "protocol_link": link,
            "created": rid in summary["created"], "revised": rid in summary["revised"],
            "note": "PROSPERO is not acquired automatically; this registration was supplied by the user"}
