"""Devices and manufacturers matched across registries through reviewable identity (#2654, MD07).

Three kinds of candidate are *proposed*, never accepted automatically and never
merged:

* **device <-> device** across FDA (510(k), PMA), AccessGUDID and EUDAMED, by
  published identifiers only: ``udi-di`` (a GUDID primary or package DI equal
  to a EUDAMED UDI-DI), ``premarket-number`` (a GUDID premarket submission
  number equal to a K or P number) and, as **low evidence** where no stronger
  identifier links the pair, ``product-code`` (a shared FDA product code is a
  device type, not a device). Device names are never used;
* **manufacturer -> ownership entity**: an organisation as a regulator
  published it (a 510(k) or PMA applicant, a GUDID labeler with its DUNS, a
  EUDAMED actor with its SRN, a recalling firm) against the legal entities of
  an ownership namespace (:class:`src.kb.ownership_store.OwnershipStore`):
  ``exact-identifier`` (DUNS, LEI, SRN) first, ``name-jurisdiction`` (equal
  normalized names, countries not contradicting) as low evidence;
* **device -> Products identity**: a GUDID DI equal to a GTIN a Products
  identity carries (``gtin-udi-di``).

Every candidate carries its method, evidence and confidence, and moves
``proposed`` -> ``accepted`` / ``rejected`` -> ``reverted`` with a reason;
accept, reject and revert are :class:`src.kb.entity_history.EntityHistoryStore`
decisions with reviewer and time (the state machine of
:mod:`src.kb.ownership_identity`). Subjects without an accepted match are
reported as **unmatched** and stay as published.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from typing import Any

from src.kb.medical_devices_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    MedicalDeviceError,
    MedicalDeviceStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-medical-device-identity-candidate-v1"
CONFIDENCE = {"udi-di": 0.95, "premarket-number": 0.9, "gtin-udi-di": 0.9, "exact-identifier": 0.95,
              "product-code": 0.3, "name-jurisdiction": 0.35}
LOW_EVIDENCE = frozenset({"product-code", "name-jurisdiction"})
DEVICE_KINDS = ("clearance", "approval", "device-identifier", "eudamed-device")
STATES = ("proposed", "accepted", "rejected", "reverted")
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS medical_device_identity_candidates (
  namespace TEXT NOT NULL, candidate_id TEXT NOT NULL, subject_key TEXT NOT NULL, subject_kind TEXT NOT NULL,
  target_key TEXT NOT NULL, target_kind TEXT NOT NULL, target_namespace TEXT NOT NULL, method TEXT NOT NULL,
  confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL,
  PRIMARY KEY(namespace, candidate_id)
);
"""
_COLUMNS = ("candidate_id", "subject_key", "subject_kind", "target_key", "target_kind", "target_namespace", "method",
            "confidence", "evidence_json", "state", "decision_id", "created_by", "created_at_ms", "history_json")


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum()).lstrip("0") or "0"


