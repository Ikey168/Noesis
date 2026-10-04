"""Places and functions across social protection sources through reviewable identity (#2741, SS06).

**Places.** Every area a series states - Eurostat GEO codes (ESSPROS: ISO 3166-1 alpha-2 except ``EL``/``UK``) and
ISO 3166-1 alpha-3 (SOCX, ILOSTAT) - is offered to the Geospatial places the platform already holds
(:class:`src.kb.geospatial.GeospatialStore`, the place identity ``society.income`` uses; no new place store), matching
by **published ISO codes before names**:

* ``published-code`` - a place whose source identifiers carry the same alpha-3 code;
* ``iso-alpha2-equivalent`` - a two-letter Eurostat GEO code read as ISO 3166-1 alpha-2 (``EL`` is ``GR``, ``UK``
  is ``GB``).

A place whose name equals the published label is listed as ``name_context`` only. One candidate is ``proposed`` with
confidence ``high``; several make it ``ambiguous`` (a reviewer chooses one); none leaves the code **unmatched**,
visible through :meth:`SocialProtectionIdentity.unmatched`.

**Functions.** An ESSPROS function (``spfunc``) or pension category, a SOCX policy area and an ILO contingency are
each publisher's own classification. One may be recorded as **related** to another publisher's - proposed by a
principal with cited evidence, or proposed because two publishers state the same label (confidence ``low``) - and
accepted only by another principal. Related is never equal: nothing is re-classified or merged and every series keeps
its own function. COFOG (``economics.public-finance``) stays a third, distinct concept and is refused as a function
relation; it is reached only through SS07 citation links.

Every assertion carries method, evidence and confidence and is ``proposed`` (or ``ambiguous``), then ``accepted`` or
``rejected`` by another principal and can be ``reverted``; decisions are recorded as
:class:`src.kb.entity_history.EntityHistoryStore` ``match``/``non-match`` decisions (``merge: false``) and reverts
undo them. Nothing is auto-accepted; queries use accepted assertions only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.social_protection_sources import COFOG_SCHEME, FUNCTION_SCHEMES
from src.kb.social_protection_records import (
    PROVIDER_ID,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    SocialProtectionError,
    authorize,
    canonical,
    digest,
    iso,
    load,
    table_exists,
)
from src.kb.social_protection_store import SocialProtectionStore

CONTRACT = "noesis-social-protection-identity-assertion-v1"
KINDS = ("place", "function")
STATES = ("proposed", "ambiguous", "accepted", "rejected", "reverted")
EUROSTAT_ISO2 = {"EL": "GR", "UK": "GB"}
SCHEME_PUBLISHER = {scheme: provider for provider, schemes in FUNCTION_SCHEMES.items() for scheme in schemes}
_ENTITY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                  "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS social_protection_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  subject_json TEXT NOT NULL, target_json TEXT, candidates_json TEXT NOT NULL, method TEXT, confidence TEXT,
  evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT, history_json TEXT NOT NULL,
  proposed_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, assertion_id)
);
"""


def area_key(area: Mapping[str, Any]) -> str:
    return f"area:{area['scheme']}:{area['code']}"


def function_key(function: Mapping[str, Any]) -> str:
    return f"function:{function['scheme']}:{function['code']}"


def _entity_id(key: str) -> str:
    return "ent-sp-" + re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:280]


class SocialProtectionIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.store = SocialProtectionStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.history = EntityHistoryStore(conn, initialize=initialize, now=now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ subjects

    def _distinct(self, namespace: str, column: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "social_protection_series"):
            return []
        return [json.loads(r[0]) for r in self.conn.execute(
            f"SELECT DISTINCT {column} FROM social_protection_series WHERE namespace=? ORDER BY 1",
            [namespace]).fetchall()]

    def areas(self, namespace: str) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for area in self._distinct(namespace, "area_json"):
            entry = found.setdefault(area_key(area), {"scheme": area["scheme"], "code": str(area["code"]),
                                                      "labels": set()})
            if area.get("label"):
                entry["labels"].add(area["label"])
        return [{**v, "labels": sorted(v["labels"])} for _, v in sorted(found.items())]

    def functions(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "social_protection_series"):
            return []
        found: dict[str, dict[str, Any]] = {}
        for scheme, code, provider, fields in self.conn.execute(
                "SELECT function_scheme, function_code, provider, fields_json FROM social_protection_series WHERE "
                "namespace=? ORDER BY series_id", [namespace]).fetchall():
            label = dict(json.loads(fields).get("function") or {}).get("label")
            found.setdefault(function_key({"scheme": scheme, "code": code}),
                             {"scheme": scheme, "code": code, "label": label, "publisher": provider})
        return [v for _, v in sorted(found.items())]

    def _places(self, geo_namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT p.place_id, r.revision_id, r.canonical_name, r.place_type, r.source_ids_json FROM "
            "geospatial_places p JOIN geospatial_place_current c ON c.place_id=p.place_id JOIN "
            "geospatial_place_revisions r ON r.revision_id=c.revision_id WHERE p.namespace IN (?, 'global') "
            "ORDER BY p.place_id", [geo_namespace]).fetchall()
        return [{"place_id": r[0], "revision_id": r[1], "name": r[2], "place_type": r[3],
                 "source_ids": {str(k): str(v) for k, v in json.loads(r[4] or "{}").items()}} for r in rows]

    @staticmethod
    def _candidates(area: Mapping[str, Any], places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scheme, code = area["scheme"], str(area["code"])
        if scheme == "iso3166-1-alpha3":
            wanted = ("iso3166-1-alpha3", code, "published-code")
        elif scheme == "eurostat-geo" and len(code) == 2:
            wanted = ("iso3166-1-alpha2", EUROSTAT_ISO2.get(code, code), "iso-alpha2-equivalent")
        else:
            return []
        key, value, method = wanted
        return [{"place_id": p["place_id"], "place_revision_id": p["revision_id"], "name": p["name"],
                 "identifier": {"key": key, "value": value}, "method": method}
                for p in places if p["source_ids"].get(key) == value]

    # ------------------------------------------------------------------ proposals

    def _row(self, namespace: str, assertion_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT assertion_id, kind, subject_key, subject_json, target_json, candidates_json, method, confidence, "
            "evidence_json, state, decision_id, history_json, proposed_by FROM social_protection_identity_assertions "
            "WHERE namespace=? AND assertion_id=?", [namespace, assertion_id]).fetchone()
        if row is None:
            raise SocialProtectionError("not_found", "no such identity assertion")
        return {"contract": CONTRACT, "assertion_id": row[0], "kind": row[1], "subject_key": row[2],
                "subject": load(row[3], {}), "target": load(row[4], None), "candidates": load(row[5], []),
                "method": row[6], "confidence": row[7], "evidence": load(row[8], {}), "state": row[9],
                "decision_id": row[10], "history": load(row[11], []), "proposed_by": row[12],
                "notice": "an assertion relates records by review; nothing is merged or re-classified and each "
                          "series keeps its own place code and function"}

    def _insert(self, namespace, kind, subject_key, subject, target, candidates, method, confidence, evidence, state,
                principal_id) -> tuple[dict[str, Any], bool]:
        assertion_id = "sp-id:" + digest([namespace, kind, subject_key, target, candidates])[:24]
        if self.conn.execute("SELECT 1 FROM social_protection_identity_assertions WHERE namespace=? AND "
                             "assertion_id=?", [namespace, assertion_id]).fetchone():
            return self._row(namespace, assertion_id), False
        self.conn.execute(
            "INSERT INTO social_protection_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, assertion_id, kind, subject_key, canonical(subject), canonical(target), canonical(candidates),
             method, confidence, canonical(evidence), state, None,
             canonical([{"state": state, "by": principal_id, "at": iso(self.now())}]), principal_id, self.now()])
        return self._row(namespace, assertion_id), True

    def propose_places(self, namespace: str, *, geo_namespace: str = "geo", principal_id: str,
                       scopes: Iterable[str]) -> dict[str, Any]:
        """Propose a place match per area code by published ISO code; never accepted here."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        places = self._places(geo_namespace)
        proposed, ambiguous, unmatched = [], [], []
        for area in self.areas(namespace):
            candidates = self._candidates(area, places)
            context = [{"place_id": p["place_id"], "name": p["name"]} for p in places
                       if p["name"].casefold() in {label.casefold() for label in area["labels"]}
                       and p["place_id"] not in {c["place_id"] for c in candidates}]
            subject = {"scheme": area["scheme"], "code": area["code"], "labels": area["labels"]}
            if not candidates:
                unmatched.append({**subject, "name_context": context,
                                  "reason": "no place carries this published ISO code"})
                continue
            target = {"kind": "geospatial-place", **candidates[0]} if len(candidates) == 1 else None
            evidence = {"identifier": candidates[0]["identifier"] if target else None, "name_context": context,
                        "names_used": False}
            row, _ = self._insert(namespace, "place", area_key(area), subject, target, candidates,
                                  candidates[0]["method"] if target else "published-code",
                                  "high" if target else "ambiguous", evidence,
                                  "proposed" if target else "ambiguous", principal_id)
            (proposed if target else ambiguous).append(row)
        return {"proposed": proposed, "ambiguous": ambiguous, "unmatched": unmatched,
                "policy": "published ISO codes before names; nothing is accepted without review"}

    def _known_function(self, namespace: str, function: Mapping[str, Any]) -> dict[str, Any]:
        scheme = str(function.get("scheme") or "")
        if scheme == COFOG_SCHEME:
            raise SocialProtectionError(
                "distinct_concept", "COFOG social protection (economics.public-finance) is a third, distinct concept: "
                                    "it is never related to or merged with an ESSPROS function, SOCX branch or ILO "
                                    "contingency; use citation links instead")
        known = next((f for f in self.functions(namespace)
                      if f["scheme"] == scheme and f["code"] == str(function.get("code"))), None)
        if known is None:
            raise SocialProtectionError("not_found", f"no held series states {scheme} {function.get('code')}")
        return known

    def propose_function_relation(self, namespace: str, left: Mapping[str, Any], right: Mapping[str, Any], *,
                                  cited: Sequence[Mapping[str, Any]], statement: str, principal_id: str,
                                  scopes: Iterable[str]) -> dict[str, Any]:
        """Record that one publisher's function is related to another publisher's (cited; never a re-classification)."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        a, b = self._known_function(namespace, left), self._known_function(namespace, right)
        if a["publisher"] == b["publisher"]:
            raise SocialProtectionError("invalid_relation", "functions are related across publishers only; a "
                                                            "publisher's own hierarchy is its own")
        if not cited or not str(statement or "").strip():
            raise SocialProtectionError("invalid_relation", "a function relation cites the published evidence it "
                                                            "rests on and states the relation")
        return self._function_assertion(namespace, a, b, "reviewer-stated", "medium",
                                        {"statement": statement.strip(), "cited": [dict(c) for c in cited]},
                                        principal_id)

    def _function_assertion(self, namespace, a, b, method, confidence, evidence, principal_id):
        pair = sorted([a, b], key=function_key)
        subject = {"functions": [{k: f[k] for k in ("scheme", "code", "label", "publisher")} for f in pair]}
        target = {"kind": "related-function", "scheme": pair[1]["scheme"], "code": pair[1]["code"]}
        row, _ = self._insert(namespace, "function", "related:" + ":".join(function_key(f) for f in pair), subject,
                              target, [], method, confidence,
                              {**evidence, "relation": "related", "merge": False, "reclassify": False}, "proposed",
                              principal_id)
        return row

    def propose_function_relations(self, namespace: str, *, principal_id: str,
                                   scopes: Iterable[str]) -> dict[str, Any]:
        """Propose functions of different publishers that state the same published label (confidence low)."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        functions = self.functions(namespace)
        proposed = []
        for index, left in enumerate(functions):
            for right in functions[index + 1:]:
                if left["publisher"] == right["publisher"] or not left.get("label") or \
                        str(left["label"]).casefold() != str(right.get("label") or "").casefold():
                    continue
                proposed.append(self._function_assertion(
                    namespace, left, right, "same-published-label", "low",
                    {"labels": [left["label"], right["label"]],
                     "caveat": "the same label does not mean the same scope; a reviewer decides"}, principal_id))
        return {"proposed": proposed, "unrelated": self.unrelated_functions(namespace),
                "policy": "related functions are never merged or re-classified; COFOG stays a distinct concept"}

    # ------------------------------------------------------------------ review

    def _transition(self, namespace, assertion, state, decision_id, principal_id, reason, target=None):
        history = assertion["history"] + [{"state": state, "by": principal_id, "reason": reason,
                                           "at": iso(self.now()), "decision_id": decision_id}]
        self.conn.execute(
            "UPDATE social_protection_identity_assertions SET state=?, decision_id=?, history_json=?, target_json=? "
            "WHERE namespace=? AND assertion_id=?",
            [state, decision_id, canonical(history), canonical(target if target is not None else assertion["target"]),
             namespace, assertion["assertion_id"]])
        return self._row(namespace, assertion["assertion_id"])

    def review(self, namespace: str, assertion_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str], place_id: str | None = None) -> dict[str, Any]:
        """Accept (``match``) or reject (``non-match``); an ambiguous one is accepted by choosing a cited candidate."""
        authorize(namespace, scopes, REVIEW_SCOPE)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise SocialProtectionError("invalid_decision", "accept or reject with a reason")
        assertion = self._row(namespace, assertion_id)
        if assertion["state"] not in {"proposed", "ambiguous"}:
            raise SocialProtectionError("invalid_state", f"assertion is {assertion['state']}")
        if assertion["proposed_by"] == principal_id:
            raise SocialProtectionError("self_review", "the proposer cannot review their own assertion")
        target = assertion["target"]
        if assertion["state"] == "ambiguous" and decision == "accept":
            chosen = next((c for c in assertion["candidates"] if c["place_id"] == place_id), None)
            if chosen is None:
                raise SocialProtectionError("invalid_decision", "choose one of the cited candidates (place_id)")
            target = {"kind": "geospatial-place", **chosen}
        if assertion["kind"] == "place":
            target_key = f"geospatial-place:{target['place_id']}" if target else "none"
            subject_key = assertion["subject_key"]
        else:
            first, second = assertion["subject"]["functions"]
            subject_key, target_key = function_key(first), function_key(second)
        left, right = _entity_id(subject_key), _entity_id(target_key)
        for entity, alias in ((left, subject_key), (right, target_key)):
            self.history.register_entity(namespace, entity, [alias], principal_id=principal_id, scopes=_ENTITY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [left, right],
            {"assertion_id": assertion_id, "method": assertion["method"], "confidence": assertion["confidence"],
             "evidence": assertion["evidence"], "reason": reason.strip(), "provenance": {"producer": PROVIDER_ID},
             "policy": {"merge": False, "note": "identity assertion only; series and functions stay as published"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_SCOPES,
            event_key=f"social-protection-identity:{namespace}:{assertion_id}:{len(assertion['history'])}")
        return self._transition(namespace, assertion, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip(), target)

    def revert(self, namespace: str, assertion_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE)
        if not str(reason or "").strip():
            raise SocialProtectionError("invalid_decision", "a revert needs a reason")
        assertion = self._row(namespace, assertion_id)
        if assertion["state"] not in {"accepted", "rejected"}:
            raise SocialProtectionError("invalid_state", "only an accepted or rejected assertion can be reverted")
        undo = self.history.undo(namespace, assertion["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_SCOPES)
        return self._transition(namespace, assertion, "reverted", undo["decision_id"], principal_id, reason.strip())

    # ------------------------------------------------------------------ reads

    def assertions(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "social_protection_identity_assertions"):
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM social_protection_identity_assertions WHERE namespace=? AND (? IS NULL OR "
            "kind=?) AND (? IS NULL OR state=?) ORDER BY assertion_id", [namespace, kind, kind, state, state]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def place_for_area(self, namespace: str, scheme: str, code: str) -> dict[str, Any] | None:
        """The accepted place of an area code (``None`` when none is accepted)."""
        if not table_exists(self.conn, "social_protection_identity_assertions"):
            return None
        row = self.conn.execute(
            "SELECT assertion_id FROM social_protection_identity_assertions WHERE namespace=? AND kind='place' AND "
            "subject_key=? AND state='accepted' ORDER BY created_at_ms DESC LIMIT 1",
            [namespace, area_key({"scheme": scheme, "code": code})]).fetchone()
        if row is None:
            return None
        assertion = self._row(namespace, row[0])
        return {**assertion["target"], "assertion_id": assertion["assertion_id"]}

    def areas_for_place(self, namespace: str, place_id: str) -> list[dict[str, Any]]:
        """Every area code whose accepted match is this place (the codes a place query reads)."""
        out = []
        for area in self.areas(namespace):
            match = self.place_for_area(namespace, area["scheme"], area["code"])
            if match and match["place_id"] == place_id:
                out.append({"scheme": area["scheme"], "code": area["code"], "assertion_id": match["assertion_id"]})
        return out

    def related_functions(self, namespace: str, scheme: str, code: str) -> list[dict[str, Any]]:
        """Other publishers' functions accepted as related to this one (with the assertion that says so)."""
        if not table_exists(self.conn, "social_protection_identity_assertions"):
            return []
        out = []
        for assertion in self.assertions(namespace, scopes={"operator"}, kind="function", state="accepted"):
            functions = assertion["subject"]["functions"]
            if not any(f["scheme"] == scheme and f["code"] == code for f in functions):
                continue
            other = next(f for f in functions if not (f["scheme"] == scheme and f["code"] == code))
            out.append({**other, "assertion_id": assertion["assertion_id"], "relation": "related"})
        return out

    def unrelated_functions(self, namespace: str) -> list[dict[str, Any]]:
        """Functions with no accepted relation to another publisher's (they stay queryable natively)."""
        return [f for f in self.functions(namespace)
                if not self.related_functions(namespace, f["scheme"], f["code"])]

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Areas with no accepted place match, with the state of any open assertion."""
        authorize(namespace, scopes, READ_SCOPE)
        out = []
        for area in self.areas(namespace):
            if self.place_for_area(namespace, area["scheme"], area["code"]):
                continue
            open_rows = self.conn.execute(
                "SELECT state FROM social_protection_identity_assertions WHERE namespace=? AND kind='place' AND "
                "subject_key=? ORDER BY created_at_ms", [namespace, area_key(area)]).fetchall() \
                if table_exists(self.conn, "social_protection_identity_assertions") else []
            out.append({"scheme": area["scheme"], "code": area["code"], "labels": area["labels"],
                        "assertion_states": [r[0] for r in open_rows]})
        return out


__all__ = ["CONTRACT", "KINDS", "SCHEME_PUBLISHER", "SocialProtectionIdentity", "area_key", "function_key"]
