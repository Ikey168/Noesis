"""Devices and manufacturers matched across openFDA, GUDID and EUDAMED through reviewable identity (#2654, MD07).

Every subject stays the record its registry published - a GUDID primary DI, a
EUDAMED Basic UDI-DI, a EUDAMED actor SRN, a company as an FDA record or a GUDID
record names it - and links to other records are *proposed* into the shared
reviewable state machine (:class:`src.kb.ownership_identity.OwnershipIdentityService`,
whose accepted and reverted decisions are
:class:`src.kb.entity_history.EntityHistoryStore` decisions). Nothing is merged
and nothing is accepted automatically; each candidate carries its method,
evidence and confidence and is proposed, reviewed (accepted or rejected) or
reverted.

Methods, published identifiers first:

* ``udi-di`` (``exact-identifier``) - a GUDID primary or package DI listed as a
  UDI-DI of a EUDAMED device (the GS1 identifier is the same code in both
  registries);
* ``gtin`` (``exact-identifier``) - a GS1-issued GUDID DI equal to a GTIN on a
  Products identity (``products.identities``);
* ``accepted-device-match`` (``cross-referenced-identifier``) - the GUDID labeler
  and the EUDAMED manufacturer (SRN) of two devices whose ``udi-di`` match was
  accepted;
* ``premarket-number`` (``cross-referenced-identifier``) - an FDA applicant and a
  GUDID labeler whose records name the same K or P number, with the names as
  published;
* ``name+country`` (``name-jurisdiction``, low confidence) - a manufacturer and a
  Corporate Ownership legal entity with equal normalised names in one country.

Product codes are categories, not identities: records sharing a product code are
grouped by the queries through the published code and are never proposed as the
same device. **Devices are never matched by name alone.** Subjects without an
accepted decision stay visible as ``unmatched``; no person is ever a subject.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.medical_devices_sources import slug
from src.kb.medical_devices_records import (
    READ_SCOPE,
    MedicalDevicesError,
    MedicalDevicesStore,
    authorize,
    table_exists,
)

SUBJECT_KINDS = ("gudid-device", "eudamed-device", "fda-manufacturer", "gudid-labeler", "eudamed-actor")
DEVICE_KINDS = ("gudid-device", "eudamed-device")
MANUFACTURER_KINDS = ("fda-manufacturer", "gudid-labeler", "eudamed-actor")
NOTICE = "a reviewable identity decision; records are never merged and devices are never matched by name alone"
_LEGAL_FORMS = re.compile(r"\b(inc|incorporated|llc|ltd|limited|gmbh|ag|sa|sas|bv|nv|plc|corp|corporation|co|"
                          r"company)\b\.?")
_COUNTRIES = {"united states": "US", "usa": "US", "us": "US", "germany": "DE", "deutschland": "DE"}


def _norm(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def company_norm(value: Any) -> str:
    """A company name for comparison only: normalised, legal-form words removed."""
    text = _LEGAL_FORMS.sub(" ", _norm(value).casefold())
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text).split())


def country_code(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return _COUNTRIES.get(text.casefold())


def entity(record_key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(record_key)


def gtin14(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(14) if 8 <= len(digits) <= 14 else None


def manufacturer_key(name: Any, country: Any) -> str:
    return f"medical-devices:fda:manufacturer:{slug(company_norm(name))}:{(country_code(country) or 'xx').lower()}"


def labeler_key(duns: Any, name: Any) -> str:
    return f"medical-devices:gudid:labeler:{duns or slug(company_norm(name))}"


class MedicalDevicesIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = MedicalDevicesStore(conn, initialize=initialize, now=self.now)
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)

    # ------------------------------------------------------------------ subjects

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Devices and manufacturers as published, each citing the record revisions it was read from."""
        out: dict[str, dict[str, Any]] = {}

        def add(key: str, kind: str, row: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
            subject = out.setdefault(key, {"record_key": key, "kind": kind, "entity_id": entity(key), "names": set(),
                                           "country": None, "identifiers": {}, "premarket_numbers": set(),
                                           "records": set(), "cited": []})
            for field, value in extra.items():
                if field == "name":
                    if value:
                        subject["names"].add(value)
                elif field == "premarket_numbers":
                    subject["premarket_numbers"] |= set(value or [])
                elif value is not None:
                    subject[field] = value
            subject["records"].add(row["record_key"])
            subject["cited"].append({"record_key": row["record_key"], "source_id": row["source_id"],
                                     "revision_id": row["revision_id"]})
            return subject

        for row in self.store.records(namespace, scopes=scopes):
            record, fields, kind = row["record"], row["record"]["fields"], row["record_kind"]
            if kind == "device-identifier":
                subject = add(row["record_key"], "gudid-device", row, name=fields.get("brand_name"),
                              premarket_numbers=record.get("premarket_numbers"))
                subject["identifiers"] = {"primary_di": fields.get("primary_di"),
                                          "issuing_agency": fields.get("issuing_agency"),
                                          "dis": sorted(record.get("udi_dis") or []),
                                          "product_codes": record.get("product_codes") or []}
                if fields.get("company_name"):
                    labeler = add(labeler_key(fields.get("duns_number"), fields["company_name"]), "gudid-labeler",
                                  row, name=fields["company_name"], premarket_numbers=record.get("premarket_numbers"))
                    labeler["identifiers"]["duns"] = fields.get("duns_number")
                    subject["labeler"] = labeler["record_key"]
            elif kind == "eudamed-device":
                subject = add(row["record_key"], "eudamed-device", row, name=fields.get("device_name"),
                              country=None)
                subject["identifiers"] = {"basic_udi_di": fields.get("basic_udi_di"),
                                          "dis": sorted(record.get("udi_dis") or []),
                                          "manufacturer_srn": fields.get("manufacturer_srn")}
            elif kind == "actor":
                subject = add(row["record_key"], "eudamed-actor", row, name=fields.get("name"),
                              country=country_code(fields.get("country")))
                subject["identifiers"] = {"srn": fields.get("srn")}
            elif kind in {"clearance", "approval", "supplement", "recall"} and record.get("manufacturer"):
                country = fields.get("country_code") or fields.get("country") or ("US" if fields.get("state") else None)
                add(manufacturer_key(record["manufacturer"], country), "fda-manufacturer", row,
                    name=record["manufacturer"], country=country_code(country),
                    premarket_numbers=record.get("premarket_numbers"))
        subjects = []
        for subject in out.values():
            subject["names"] = sorted(subject["names"])
            subject["premarket_numbers"] = sorted(subject["premarket_numbers"])
            subject["records"] = sorted(subject["records"])
            seen, cited = set(), []
            for c in subject["cited"]:
                if c["revision_id"] not in seen:
                    seen.add(c["revision_id"])
                    cited.append(c)
            subject["cited"] = cited[:20]
            subjects.append(subject)
        return sorted(subjects, key=lambda s: s["record_key"])

    # ------------------------------------------------------------------ targets (other owners, optional)

    def _ownership_entities(self, namespace: str, principal_id: str, scopes: set[str]) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ownership_records"):
            return []
        from src.kb.ownership_store import OwnershipStore

        return [e for e in OwnershipStore(self.conn, initialize=False).records(
            namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",)) if not e.get("redacted")]

    def _product_identities(self, namespace: str, scopes: set[str]) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "product_identities"):
            return []
        from src.kb.products import READ_SCOPE as PRODUCTS_READ

        if "operator" not in scopes and PRODUCTS_READ not in scopes:
            raise MedicalDevicesError("unauthorized", f"{PRODUCTS_READ} is required to read Products identities")
        out = []
        for identity_id, brand, designation, identifiers in self.conn.execute(
                "SELECT identity_id, brand, designation, identifiers_json FROM product_identities WHERE namespace=? "
                "ORDER BY identity_id", [namespace]).fetchall():
            gtins = sorted({g for g in (gtin14(i.get("value")) for i in (json.loads(identifiers or "{}")
                                                                         .get("gtin") or [])
                                        if isinstance(i, Mapping) and i.get("state", "valid") == "valid") if g})
            if gtins:
                out.append({"identity_id": identity_id, "brand": brand, "designation": designation, "gtins": gtins})
        return out

    # ------------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                ownership_namespace: str | None = None, products_namespace: str | None = None) -> dict[str, Any]:
        """Offer reviewable candidates; idempotent, identifiers before names, never an automatic merge."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        subjects = self.subjects(namespace, scopes=scopes)
        offered, unavailable = [], []
        gudid = [s for s in subjects if s["kind"] == "gudid-device"]
        eudamed = [s for s in subjects if s["kind"] == "eudamed-device"]
        by_key = {s["record_key"]: s for s in subjects}
        # 1. devices across registries by UDI-DI
        for left in gudid:
            for right in eudamed:
                shared = sorted(set(left["identifiers"]["dis"]) & set(right["identifiers"]["dis"]))
                if shared:
                    offered.append(self._offer(namespace, left, right["record_key"], right["entity_id"],
                                               "exact-identifier", "udi-di", {
                                                   "scheme": "udi-di", "value": shared[0], "shared": shared,
                                                   "right": {"record_key": right["record_key"],
                                                             "cited": right["cited"][:1]}}, principal_id, scopes))
        # 2. manufacturers through an accepted device match
        for left in gudid:
            for match in self.accepted(namespace, left["record_key"], scopes=scopes):
                right = by_key.get(match["record_key"])
                if not right or right["kind"] != "eudamed-device" or not left.get("labeler"):
                    continue
                srn = right["identifiers"].get("manufacturer_srn")
                actor = by_key.get(f"medical-devices:eudamed:actor:{srn}") if srn else None
                labeler = by_key.get(left["labeler"])
                if actor and labeler:
                    offered.append(self._offer(namespace, labeler, actor["record_key"], actor["entity_id"],
                                               "cross-referenced-identifier", "accepted-device-match", {
                                                   "via_candidate": match["candidate_id"], "srn": srn,
                                                   "device_records": [left["record_key"], right["record_key"]],
                                                   "names_as_published": [labeler["names"], actor["names"]],
                                                   "right": {"record_key": actor["record_key"],
                                                             "cited": actor["cited"][:1]}}, principal_id, scopes))
        # 3. FDA applicants and GUDID labelers naming the same K or P number
        labelers = [s for s in subjects if s["kind"] == "gudid-labeler"]
        for left in [s for s in subjects if s["kind"] == "fda-manufacturer"]:
            for right in labelers:
                shared = sorted(set(left["premarket_numbers"]) & set(right["premarket_numbers"]))
                if shared and {company_norm(n) for n in left["names"]} & {company_norm(n) for n in right["names"]}:
                    offered.append(self._offer(namespace, left, right["record_key"], right["entity_id"],
                                               "cross-referenced-identifier", "premarket-number", {
                                                   "scheme": "premarket-number", "value": shared[0],
                                                   "shared": shared,
                                                   "names_as_published": [left["names"], right["names"]],
                                                   "right": {"record_key": right["record_key"],
                                                             "cited": right["cited"][:1]}}, principal_id, scopes))
        # 4. devices and Products identities by GTIN (GS1-issued DIs only)
        if products_namespace:
            try:
                products = self._product_identities(products_namespace, scopes)
            except MedicalDevicesError as exc:
                products = []
                unavailable.append({"provider": "products.core", "reason": exc.code})
            if not products:
                unavailable.append({"provider": "products.core", "reason": "no product identity carries a GTIN"})
            for left in gudid:
                if str(left["identifiers"].get("issuing_agency") or "").upper() != "GS1":
                    continue
                mine = {gtin14(d) for d in left["identifiers"]["dis"]} - {None}
                for product in products:
                    shared = sorted(mine & set(product["gtins"]))
                    if shared:
                        offered.append(self._offer(namespace, left, product["identity_id"],
                                                   entity(product["identity_id"]), "exact-identifier", "gtin", {
                                                       "scheme": "gtin", "value": shared[0],
                                                       "right": {"identity_id": product["identity_id"],
                                                                 "products_namespace": products_namespace,
                                                                 "brand": product["brand"],
                                                                 "designation": product["designation"]}},
                                                   principal_id, scopes))
        else:
            unavailable.append({"provider": "products.core", "reason": "no products namespace given"})
        # 5. manufacturers and Corporate Ownership legal entities (names in one country: low confidence)
        manufacturers = [s for s in subjects if s["kind"] in MANUFACTURER_KINDS]
        if ownership_namespace:
            try:
                owned = self._ownership_entities(ownership_namespace, principal_id, scopes)
            except Exception as exc:  # noqa: BLE001 - ownership access is optional; report it
                owned = []
                unavailable.append({"provider": "ownership.core", "reason": getattr(exc, "code", "unavailable")})
            if not owned:
                unavailable.append({"provider": "ownership.core", "reason": "no legal entities in the namespace"})
            for subject in manufacturers:
                names = {company_norm(n) for n in subject["names"]}
                for view in owned:
                    body = view["record"]
                    country = country_code(str(body.get("jurisdiction") or "").split("-")[0])
                    if not names or company_norm(body.get("name")) not in names:
                        continue
                    if subject["country"] and country and subject["country"] != country:
                        continue  # the published countries contradict the pairing
                    offered.append(self._offer(namespace, subject, body["record_key"], entity(body["record_key"]),
                                               "name-jurisdiction", "name+country", {
                                                   "value": body.get("name"), "country": country,
                                                   "subject_country": subject["country"],
                                                   "right": {"record_key": body["record_key"],
                                                             "record_id": view["record_id"],
                                                             "revision": view["revision"],
                                                             "ownership_namespace": ownership_namespace},
                                                   "note": "equal company names in one country are a weak signal; "
                                                           "a reviewer decides"}, principal_id, scopes))
        else:
            unavailable.append({"provider": "ownership.core", "reason": "no ownership namespace given"})
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes), "unavailable": unavailable,
                "notice": NOTICE}

    def _offer(self, namespace, subject, right_key, right_entity, basis, method, evidence, principal_id, scopes):
        return self.service.offer(
            namespace, left_key=subject["record_key"], right_key=right_key, left_entity=subject["entity_id"],
            right_entity=right_entity, basis=basis,
            evidence=[{**evidence, "method": method,
                       "left": {"record_key": subject["record_key"], "kind": subject["kind"],
                                "names_as_published": subject["names"], "cited": subject["cited"][:5]}}],
            principal_id=principal_id, scopes=scopes)

    # ------------------------------------------------------------------ review and reads

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        evidence = candidate["evidence"][0] if candidate["evidence"] else {}
        return {
            "candidate_id": candidate["candidate_id"], "state": candidate["state"],
            "review_state": {"accepted": "reviewed-match", "rejected": "reviewed-non-match",
                             "reverted": "reverted", "proposed": "unreviewed-candidate"}[candidate["state"]],
            "basis": candidate["basis"], "method": evidence.get("method"), "confidence": candidate["confidence"],
            "records": [candidate["left_key"], candidate["right_key"]],
            "entities": [candidate["left_entity"], candidate["right_entity"]],
            "decision_id": candidate["decision_id"],
            "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
            "reason": last.get("reason"), "evidence": candidate["evidence"], "history": candidate["history"],
            "notice": NOTICE,
        }

    def candidates(self, namespace: str, *, scopes: Iterable[str], record_key: str | None = None
                   ) -> list[dict[str, Any]]:
        rows = [c for c in self.service.candidates(namespace, scopes=scopes, record_key=record_key)
                if c["left_key"].startswith("medical-devices:") or c["right_key"].startswith("medical-devices:")]
        return [self.view(c) for c in rows]

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        self._own(namespace, candidate_id, scopes)
        return self.view(self.service.review(namespace, candidate_id, decision, reason, principal_id=principal_id,
                                             scopes=scopes))

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        self._own(namespace, candidate_id, scopes)
        return self.view(self.service.revert(namespace, candidate_id, reason, principal_id=principal_id,
                                             scopes=scopes))

    def _own(self, namespace: str, candidate_id: str, scopes: Iterable[str]) -> None:
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise MedicalDevicesError("not_found", "no medical-devices identity candidate with that id")

    def accepted(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Accepted, unreverted links of one subject: the other record, method and decision."""
        try:
            views = self.candidates(namespace, scopes=scopes, record_key=record_key)
        except Exception:  # noqa: BLE001 - identity access is optional for an answer
            return []
        out = []
        for view in views:
            if view["state"] != "accepted":
                continue
            other = view["records"][1] if view["records"][0] == record_key else view["records"][0]
            out.append({"candidate_id": view["candidate_id"], "record_key": other, "basis": view["basis"],
                        "method": view["method"], "confidence": view["confidence"],
                        "decision_id": view["decision_id"], "reviewer": view["reviewer"]})
        return out

    def identity(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Accepted links and open candidates for one subject; without an accepted link it is unmatched."""
        try:
            views = self.candidates(namespace, scopes=scopes, record_key=record_key)
        except Exception as exc:  # noqa: BLE001 - identity access is optional for an answer
            return {"state": "unmatched", "links": [], "candidates": [], "unavailable": [getattr(exc, "code", "x")]}
        links = [v for v in views if v["state"] == "accepted"]
        return {"state": "matched" if links else "unmatched",
                "links": [{"candidate_id": v["candidate_id"], "records": v["records"], "method": v["method"],
                           "confidence": v["confidence"], "reviewer": v["reviewer"]} for v in links],
                "candidates": [v["candidate_id"] for v in views if v["state"] == "proposed"]}

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Device and manufacturer subjects without an accepted link (visible, never dropped)."""
        scopes = set(scopes)
        return {"unmatched": [{"record_key": s["record_key"], "kind": s["kind"], "names": s["names"],
                               "state": "unmatched"}
                              for s in self.subjects(namespace, scopes=scopes)
                              if self.identity(namespace, s["record_key"], scopes=scopes)["state"] == "unmatched"],
                "notice": NOTICE}
