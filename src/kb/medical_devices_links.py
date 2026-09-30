"""Medical-device records linked to other packs by citation, shared identifier or accepted match (#2654, MD08).

Every link points at a specific record revision on both sides and records its
basis:

* ``product-safety`` - a device recall and a Product safety notice
  (:mod:`src.kb.product_safety`) whose number equals the recall number
  (``shared-identifier``) or whose published text quotes it (``citation``, with
  the JSON pointer of the quoting field);
* ``medicines`` - a device record whose published text cites a Drugs@FDA
  application number (a combination product) and the Medicines record
  (``medicinal-product``, :mod:`src.kb.clinical_medicines`) with that number
  (``citation``);
* ``trial`` - a registered trial (:mod:`src.kb.clinical_records`) whose title,
  summary or interventions name a device's K or P number or UDI-DI, and that
  device record (``citation``);
* ``ownership`` - a manufacturer's records and the Corporate Ownership record of
  an accepted MD07 ``name+country`` match (``accepted-match``);
* ``products`` - a GUDID device and the Products identity of an accepted MD07
  ``gtin`` match (``accepted-match``).

Absent providers are reported as ``provider_unavailable`` and cited targets that
are not on record as ``missing_targets``; neither is dropped. A link states a
citation or a reviewed match only: it is never a safety conclusion.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.medical_devices_identity import MedicalDevicesIdentity
from src.kb.medical_devices_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    MedicalDevicesError,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-medical-device-link-v1"
LINK_KINDS = ("product-safety", "medicines", "trial", "ownership", "products")
NOTICE = ("a link states a citation, a shared identifier or a reviewed identity match; it is not a safety "
          "conclusion, a causal reading or clinical advice")
APPLICATION = re.compile(r"\b(NDA|ANDA|BLA)\s?(\d{6})\b")
_DDL = """
CREATE TABLE IF NOT EXISTS medical_device_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, link_kind TEXT NOT NULL, record_key TEXT NOT NULL,
  record_revision_id TEXT NOT NULL, target_key TEXT NOT NULL, target_namespace TEXT NOT NULL,
  target_revision TEXT, basis_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""
_COLUMNS = ("link_id", "link_kind", "record_key", "record_revision_id", "target_key", "target_namespace",
            "target_revision", "basis_json", "created_by", "created_at_ms")
# Published text fields that may quote another register's identifier.
TEXT_FIELDS = ("ao_statement", "statement_or_summary", "device_description", "product_description",
               "reason_for_recall", "supplement_reason", "code_info")


def _link_view(row) -> dict[str, Any]:
    view = dict(zip(_COLUMNS, row))
    view["basis"] = json.loads(view.pop("basis_json"))
    return {"contract": CONTRACT, **view, "notice": NOTICE}


def token(value: str) -> re.Pattern[str]:
    return re.compile(r"(?<![0-9A-Za-z])" + re.escape(value) + r"(?![0-9A-Za-z])")


