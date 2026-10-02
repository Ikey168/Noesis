"""Places and related indicators across income sources through reviewable identity (IP06, #2613).

**Places.** Every area a series states - ISO 3166-1 alpha-3 (PIP, OECD), Eurostat GEO codes (EU-SILC: ISO alpha-2
except ``EL``/``UK``, NUTS codes for regions) and World Bank region codes (PIP aggregates) - is offered to the
Geospatial places the platform already holds (:class:`src.kb.geospatial.GeospatialStore`; no new place store),
matching by **published identifiers first**:

* ``published-code`` - a place whose source identifiers carry the same code under the scheme's key
  (``iso3166-1-alpha3``, ``nuts``, ``wb-region``);
* ``iso-alpha2-equivalent`` - a two-letter Eurostat GEO code read as ISO 3166-1 alpha-2 (``EL`` is ``GR``, ``UK``
  is ``GB``).

Names are never a match: a place whose name equals the published label is listed as ``name_context`` only. A code
with exactly one candidate is ``proposed`` with confidence ``high``; several candidates make it ``ambiguous`` (a
reviewer chooses one); none leaves it **unmatched**, visible through :meth:`IncomeIdentity.unmatched`.

**Related indicators.** The same concept for the same place from different sources (a PIP headcount, an EU-SILC
at-risk-of-poverty rate, an OECD poverty rate) is proposed as ``related`` with the recorded differences of the two
keys (welfare concept, equivalence scale, line, survey, methodology). Related is never merged: each series keeps its
own values, and nothing is combined or re-harmonised.

Every assertion carries method, evidence and confidence and is ``proposed`` (or ``ambiguous``), then ``accepted`` or
``rejected`` by another principal and can be ``reverted``; accepted and rejected decisions are recorded as
:class:`src.kb.entity_history.EntityHistoryStore` ``match``/``non-match`` decisions (``merge: false``) and reverts
undo them. Nothing is auto-accepted; queries use accepted place matches only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.income_distribution_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    IncomeError,
    authorize,
    canonical,
    comparability_basis,
    digest,
    iso,
    load,
    table_exists,
)
from src.kb.income_distribution_store import IncomeDistributionStore

CONTRACT = "noesis-income-identity-assertion-v1"
KINDS = ("place", "related_indicator")
STATES = ("proposed", "ambiguous", "accepted", "rejected", "reverted")
PLACE_KEYS = {"iso3166-1-alpha3": "iso3166-1-alpha3", "wb-region": "wb-region"}
EUROSTAT_ISO2 = {"EL": "GR", "UK": "GB"}
_ENTITY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                  "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS income_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  subject_json TEXT NOT NULL, target_json TEXT, candidates_json TEXT NOT NULL, method TEXT, confidence TEXT,
  evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT, history_json TEXT NOT NULL,
  proposed_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, assertion_id)
);
"""


def area_key(area: Mapping[str, Any]) -> str:
    return f"area:{area['scheme']}:{area['code']}"


def _entity_id(key: str) -> str:
    return "ent-inc-" + re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:280]


class IncomeIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.store = IncomeDistributionStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.history = EntityHistoryStore(conn, initialize=initialize, now=now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ subjects

    def areas(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "income_series"):
            return []
        found: dict[str, dict[str, Any]] = {}
        for (area,) in self.conn.execute("SELECT area_json FROM income_series WHERE namespace=? ORDER BY series_id",
                                         [namespace]).fetchall():
            area = json.loads(area)
            entry = found.setdefault(area_key(area), {"scheme": area["scheme"], "code": str(area["code"]),
                                                      "labels": set()})
            if area.get("label"):
                entry["labels"].add(area["label"])
        return [{**v, "labels": sorted(v["labels"])} for _, v in sorted(found.items())]

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
        wanted: list[tuple[str, str, str]] = []
        if scheme in PLACE_KEYS:
            wanted.append((PLACE_KEYS[scheme], code, "published-code"))
        elif scheme == "eurostat-geo":
            if len(code) == 2:
                wanted.append(("iso3166-1-alpha2", EUROSTAT_ISO2.get(code, code), "iso-alpha2-equivalent"))
            else:
                wanted.append(("nuts", code, "published-code"))
        out = []
        for place in places:
            for key, value, method in wanted:
                if place["source_ids"].get(key) == value:
                    out.append({"place_id": place["place_id"], "place_revision_id": place["revision_id"],
                                "name": place["name"], "identifier": {"key": key, "value": value}, "method": method})
        return out

    # ------------------------------------------------------------------ proposals

    def _row(self, namespace: str, assertion_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT assertion_id, kind, subject_key, subject_json, target_json, candidates_json, method, confidence, "
            "evidence_json, state, decision_id, history_json, proposed_by FROM income_identity_assertions WHERE "
            "namespace=? AND assertion_id=?", [namespace, assertion_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "no such identity assertion")
        return {"contract": CONTRACT, "assertion_id": row[0], "kind": row[1], "subject_key": row[2],
                "subject": load(row[3], {}), "target": load(row[4], None), "candidates": load(row[5], []),
                "method": row[6], "confidence": row[7], "evidence": load(row[8], {}), "state": row[9],
                "decision_id": row[10], "history": load(row[11], []), "proposed_by": row[12],
                "notice": "an assertion links records by review; nothing is merged and each series keeps its values"}

    def _insert(self, namespace, kind, subject_key, subject, target, candidates, method, confidence, evidence, state,
                principal_id) -> tuple[dict[str, Any], bool]:
        assertion_id = "inc-id:" + digest([namespace, kind, subject_key, target, candidates])[:24]
        if self.conn.execute("SELECT 1 FROM income_identity_assertions WHERE namespace=? AND assertion_id=?",
                             [namespace, assertion_id]).fetchone():
            return self._row(namespace, assertion_id), False
        self.conn.execute(
            "INSERT INTO income_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, assertion_id, kind, subject_key, canonical(subject), canonical(target), canonical(candidates),
             method, confidence, canonical(evidence), state, None,
             canonical([{"state": state, "by": principal_id, "at": iso(self.now())}]), principal_id, self.now()])
        return self._row(namespace, assertion_id), True

    def propose_places(self, namespace: str, *, geo_namespace: str = "geo", principal_id: str,
                       scopes: Iterable[str]) -> dict[str, Any]:
        """Propose a place match per area code by published identifier; never accepted here."""
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
                                  "reason": "no place carries this published identifier"})
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
                "policy": "published identifiers before names; nothing is accepted without review"}

    def propose_related(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """The same concept for the same place from different sources, as related assertions (never merged)."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        series = self.store.find_series(namespace)
        places = {}
        for item in series:
            match = self.place_for_area(namespace, item["area"]["scheme"], item["area"]["code"])
            places[item["series_id"]] = match["place_id"] if match else None
        proposed = []
        for index, left in enumerate(series):
            for right in series[index + 1:]:
                if left["provider"] == right["provider"] or left["indicator"]["concept"] != right["indicator"]["concept"]:
                    continue
                same_place = places[left["series_id"]] and places[left["series_id"]] == places[right["series_id"]]
                if not same_place:
                    continue
                pair = sorted([left["series_id"], right["series_id"]])
                differences = comparability_basis(left["key"], right["key"])
                row, _ = self._insert(
                    namespace, "related_indicator", "related:" + ":".join(pair),
                    {"series_ids": pair, "concept": left["indicator"]["concept"]},
                    {"kind": "income-series", "series_id": pair[1]}, [], "same-concept-same-accepted-place",
                    "related-not-equal", {"place_id": places[left["series_id"]], "differences": differences,
                                          "merge": False}, "proposed", principal_id)
                proposed.append(row)
        return {"proposed": proposed, "policy": "related indicators are never merged, blended or re-harmonised"}

    # ------------------------------------------------------------------ review

    def _transition(self, namespace, assertion, state, decision_id, principal_id, reason, target=None):
        history = assertion["history"] + [{"state": state, "by": principal_id, "reason": reason,
                                           "at": iso(self.now()), "decision_id": decision_id}]
        self.conn.execute(
            "UPDATE income_identity_assertions SET state=?, decision_id=?, history_json=?, target_json=? WHERE "
            "namespace=? AND assertion_id=?",
            [state, decision_id, canonical(history), canonical(target if target is not None else assertion["target"]),
             namespace, assertion["assertion_id"]])
        return self._row(namespace, assertion["assertion_id"])

    def review(self, namespace: str, assertion_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str], place_id: str | None = None) -> dict[str, Any]:
        """Accept (``match``) or reject (``non-match``); an ambiguous one is accepted by choosing a cited candidate."""
        authorize(namespace, scopes, REVIEW_SCOPE)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise IncomeError("invalid_decision", "accept or reject with a reason")
        assertion = self._row(namespace, assertion_id)
        if assertion["state"] not in {"proposed", "ambiguous"}:
            raise IncomeError("invalid_state", f"assertion is {assertion['state']}")
        if assertion["proposed_by"] == principal_id:
            raise IncomeError("self_review", "the proposer cannot review their own assertion")
        target = assertion["target"]
        if assertion["state"] == "ambiguous" and decision == "accept":
            chosen = next((c for c in assertion["candidates"] if c["place_id"] == place_id), None)
            if chosen is None:
                raise IncomeError("invalid_decision", "choose one of the cited candidates (place_id)")
            target = {"kind": "geospatial-place", **chosen}
        target_key = f"{target['kind']}:{target.get('place_id') or target.get('series_id')}" if target else "none"
        left, right = _entity_id(assertion["subject_key"]), _entity_id(target_key)
        for entity, alias in ((left, assertion["subject_key"]), (right, target_key)):
            self.history.register_entity(namespace, entity, [alias], principal_id=principal_id, scopes=_ENTITY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [left, right],
            {"assertion_id": assertion_id, "method": assertion["method"], "confidence": assertion["confidence"],
             "evidence": assertion["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "society.income"},
             "policy": {"merge": False, "note": "identity assertion only; series stay as published"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_SCOPES,
            event_key=f"income-identity:{namespace}:{assertion_id}:{len(assertion['history'])}")
        return self._transition(namespace, assertion, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip(), target)

    def revert(self, namespace: str, assertion_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE)
        if not str(reason or "").strip():
            raise IncomeError("invalid_decision", "a revert needs a reason")
        assertion = self._row(namespace, assertion_id)
        if assertion["state"] not in {"accepted", "rejected"}:
            raise IncomeError("invalid_state", "only an accepted or rejected assertion can be reverted")
        undo = self.history.undo(namespace, assertion["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_SCOPES)
        return self._transition(namespace, assertion, "reverted", undo["decision_id"], principal_id, reason.strip())

    # ------------------------------------------------------------------ reads

    def assertions(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "income_identity_assertions"):
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM income_identity_assertions WHERE namespace=? AND (? IS NULL OR kind=?) AND "
            "(? IS NULL OR state=?) ORDER BY assertion_id", [namespace, kind, kind, state, state]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def place_for_area(self, namespace: str, scheme: str, code: str) -> dict[str, Any] | None:
        """The accepted place of an area code (``None`` when none is accepted)."""
        if not table_exists(self.conn, "income_identity_assertions"):
            return None
        row = self.conn.execute(
            "SELECT assertion_id FROM income_identity_assertions WHERE namespace=? AND kind='place' AND subject_key=? "
            "AND state='accepted' ORDER BY created_at_ms DESC LIMIT 1",
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

    def related(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "income_identity_assertions"):
            return []
        return [a for a in self.assertions(namespace, scopes={"operator"}, kind="related_indicator")
                if series_id in a["subject"]["series_ids"] and a["state"] in {"proposed", "accepted"}]

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Areas with no accepted place match, with the state of any open assertion."""
        authorize(namespace, scopes, READ_SCOPE)
        out = []
        for area in self.areas(namespace):
            if self.place_for_area(namespace, area["scheme"], area["code"]):
                continue
            open_rows = self.conn.execute(
                "SELECT state FROM income_identity_assertions WHERE namespace=? AND kind='place' AND subject_key=? "
                "ORDER BY created_at_ms", [namespace, area_key(area)]).fetchall() \
                if table_exists(self.conn, "income_identity_assertions") else []
            out.append({"scheme": area["scheme"], "code": area["code"], "labels": area["labels"],
                        "assertion_states": [r[0] for r in open_rows]})
        return out


__all__ = ["CONTRACT", "IncomeIdentity", "area_key"]
