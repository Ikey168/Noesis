"""Medical device records linked to other packs by citation, shared identifier or accepted match (#2654, MD08).

Links are derived from what a record publishes and are never inferred:

* **recall -> Product safety notice** (``products.safety-notices``): a notice
  whose notice number is the recall number or recall event id
  (``shared-identifier``), reusing the Product safety notice tables;
* **recall -> clearance / approval** in this pack: the K and P numbers the
  recall cites (``citation``);
* **MAUDE report -> GUDID device**: the UDI-DI the report publishes
  (``shared-identifier``); **certificate -> EUDAMED device** and **EUDAMED
  device -> actor**: the Basic UDI-DI and SRN they cite (``citation``);
* **combination product -> Medicines record** (``clinical.medicines``): a drug
  application number (NDA, BLA, ANDA) a GUDID record publishes among its
  premarket submissions, equal to a Drugs@FDA medicinal-product
  (``shared-identifier``);
* **device -> clinical-trial record** (``clinical.core``): a trial registration
  whose published text names the device's K number, P number, UDI-DI or Basic
  UDI-DI exactly (``citation``, identifier only - never a device name);
* **manufacturer -> Corporate Ownership entity**: an accepted MD07 match
  (``accepted-match``).

Every link points at the specific revision of both records. A target the pack
does not hold is reported as ``target-missing``; a pack that is not installed
as ``provider-missing`` - never dropped. No link carries a safety conclusion.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from src.kb.medical_devices_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    MedicalDeviceStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-medical-device-link-v1"
BASES = ("citation", "shared-identifier", "accepted-match")
STATUSES = ("linked", "target-missing", "provider-missing")
_DDL = """
CREATE TABLE IF NOT EXISTS medical_device_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_key TEXT NOT NULL, record_revision_id TEXT NOT NULL,
  target_pack TEXT NOT NULL, target_kind TEXT NOT NULL, target_key TEXT NOT NULL, target_revision TEXT,
  target_namespace TEXT NOT NULL, basis TEXT NOT NULL, status TEXT NOT NULL, evidence_json TEXT NOT NULL,
  linked_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""
_COLUMNS = ("link_id", "record_key", "record_revision_id", "target_pack", "target_kind", "target_key",
            "target_revision", "target_namespace", "basis", "status", "evidence_json", "linked_at_ms")


def _token(value: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])")


class MedicalDeviceLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = MedicalDeviceStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _put(self, namespace, view, target_pack, target_kind, target_key, target_revision, target_namespace, basis,
             status, evidence, out) -> None:
        link_id = "md-link:" + digest([namespace, view["revision_id"], target_pack, target_key, basis])[:24]
        self.conn.execute("DELETE FROM medical_device_links WHERE namespace=? AND link_id=?", [namespace, link_id])
        self.conn.execute("INSERT INTO medical_device_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, view["record_key"], view["revision_id"], target_pack, target_kind,
                           target_key, None if target_revision is None else str(target_revision), target_namespace,
                           basis, status, canonical(evidence), self.now()])
        out.append(link_id)

    # ------------------------------------------------------------------ link

    def link(self, namespace: str, *, scopes: Iterable[str], ownership_namespace: str | None = None,
             safety_namespace: str = "global") -> dict[str, Any]:
        """(Re)derive every link of the current revisions; idempotent. Missing providers are recorded, not dropped."""
        from src.kb.medical_devices_identity import MedicalDeviceIdentity

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not table_exists(self.conn, "medical_device_links"):
            self.conn.execute(_DDL)
        views = self.store.records(namespace, scopes=scopes, include_unpublished=False)
        by_key = {v["record_key"]: v for v in views}
        out: list[str] = []
        self.conn.execute("BEGIN")
        try:
            for view in views:
                record, fields = view["record"], view["record"]["fields"]
                kind = record["record_kind"]
                if kind == "recall":
                    self._safety(namespace, view, safety_namespace, out)
                    for scheme, prefix in (("k_numbers", "510k"), ("pma_numbers", "pma")):
                        for number in fields.get(scheme) or []:
                            key = f"medical-devices:fda:{prefix}:{number}"
                            target = by_key.get(key)
                            self._put(namespace, view, "clinical-evidence", "clinical.devices", key,
                                      target["revision_id"] if target else None, namespace, "citation",
                                      "linked" if target else "target-missing",
                                      {"cited": number, "field": f"fields.{scheme}"}, out)
                if kind == "adverse-event-report":
                    for device in fields.get("devices") or []:
                        if device.get("udi_di"):
                            key = f"medical-devices:gudid:di:{device['udi_di']}"
                            target = by_key.get(key)
                            self._put(namespace, view, "clinical-evidence", "clinical.devices", key,
                                      target["revision_id"] if target else None, namespace, "shared-identifier",
                                      "linked" if target else "target-missing",
                                      {"udi_di": device["udi_di"], "field": "fields.devices[].udi_di"}, out)
                if kind == "eudamed-certificate":
                    for basic in fields.get("basic_udi_dis") or []:
                        key = f"medical-devices:eudamed:basic-udi-di:{basic}"
                        target = by_key.get(key)
                        self._put(namespace, view, "clinical-evidence", "clinical.devices", key,
                                  target["revision_id"] if target else None, namespace, "citation",
                                  "linked" if target else "target-missing", {"basic_udi_di": basic}, out)
                if kind in {"eudamed-device", "eudamed-certificate"} and fields.get("manufacturer_srn"):
                    key = f"medical-devices:eudamed:actor:{fields['manufacturer_srn']}"
                    target = by_key.get(key)
                    self._put(namespace, view, "clinical-evidence", "clinical.devices", key,
                              target["revision_id"] if target else None, namespace, "citation",
                              "linked" if target else "target-missing", {"srn": fields["manufacturer_srn"]}, out)
                if kind == "device-identifier":
                    self._medicines(namespace, view, out)
                if kind in {"clearance", "approval", "device-identifier", "eudamed-device"}:
                    self._trials(namespace, view, out)
            if ownership_namespace:
                identity = MedicalDeviceIdentity(self.conn, initialize=False, now=self.now)
                for subject in identity.manufacturers(namespace, scopes=scopes):
                    for match in identity.accepted(namespace, subject["key"], scopes=scopes,
                                                   target_kind="ownership-entity"):
                        target = self._ownership_revision(match["namespace"], match["key"])
                        for source in subject["records"]:
                            view = by_key.get(source["record_key"])
                            if view is None:
                                continue
                            self._put(namespace, view, "corporate-ownership", "ownership.core", match["key"],
                                      target, match["namespace"], "accepted-match",
                                      "linked" if target else "target-missing",
                                      {"manufacturer_key": subject["key"], "candidate_id": match["candidate_id"],
                                       "decision_id": match["decision_id"], "method": match["method"]}, out)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        links = self.links(namespace, scopes=scopes)
        return {"derived": len(set(out)), "links": links,
                "summary": {s: sum(link["status"] == s for link in links) for s in STATUSES},
                "notice": "links record their basis and point at record revisions; no safety conclusion is drawn"}

    def _safety(self, namespace, view, safety_namespace, out) -> None:
        fields = view["record"]["fields"]
        wanted = [v for v in (fields.get("recall_number"), fields.get("event_id")) if v]
        if not table_exists(self.conn, "product_safety_notices"):
            self._put(namespace, view, "products", "products.safety-notices", fields["recall_number"], None,
                      safety_namespace, "shared-identifier", "provider-missing",
                      {"recall_number": fields["recall_number"], "reason": "the Products safety feature is not "
                                                                           "installed in this deployment"}, out)
            return
        rows = self.conn.execute(
            "SELECT n.notice_id, n.provider, n.notice_number, c.revision_id FROM product_safety_notices n LEFT JOIN "
            "product_safety_current c ON c.namespace=n.namespace AND c.notice_id=n.notice_id WHERE n.namespace=? AND "
            "n.notice_number IN (" + ",".join("?" * len(wanted)) + ") ORDER BY n.notice_id",
            [safety_namespace, *wanted]).fetchall()
        if not rows:
            self._put(namespace, view, "products", "products.safety-notices", fields["recall_number"], None,
                      safety_namespace, "shared-identifier", "target-missing",
                      {"recall_number": fields["recall_number"], "event_id": fields.get("event_id")}, out)
        for notice_id, provider, number, revision in rows:
            self._put(namespace, view, "products", "products.safety-notices", notice_id, revision, safety_namespace,
                      "shared-identifier", "linked", {"notice_number": number, "provider": provider}, out)

    def _medicines(self, namespace, view, out) -> None:
        numbers = [s["submission_number"] for s in view["record"]["fields"].get("premarket_submissions") or []
                   if re.fullmatch(r"(NDA|ANDA|BLA)\d{6}", str(s.get("submission_number") or ""))]
        if not numbers:
            return
        present = table_exists(self.conn, "clinical_records")
        for number in numbers:
            row = self.conn.execute(
                "SELECT record_id, revision FROM clinical_records WHERE namespace=? AND provider='openfda' AND "
                "native_id=? AND record_kind='medicinal-product'", [namespace, number]).fetchone() if present else None
            self._put(namespace, view, "clinical-evidence", "clinical.medicines", row[0] if row else number,
                      row[1] if row else None, namespace, "shared-identifier",
                      "linked" if row else "target-missing" if present else "provider-missing",
                      {"application_number": number, "field": "fields.premarket_submissions"}, out)

    def _trials(self, namespace, view, out) -> None:
        if not table_exists(self.conn, "clinical_records"):
            return  # reported once per answer through coverage; trials are optional context
        identifiers = [i["value"] for i in view["record"].get("identifiers") or []
                       if i["scheme"] in {"fda-510k", "fda-pma", "udi-di", "basic-udi-di"}]
        if not identifiers:
            return
        rows = self.conn.execute(
            "SELECT c.record_id, c.revision, r.content_json FROM clinical_records c JOIN clinical_record_revisions r "
            "ON r.record_id=c.record_id AND r.revision=c.revision WHERE c.namespace=? AND "
            "c.record_kind='registered-trial' ORDER BY c.record_id", [namespace]).fetchall()
        for record_id, revision, content in rows:
            text = json.dumps(json.loads(content), ensure_ascii=False)
            for value in identifiers:
                if _token(value).search(text):
                    self._put(namespace, view, "clinical-evidence", "clinical.core", record_id, revision, namespace,
                              "citation", "linked", {"identifier": value, "match": "exact identifier in the trial "
                                                                                   "registration text"}, out)

    def _ownership_revision(self, ownership_namespace: str, key: str) -> str | None:
        from src.kb.ownership_store import record_id

        if not table_exists(self.conn, "ownership_records"):
            return None
        row = self.conn.execute(
            "SELECT v.revision_id FROM ownership_records r JOIN ownership_record_revisions v ON "
            "v.namespace=r.namespace AND v.record_id=r.record_id AND v.revision=r.current_revision WHERE "
            "r.namespace=? AND r.record_id=?", [ownership_namespace, record_id(ownership_namespace, key)]).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, *, scopes: Iterable[str], record_key: str | None = None,
              status: str | None = None, target_pack: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "medical_device_links"):
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM medical_device_links WHERE namespace=? AND (? IS NULL OR "
            "record_key=?) AND (? IS NULL OR status=?) AND (? IS NULL OR target_pack=?) ORDER BY record_key, "
            "target_pack, target_key", [namespace, record_key, record_key, status, status, target_pack,
                                        target_pack]).fetchall()
        out = []
        for row in rows:
            item = dict(zip(_COLUMNS, row))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            out.append({"contract": CONTRACT, **item})
        return out


__all__ = ["BASES", "CONTRACT", "STATUSES", "MedicalDeviceLinks"]