def _name(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


_COUNTRIES = {"UNITED STATES": "US", "USA": "US", "GERMANY": "DE", "UNITED KINGDOM": "GB"}


def _country(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    return _COUNTRIES.get(text, text.split("-")[0] if len(text.split("-")[0]) == 2 else None)


def entity_for(key: str) -> str:
    return "ent-md-" + digest(key)[:24]


def manufacturer_key(provider: str, name: str | None, *, duns: str | None = None) -> str:
    if duns:
        return f"medical-devices:manufacturer:duns:{_norm(duns)}"
    return f"medical-devices:manufacturer:{provider}:{_name(name).replace(' ', '-')}"


class MedicalDeviceIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = MedicalDeviceStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, now=self.now, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "medical_device_identity_candidates")

    # -------------------------------------------------------------- subjects

    def devices(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Published device records of every registry with their published identifiers."""
        out = []
        for view in self.store.records(namespace, scopes=scopes, kinds=DEVICE_KINDS, include_unpublished=False):
            record, fields = view["record"], view["record"]["fields"]
            ids = {(i["scheme"], i["value"]) for i in record.get("identifiers") or []}
            ids |= {(i["scheme"], i["value"]) for i in record.get("links_as_published") or []}
            codes = {v for s, v in ids if s == "fda-product-code"}
            if record["record_kind"] == "device-identifier":
                codes |= {c["code"] for c in fields.get("product_codes") or [] if c.get("code")}
            out.append({"key": record["record_key"], "kind": record["record_kind"], "provider": record["provider"],
                        "jurisdiction": record["jurisdiction"], "revision_id": view["revision_id"],
                        "identifiers": sorted(ids), "product_codes": sorted(codes)})
        return out

    def manufacturers(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Organisations as regulators published them, grouped by provider and name (or DUNS / SRN)."""
        grouped: dict[str, dict[str, Any]] = {}

        def add(key, provider, name, country, identifiers, view):
            entry = grouped.setdefault(key, {"key": key, "provider": provider, "name_as_published": name,
                                             "country": _country(country), "identifiers": [], "records": []})
            for item in identifiers:
                if item not in entry["identifiers"]:
                    entry["identifiers"].append(item)
            entry["records"].append({"record_key": view["record_key"], "revision_id": view["revision_id"]})

        for view in self.store.records(namespace, scopes=scopes, include_unpublished=False,
                                       kinds=("clearance", "approval", "recall", "device-identifier",
                                              "eudamed-actor")):
            record, fields = view["record"], view["record"]["fields"]
            kind = record["record_kind"]
            if kind in {"clearance", "approval"} and (fields.get("applicant") or {}).get("name_as_published"):
                firm = fields["applicant"]
                add(manufacturer_key(record["provider"], firm["name_as_published"]), record["provider"],
                    firm["name_as_published"], firm.get("country"), [], view)
            elif kind == "recall" and (fields.get("recalling_firm") or {}).get("name_as_published"):
                firm = fields["recalling_firm"]
                add(manufacturer_key(record["provider"], firm["name_as_published"]), record["provider"],
                    firm["name_as_published"], firm.get("country"), [], view)
            elif kind == "device-identifier" and fields.get("company_name"):
                duns = fields.get("labeler_duns")
                add(manufacturer_key("accessgudid", fields["company_name"], duns=duns), "accessgudid",
                    fields["company_name"], "US" if duns else None,
                    [{"scheme": "duns", "value": duns}] if duns else [], view)
            elif kind == "eudamed-actor":
                add(record["record_key"], "eudamed", fields.get("name"), fields.get("country"),
                    [{"scheme": "eudamed-srn", "value": fields["srn"]}], view)
        return [grouped[k] for k in sorted(grouped)]

    # ------------------------------------------------------------- proposals

    def _offer(self, namespace, subject_key, subject_kind, target_key, target_kind, target_namespace, method,
               evidence, principal_id) -> dict[str, Any]:
        candidate_id = "md-idc:" + digest([namespace, subject_key, target_namespace, target_key])[:24]
        evidence = {**evidence, "method": method, "low_evidence": method in LOW_EVIDENCE}
        row = self.conn.execute("SELECT state, method, evidence_json, history_json FROM "
                                "medical_device_identity_candidates WHERE namespace=? AND candidate_id=?",
                                [namespace, candidate_id]).fetchone()
        now = self.now()
        if row is None:
            self.conn.execute(
                "INSERT INTO medical_device_identity_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, candidate_id, subject_key, subject_kind, target_key, target_kind, target_namespace,
                 method, CONFIDENCE[method], canonical(evidence), "proposed", None, principal_id, now,
                 canonical([{"state": "proposed", "by": principal_id, "at_ms": now}])])
            return {"candidate_id": candidate_id, "change": "created"}
        state, old_method, old_evidence, history = row[0], row[1], json.loads(row[2]), json.loads(row[3])
        stronger = CONFIDENCE[method] > CONFIDENCE[old_method]
        new = digest(evidence) != digest(old_evidence)
        if state == "proposed" and stronger:
            change = "upgraded"
        elif state in {"rejected", "reverted"} and new:
            change = "reproposed"
        else:
            return {"candidate_id": candidate_id, "change": None}
        history.append({"state": "proposed", "by": principal_id, "at_ms": now, "change": change,
                        "previous_state": state, "previous_method": old_method, "previous_evidence": old_evidence})
        self.conn.execute("UPDATE medical_device_identity_candidates SET state='proposed', decision_id=NULL, "
                          "method=?, confidence=?, evidence_json=?, history_json=? WHERE namespace=? AND "
                          "candidate_id=?", [method, CONFIDENCE[method], canonical(evidence), canonical(history),
                                             namespace, candidate_id])
        return {"candidate_id": candidate_id, "change": change}

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                ownership_namespace: str | None = None, products_namespace: str | None = None) -> dict[str, Any]:
        """Offer identifier candidates first and low-evidence ones only where no identifier links a pair."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not self._ready():
            self.conn.execute(_DDL)
        offered = []
        devices = self.devices(namespace, scopes=scopes)
        strong_pairs = set()
        for left in devices:
            for right in devices:
                if left["key"] >= right["key"] or left["provider"] == right["provider"] and \
                        left["kind"] == right["kind"]:
                    continue
                shared = sorted(set(map(tuple, left["identifiers"])) & set(map(tuple, right["identifiers"])))
                udi = [i for i in shared if i[0] in {"udi-di", "udi-di-package"}]
                premarket = [i for i in shared if i[0] in {"fda-510k", "fda-pma"}]
                method = "udi-di" if udi else "premarket-number" if premarket else None
                if method:
                    strong_pairs.add((left["key"], right["key"]))
                    offered.append(self._offer(
                        namespace, left["key"], "device", right["key"], "device", namespace, method,
                        {"shared_identifiers": [{"scheme": s, "value": v} for s, v in (udi or premarket)],
                         "left": {"record_key": left["key"], "revision_id": left["revision_id"]},
                         "right": {"record_key": right["key"], "revision_id": right["revision_id"]}}, principal_id))
        for left in devices:
            for right in devices:
                if left["key"] >= right["key"] or left["provider"] == right["provider"] or \
                        (left["key"], right["key"]) in strong_pairs:
                    continue
                codes = sorted(set(left["product_codes"]) & set(right["product_codes"]))
                if codes:
                    offered.append(self._offer(
                        namespace, left["key"], "device", right["key"], "device", namespace, "product-code",
                        {"shared_product_codes": codes, "note": "a product code is a device type, not a device; "
                                                                "low evidence, a reviewer decides",
                         "left": {"record_key": left["key"], "revision_id": left["revision_id"]},
                         "right": {"record_key": right["key"], "revision_id": right["revision_id"]}}, principal_id))
        coverage = {"ownership": "not requested", "products": "not requested"}
        if ownership_namespace:
            coverage["ownership"] = self._propose_ownership(namespace, ownership_namespace, principal_id, scopes,
                                                            offered)
        if products_namespace:
            coverage["products"] = self._propose_products(namespace, products_namespace, devices, principal_id,
                                                          scopes, offered)
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["change"]}),
                "coverage": coverage, "candidates": self.candidates(namespace, scopes=scopes),
                "unmatched": self.unmatched(namespace, scopes=scopes)}

    def _propose_ownership(self, namespace, ownership_namespace, principal_id, scopes, offered) -> str:
        from src.kb.ownership_records import READ_SCOPE as OWNERSHIP_READ
        from src.kb.ownership_store import OwnershipError, OwnershipStore

        if not table_exists(self.conn, "ownership_records"):
            return "provider missing: no ownership records in this deployment"
        try:
            entities = OwnershipStore(self.conn, initialize=False).records(
                ownership_namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",))
        except OwnershipError as exc:
            raise MedicalDeviceError(exc.code, f"ownership namespace: {exc} ({OWNERSHIP_READ})") from exc
        for subject in self.manufacturers(namespace, scopes=scopes):
            published = {(i["scheme"], _norm(i["value"])) for i in subject["identifiers"]}
            for entity in entities:
                body = entity["record"]
                right = {"record_key": body["record_key"], "revision": entity["revision"],
                         "revision_id": entity["revision_id"]}
                shared = [i for i in body.get("identifiers") or [] if (i["scheme"], _norm(i["value"])) in published]
                left = {"key": subject["key"], "name_as_published": subject["name_as_published"],
                        "records": subject["records"]}
                if shared:
                    offered.append(self._offer(namespace, subject["key"], "manufacturer", body["record_key"],
                                               "ownership-entity", ownership_namespace, "exact-identifier",
                                               {"identifiers": shared, "left": left, "right": right}, principal_id))
                    continue
                if _name(body.get("name")) != _name(subject["name_as_published"]):
                    continue
                theirs = _country(body.get("jurisdiction"))
                ours = subject.get("country")
                if ours and theirs and ours != theirs:
                    continue  # a contradicting country is not a candidate
                offered.append(self._offer(namespace, subject["key"], "manufacturer", body["record_key"],
                                           "ownership-entity", ownership_namespace, "name-jurisdiction",
                                           {"normalized_name": _name(body.get("name")), "country": ours or theirs,
                                            "left": left, "right": right,
                                            "note": "a name is low evidence and never accepted automatically"},
                                           principal_id))
        return "proposed"

    def _propose_products(self, namespace, products_namespace, devices, principal_id, scopes, offered) -> str:
        if not table_exists(self.conn, "product_identities"):
            return "provider missing: no Products identities in this deployment"
        authorize(products_namespace, scopes, "knowledge:products:read")
        rows = self.conn.execute("SELECT identity_id, identifiers_json FROM product_identities WHERE namespace=? "
                                 "ORDER BY identity_id", [products_namespace]).fetchall()
        gtins: dict[str, list[str]] = {}
        for identity_id, identifiers in rows:
            for gtin in (json.loads(identifiers or "{}") or {}).get("gtin") or []:
                gtins.setdefault(_norm(gtin.get("value")), []).append(identity_id)
        for device in devices:
            if device["kind"] != "device-identifier":
                continue
            for scheme, value in device["identifiers"]:
                if scheme not in {"udi-di", "udi-di-package"}:
                    continue
                for identity_id in gtins.get(_norm(value), []):
                    offered.append(self._offer(namespace, device["key"], "device", identity_id, "products-identity",
                                               products_namespace, "gtin-udi-di",
                                               {"udi_di": value, "gtin_scheme": scheme,
                                                "left": {"record_key": device["key"],
                                                         "revision_id": device["revision_id"]},
                                                "right": {"identity_id": identity_id}}, principal_id))
        return "proposed"

    # --------------------------------------------------------------- reviews

    def _row(self, namespace: str, candidate_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT " + ", ".join(_COLUMNS) + " FROM medical_device_identity_candidates "
                                "WHERE namespace=? AND candidate_id=?", [namespace, candidate_id]).fetchone() \
            if self._ready() else None
        if row is None:
            raise MedicalDeviceError("not_found", "no medical-device identity candidate with that id")
        item = dict(zip(_COLUMNS, row))
        history = json.loads(item.pop("history_json"))
        evidence = json.loads(item.pop("evidence_json"))
        last = history[-1]
        reviewed = item["state"] != "proposed"
        return {"contract": CONTRACT, "namespace": namespace, **item,
                "evidence": evidence, "low_evidence": item["method"] in LOW_EVIDENCE,
                "reviewer": last.get("by") if reviewed else None, "reviewed_at_ms": last.get("at_ms") if reviewed
                else None, "reason": last.get("reason"), "history": history,
                "notice": "a reviewable identity proposal; records are never merged or rewritten"}

    def candidates(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                   subject_key: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT candidate_id FROM medical_device_identity_candidates WHERE namespace=? AND (? IS NULL OR "
            "state=?) AND (? IS NULL OR subject_key=? OR target_key=?) ORDER BY candidate_id",
            [namespace, state, state, subject_key, subject_key, subject_key]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def _transition(self, namespace, candidate, state, decision_id, principal_id, reason) -> dict[str, Any]:
        history = candidate["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                           "decision_id": decision_id}]
        self.conn.execute("UPDATE medical_device_identity_candidates SET state=?, decision_id=?, history_json=? "
                          "WHERE namespace=? AND candidate_id=?",
                          [state, decision_id, canonical(history), namespace, candidate["candidate_id"]])
        return self._row(namespace, candidate["candidate_id"])

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept or reject a proposed candidate with a reason (an entity identity decision; nothing is merged)."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise MedicalDeviceError("invalid_decision", "accept or reject with a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] != "proposed":
            raise MedicalDeviceError("invalid_state", f"candidate is {candidate['state']}; propose again to review")
        left, right = entity_for(candidate["subject_key"]), entity_for(candidate["target_key"])
        for entity, key in ((left, candidate["subject_key"]), (right, candidate["target_key"])):
            self.history.register_entity(namespace, entity, [key], principal_id=principal_id,
                                         scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [left, right],
            {"candidate_id": candidate_id, "method": candidate["method"], "confidence": candidate["confidence"],
             "evidence": candidate["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "clinical.devices", "records": [candidate["subject_key"],
                                                                        candidate["target_key"]]},
             "policy": {"merge": False, "note": "identity decision only; records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"medical-device-identity:{namespace}:{candidate_id}")
        return self._transition(namespace, candidate, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise MedicalDeviceError("invalid_decision", "a revert needs a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] not in {"accepted", "rejected"}:
            raise MedicalDeviceError("invalid_state", "only an accepted or rejected candidate can be reverted")
        undo = self.history.undo(namespace, candidate["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, candidate, "reverted", undo["decision_id"], principal_id, reason.strip())

    # ------------------------------------------------------------ reporting

    def accepted(self, namespace: str, key: str, *, scopes: Iterable[str], target_kind: str | None = None
                 ) -> list[dict[str, Any]]:
        """Accepted, unreverted counterparts of a record or manufacturer key (either side of the candidate)."""
        out = []
        for item in self.candidates(namespace, scopes=scopes, state="accepted", subject_key=key):
            other = item["target_key"] if item["subject_key"] == key else item["subject_key"]
            other_kind = item["target_kind"] if item["subject_key"] == key else item["subject_kind"]
            if target_kind and other_kind != target_kind:
                continue
            out.append({"key": other, "kind": other_kind, "namespace": item["target_namespace"],
                        "candidate_id": item["candidate_id"], "method": item["method"],
                        "confidence": item["confidence"], "low_evidence": item["low_evidence"],
                        "reviewer": item["reviewer"], "reviewed_at_ms": item["reviewed_at_ms"],
                        "decision_id": item["decision_id"]})
        return out

    def device_cluster(self, namespace: str, key: str, *, scopes: Iterable[str]) -> list[str]:
        """The device records reachable from one through accepted device matches (the record itself first)."""
        seen, queue = [key], [key]
        while queue:
            current = queue.pop()
            for item in self.accepted(namespace, current, scopes=scopes, target_kind="device"):
                if item["key"] not in seen:
                    seen.append(item["key"])
                    queue.append(item["key"])
        return seen

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Devices and manufacturers with no accepted match: kept as published, with pending candidate counts."""
        views = self.candidates(namespace, scopes=scopes)
        out = []
        subjects = [(d["key"], "device", d["provider"], None) for d in self.devices(namespace, scopes=scopes)]
        subjects += [(m["key"], "manufacturer", m["provider"], m["name_as_published"])
                     for m in self.manufacturers(namespace, scopes=scopes)]
        for key, kind, provider, name in subjects:
            mine = [v for v in views if key in (v["subject_key"], v["target_key"])]
            if any(v["state"] == "accepted" for v in mine):
                continue
            out.append({"key": key, "subject": kind, "provider": provider, "name_as_published": name,
                        "pending_candidates": sum(v["state"] == "proposed" for v in mine), "status": "unmatched"})
        return out


__all__ = ["CONFIDENCE", "CONTRACT", "LOW_EVIDENCE", "MedicalDeviceIdentity", "entity_for", "manufacturer_key"]