def _quotes(value: Any, pattern: re.Pattern[str], path: str = "") -> list[str]:
    """JSON pointers of every string in ``value`` that contains ``pattern``."""
    if isinstance(value, str):
        return [path or "/"] if pattern.search(value) else []
    if isinstance(value, Mapping):
        return [p for k, v in value.items() for p in _quotes(v, pattern, f"{path}/{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _quotes(v, pattern, f"{path}/{i}")]
    return []


class MedicalDevicesLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.identity = MedicalDevicesIdentity(conn, now=now, initialize=initialize)
        self.store = self.identity.store
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "medical_device_links")

    def _insert(self, namespace: str, kind: str, row: Mapping[str, Any], target_key: str, target_namespace: str,
                target_revision: str | None, basis: Mapping[str, Any], principal_id: str
                ) -> tuple[dict[str, Any], bool]:
        link_id = "md-link:" + digest([namespace, kind, row["revision_id"], target_key, target_namespace,
                                       target_revision])[:24]
        created = not self.conn.execute("SELECT 1 FROM medical_device_links WHERE namespace=? AND link_id=?",
                                        [namespace, link_id]).fetchone()
        if created:
            self.conn.execute("INSERT INTO medical_device_links VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, link_id, kind, row["record_key"], row["revision_id"], target_key,
                               target_namespace, target_revision, canonical(basis), principal_id, self.now()])
        found = self.conn.execute("SELECT " + ", ".join(_COLUMNS) + " FROM medical_device_links WHERE namespace=? "
                                  "AND link_id=?", [namespace, link_id]).fetchone()
        return _link_view(found), created

    @staticmethod
    def _result(kind: str, links: list, created: int, missing: list, **extra: Any) -> dict[str, Any]:
        return {"kind": kind, "status": "linked" if links else "none_linked", "links": links, "created": created,
                "missing_targets": missing, **extra}

    @staticmethod
    def _unavailable(kind: str, provider: str, reason: str) -> dict[str, Any]:
        return {"kind": kind, "status": "provider_unavailable", "provider": provider, "reason": reason, "links": [],
                "created": 0, "missing_targets": []}

    # ------------------------------------------------------------------ product safety (recall numbers)

    def link_product_safety(self, namespace: str, products_namespace: str | None, *, principal_id: str,
                            scopes: set[str]) -> dict[str, Any]:
        kind = "product-safety"
        if not products_namespace or not table_exists(self.conn, "product_safety_notices"):
            return self._unavailable(kind, "products.safety", "no Product safety notices are on record")
        from src.kb.product_safety import READ_SCOPE as PRODUCTS_READ
        from src.kb.product_safety import authorize as products_authorize

        try:
            products_authorize(products_namespace, scopes, PRODUCTS_READ)
        except Exception as exc:  # noqa: BLE001 - a missing grant degrades to an unavailable provider
            return self._unavailable(kind, "products.safety", getattr(exc, "code", "unauthorized"))
        notices = self.conn.execute(
            "SELECT n.notice_id, n.provider, n.notice_number, c.revision_id, v.statement_json FROM "
            "product_safety_notices n JOIN product_safety_current c ON c.namespace=n.namespace AND "
            "c.notice_id=n.notice_id JOIN product_safety_revisions v ON v.namespace=c.namespace AND "
            "v.revision_id=c.revision_id WHERE n.namespace=? ORDER BY n.notice_id", [products_namespace]).fetchall()
        links, created, missing = [], 0, []
        for row in self.store.records(namespace, scopes=scopes, kinds=["recall"]):
            number = row["record"]["fields"]["recall_number"]
            pattern, found = token(number), False
            for notice_id, provider, notice_number, revision_id, statement in notices:
                if notice_number == number:
                    basis = {"basis": "shared-identifier", "recall_number": number, "notice_provider": provider}
                else:
                    pointers = _quotes(json.loads(statement), pattern)
                    if not pointers:
                        continue
                    basis = {"basis": "citation", "recall_number": number, "notice_provider": provider,
                             "notice_number": notice_number, "quoted_at": pointers}
                found = True
                view, new = self._insert(namespace, kind, row, notice_id, products_namespace, revision_id, basis,
                                         principal_id)
                links.append(view)
                created += new
            if not found:
                missing.append({"record_key": row["record_key"], "recall_number": number,
                                "reason": "no Product safety notice on record names this recall number"})
        return self._result(kind, links, created, missing)

    # ------------------------------------------------------------------ medicines and trials (clinical store)

    def _clinical(self, namespace: str, scopes: set[str], kinds: list[str]) -> list[dict[str, Any]] | None:
        if not table_exists(self.conn, "clinical_records"):
            return None
        from src.kb.clinical_records import ClinicalRecordStore

        return ClinicalRecordStore(self.conn, initialize=False).find(namespace, scopes=scopes, kinds=kinds)

    def link_medicines(self, namespace: str, clinical_namespace: str | None, *, principal_id: str,
                       scopes: set[str]) -> dict[str, Any]:
        kind = "medicines"
        try:
            products = self._clinical(clinical_namespace or namespace, scopes, ["medicinal-product"])
        except Exception as exc:  # noqa: BLE001 - access to the Medicines records is optional
            return self._unavailable(kind, "clinical.medicines", getattr(exc, "code", "unavailable"))
        if products is None:
            return self._unavailable(kind, "clinical.medicines", "no clinical record store")
        by_number = {p["native_id"]: p for p in products if p["provider"] == "openfda"}
        links, created, missing = [], 0, []
        for row in self.store.records(namespace, scopes=scopes):
            fields = row["record"]["fields"]
            cited: dict[str, list[str]] = {}
            for field in TEXT_FIELDS:
                for match in APPLICATION.finditer(str(fields.get(field) or "")):
                    cited.setdefault(match[1] + match[2], []).append(f"/fields/{field}")
            for number, pointers in sorted(cited.items()):
                target = by_number.get(number)
                if target is None:
                    missing.append({"record_key": row["record_key"], "application_number": number,
                                    "reason": "the cited application is not on record in the Medicines records"})
                    continue
                view, new = self._insert(namespace, kind, row, target["record_id"], clinical_namespace or namespace,
                                         str(target["revision"]), {"basis": "citation", "application_number": number,
                                                                   "quoted_at": pointers,
                                                                   "target_record_kind": "medicinal-product"},
                                         principal_id)
                links.append(view)
                created += new
        return self._result(kind, links, created, missing)

    def link_trials(self, namespace: str, clinical_namespace: str | None, *, principal_id: str,
                    scopes: set[str]) -> dict[str, Any]:
        kind = "trial"
        try:
            trials = self._clinical(clinical_namespace or namespace, scopes, ["registered-trial"])
        except Exception as exc:  # noqa: BLE001 - access to the trial records is optional
            return self._unavailable(kind, "clinical.core", getattr(exc, "code", "unavailable"))
        if trials is None:
            return self._unavailable(kind, "clinical.core", "no clinical record store")
        links, created = [], 0
        for row in self.store.records(namespace, scopes=scopes, kinds=["clearance", "approval", "device-identifier"]):
            record = row["record"]
            identifiers = sorted(set(record.get("premarket_numbers") or []) | set(record.get("udi_dis") or []))
            if row["record_kind"] == "device-identifier":
                identifiers = sorted(set(record.get("udi_dis") or []))  # the DI names the device itself
            for trial in trials:
                text = {k: trial["record"].get(k) for k in ("title", "brief_summary", "interventions")}
                for identifier in identifiers:
                    pointers = _quotes(text, token(identifier))
                    if not pointers:
                        continue
                    view, new = self._insert(namespace, kind, row, trial["record_id"],
                                             clinical_namespace or namespace, str(trial["revision"]),
                                             {"basis": "citation", "identifier": identifier, "quoted_at": pointers,
                                              "trial": trial["native_id"], "registry": trial["provider"]},
                                             principal_id)
                    links.append(view)
                    created += new
        return self._result(kind, links, created, [])

    # ------------------------------------------------------------------ accepted matches (ownership, products)

    def link_accepted(self, namespace: str, *, principal_id: str, scopes: set[str],
                      ownership_namespace: str | None = None) -> list[dict[str, Any]]:
        subjects = {s["record_key"]: s for s in self.identity.subjects(namespace, scopes=scopes)}
        rows = {r["record_key"]: r for r in self.store.records(namespace, scopes=scopes)}
        out = []
        for kind, provider, methods in (("ownership", "ownership.core", {"name+country"}),
                                        ("products", "products.core", {"gtin"})):
            links, created, missing = [], 0, []
            for key, subject in subjects.items():
                for match in self.identity.accepted(namespace, key, scopes=scopes):
                    if match["method"] not in methods:
                        continue
                    target_revision, target_namespace = None, None
                    if kind == "ownership":
                        target_revision = self._ownership_revision(ownership_namespace, match["record_key"])
                        target_namespace = ownership_namespace if target_revision else None
                    else:
                        target_namespace, target_revision = self._product_revision(match["record_key"])
                    if target_namespace is None:
                        missing.append({"subject_key": key, "target_key": match["record_key"],
                                        "reason": f"the accepted {provider} record is not on record"})
                        continue
                    for record_key in subject["records"]:
                        view, new = self._insert(namespace, kind, rows[record_key], match["record_key"],
                                                 target_namespace, target_revision,
                                                 {"basis": "accepted-match", "method": match["method"],
                                                  "identity_candidate_id": match["candidate_id"],
                                                  "decision_id": match["decision_id"], "subject": key},
                                                 principal_id)
                        links.append(view)
                        created += new
            out.append(self._result(kind, links, created, missing))
        return out

    def _ownership_revision(self, ownership_namespace: str | None, record_key: str) -> str | None:
        if not ownership_namespace or not table_exists(self.conn, "ownership_records"):
            return None
        row = self.conn.execute(
            "SELECT v.revision_id FROM ownership_records r JOIN ownership_record_revisions v ON "
            "v.namespace=r.namespace AND v.record_id=r.record_id AND v.revision=r.current_revision WHERE "
            "r.namespace=? AND r.record_key=? ORDER BY r.record_id LIMIT 1", [ownership_namespace, record_key]
        ).fetchone()
        return row[0] if row else None

    def _product_revision(self, identity_id: str) -> tuple[str | None, str | None]:
        if not table_exists(self.conn, "product_identities"):
            return None, None
        row = self.conn.execute("SELECT namespace FROM product_identities WHERE identity_id=?",
                                [identity_id]).fetchone()
        if row is None:
            return None, None
        current = self.conn.execute("SELECT revision_id FROM product_current WHERE variant_id=?",
                                    [identity_id]).fetchone() if table_exists(self.conn, "product_current") else None
        return row[0], current[0] if current else None

    # ------------------------------------------------------------------ entry point and reads

    def link(self, namespace: str, *, principal_id: str, scopes: Iterable[str], kinds: Iterable[str] | None = None,
             products_namespace: str | None = None, clinical_namespace: str | None = None,
             ownership_namespace: str | None = None) -> dict[str, Any]:
        """Link every record kind asked for; absent providers and missing targets are reported, never dropped."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        authorize(namespace, scopes, READ_SCOPE)
        kinds = list(kinds or LINK_KINDS)
        unknown = set(kinds) - set(LINK_KINDS)
        if unknown:
            raise MedicalDevicesError("invalid_request", f"link kinds are {LINK_KINDS}")
        results = []
        if "product-safety" in kinds:
            results.append(self.link_product_safety(namespace, products_namespace, principal_id=principal_id,
                                                    scopes=scopes))
        if "medicines" in kinds:
            results.append(self.link_medicines(namespace, clinical_namespace, principal_id=principal_id,
                                               scopes=scopes))
        if "trial" in kinds:
            results.append(self.link_trials(namespace, clinical_namespace, principal_id=principal_id, scopes=scopes))
        if {"ownership", "products"} & set(kinds):
            results += [r for r in self.link_accepted(namespace, principal_id=principal_id, scopes=scopes,
                                                      ownership_namespace=ownership_namespace)
                        if r["kind"] in kinds]
        return {"contract": CONTRACT, "namespace": namespace, "results": results,
                "unavailable": [r for r in results if r["status"] == "provider_unavailable"],
                "missing_targets": [m for r in results for m in r["missing_targets"]], "notice": NOTICE}

    def links(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
              record_key: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM medical_device_links WHERE namespace=? AND (? IS NULL OR "
            "link_kind=?) AND (? IS NULL OR record_key=?) ORDER BY link_kind, record_key, target_key",
            [namespace, kind, kind, record_key, record_key]).fetchall()
        return [_link_view(r) for r in rows]
