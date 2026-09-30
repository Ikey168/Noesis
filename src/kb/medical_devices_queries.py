"""Medical-device answers: a device's regulatory history as of a date and adverse-event report counts with caveats
(#2654, MD09, MD10).

* :meth:`MedicalDevicesQueries.regulatory_history` - given a device (GUDID DI,
  EUDAMED Basic UDI-DI), a premarket number (K or P), a product code, a
  manufacturer (EUDAMED SRN or an MD07 manufacturer subject) or a recall number,
  the classification, clearances, approvals with their supplements, recalls and
  device identifiers (US) and the device registrations, certificates and actors
  (EU) in force at a date, jurisdictions shown separately. Every event cites the
  record revision it was read from (source, revision, as-of time); decisions
  after the date are listed as later events. EUDAMED modules that are not
  acquired and sources with no record for the subject are stated. Records are
  reached through published identifiers (DI, K or P number, product code, SRN)
  and accepted MD07 matches only.
* :meth:`MedicalDevicesQueries.adverse_event_counts` - MAUDE report counts per
  event type and period as openFDA published them for the subject's product
  codes, beside the reports on record in the acquired selection counted per
  event type and month. Counts are labelled as reports, never as rates or causal
  events; MAUDE's caveats, the query window and the source revisions come with
  every answer.

No safety-signal detection, no causality from adverse-event reports, no clinical
advice and no patient data beyond what regulators publish.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.medical_devices_sources import (
    EUDAMED_MODULES,
    K_NUMBER,
    MAUDE_CAVEATS,
    P_NUMBER,
    PRODUCT_CODE,
    RECALL_NUMBER,
    SRN,
    actor_key,
    eudamed_device_key,
    gudid_key,
    premarket_key,
    recall_key,
)
from src.kb.medical_devices_identity import MedicalDevicesIdentity
from src.kb.medical_devices_records import (
    EXCLUSIONS,
    READ_SCOPE,
    MedicalDevicesStore,
    authorize,
)

ANSWER_CONTRACT = "noesis-medical-device-answer-v1"
COUNT_NOTICE = ("counts are numbers of MAUDE reports as published (or on record in the acquired selection); they are "
                "not incidence, not rates and not evidence that a device caused an event")
US_KINDS = ("classification", "clearance", "approval", "supplement", "recall", "device-identifier")
EU_KINDS = ("eudamed-device", "certificate", "actor")


def resolve_subject(value: str) -> tuple[str, str]:
    """(subject kind, record key or code) for a DI, K/P number, product code, recall number, SRN or record key."""
    text = str(value or "").strip()
    if text.startswith("medical-devices:"):
        return "record", text
    upper = text.upper()
    if PRODUCT_CODE.fullmatch(upper):
        return "product-code", upper
    if RECALL_NUMBER.fullmatch(upper):
        return "record", recall_key(upper)
    if SRN.fullmatch(upper):
        return "record", actor_key(upper)
    if K_NUMBER.fullmatch(upper) or P_NUMBER.fullmatch(upper):
        return "record", premarket_key(upper)
    if re.fullmatch(r"\d{8,14}", text):
        return "record", gudid_key(text)
    return "record", eudamed_device_key(text)


def cite(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row["citation"])


class MedicalDevicesQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.store = MedicalDevicesStore(conn, initialize=False, now=now)
        self.identity = MedicalDevicesIdentity(conn, now=now, initialize=False)

    # ------------------------------------------------------------------ scope: which records a subject reaches

    def subject_records(self, namespace: str, subject: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """The records a subject reaches through published identifiers and accepted MD07 matches, with the path."""
        scopes = set(scopes)
        kind, key = resolve_subject(subject)
        rows = self.store.records(namespace, scopes=scopes)
        by_key: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_key.setdefault(row["record_key"], []).append(row)
        reached: dict[str, str] = {}
        codes: set[str] = set()
        numbers: set[str] = set()
        dis: set[str] = set()
        srns: set[str] = set()

        def reach(record_key: str, why: str) -> None:
            if record_key in by_key and record_key not in reached:
                reached[record_key] = why
                for row in by_key[record_key]:
                    record = row["record"]
                    if row["record_kind"] in {"clearance", "approval", "supplement", "device-identifier",
                                              "classification"}:
                        codes.update(record.get("product_codes") or [])
                    if row["record_kind"] in {"clearance", "approval", "supplement", "device-identifier"}:
                        numbers.update(record.get("premarket_numbers") or [])
                    if row["record_kind"] in {"device-identifier", "eudamed-device"}:
                        dis.update(record.get("udi_dis") or [])
                    if row["record_kind"] in {"eudamed-device", "actor"} and record.get("manufacturer_srn"):
                        srns.add(record["manufacturer_srn"])

        if kind == "product-code":
            codes.add(key)
        elif key.startswith(("medical-devices:fda:manufacturer:", "medical-devices:gudid:labeler:")):
            subjects = {s["record_key"]: s for s in self.identity.subjects(namespace, scopes=scopes)}
            for record_key in (subjects.get(key) or {}).get("records") or []:
                reach(record_key, f"named by the manufacturer subject {key}")
        else:
            reach(key, "the subject itself")
        # accepted identity matches of the subject and of the devices it reached (one hop, reviewed only)
        for record_key in [key, *list(reached)]:
            for match in self.identity.accepted(namespace, record_key, scopes=scopes):
                if match["record_key"].startswith("medical-devices:"):
                    subjects = {s["record_key"]: s for s in self.identity.subjects(namespace, scopes=scopes)}
                    other = subjects.get(match["record_key"])
                    targets = (other or {}).get("records") or [match["record_key"]]
                    for target in targets:
                        reach(target, f"accepted identity match {match['candidate_id']} ({match['method']})")
        # published identifiers: product codes, premarket numbers, DIs and SRNs group the other records
        for row in rows:
            record = row["record"]
            if row["record_key"] in reached:
                continue
            if row["record_kind"] == "classification" and set(record.get("product_codes") or []) & codes:
                reach(row["record_key"], "product code as published")
            elif row["record_kind"] in {"clearance", "approval", "supplement"} and (
                    set(record.get("premarket_numbers") or []) & numbers
                    or (kind == "product-code" and set(record.get("product_codes") or []) & codes)):
                reach(row["record_key"], "premarket number or product code as published")
            elif row["record_kind"] == "recall" and (set(record.get("premarket_numbers") or []) & numbers or
                                                     set(record.get("product_codes") or []) & codes and
                                                     (kind == "product-code" or not numbers)):
                reach(row["record_key"], "premarket number or product code named by the recall")
            elif row["record_kind"] == "device-identifier" and (
                    set(record.get("premarket_numbers") or []) & numbers
                    or (kind == "product-code" and set(record.get("product_codes") or []) & codes)):
                reach(row["record_key"], "premarket number or product code on the GUDID record")
            elif row["record_kind"] == "certificate" and srns and record.get("manufacturer_srn") in srns and (
                    set(record["fields"].get("basic_udi_dis") or []) & {r.rsplit(":", 1)[1] for r in reached
                                                                          if ":basic-udi-di:" in r}
                    or key.startswith("medical-devices:eudamed:actor:")):
                reach(row["record_key"], "certificate covering the device (Basic UDI-DI) or manufacturer (SRN)")
            elif record.get("manufacturer_srn") in srns and (
                    row["record_kind"] == "actor" or row["record_kind"] == "eudamed-device"
                    and key.startswith("medical-devices:eudamed:actor:")):
                reach(row["record_key"], "manufacturer SRN as published")
        # a second pass lets certificates follow devices reached through actors
        for row in rows:
            record = row["record"]
            if row["record_key"] not in reached and row["record_kind"] == "certificate" and \
                    set(record["fields"].get("basic_udi_dis") or []) & {r.rsplit(":", 1)[1] for r in reached
                                                                        if ":basic-udi-di:" in r}:
                reach(row["record_key"], "certificate covering the device (Basic UDI-DI)")
        return {"subject": subject, "subject_kind": kind, "subject_key": key, "reached": reached,
                "rows": [r for r in rows if r["record_key"] in reached], "product_codes": sorted(codes),
                "premarket_numbers": sorted(numbers), "udi_dis": sorted(dis), "srns": sorted(srns)}

    # ------------------------------------------------------------------ MD09 regulatory history as of a date

    def _event(self, row: Mapping[str, Any], path: str) -> dict[str, Any]:
        fields, kind = row["record"]["fields"], row["record_kind"]
        event = {"record_key": row["record_key"], "kind": kind, "source_id": row["source_id"],
                 "date": row["effective_on"], "title": row["record"]["title"], "reached_by": path,
                 "revision_no": row["revision_no"], "citation": cite(row)}
        if kind == "clearance":
            event.update(k_number=fields.get("k_number"), decision=fields.get("decision_description"),
                         decision_code=fields.get("decision_code"), decision_date=fields.get("decision_date"),
                         applicant=fields.get("applicant"))
        elif kind in {"approval", "supplement"}:
            event.update(pma_number=fields.get("pma_number"), supplement_number=fields.get("supplement_number"),
                         supplement_type=fields.get("supplement_type"), decision_code=fields.get("decision_code"),
                         decision_date=fields.get("decision_date"), applicant=fields.get("applicant"))
        elif kind == "recall":
            event.update(recall_number=fields.get("recall_number"),
                         status_as_published=fields.get("recall_status") or fields.get("status"),
                         recall_class_as_published=fields.get("recall_class"),
                         initiated=fields.get("event_date_initiated") or fields.get("recall_initiation_date"),
                         terminated=fields.get("event_date_terminated") or fields.get("termination_date"),
                         reason_as_published=fields.get("reason_for_recall"))
        elif kind == "classification":
            event.update(product_code=fields.get("product_code"), device_class=fields.get("device_class"),
                         regulation_number=fields.get("regulation_number"), device_name=fields.get("device_name"))
        elif kind == "device-identifier":
            event.update(primary_di=fields.get("primary_di"), package_dis=fields.get("package_dis"),
                         public_version_number=fields.get("public_version_number"),
                         commercial_distribution_status=fields.get("commercial_distribution_status"),
                         premarket_submissions=fields.get("premarket_submissions"))
        elif kind == "eudamed-device":
            event.update(basic_udi_di=fields.get("basic_udi_di"), risk_class=fields.get("risk_class"),
                         status_as_published=fields.get("status"), udi_dis=[u["udi_di"] for u in fields["udi_dis"]])
        elif kind == "certificate":
            event.update(certificate_number=fields.get("certificate_number"),
                         status_as_published=fields.get("status"), issue_date=fields.get("issue_date"),
                         expiry_date=fields.get("expiry_date"), notified_body=fields.get("notified_body_number"))
        elif kind == "actor":
            event.update(srn=fields.get("srn"), actor_type=fields.get("actor_type"), name=fields.get("name"),
                         country=fields.get("country"), status_as_published=fields.get("status"))
        return event

    def regulatory_history(self, namespace: str, subject: str, *, scopes: Iterable[str], as_of: str | None = None
                           ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        day = str(as_of)[:10] if as_of else None
        scope = self.subject_records(namespace, subject, scopes=scopes)
        answer: dict[str, Any] = {"contract": ANSWER_CONTRACT, "query": "regulatory_history", "namespace": namespace,
                                  "subject": subject, "subject_key": scope["subject_key"], "as_of": day,
                                  "exclusions": list(EXCLUSIONS)}
        jurisdictions: dict[str, dict[str, list[dict[str, Any]]]] = {
            "US": {k: [] for k in ("classification", "clearances", "approvals", "supplements", "recalls",
                                   "device_identifiers")},
            "EU": {k: [] for k in ("devices", "certificates", "actors")},
        }
        group = {"classification": ("US", "classification"), "clearance": ("US", "clearances"),
                 "approval": ("US", "approvals"), "supplement": ("US", "supplements"), "recall": ("US", "recalls"),
                 "device-identifier": ("US", "device_identifiers"), "eudamed-device": ("EU", "devices"),
                 "certificate": ("EU", "certificates"), "actor": ("EU", "actors")}
        later: list[dict[str, Any]] = []
        for row in scope["rows"]:
            if row["record_kind"] not in group:
                continue
            path = scope["reached"][row["record_key"]]
            chosen = row
            if day:
                state = self.store.as_of(namespace, row["record_key"], day, scopes=scopes,
                                         source_id=row["source_id"])["sources"][0]
                later += [{"record_key": row["record_key"], "kind": row["record_kind"], "date": r["date"],
                           "citation": r["citation"]} for r in state["later_revisions"]]
                if state["revision"] is None:
                    continue
                chosen = {**row, **state["revision"], "record_kind": row["record_kind"]}
            jurisdiction, bucket = group[row["record_kind"]]
            jurisdictions[jurisdiction][bucket].append(self._event(chosen, path))
        for bucket in jurisdictions.values():
            for events in bucket.values():
                events.sort(key=lambda e: (e["date"] or "", e["record_key"], e["source_id"]))
        # supplements listed under their approval, as published
        for approval in jurisdictions["US"]["approvals"]:
            approval["supplements"] = [s for s in jurisdictions["US"]["supplements"]
                                       if s["pma_number"] == approval["pma_number"]]
        found = any(events for bucket in jurisdictions.values() for events in bucket.values())
        acquired = {r["provider"] for r in self.store.records(namespace, scopes=scopes)}
        unavailable = [{"module": f"EUDAMED {name}", "reason": module["gap"]}
                       for name, module in EUDAMED_MODULES.items() if not module["acquired"]]
        unavailable += [{"provider": p, "reason": "no record of this provider is on record in the namespace"}
                        for p in ("openfda-device", "accessgudid", "eudamed") if p not in acquired]
        return {**answer, "status": "answered" if found else "none_on_record", "jurisdictions": jurisdictions,
                "later_events": sorted(later, key=lambda e: (e["date"] or "", e["record_key"])),
                "identity": self.identity.identity(namespace, scope["subject_key"], scopes=scopes),
                "grouped_by": {"product_codes": scope["product_codes"],
                               "premarket_numbers": scope["premarket_numbers"], "udi_dis": scope["udi_dis"],
                               "srns": scope["srns"]},
                "unavailable": unavailable,
                **({} if found else {"note": "no regulatory record of this subject is on record in the acquired "
                                             "coverage"})}

    # ------------------------------------------------------------------ MD10 adverse-event report counts

    def adverse_event_counts(self, namespace: str, subject: str, *, scopes: Iterable[str],
                             window_from: str | None = None, window_to: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        start, end = (str(window_from)[:10] if window_from else None), (str(window_to)[:10] if window_to else None)
        scope = self.subject_records(namespace, subject, scopes=scopes)
        codes = set(scope["product_codes"])
        answer: dict[str, Any] = {
            "contract": ANSWER_CONTRACT, "query": "adverse_event_counts", "namespace": namespace, "subject": subject,
            "subject_key": scope["subject_key"], "product_codes": sorted(codes),
            "query_window": {"from": start, "to": end}, "unit": "reports", "caveats": list(MAUDE_CAVEATS),
            "notice": COUNT_NOTICE, "exclusions": list(EXCLUSIONS),
        }

        def inside(first: str | None, last: str | None) -> bool:
            return (start is None or (last or "") >= start) and (end is None or (first or "9999") <= end)

        published = []
        for row in self.store.records(namespace, scopes=scopes, kinds=["adverse-event-count"]):
            fields = row["record"]["fields"]
            window = fields["window"]
            if fields["product_code"] not in codes or not inside(window["from"], window["to"]):
                continue
            published.append({"product_code": fields["product_code"], "period": window,
                              "reports_by_event_type_as_published": fields["counts_as_published"],
                              "reports_in_period_as_published": sum(c["reports"] for c in
                                                                    fields["counts_as_published"]),
                              "count_field": fields["count_field"], "search": fields["search"],
                              "within_query_window": (start is None or window["from"] >= start) and
                                                     (end is None or window["to"] <= end),
                              "citation": cite(row)})
        published.sort(key=lambda p: (p["product_code"], p["period"]["from"]))
        on_record: dict[tuple[str, str], dict[str, Any]] = {}
        reports = []
        for row in self.store.records(namespace, scopes=scopes, kinds=["adverse-event-report"]):
            fields = row["record"]["fields"]
            received = fields.get("date_received")
            if not set(row["record"].get("product_codes") or []) & codes or not inside(received, received):
                continue
            period = (received or "unknown")[:7]
            event_type = fields.get("event_type") or "not published"
            bucket = on_record.setdefault((period, event_type), {"period": period,
                                                                 "event_type_as_published": event_type,
                                                                 "reports": 0, "report_numbers": [],
                                                                 "citations": []})
            bucket["reports"] += 1
            bucket["report_numbers"].append(fields["report_number"])
            bucket["citations"].append(cite(row))
            reports.append(row["record_key"])
        found = bool(published or on_record)
        return {**answer, "status": "answered" if found else "none_on_record",
                "published_counts": published,
                "reports_on_record": {
                    "by_period_and_event_type": [on_record[k] for k in sorted(on_record)],
                    "total_reports": len(reports),
                    "basis": "MAUDE reports acquired in the declared selection, counted per month received and "
                             "event type as published; the acquired selection is not all of MAUDE"},
                "source_revisions": sorted({p["citation"]["revision_id"] for p in published} |
                                           {c["revision_id"] for b in on_record.values() for c in b["citations"]}),
                **({} if found else {"note": "no MAUDE report or report count of this subject's product codes is on "
                                             "record for the window"})}

    # ------------------------------------------------------------------ evidence bundles

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing the record revision behind it (source, revision and as-of time)."""
        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []

        def add(identifier: str, text: str, citation: Mapping[str, Any] | None) -> None:
            if not citation:
                return
            bibliography.setdefault(citation["revision_id"], {
                "id": citation["revision_id"],
                "text": f"{citation['provider']} {citation['record_key']} (source {citation['source_id']}, revision "
                        f"{citation['revision_no']}, native revision {citation.get('native_revision') or 'n/a'}, "
                        f"source date {citation.get('effective_on') or 'n/a'}, observed "
                        f"{citation.get('observed_at') or citation.get('observed_at_ms')}, "
                        f"{citation['evidence_origin']} evidence), {citation['locator']}"})
            assertions.append({"id": identifier, "text": text, "kind": "sourced",
                               "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                                 "id": citation["record_key"], "revision": citation["revision_id"],
                                                 "locator": {"section": citation["record_key"]}}],
                               "citations": [citation["revision_id"]]})

        for jurisdiction, buckets in (answer.get("jurisdictions") or {}).items():
            for bucket, events in buckets.items():
                for event in events:
                    add(f"{jurisdiction}-{bucket}-{event['record_key']}-{event['source_id']}",
                        f"{jurisdiction} {bucket.replace('_', ' ')}: {event['title']} (date {event['date']})",
                        event["citation"])
        for count in answer.get("published_counts") or []:
            add(f"count-{count['product_code']}-{count['period']['from']}",
                f"MAUDE reports for {count['product_code']} received {count['period']['from']} to "
                f"{count['period']['to']} by event type as published: {count['reports_by_event_type_as_published']} "
                f"(reports, not rates)", count["citation"])
        for bucket in (answer.get("reports_on_record") or {}).get("by_period_and_event_type") or []:
            for number, citation in zip(bucket["report_numbers"], bucket["citations"]):
                add(f"report-{number}", f"MAUDE report {number} ({bucket['event_type_as_published']}, received "
                    f"{bucket['period']})", citation)
        title = answer.get("subject_key") or answer.get("subject")
        return {"sections": [{"id": answer.get("query", "answer"), "title": f"{title} as of {answer.get('as_of')}",
                              "assertions": assertions}],
                "bibliography": list(bibliography.values()), "exclusions": list(EXCLUSIONS),
                "caveats": list(answer.get("caveats") or [])}
