"""Places, facilities and indicators across waste sources through reviewable identity (#2740, WC06).

Follows :mod:`src.kb.business_statistics_identity` (reviewable assertions with history) and
:mod:`src.kb.entity_history` (proposed, reviewed, reverted; nothing auto-merged). Three kinds of assertion, each
carrying a **method**, **evidence** and **confidence**:

* ``area`` - every place a series states (Eurostat GEO ``DE``, OECD ISO 3166-1 alpha-3 ``DEU``) is offered to the
  Geospatial places the platform already holds (no new place store) by the **published code** only: a place whose
  source identifiers carry the same code (``nuts``/``eurostat-geo`` or ``iso3166-1-alpha3``; confidence ``high``), or
  a two-letter Eurostat code read as ISO 3166-1 alpha-2 (``EL`` is ``GR``; confidence ``medium``). Several candidates
  are ``ambiguous``; none is ``unmatched``. Names are context, never a match.
* ``facility`` - every INSPIRE id a transfer row states is matched to the ``environment.core`` facility record that
  carries it (``eea-industry`` record keyed by the INSPIRE id; confidence ``high``). An id no facility record carries
  stays ``unmatched`` and visible with its rows; **no facility is ever created**. Without the environment.core store
  the assertion is ``provider_absent``.
* ``indicator`` - the same or a related indicator published by Eurostat and by the OECD for the same accepted place is
  recorded as **related** (``same-indicator`` or ``related-different-scope``), never merged: both series keep their
  own values, definitions and vintages.

Every assertion is ``proposed`` (or ``ambiguous``/``unmatched``/``provider_absent``), then ``accepted`` or
``rejected`` by a reviewer (reviewer, reason and time recorded) and can be ``reverted``; queries use accepted
assertions only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from itertools import combinations
from typing import Any

from src.kb.waste_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    WasteError,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)
from src.kb.waste_store import FACILITY_PROVIDER, WasteStore

CONTRACT = "noesis-waste-identity-v1"
GEO_READ = "knowledge:geospatial:read"
KINDS = ("area", "facility", "indicator")
STATES = ("proposed", "ambiguous", "unmatched", "provider_absent", "accepted", "rejected", "reverted")
PLACE_KEYS = {"eurostat-geo": ("nuts", "eurostat-geo"), "iso3166-1-alpha3": ("iso3166-1-alpha3",)}
EUROSTAT_ISO2 = {"EL": "GR", "UK": "GB"}
# Indicators Eurostat and the OECD publish that answer the same (or a related) question; recorded as related only.
RELATED_INDICATORS = {
    frozenset({"municipal_waste_generated"}): (
        "same-indicator",
        ("the same indicator published by two sources; the OECD and Eurostat figures share the joint questionnaire "
         "(verify) yet stay two series: a difference is shown, never reconciled")),
    frozenset({"municipal_waste_generated", "waste_generated"}): (
        "related-different-scope",
        ("OECD municipal waste generated and Eurostat total waste generated rest on the OECD/Eurostat joint "
         "questionnaire (verify) but cover different scopes (municipal waste against all waste of all NACE "
         "activities and households); they stay two series and are never compared as one figure")),
}
_DDL = """
CREATE TABLE IF NOT EXISTS waste_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  subject_json TEXT NOT NULL, target_json TEXT, method TEXT, relation TEXT, confidence TEXT, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL, reason TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, assertion_id)
);
"""


class WasteIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = WasteStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "waste_identity_assertions")

    # ------------------------------------------------------------------ bookkeeping

    def _latest(self, namespace: str, kind: str, subject_key: str) -> dict[str, Any] | None:
        if not self.ready():
            return None
        row = self.conn.execute(
            "SELECT assertion_id FROM waste_identity_assertions WHERE namespace=? AND kind=? AND subject_key=? "
            "ORDER BY created_at_ms DESC, assertion_id DESC LIMIT 1", [namespace, kind, subject_key]).fetchone()
        return None if row is None else self.assertion(namespace, row[0], scopes={"operator"})

    def _record(self, namespace, kind, subject, target, method, relation, confidence, evidence, state, reason,
                principal_id):
        subject_key = canonical(subject)
        latest = self._latest(namespace, kind, subject_key)
        if latest is not None and latest["target"] == target and latest["method"] == method and \
                latest["state"] in {state, "reverted"}:
            return latest["assertion_id"], False
        number = 1 + int(self.conn.execute(
            "SELECT count(*) FROM waste_identity_assertions WHERE namespace=? AND kind=? AND subject_key=?",
            [namespace, kind, subject_key]).fetchone()[0])
        assertion_id = "waste-identity:" + digest([namespace, kind, subject, number, target, method, state])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT INTO waste_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, assertion_id, kind, subject_key, canonical(subject),
             None if target is None else canonical(target), method, relation, confidence, canonical(evidence), state,
             reason, canonical([{"state": state, "by": principal_id, "at_ms": now, "reason": reason}]), principal_id,
             now])
        return assertion_id, True

    # ------------------------------------------------------------------ places

    def _areas(self, namespace: str) -> list[dict[str, Any]]:
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for series in self.store.find_series(namespace):
            area = series["area"]
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

    @staticmethod
    def _candidates(area: Mapping[str, Any], places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scheme, code = area["scheme"], str(area["code"])
        rules = [("published-code", key, code, "high") for key in PLACE_KEYS.get(scheme, ())]
        if scheme == "eurostat-geo" and len(code) == 2:
            rules.append(("iso-alpha2-equivalent", "iso3166-1-alpha2", EUROSTAT_ISO2.get(code, code), "medium"))
        out = []
        for place in places:
            for method, key, value, confidence in rules:
                if place["source_ids"].get(key) == value:
                    out.append({"place_id": place["place_id"], "place_revision_id": place["revision_id"],
                                "place_name": place["name"], "place_type": place["place_type"], "method": method,
                                "confidence": confidence,
                                "evidence": {"source_id_key": key, "value": value,
                                             "rule": "the place carries the published code" if method ==
                                             "published-code" else "Eurostat GEO country code read as ISO 3166-1 "
                                                                   "alpha-2 (EL=GR, UK=GB)"}})
                    break
        return out

    def propose_places(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                       geo_namespace: str = "global") -> dict[str, Any]:
        """Offer every stated area code to Geospatial places by the published code; idempotent; nothing merged."""
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
                        "note": "names are context only; matching rests on published codes"}
            distinct = {c["place_id"] for c in candidates}
            if len(distinct) == 1:
                chosen = candidates[0]
                target = {k: chosen[k] for k in ("place_id", "place_revision_id", "place_name", "place_type")}
                method, confidence, state, reason = chosen["method"], chosen["confidence"], "proposed", None
            elif distinct:
                target, method, confidence, state = None, None, None, "ambiguous"
                reason = "more than one place carries this code; a reviewer chooses one of the cited candidates"
            else:
                target, method, confidence, state, reason = None, None, None, "unmatched", "no place carries this code"
            assertion_id, new = self._record(namespace, "area", subject, target, method, "exact" if target else None,
                                             confidence, evidence, state, reason, principal_id)
            created += [assertion_id] if new else []
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    # ------------------------------------------------------------------ facilities

    def propose_facilities(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Match each stated INSPIRE id to the environment.core facility record carrying it; never create one."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        held = table_exists(self.conn, "environment_records")
        created, out, unmatched = [], [], []
        for inspire_id in sorted({r["inspire_id"] for r in self.store.transfer_rows(namespace)}):
            subject = {"inspire_id": inspire_id}
            latest = self._latest(namespace, "facility", canonical(subject))
            if latest and latest["state"] in {"accepted", "rejected"}:
                out.append(latest["assertion_id"])
                continue
            rows = [r["row_id"] for r in self.store.transfer_rows(namespace, inspire_id=inspire_id)]
            record = self._facility_record(namespace, inspire_id) if held else None
            if record is not None:
                target = {"record_id": record["record_id"], "revision_id": record["revision_id"],
                          "revision": record["revision"], "provider": FACILITY_PROVIDER,
                          "native_id": record["native_id"], "provider_id": "environment.core"}
                state, method, confidence, reason = "proposed", "published-code (INSPIRE id)", "high", None
            elif held:
                target, state, method, confidence = None, "unmatched", None, None
                reason = ("no environment.core facility record carries this INSPIRE id; its transfer rows stay "
                          "visible as unmatched and no facility is created")
                unmatched.append(inspire_id)
            else:
                target, state, method, confidence = None, "provider_absent", None, None
                reason = "the environment.core store is not composed; the transfer rows stay visible"
            evidence = {"transfer_rows": rows, "basis": "the INSPIRE id published in the EEA Industrial Reporting "
                                                        "transfer rows and in the environment.core facility record"}
            assertion_id, new = self._record(namespace, "facility", subject, target, method,
                                             "exact" if target else None, confidence, evidence, state, reason,
                                             principal_id)
            created += [assertion_id] if new else []
            out.append(assertion_id)
        return {"created": created, "unmatched_inspire_ids": unmatched,
                "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out],
                "note": "facility identity stays with environment.core; nothing here creates or merges a facility"}

    def _facility_record(self, namespace: str, inspire_id: str) -> dict[str, Any] | None:
        from src.kb.environment_store import EnvironmentStore
        from src.kb.waste_store import environment_scopes

        env = EnvironmentStore(self.conn, initialize=False)
        rid = env.find(namespace, "facility", FACILITY_PROVIDER, inspire_id)
        return None if rid is None else env.record(namespace, rid, scopes=environment_scopes(namespace))

    # ------------------------------------------------------------------ related indicators

    def propose_related_indicators(self, namespace: str, *, principal_id: str,
                                   scopes: Iterable[str]) -> dict[str, Any]:
        """Eurostat and OECD series of the same accepted place whose indicators match or relate: related, never
        merged."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        series = self.store.find_series(namespace)
        created, out = [], []
        for left, right in combinations(series, 2):
            if left["provider"].split("-")[0] == right["provider"].split("-")[0]:
                continue
            if {left["hazard"]["code"], right["hazard"]["code"]} - {"HAZ_NHAZ", "not_applicable"}:
                continue  # a hazardous-only series answers another question
            spec = RELATED_INDICATORS.get(frozenset({left["indicator"]["concept"], right["indicator"]["concept"]}))
            if spec is None:
                continue
            a = self.place_for_area(namespace, left["area"]["scheme"], left["area"]["code"])
            b = self.place_for_area(namespace, right["area"]["scheme"], right["area"]["code"])
            if a is None or b is None or a["place_id"] != b["place_id"]:
                continue
            pair = sorted([left["series_id"], right["series_id"]])
            subject = {"pair": pair}
            latest = self._latest(namespace, "indicator", canonical(subject))
            if latest and latest["state"] in {"accepted", "rejected"}:
                out.append(latest["assertion_id"])
                continue
            relation, statement = spec
            evidence = {"statement": statement, "place_id": a["place_id"],
                        "place_assertions": [a["assertion_id"], b["assertion_id"]],
                        "series": {s["series_id"]: {"provider": s["provider"], "dataset": s["dataset"],
                                                    "concept": s["indicator"]["concept"],
                                                    "area": s["area"]} for s in (left, right)}}
            assertion_id, new = self._record(namespace, "indicator", subject, {"pair": pair, "merge": False},
                                             "declared-indicator-relation", relation, "medium", evidence,
                                             "proposed", None, principal_id)
            created += [assertion_id] if new else []
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out],
                "note": "related indicators are recorded, never merged or reconciled"}

    # ------------------------------------------------------------------ review

    def assertion(self, namespace: str, assertion_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT assertion_id, kind, subject_json, target_json, method, relation, confidence, evidence_json, state, "
            "reason, history_json, created_by, created_at_ms FROM waste_identity_assertions WHERE namespace=? AND "
            "assertion_id=?", [namespace, assertion_id]).fetchone()
        if row is None:
            raise WasteError("not_found", "identity assertion is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace, "assertion_id": row[0], "kind": row[1],
                "subject": json.loads(row[2]), "target": None if row[3] is None else json.loads(row[3]),
                "method": row[4], "relation": row[5], "confidence": row[6], "evidence": json.loads(row[7]),
                "state": row[8], "reason": row[9], "history": json.loads(row[10]), "created_by": row[11],
                "created_at_ms": row[12], "merged": False}

    def assertions(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM waste_identity_assertions WHERE namespace=? AND (? IS NULL OR kind=?) "
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
                "UPDATE waste_identity_assertions SET state=?, history_json=?, target_json=?, method=?, relation=?, "
                "confidence=? WHERE namespace=? AND assertion_id=?",
                [state, canonical(history), canonical(target["target"]), target["method"], "exact",
                 target["confidence"], namespace, item["assertion_id"]])
        else:
            self.conn.execute(
                "UPDATE waste_identity_assertions SET state=?, history_json=? WHERE namespace=? AND assertion_id=?",
                [state, canonical(history), namespace, item["assertion_id"]])
        return self.assertion(namespace, item["assertion_id"], scopes={"operator"})

    def review(self, namespace, assertion_id, decision, reason, *, principal_id, scopes, place_id=None):
        """Accept or reject a proposal; an ambiguous place is accepted by choosing one of the cited candidates."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise WasteError("invalid_decision", "accept or reject with a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] == "ambiguous":
            if decision == "reject":
                return self._transition(namespace, item, "rejected", principal_id, reason.strip())
            chosen = next((c for c in item["evidence"]["candidates"] if c["place_id"] == place_id), None)
            if chosen is None:
                raise WasteError("invalid_decision", "choose one of the cited candidate places")
            target = {k: chosen[k] for k in ("place_id", "place_revision_id", "place_name", "place_type")}
            return self._transition(namespace, item, "accepted", principal_id, reason.strip(),
                                    target={"target": target, "method": chosen["method"],
                                            "confidence": chosen["confidence"]})
        if item["state"] != "proposed":
            raise WasteError("invalid_state", f"assertion is {item['state']}; only a proposal is reviewed")
        return self._transition(namespace, item, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason.strip())

    def revert(self, namespace, assertion_id, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise WasteError("invalid_decision", "a revert needs a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] not in {"accepted", "rejected"}:
            raise WasteError("invalid_state", "only an accepted or rejected assertion can be reverted")
        return self._transition(namespace, item, "reverted", principal_id, reason.strip())

    # ------------------------------------------------------------------ use by queries and links

    def area_codes_for_place(self, namespace: str, place_id: str) -> list[dict[str, Any]]:
        """The area codes accepted as this place, each with the assertion it rests on."""
        return [{"scheme": a["subject"]["scheme"], "code": a["subject"]["code"], "assertion_id": a["assertion_id"],
                 "method": a["method"], "confidence": a["confidence"], "reviewed": a["history"][-1]}
                for a in self.assertions(namespace, scopes={"operator"}, kind="area", state="accepted")
                if a["target"]["place_id"] == place_id]

    def place_for_area(self, namespace: str, scheme: str, code: str) -> dict[str, Any] | None:
        latest = self._latest(namespace, "area", canonical({"scheme": scheme, "code": str(code)}))
        if latest is None or latest["state"] != "accepted":
            return None
        return {**latest["target"], "assertion_id": latest["assertion_id"]}

    def facility_for(self, namespace: str, inspire_id: str) -> dict[str, Any]:
        """The identity state of an INSPIRE id: the accepted environment.core record, or why there is none."""
        latest = self._latest(namespace, "facility", canonical({"inspire_id": inspire_id}))
        if latest is None:
            return {"state": "not_reviewed", "inspire_id": inspire_id, "target": None}
        return {"state": latest["state"], "inspire_id": inspire_id, "assertion_id": latest["assertion_id"],
                "target": latest["target"] if latest["state"] == "accepted" else None, "reason": latest["reason"],
                "method": latest["method"], "confidence": latest["confidence"]}

    def related(self, namespace: str, series_id: str, *, state: str = "accepted") -> list[dict[str, Any]]:
        out = []
        for a in self.assertions(namespace, scopes={"operator"}, kind="indicator", state=state):
            pair = a["subject"]["pair"]
            if series_id in pair:
                out.append({"series_id": pair[1] if pair[0] == series_id else pair[0], "relation": a["relation"],
                            "assertion_id": a["assertion_id"], "statement": a["evidence"]["statement"],
                            "merged": False})
        return out


__all__ = ["CONTRACT", "KINDS", "RELATED_INDICATORS", "STATES", "WasteIdentity"]
