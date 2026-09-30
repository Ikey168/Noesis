"""Places and related indicators across income sources through reviewable identity (#2583, IP06).

**Places.** Every area code an income series states - ISO 3166-1 alpha-3 (PIP countries, OECD), Eurostat GEO codes
(EU-SILC countries and NUTS regions) and World Bank region codes (PIP aggregates) - is offered to the Geospatial
places the platform already holds (:class:`src.kb.geospatial.GeospatialStore`; no new place store), by the published
code only, before any name:

* ``published-code`` - a place whose source identifiers carry the same code under the scheme's key (``nuts`` for a
  Eurostat NUTS code, ``wb-region`` for a World Bank region);
* ``iso-alpha2-equivalent`` - a two-letter Eurostat GEO code read as ISO 3166-1 alpha-2 (``EL`` is ``GR``).

A code whose candidates name more than one place is ``ambiguous`` (a reviewer chooses one cited candidate); a code
with none stays ``unmatched`` and visible as such. Names equal to a place label are context, never a match.

**Related indicators.** The same indicator concept published by two sources for the same place (by an accepted
place mapping or the same published code) is proposed as ``related``, citing both series, their definitions and the
recorded differences (welfare concept, equivalence scale, poverty line, PPP round, survey). An accepted relation
only lets answers show the series beside each other; nothing is ever merged, averaged or re-harmonised.

Every assertion is ``proposed`` (or ``ambiguous``/``unmatched``), then ``accepted`` or ``rejected`` by a reviewer
other than its proposer, with reviewer, reason and time recorded, and can be ``reverted`` (the pattern of
:mod:`src.kb.labour_identity` and the reversible decisions of :mod:`src.kb.entity_history`). Queries use accepted
assertions only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from itertools import combinations
from typing import Any

from src.kb.income_distribution_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    IncomeError,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)
from src.kb.income_distribution_store import IncomeStore, comparability_basis

CONTRACT = "noesis-income-identity-v1"
GEO_READ = "knowledge:geospatial:read"
KINDS = ("area", "related")
PLACE_KEYS = {
    "iso3166-1-alpha3": "iso3166-1-alpha3",
    "iso3166-1-alpha2": "iso3166-1-alpha2",
    "eurostat-geo": "nuts",
    "wb-region": "wb-region",
}
EUROSTAT_ISO2 = {"EL": "GR", "UK": "GB"}
RULES = {
    "published-code": "the place carries the published code",
    "iso-alpha2-equivalent": "Eurostat GEO country code read as ISO 3166-1 alpha-2 (EL=GR, UK=GB)",
}
_DDL = """
CREATE TABLE IF NOT EXISTS income_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  subject_json TEXT NOT NULL, target_json TEXT, method TEXT, evidence_json TEXT NOT NULL, state TEXT NOT NULL,
  reason TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, assertion_id)
);
"""


class IncomeIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = IncomeStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ subjects

    def _areas(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "income_series"):
            return []
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for (area,) in self.conn.execute("SELECT area_json FROM income_series WHERE namespace=? ORDER BY series_id",
                                         [namespace]).fetchall():
            area = json.loads(area)
            entry = found.setdefault((area["scheme"], str(area["code"])), {**area, "labels": set()})
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

    def _canonical_names(self, labels: list[str]) -> list[dict[str, Any]]:
        if not labels or not table_exists(self.conn, "canonical_entities"):
            return []
        wanted = {label.casefold() for label in labels}
        return [{"canonical_id": r[0], "preferred_name": r[1], "entity_type": r[2],
                 "note": "equal name only; context, never an accepted match on its own"}
                for r in self.conn.execute(
                    "SELECT canonical_id, preferred_name, entity_type FROM canonical_entities ORDER BY canonical_id"
                ).fetchall() if str(r[1]).casefold() in wanted]

    @staticmethod
    def _candidates(area: Mapping[str, Any], places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scheme, code = area["scheme"], str(area["code"])
        rules: list[tuple[str, str, str]] = []
        if scheme in PLACE_KEYS:
            rules.append(("published-code", PLACE_KEYS[scheme], code))
        if scheme == "eurostat-geo" and len(code) == 2:
            rules.append(("iso-alpha2-equivalent", "iso3166-1-alpha2", EUROSTAT_ISO2.get(code, code)))
        out = []
        for place in places:
            for method, key, value in rules:
                if place["source_ids"].get(key) == value:
                    out.append({"place_id": place["place_id"], "place_revision_id": place["revision_id"],
                                "place_name": place["name"], "place_type": place["place_type"], "method": method,
                                "evidence": {"source_id_key": key, "value": value, "rule": RULES[method]}})
                    break
        return out

    def _latest(self, namespace: str, kind: str, subject_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT assertion_id FROM income_identity_assertions WHERE namespace=? AND kind=? AND subject_key=? "
            "ORDER BY created_at_ms DESC, assertion_id DESC LIMIT 1", [namespace, kind, subject_key]).fetchone()
        return None if row is None else self.assertion(namespace, row[0], scopes={"operator"})

    def _record(self, namespace, kind, subject, target, method, evidence, state, reason, principal_id):
        subject_key = canonical(subject)
        latest = self._latest(namespace, kind, subject_key)
        if latest is not None and latest["target"] == target and latest["method"] == method and latest["state"] in {
                state, "reverted"}:
            return latest["assertion_id"], False
        number = 1 + int(self.conn.execute(
            "SELECT count(*) FROM income_identity_assertions WHERE namespace=? AND kind=? AND subject_key=?",
            [namespace, kind, subject_key]).fetchone()[0])
        assertion_id = "inc-identity:" + digest([namespace, kind, subject, number, target, method, state])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT INTO income_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, assertion_id, kind, subject_key, canonical(subject),
             None if target is None else canonical(target), method, canonical(evidence), state, reason,
             canonical([{"state": state, "by": principal_id, "at_ms": now, "reason": reason}]), principal_id, now])
        return assertion_id, True

    # ------------------------------------------------------------------ places

    def propose_places(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                       geo_namespace: str = "global") -> dict[str, Any]:
        """Offer every stated area code to Geospatial places by the published code; idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_READ)
        places = self._places(geo_namespace)
        created, out = [], []
        for area in self._areas(namespace):
            subject = {"scheme": area["scheme"], "code": str(area["code"])}
            latest = self._latest(namespace, "area", canonical(subject))
            if latest and latest["state"] in {"accepted", "rejected"}:
                out.append(latest["assertion_id"])
                continue
            candidates = self._candidates(area, places)
            evidence = {"labels": area["labels"], "candidates": candidates,
                        "canonical_entities": self._canonical_names(area["labels"]),
                        "basis": "published identifiers before names"}
            distinct = {c["place_id"] for c in candidates}
            if len(distinct) == 1:
                chosen = candidates[0]
                target = {k: chosen[k] for k in ("place_id", "place_revision_id", "place_name", "place_type")}
                method, state, reason = chosen["method"], "proposed", None
            elif distinct:
                target, method, state = None, None, "ambiguous"
                reason = "more than one place carries this code; a reviewer chooses one of the cited candidates"
            else:
                target, method, state, reason = None, None, "unmatched", "no place carries this code"
            assertion_id, new = self._record(namespace, "area", subject, target, method, evidence, state, reason,
                                             principal_id)
            if new:
                created.append(assertion_id)
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    def place_key(self, namespace: str, area: Mapping[str, Any]) -> str:
        """The accepted place of an area code, else the code itself (a published identifier, never a name)."""
        latest = self.place_for_area(namespace, area["scheme"], str(area["code"]))
        if latest is not None and latest["state"] == "accepted":
            return "place:" + latest["target"]["place_id"]
        return f"code:{area['scheme']}:{area['code']}"

    # ------------------------------------------------------------------ related indicators

    def propose_related(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Propose ``related`` for same-concept series of different sources for the same place; never a merge."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for series in self.store.find_series(namespace):
            groups.setdefault((series["indicator"]["concept"], self.place_key(namespace, series["area"])),
                              []).append(series)
        created, out = [], []
        for (concept, place), members in sorted(groups.items()):
            for left, right in combinations(members, 2):
                if left["provider"] == right["provider"]:
                    continue
                pair = sorted([left["series_id"], right["series_id"]])
                subject = {"series": pair}
                latest = self._latest(namespace, "related", canonical(subject))
                if latest and latest["state"] in {"accepted", "rejected"}:
                    out.append(latest["assertion_id"])
                    continue
                by_id = {left["series_id"]: left, right["series_id"]: right}
                target = {"relation": "related-indicator", "concept": concept, "place": place,
                          "series": [{"series_id": s, "provider": by_id[s]["provider"],
                                      "native_key": by_id[s]["native_key"],
                                      "definition_id": by_id[s]["current_definition_id"]} for s in pair]}
                evidence = {"basis": "same concept and same place by accepted mapping or published code",
                            "place_basis": "accepted place mapping" if place.startswith("place:") else "same code",
                            "recorded_differences": comparability_basis(by_id[pair[0]], by_id[pair[1]]),
                            "note": "related series stay separate; values are never merged, averaged or "
                            "re-harmonised"}
                assertion_id, new = self._record(namespace, "related", subject, target, "same-concept-same-place",
                                                 evidence, "proposed", None, principal_id)
                if new:
                    created.append(assertion_id)
                out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    def related_series(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        """Series accepted as related to this one (shown beside, never merged)."""
        if not table_exists(self.conn, "income_identity_assertions"):
            return []
        out = []
        for item in self.assertions(namespace, scopes={"operator"}, kind="related", state="accepted"):
            ids = item["subject"]["series"]
            if series_id in ids:
                other = next(s for s in item["target"]["series"] if s["series_id"] != series_id)
                out.append({**other, "assertion_id": item["assertion_id"], "reviewed": item["history"][-1]})
        return out

    # ------------------------------------------------------------------ review

    def assertion(self, namespace: str, assertion_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT assertion_id, kind, subject_json, target_json, method, evidence_json, state, reason, history_json, "
            "created_by, created_at_ms FROM income_identity_assertions WHERE namespace=? AND assertion_id=?",
            [namespace, assertion_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "identity assertion is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace, "assertion_id": row[0], "kind": row[1],
                "subject": json.loads(row[2]), "target": None if row[3] is None else json.loads(row[3]),
                "method": row[4], "evidence": json.loads(row[5]), "state": row[6], "reason": row[7],
                "history": json.loads(row[8]), "created_by": row[9], "created_at_ms": row[10]}

    def assertions(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "income_identity_assertions"):
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM income_identity_assertions WHERE namespace=? AND (? IS NULL OR kind=?) "
            "ORDER BY kind, subject_key, created_at_ms, assertion_id", [namespace, kind, kind]).fetchall()
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for (assertion_id,) in rows:
            item = self.assertion(namespace, assertion_id, scopes={"operator"})
            latest[(item["kind"], canonical(item["subject"]))] = item
        return [a for a in latest.values() if state is None or a["state"] == state]

    def _transition(self, namespace, item, state, principal_id, reason, target=None):
        history = item["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}]
        if target is not None:
            self.conn.execute(
                "UPDATE income_identity_assertions SET state=?, history_json=?, target_json=?, method=? "
                "WHERE namespace=? AND assertion_id=?",
                [state, canonical(history), canonical(target["target"]), target["method"], namespace,
                 item["assertion_id"]])
        else:
            self.conn.execute(
                "UPDATE income_identity_assertions SET state=?, history_json=? WHERE namespace=? AND assertion_id=?",
                [state, canonical(history), namespace, item["assertion_id"]])
        return self.assertion(namespace, item["assertion_id"], scopes={"operator"})

    def review(self, namespace, assertion_id, decision, reason, *, principal_id, scopes, place_id=None):
        """Accept or reject a proposal (not one's own); an ambiguous place is accepted by choosing a cited candidate."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise IncomeError("invalid_decision", "accept or reject with a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["created_by"] == principal_id:
            raise IncomeError("self_review", "a mapping is reviewed by someone other than its proposer")
        if item["state"] == "ambiguous":
            if decision == "reject":
                return self._transition(namespace, item, "rejected", principal_id, reason.strip())
            chosen = next((c for c in item["evidence"]["candidates"] if c["place_id"] == place_id), None)
            if chosen is None:
                raise IncomeError("invalid_decision", "choose one of the cited candidate places")
            target = {k: chosen[k] for k in ("place_id", "place_revision_id", "place_name", "place_type")}
            return self._transition(namespace, item, "accepted", principal_id, reason.strip(),
                                    target={"target": target, "method": chosen["method"]})
        if item["state"] != "proposed":
            raise IncomeError("invalid_state", f"assertion is {item['state']}; only a proposed mapping is reviewed")
        return self._transition(namespace, item, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason.strip())

    def revert(self, namespace, assertion_id, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise IncomeError("invalid_decision", "a revert needs a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] not in {"accepted", "rejected"}:
            raise IncomeError("invalid_state", "only an accepted or rejected assertion can be reverted")
        return self._transition(namespace, item, "reverted", principal_id, reason.strip())

    # ------------------------------------------------------------------ use by queries and links

    def area_codes_for_place(self, namespace: str, place_id: str) -> list[dict[str, Any]]:
        """The area codes accepted as this place, each with the assertion it rests on."""
        return [{"scheme": a["subject"]["scheme"], "code": a["subject"]["code"], "assertion_id": a["assertion_id"],
                 "method": a["method"], "reviewed": a["history"][-1]}
                for a in self.assertions(namespace, scopes={"operator"}, kind="area", state="accepted")
                if a["target"]["place_id"] == place_id]

    def place_for_area(self, namespace: str, scheme: str, code: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "income_identity_assertions"):
            return None
        return self._latest(namespace, "area", canonical({"scheme": scheme, "code": str(code)}))

    def unmatched(self, namespace: str) -> list[dict[str, Any]]:
        """Area codes without an accepted place, visible as such."""
        return [{"scheme": a["subject"]["scheme"], "code": a["subject"]["code"], "state": a["state"],
                 "reason": a["reason"]}
                for a in self.assertions(namespace, scopes={"operator"}, kind="area") if a["state"] != "accepted"]


__all__ = ["CONTRACT", "PLACE_KEYS", "IncomeIdentity"]
