"""A device's regulatory history as of a date and adverse-event report counts with caveats (#2654, MD09, MD10).

:func:`regulatory_history` takes a device (K or P number, primary DI, UDI-DI,
Basic UDI-DI or record key) or an FDA product code and returns, as of a date,
the clearances, approvals, supplements, recalls and EU certificates on record
**with each event citing the record revision it comes from** - the revision
the source had published by that date where one is on record. Jurisdictions
are shown separately (US: FDA; EU: EUDAMED) and never merged. Records are
reached from the subject by published identifiers and citations, and across
registries only through **accepted** MD07 identity matches; the reach of
every record is stated. EUDAMED modules that are not public, providers with no
record and features not selected are stated as gaps; a subject with nothing on
record says so.

:func:`adverse_event_counts` returns MAUDE report counts per event type and
period **as published** (the openFDA ``count`` tally for a product code and
received-date window) beside the reports on record in this namespace, always
labelled as **reports** - never rates, incidence or causal events - with FDA's
caveats, the query window and the source revisions cited. Nothing here detects
safety signals, compares devices or gives clinical advice.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.medical_devices_sources import (
    COUNT_SEMANTICS,
    EUDAMED_MODULES,
    FEATURES,
    MAUDE_CAVEATS,
    REVIEW_BOUNDARY,
    key_510k,
    key_basic_udi,
    key_di,
    key_pma,
    key_product_code,
    key_recall,
)
from src.kb.medical_devices_records import (
    EXCLUSIONS,
    READ_SCOPE,
    MedicalDeviceError,
    MedicalDeviceStore,
    authorize,
    forbidden_keys,
    selected_features,
)

HISTORY_CONTRACT = "noesis-medical-device-regulatory-history-v1"
COUNTS_CONTRACT = "noesis-medical-device-report-counts-v1"
BUNDLE_CONTRACT = "noesis-medical-device-evidence-bundle-v1"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KINDS_BY_JURISDICTION = {"US": ("classification", "clearance", "approval", "approval-supplement", "recall",
                                 "device-identifier"),
                          "EU": ("eudamed-device", "eudamed-certificate", "eudamed-actor")}


def _day(value: Any, field: str) -> str:
    text = str(value or "")
    if not _DATE.fullmatch(text):
        raise MedicalDeviceError("invalid_request", f"{field} is a YYYY-MM-DD date")
    return text


def _guard(answer: dict[str, Any]) -> dict[str, Any]:
    found = forbidden_keys(answer)
    if found:
        raise MedicalDeviceError("assessment_forbidden", "answers carry no signal, causality, rate or advice",
                                 paths=found)
    return answer


def resolve_subject(subject: str) -> dict[str, Any]:
    """The record key or product code a subject names, by its published identifier form (never a name)."""
    text = str(subject or "").strip()
    if text.startswith("medical-devices:"):
        return {"kind": "record", "key": text}
    if re.fullmatch(r"K\d{6}", text):
        return {"kind": "record", "key": key_510k(text)}
    if re.fullmatch(r"[PN]\d{5,6}", text):
        return {"kind": "record", "key": key_pma(text)}
    if re.fullmatch(r"[A-Z]{3}", text):
        return {"kind": "product-code", "key": key_product_code(text), "product_code": text}
    if re.fullmatch(r"Z-\d{4}-\d{4}", text):
        return {"kind": "record", "key": key_recall(text)}
    if re.fullmatch(r"\d{8,14}", text):
        return {"kind": "udi-di", "key": key_di(text), "udi_di": text}
    if re.fullmatch(r"[0-9A-Z+$/.\-]{6,40}", text):
        return {"kind": "record", "key": key_basic_udi(text), "basic_udi_di": text}
    raise MedicalDeviceError("invalid_request", "name a device by K or P number, DI, Basic UDI-DI or record key, "
                                                "or a three-letter FDA product code (names are not resolved)")


class MedicalDeviceQueries:
    def __init__(self, conn: Any) -> None:
        from src.kb.medical_devices_identity import MedicalDeviceIdentity

        self.conn = conn
        self.store = MedicalDeviceStore(conn, initialize=False)
        self.identity = MedicalDeviceIdentity(conn, initialize=False)

    # ------------------------------------------------------------- reach

    def reach(self, namespace: str, subject: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """The records a subject reaches and how: subject, accepted match, citation or shared product code."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        resolved = resolve_subject(subject)
        views = {v["record_key"]: v for v in self.store.records(namespace, scopes=scopes)}
        # A record the publisher no longer answers is still reached through what it last published.
        published = dict(views)
        for key, view in views.items():
            if view["publication_state"] != "published":
                earlier = [v for v in self.store.history(namespace, key, scopes=scopes)
                           if v["publication_state"] == "published"]
                if earlier:
                    published[key] = earlier[-1]
        reach: dict[str, dict[str, Any]] = {}

        def add(key, basis, via=None, match=None):
            if key in views and key not in reach:
                reach[key] = {"record_key": key, "basis": basis, "via": via, "match": match}

        seeds = [resolved["key"]]
        if resolved["kind"] == "udi-di":
            seeds += [k for k, v in views.items() if v["record_kind"] == "eudamed-device" and any(
                u.get("udi_di") == resolved["udi_di"] for u in v["record"]["fields"].get("udi_dis") or [])]
        for seed in seeds:
            add(seed, "subject")
        matches = []
        for seed in [s for s in seeds if s in views]:
            for key in self.identity.device_cluster(namespace, seed, scopes=scopes)[1:]:
                match = next((m for m in self.identity.accepted(namespace, key, scopes=scopes, target_kind="device")),
                             None)
                add(key, "accepted-match", seed, match)
                if match:
                    matches.append(match)
        codes = set()
        if resolved["kind"] == "product-code":
            codes.add(resolved["product_code"])
        for key in list(reach):
            record = published[key]["record"]
            fields = record["fields"]
            for link in record.get("links_as_published") or []:
                if link["scheme"] == "fda-510k":
                    add(key_510k(link["value"]), "cited-by-record", key)
                elif link["scheme"] == "fda-pma":
                    add(key_pma(link["value"]), "cited-by-record", key)
                elif link["scheme"] == "fda-product-code":
                    codes.add(link["value"])
            if record["record_kind"] == "eudamed-device" and fields.get("manufacturer_srn"):
                add(f"medical-devices:eudamed:actor:{fields['manufacturer_srn']}", "cited-by-record", key)
            if record["record_kind"] == "device-identifier":
                codes |= {c["code"] for c in fields.get("product_codes") or [] if c.get("code")}
        pmas = {k for k in reach if published[k]["record"]["record_kind"] == "approval"}
        numbers = {published[k]["record"]["fields"].get("k_number") for k in reach} | \
            {published[k]["record"]["fields"].get("pma_number") for k in reach}
        basics = {published[k]["record"]["fields"].get("basic_udi_di") for k in reach
                  if published[k]["record"]["record_kind"] == "eudamed-device"}
        for key, view in sorted(published.items()):
            fields, kind = view["record"]["fields"], view["record"]["record_kind"]
            if kind == "approval-supplement" and fields.get("approval_key") in pmas:
                add(key, "supplement-of", fields["approval_key"])
            elif kind == "recall" and (set(fields.get("k_numbers") or []) | set(fields.get("pma_numbers") or [])) \
                    & numbers or kind == "eudamed-certificate" and set(fields.get("basic_udi_dis") or []) & basics:
                add(key, "cites-device", None)
            elif kind == "classification" and fields.get("product_code") in codes:
                add(key, "product-code", fields["product_code"])
            elif resolved["kind"] == "product-code" and fields.get("product_code") == resolved["product_code"] \
                    and kind in {"clearance", "approval", "approval-supplement", "recall"}:
                add(key, "product-code", resolved["product_code"])
        return {"subject": subject, "resolved": resolved, "records": [reach[k] for k in sorted(reach)],
                "product_codes": sorted(codes), "matches_used": matches, "views": views}

    def _cite(self, namespace, key, scopes, date=None) -> tuple[dict[str, Any] | None, str]:
        """The revision published by ``date`` (else the current one, said so) and its citation."""
        chosen = self.store.as_of(namespace, key, scopes=scopes, published_by=date) if date else None
        basis = "published by the date"
        if chosen is None:
            history = self.store.history(namespace, key, scopes=scopes)
            current = [v for v in history if v["change"] != "older-observation"]
            chosen = current[-1] if current else None
            basis = "current revision (no revision on record published by the date)" if date else "current revision"
        return chosen, basis

    # ------------------------------------------------------------- MD09

    def regulatory_history(self, namespace: str, subject: str, as_of: str, *, scopes: Iterable[str]
                           ) -> dict[str, Any]:
        scopes = set(scopes)
        day = _day(as_of, "as_of")
        reached = self.reach(namespace, subject, scopes=scopes)
        views = reached["views"]
        us = {"authority": "FDA", "classification": [], "clearances": [], "approvals": [], "supplements": [],
              "recalls": [], "device_identifiers": []}
        eu = {"authority": "European Commission (EUDAMED)", "devices": [], "certificates": [], "actors": []}
        later, removed = [], []
        for entry in reached["records"]:
            key = entry["record_key"]
            current = views[key]
            if current["publication_state"] != "published":
                removed.append({"record_key": key, "record_kind": current["record_kind"],
                                "note": "the publisher no longer answers this declared record (a removal is a "
                                        "revision; earlier revisions stay cited)", "citation": current["citation"],
                                "reach": entry})
            view, basis = self._cite(namespace, key, scopes, day)
            if view is None or view["publication_state"] != "published":
                history = [v for v in self.store.history(namespace, key, scopes=scopes)
                           if v["publication_state"] == "published"]
                view, basis = (history[-1], "last published revision before its removal") if history else (None, "")
            if view is None:
                continue
            fields, kind = view["record"]["fields"], view["record"]["record_kind"]
            event_date = (fields.get("decision_date") or fields.get("event_date_initiated")
                          or fields.get("issue_date") or fields.get("public_version_date"))
            cite = {**view["citation"], "revision_basis": basis}
            if kind in {"clearance", "approval", "approval-supplement", "recall", "eudamed-certificate"} and (
                    event_date is None or event_date > day):
                later.append({"record_key": key, "record_kind": kind, "event_date": event_date,
                              "note": "after the date" if event_date else "event date not published"})
                continue
            item = {"record_key": key, "reach": {k: v for k, v in entry.items() if k != "record_key"},
                    "citation": cite, "unknowns": view["record"].get("unknowns") or []}
            if kind == "classification":
                us["classification"].append({**item, "product_code": fields["product_code"],
                                             "device_class": fields.get("device_class"),
                                             "regulation_number": fields.get("regulation_number"),
                                             "device_name": fields.get("device_name")})
            elif kind == "clearance":
                us["clearances"].append({**item, "k_number": fields["k_number"], "decision_date": event_date,
                                         "decision_code": fields.get("decision_code"),
                                         "decision_description": fields.get("decision_description"),
                                         "device_name": fields.get("device_name"),
                                         "applicant_as_published": (fields.get("applicant") or {}).get(
                                             "name_as_published"), "product_code": fields.get("product_code")})
            elif kind in {"approval", "approval-supplement"}:
                target = us["approvals"] if kind == "approval" else us["supplements"]
                target.append({**item, "pma_number": fields["pma_number"],
                               "supplement_number": fields.get("supplement_number"), "decision_date": event_date,
                               "decision_code": fields.get("decision_code"),
                               "supplement_type": fields.get("supplement_type"),
                               "supplement_reason": fields.get("supplement_reason"),
                               "trade_name": fields.get("trade_name"), "product_code": fields.get("product_code")})
            elif kind == "recall":
                terminated = fields.get("event_date_terminated")
                us["recalls"].append({**item, "recall_number": fields["recall_number"],
                                      "recall_class": fields.get("recall_class"),
                                      "status_as_published": fields.get("status_as_published"),
                                      "initiated": event_date, "terminated": terminated if terminated and
                                      terminated <= day else None,
                                      "reason_for_recall": fields.get("reason_for_recall"),
                                      "recalling_firm_as_published": (fields.get("recalling_firm") or {}).get(
                                          "name_as_published")})
            elif kind == "device-identifier":
                us["device_identifiers"].append({**item, "primary_di": fields["primary_di"],
                                                 "brand_name": fields.get("brand_name"),
                                                 "version_model_number": fields.get("version_model_number"),
                                                 "public_version_number": fields.get("public_version_number"),
                                                 "package_dis": [p["di"] for p in fields.get("package_dis") or []]})
            elif kind == "eudamed-device":
                eu["devices"].append({**item, "basic_udi_di": fields["basic_udi_di"],
                                      "risk_class": fields.get("risk_class"), "legislation": fields.get("legislation"),
                                      "udi_dis": fields.get("udi_dis") or [],
                                      "manufacturer_srn": fields.get("manufacturer_srn")})
            elif kind == "eudamed-certificate":
                eu["certificates"].append({**item, "certificate_number": fields["certificate_number"],
                                           "notified_body_number": fields["notified_body_number"],
                                           "status_as_published": fields.get("status_as_published"),
                                           "issue_date": fields.get("issue_date"),
                                           "expiry_date": fields.get("expiry_date"),
                                           "status_change_reason": fields.get("status_change_reason")})
            elif kind == "eudamed-actor":
                eu["actors"].append({**item, "srn": fields["srn"], "name_as_published": fields.get("name"),
                                     "role": fields.get("role"), "country": fields.get("country")})
        for group in (us, eu):
            for items in group.values():
                if isinstance(items, list):
                    items.sort(key=lambda i: (i.get("decision_date") or i.get("initiated") or i.get("issue_date")
                                              or "", i["record_key"]))
        on_record = bool(reached["records"])
        providers_reached = {views[e["record_key"]]["provider"] for e in reached["records"]}
        selected = selected_features(self.conn)
        answer = {
            "contract": HISTORY_CONTRACT, "subject": subject, "resolved": reached["resolved"], "as_of": day,
            "on_record": on_record, "jurisdictions": {"US": us, "EU": eu},
            "identity_matches_used": reached["matches_used"], "later_events": later, "removed_by_source": removed,
            "gaps": {
                "eudamed_modules": {m: v for m, v in EUDAMED_MODULES.items() if v["status"] != "available"},
                "providers_without_records": sorted(set(FEATURES) - providers_reached),
                "features_not_selected": sorted(f for f in FEATURES.values() if f not in selected),
                "note": "a provider without records here is unknown, not a clean record",
            },
            "boundary": REVIEW_BOUNDARY, "exclusions": list(EXCLUSIONS),
        }
        if not on_record:
            answer["message"] = (f"No regulatory record of {subject!r} is on record in this namespace; this says "
                                 "nothing about the device.")
        return _guard(answer)

    # ------------------------------------------------------------- MD10

    def adverse_event_counts(self, namespace: str, subject: str, received_from: str, received_to: str, *,
                             scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        start, end = _day(received_from, "received_from"), _day(received_to, "received_to")
        if start > end:
            raise MedicalDeviceError("invalid_request", "the query window ends before it starts")
        reached = self.reach(namespace, subject, scopes=scopes)
        codes = set(reached["product_codes"])
        dis = set()
        if reached["resolved"]["kind"] == "udi-di":
            dis.add(reached["resolved"]["udi_di"])
        for entry in reached["records"]:
            fields = reached["views"][entry["record_key"]]["record"]["fields"]
            if fields.get("primary_di"):
                dis.add(fields["primary_di"])
        device_scoped = reached["resolved"]["kind"] != "product-code"
        published, outside = [], []
        for view in self.store.records(namespace, scopes=scopes, kinds=["report-count"]):
            fields = view["record"]["fields"]
            if fields["product_code"] not in codes:
                continue
            window = fields["window"]
            first = f"{window['received_from'][:4]}-{window['received_from'][4:6]}-{window['received_from'][6:]}"
            last = f"{window['received_to'][:4]}-{window['received_to'][4:6]}-{window['received_to'][6:]}"
            entry = {"product_code": fields["product_code"], "window": {"received_from": first, "received_to": last},
                     "counts_as_published": [{"event_type": c["term"], "reports": c["count"]}
                                             for c in fields.get("counts_as_published") or []],
                     "publication_state": view["publication_state"], "citation": view["citation"],
                     "label": "reports received by FDA for the product code in this window, as published"}
            if view["publication_state"] != "published":
                entry["note"] = ("the publisher answered the count query with no matches (NOT_FOUND); no count is "
                                 "inferred from that")
            if start <= first and last <= end:
                published.append(entry)
            elif not (last < start or first > end):
                outside.append({**entry, "note": "the published window only partly overlaps the query window; "
                                                 "it is not split or pro-rated"})
        reports = []
        for view in self.store.records(namespace, scopes=scopes, kinds=["adverse-event-report"],
                                       include_unpublished=False):
            fields = view["record"]["fields"]
            received = fields.get("date_received")
            if not received or not start <= received <= end:
                continue
            devices = fields.get("devices") or []
            named_codes = {d.get("product_code") for d in devices}
            named_dis = {d.get("udi_di") for d in devices if d.get("udi_di")}
            if not (named_codes & codes or named_dis & dis):
                continue
            reports.append({"report_number": fields["report_number"], "event_type": fields.get("event_type"),
                            "date_received": received, "period": received[:7],
                            "names_device_di": bool(named_dis & dis), "citation": view["citation"]})
        tally: dict[str, dict[str, int]] = {}
        for report in reports:
            tally.setdefault(report["event_type"] or "not published", {}).setdefault(report["period"], 0)
            tally[report["event_type"] or "not published"][report["period"]] += 1
        answer = {
            "contract": COUNTS_CONTRACT, "subject": subject, "resolved": reached["resolved"],
            "query_window": {"received_from": start, "received_to": end, "date_basis": "date FDA received the report"},
            "product_codes": sorted(codes),
            "published_counts": published, "published_counts_outside_window": outside,
            "reports_on_record": {
                "by_event_type_and_period": [{"event_type": t, "period": p, "reports": n}
                                             for t, periods in sorted(tally.items()) for p, n in sorted(periods.items())],
                "reports": sorted(reports, key=lambda r: (r["date_received"], r["report_number"])),
                "total_reports": len(reports),
                "label": "reports acquired into this namespace within the bounded selection; not every report FDA "
                         "holds",
                "device_scope": ("reports naming the product codes or the UDI-DI of the device" if device_scoped
                                 else "reports naming the product code"),
            },
            "count_semantics": COUNT_SEMANTICS, "caveats": list(MAUDE_CAVEATS),
            "sources": sorted({c["citation"]["revision_id"] for c in published + outside} |
                              {r["citation"]["revision_id"] for r in reports}),
            "on_record": bool(published or reports or outside),
            "boundary": REVIEW_BOUNDARY, "exclusions": list(EXCLUSIONS),
        }
        if not answer["on_record"]:
            answer["message"] = (f"No MAUDE report or published count for {subject!r} is on record in this window; "
                                 "that is not a statement that no event occurred.")
        return _guard(answer)

    # ------------------------------------------------------------- export

    def evidence_bundle(self, namespace: str, subject: str, as_of: str, *, scopes: Iterable[str],
                        received_from: str | None = None, received_to: str | None = None) -> dict[str, Any]:
        """Every item of the history (and counts, with a window) cited with source, record revision and as-of time."""
        history = self.regulatory_history(namespace, subject, as_of, scopes=scopes)
        counts = (self.adverse_event_counts(namespace, subject, received_from, received_to, scopes=scopes)
                  if received_from and received_to else None)
        items = []

        def add(kind: str, jurisdiction: str, statement: Mapping[str, Any], citation: Mapping[str, Any]) -> None:
            items.append({
                "item_id": f"{kind}:{citation['record_key']}:{citation['revision_id']}", "kind": kind,
                "jurisdiction": jurisdiction, "statement": dict(statement),
                "source": {"provider": citation["provider"], "authority": citation["authority"],
                           "locator": citation["locator"], "attribution": citation.get("attribution"),
                           "license": citation.get("license"), "disclaimer": citation.get("disclaimer"),
                           "evidence_origin": citation["evidence_origin"]},
                "record_revision": {"record_key": citation["record_key"], "revision_id": citation["revision_id"],
                                    "revision_no": citation["revision_no"],
                                    "native_revision": citation["native_revision"]},
                "as_of": {"source_as_of": citation["as_of"], "observed_at_ms": citation["observed_at_ms"]},
            })

        for jurisdiction, groups in history["jurisdictions"].items():
            for group, entries in groups.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    statement = {k: v for k, v in entry.items() if k not in {"citation", "reach", "unknowns"}}
                    add(group, jurisdiction, statement, entry["citation"])
        if counts:
            for entry in counts["published_counts"]:
                add("published-report-counts", "US", {"product_code": entry["product_code"],
                                                      "window": entry["window"],
                                                      "counts_as_published": entry["counts_as_published"],
                                                      "label": entry["label"]}, entry["citation"])
        missing = [i["item_id"] for i in items if not (i["source"]["locator"] and i["record_revision"]["revision_id"]
                                                        and i["as_of"]["observed_at_ms"])]
        if missing:
            raise MedicalDeviceError("uncited_item", "every exported item cites source, revision and as-of time",
                                     items=missing)
        return {"contract": BUNDLE_CONTRACT, "subject": subject, "as_of": history["as_of"],
                "query_window": counts["query_window"] if counts else None, "items": items,
                "caveats": list(MAUDE_CAVEATS) if counts else [], "count_semantics": COUNT_SEMANTICS if counts else None,
                "gaps": history["gaps"], "later_events": history["later_events"],
                "removed_by_source": history["removed_by_source"],
                "identity_matches_used": history["identity_matches_used"], "on_record": history["on_record"],
                "boundary": REVIEW_BOUNDARY, "exclusions": list(EXCLUSIONS),
                "minimisation": "no personal fields are stored; narratives are not exported"}


def regulatory_history(conn: Any, namespace: str, subject: str, as_of: str, *, scopes: Iterable[str]
                       ) -> dict[str, Any]:
    return MedicalDeviceQueries(conn).regulatory_history(namespace, subject, as_of, scopes=scopes)


def adverse_event_counts(conn: Any, namespace: str, subject: str, received_from: str, received_to: str, *,
                         scopes: Iterable[str]) -> dict[str, Any]:
    return MedicalDeviceQueries(conn).adverse_event_counts(namespace, subject, received_from, received_to,
                                                           scopes=scopes)


__all__ = ["BUNDLE_CONTRACT", "COUNTS_CONTRACT", "HISTORY_CONTRACT", "MedicalDeviceQueries", "adverse_event_counts",
           "regulatory_history", "resolve_subject"]
