"""Holding-structure classification of ownership entities, only as a source states it (#2890).

Large fortunes are held through family offices, private foundations, trusts,
holding companies and special-purpose vehicles. Those are ordinary legal
entities in the ownership store (``noesis-ownership-record-v1``/``v2``); this
module lets one be *marked* as such a structure, and only when a source says
so. A classification (``noesis-ownership-structure-classification-v1``, a new
contract beside the record contracts, which stay unchanged) is a sourced,
reviewable decision:

* its ``basis`` is what the source states - a register legal-form code or
  legal-form text (``register-legal-form``), a self-description in a filing or
  register document (``self-description``) or a category a source declares
  (``declared-category``). There is no name basis: a classification citing a
  name field is refused (``name_inference_refused``), and nothing here reads
  an entity's name;
* its ``citation`` is either one ownership record revision and the field that
  states the value (``ownership-record``; the stated value must equal what
  that revision holds at that field), or an official document the user
  obtained (``document``: reference, SHA-256, locator and the verbatim quote,
  which must contain the stated value);
* it starts ``proposed`` and classifies the entity only once a reviewer with
  ``knowledge:ownership:review`` accepts it (with a reason); a rejection or a
  revert is recorded the same way and nothing is deleted. Accepted
  classifications from different sources that disagree are returned side by
  side, never resolved.

An entity without an accepted classification is ``unclassified``; it is never
given a default type. :func:`StructureClassifications.propose_from_records`
offers proposals only where a record's *stated* legal form equals an entry of
:data:`STATED_FORMS` exactly (no substring, no name); they still need review.

Natural persons (``person`` records) are classified only in person-research
mode (``NOESIS_PERSON_RESEARCH``, docs/security/person-research-mode.md).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.ownership_records import (
    ENTITY_KINDS,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    canonical,
    digest,
)
from src.kb.ownership_store import OwnershipError, OwnershipStore, authorize

CONTRACT = "noesis-ownership-structure-classification-v1"
STRUCTURE_TYPES = ("family_office", "private_foundation", "trust", "trustee", "holding_company",
                   "special_purpose_vehicle")
BASES = ("register-legal-form", "self-description", "declared-category")
STATES = ("proposed", "accepted", "rejected", "reverted")
CITATION_KINDS = ("ownership-record", "document")
# Record fields a classification may cite: the stated legal form / entity type and the source's own wording.
CITABLE_ROOTS = frozenset({"entity_type", "native", "description", "register"})
# Name fields are never evidence of a structure type ("Example Family Office GmbH" stays unclassified).
NAME_FIELDS = frozenset({"name", "names", "other_names", "legal_name", "previous_names", "officer", "holder"})
NOTICE = ("A structure type is shown only where a source states it (legal form, self-description or declared "
          "category) and a reviewer accepted it; an entity without one is unclassified. Never inferred from names.")
# Exact stated values (normalised: case-folded, separators collapsed) that are *offered* for review by
# propose_from_records. Each names the vocabulary it comes from; matching is equality, never substring.
STATED_FORMS = (
    {"field": "entity_type.subtype", "value": "trust", "structure_type": "trust", "basis": "declared-category",
     "vocabulary": "BODS 0.4 entityType.subtype (arrangement subtype 'trust')"},
    {"field": "entity_type", "value": "trust", "structure_type": "trust", "basis": "register-legal-form",
     "vocabulary": "legal form as stated by the register or filer"},
    {"field": "entity_type", "value": "private foundation", "structure_type": "private_foundation",
     "basis": "register-legal-form", "vocabulary": "legal form as stated by the register or filer"},
    {"field": "entity_type", "value": "privatstiftung", "structure_type": "private_foundation",
     "basis": "register-legal-form", "vocabulary": "Austrian legal form Privatstiftung as stated"},
    {"field": "entity_type", "value": "family office", "structure_type": "family_office",
     "basis": "declared-category", "vocabulary": "category as stated by the register or filer"},
    {"field": "entity_type", "value": "holding company", "structure_type": "holding_company",
     "basis": "declared-category", "vocabulary": "category as stated by the register or filer"},
    {"field": "entity_type", "value": "special purpose vehicle", "structure_type": "special_purpose_vehicle",
     "basis": "declared-category", "vocabulary": "category as stated by the register or filer"},
)
_DDL = """
CREATE TABLE IF NOT EXISTS ownership_structure_classifications (
  namespace TEXT NOT NULL, classification_id TEXT NOT NULL, entity_key TEXT NOT NULL, structure_type TEXT NOT NULL,
  basis TEXT NOT NULL, stated_value TEXT NOT NULL, citation_json TEXT NOT NULL, state TEXT NOT NULL,
  decision_json TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, classification_id)
);
"""
_COLUMNS = ("classification_id", "entity_key", "structure_type", "basis", "stated_value", "citation_json", "state",
            "decision_json", "history_json", "created_by", "created_at_ms")


def normalize(value: Any) -> str:
    return re.sub(r"[\s_\-/]+", " ", str(value or "")).strip().casefold()


def _at(body: Mapping[str, Any], path: str) -> Any:
    value: Any = body
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _states(value: Any, stated: str) -> bool:
    """Whether a record value states ``stated`` exactly (a list states each of its scalar items)."""
    wanted = normalize(stated)
    if isinstance(value, list):
        return any(not isinstance(v, (Mapping, list)) and normalize(v) == wanted for v in value)
    return value is not None and not isinstance(value, Mapping) and normalize(value) == wanted


def _view(namespace: str, row: tuple) -> dict[str, Any]:
    item = dict(zip(_COLUMNS, row))
    item["citation"] = json.loads(item.pop("citation_json"))
    item["decision"] = json.loads(item.pop("decision_json")) if item["decision_json"] else None
    item["history"] = json.loads(item.pop("history_json"))
    return {"contract": CONTRACT, "namespace": namespace, **item,
            "classifies": item["state"] == "accepted",
            "notice": "a reviewable classification as the cited source states it; records are never rewritten"}


class StructureClassifications:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = OwnershipStore(conn, now=self.now, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return bool(self.conn.execute("SELECT 1 FROM information_schema.tables "
                                      "WHERE table_name='ownership_structure_classifications'").fetchone())

    # ------------------------------------------------------------ proposals

    def _entity(self, namespace: str, entity_key: str, principal_id: str | None, scopes: set[str]) -> dict[str, Any]:
        view = self.store.by_key(namespace, entity_key, principal_id=principal_id, scopes=scopes)
        if view is None or view["record"]["kind"] not in ENTITY_KINDS:
            raise OwnershipError("not_found", "no ownership entity record carries this key in this namespace")
        if view.get("redacted"):
            raise OwnershipError("unauthorized", "the entity record is owner-scoped and redacted for this principal")
        if view["record"]["kind"] == "person":
            from src.config.env import person_research_enabled

            if not person_research_enabled():
                raise OwnershipError("person_research_required",
                                     "natural persons are classified only in person-research mode "
                                     "(NOESIS_PERSON_RESEARCH)")
        return view

    def _check_citation(self, namespace: str, entity_key: str, stated_value: str, citation: Mapping[str, Any],
                        principal_id: str | None, scopes: set[str]) -> dict[str, Any]:
        if not isinstance(citation, Mapping) or citation.get("kind") not in CITATION_KINDS:
            raise OwnershipError("invalid_request", "a classification cites an ownership-record revision or a document")
        field = str(citation.get("field") or "")
        root = field.split(".")[0]
        stated_at = citation.get("locator") if isinstance(citation.get("locator"), Mapping) else {}
        if {root, str(stated_at.get("field") or "").split(".")[0]} & NAME_FIELDS:
            raise OwnershipError("name_inference_refused", "a name is never evidence of a structure type")
        if citation["kind"] == "ownership-record":
            if root not in CITABLE_ROOTS:
                raise OwnershipError("invalid_request", "cite the field stating the legal form, category or the "
                                                        "source's own wording (" + ", ".join(sorted(CITABLE_ROOTS)) + ")")
            try:
                view = self.store.get(namespace, str(citation.get("record_id")), principal_id=principal_id,
                                      scopes=scopes, revision=int(citation.get("revision")))
            except (TypeError, ValueError) as exc:
                raise OwnershipError("invalid_request", "an ownership-record citation needs record_id and revision") from exc
            body = view["record"]
            if view.get("redacted"):
                raise OwnershipError("unauthorized", "the cited record is redacted for this principal")
            if entity_key not in {body.get("record_key"), body.get("entity_key"), body.get("subject_key")}:
                raise OwnershipError("invalid_request", "the cited record is not about this entity")
            if not _states(_at(body, field), stated_value):
                raise OwnershipError("not_stated", "the cited record revision does not state this value at this field")
            return {"kind": "ownership-record", "namespace": namespace, "record_id": view["record_id"],
                    "revision": view["revision"], "revision_id": view["revision_id"],
                    "record_hash": view["record_hash"], "field": field, "provider": body["source"]["provider"],
                    "provider_record_id": body["source"].get("provider_record_id"),
                    "url": body["source"].get("url"), "locator": body["source"].get("locator")}
        if not citation.get("reference") or not re.fullmatch(r"[0-9a-f]{64}", str(citation.get("sha256") or "")):
            raise OwnershipError("invalid_request", "a document citation needs the official reference and its SHA-256")
        if citation.get("url") is not None and not str(citation["url"]).startswith("https://"):
            raise OwnershipError("invalid_request", "a document URL must be an HTTPS locator")
        quote, locator = citation.get("quote"), citation.get("locator")
        if not isinstance(locator, Mapping) or not locator:
            raise OwnershipError("invalid_request", "a document citation needs a locator (page, section or field)")
        if not isinstance(quote, str) or normalize(stated_value) not in normalize(quote):
            raise OwnershipError("not_stated", "the quoted passage must contain the stated value verbatim")
        return {"kind": "document", "publisher": citation.get("publisher"), "reference": citation["reference"],
                "sha256": citation["sha256"], "url": citation.get("url"), "locator": dict(locator), "quote": quote,
                "retrieved_on": citation.get("retrieved_on")}

    def propose(self, namespace: str, *, entity_key: str, structure_type: str, basis: str, stated_value: str,
                citation: Mapping[str, Any], principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Record a proposed classification; it classifies nothing until a reviewer accepts it."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if structure_type not in STRUCTURE_TYPES:
            raise OwnershipError("invalid_request", "structure_type is one of " + ", ".join(STRUCTURE_TYPES))
        if basis not in BASES:
            raise OwnershipError("invalid_request", "basis is what the source states: " + ", ".join(BASES)
                                 + " (names are never a basis)")
        if not isinstance(stated_value, str) or not stated_value.strip() or len(stated_value) > 500:
            raise OwnershipError("invalid_request", "stated_value is the value as the source states it")
        self._entity(namespace, entity_key, principal_id, scopes)
        checked = self._check_citation(namespace, entity_key, stated_value, citation, principal_id, scopes)
        anchor = (checked.get("record_id"), checked.get("revision"), checked.get("field")) if checked[
            "kind"] == "ownership-record" else (checked["reference"], checked["sha256"])
        classification_id = "own-struct:" + digest([namespace, entity_key, structure_type, basis,
                                                    normalize(stated_value), anchor])[:24]
        existing = self.conn.execute("SELECT state FROM ownership_structure_classifications WHERE namespace=? AND "
                                     "classification_id=?", [namespace, classification_id]).fetchone()
        if existing:
            return {**self.get(namespace, classification_id, scopes=scopes), "created": False}
        stamp = self.now()
        self.conn.execute("INSERT INTO ownership_structure_classifications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, classification_id, entity_key, structure_type, basis, stated_value.strip(),
                           canonical(checked), "proposed", None,
                           canonical([{"state": "proposed", "by": principal_id, "at_ms": stamp}]), principal_id, stamp])
        return {**self.get(namespace, classification_id, scopes=scopes), "created": True}

    def propose_from_records(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Offer proposals where an entity record's stated legal form equals a :data:`STATED_FORMS` entry exactly."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        proposed, offered = [], 0
        for view in self.store.records(namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",)):
            if view.get("redacted"):
                continue
            body = view["record"]
            for form in STATED_FORMS:
                value = _at(body, form["field"])
                if not _states(value, form["value"]):
                    continue
                offered += 1
                result = self.propose(namespace, entity_key=body["record_key"], structure_type=form["structure_type"],
                                      basis=form["basis"], stated_value=str(value),
                                      citation={"kind": "ownership-record", "record_id": view["record_id"],
                                                "revision": view["revision"], "field": form["field"]},
                                      principal_id=principal_id, scopes=scopes)
                if result["created"]:
                    proposed.append(result["classification_id"])
        return {"proposed": sorted(proposed), "offered": offered,
                "note": "proposals only, from stated legal forms equal to a listed vocabulary entry; a reviewer "
                        "accepts or rejects each; names are never read"}

    # ------------------------------------------------------------ decisions

    def _transition(self, namespace: str, classification_id: str, state: str, reason: str, principal_id: str,
                    scopes: Iterable[str], allowed: tuple[str, ...]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise OwnershipError("invalid_decision", "a decision needs a reason")
        current = self.get(namespace, classification_id, scopes=scopes)
        if current["state"] not in allowed:
            raise OwnershipError("invalid_state", f"classification is {current['state']}")
        stamp = self.now()
        decision = {"state": state, "by": principal_id, "reason": reason.strip(), "at_ms": stamp,
                    "decision_id": "own-struct-dec:" + digest([classification_id, state, principal_id, stamp])[:24]}
        history = current["history"] + [decision]
        self.conn.execute("UPDATE ownership_structure_classifications SET state=?, decision_json=?, history_json=? "
                          "WHERE namespace=? AND classification_id=?",
                          [state, canonical(decision), canonical(history), namespace, classification_id])
        return self.get(namespace, classification_id, scopes=scopes)

    def review(self, namespace: str, classification_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept or reject a proposed classification (``knowledge:ownership:review``)."""
        if decision not in {"accept", "reject"}:
            raise OwnershipError("invalid_decision", "accept or reject with a reason")
        return self._transition(namespace, classification_id, "accepted" if decision == "accept" else "rejected",
                                reason, principal_id, scopes, ("proposed",))

    def revert(self, namespace: str, classification_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Undo an accepted or rejected decision; the entity is unclassified again unless another one stands."""
        return self._transition(namespace, classification_id, "reverted", reason, principal_id, scopes,
                                ("accepted", "rejected"))

    # ------------------------------------------------------------ reads

    def get(self, namespace: str, classification_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute("SELECT " + ", ".join(_COLUMNS) + " FROM ownership_structure_classifications "
                                "WHERE namespace=? AND classification_id=?", [namespace, classification_id]).fetchone() \
            if self.ready() else None
        if row is None:
            raise OwnershipError("not_found", "classification is not visible in this namespace")
        return _view(namespace, row)

    def list(self, namespace: str, *, scopes: Iterable[str], entity_keys: Iterable[str] | None = None,
             state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute("SELECT " + ", ".join(_COLUMNS) + " FROM ownership_structure_classifications "
                                 "WHERE namespace=? AND (? IS NULL OR state=?) ORDER BY entity_key, classification_id",
                                 [namespace, state, state]).fetchall()
        keys = None if entity_keys is None else set(entity_keys)
        return [_view(namespace, r) for r in rows if keys is None or r[1] in keys]

    def classification(self, namespace: str, entity_keys: Iterable[str], *, scopes: Iterable[str]) -> dict[str, Any]:
        """The accepted structure types of an entity's records, side by side; ``unclassified`` without one."""
        items = self.list(namespace, scopes=scopes, entity_keys=entity_keys)
        accepted = [i for i in items if i["state"] == "accepted"]
        types = sorted({i["structure_type"] for i in accepted})
        return {"status": "classified" if accepted else "unclassified", "structure_types": types,
                "differ": len(types) > 1, "accepted": accepted,
                "pending": [i for i in items if i["state"] == "proposed"],
                "not_in_force": [{"classification_id": i["classification_id"], "state": i["state"],
                                  "structure_type": i["structure_type"]} for i in items
                                 if i["state"] in {"rejected", "reverted"}],
                "notice": NOTICE}
