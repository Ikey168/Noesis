"""Reviewable matches of stations and water bodies to places and rivers (#2582, WA06 #2612).

Stations and water bodies are matched to places registered with the
geospatial owner (:class:`src.kb.geospatial.GeospatialStore`: boundaries,
rivers, counties) only through **reviewed proposals**. Nothing is merged and
nothing is accepted automatically. Published identifiers are tried before
names, and a name is never used when an identifier already proposes a match:

* ``published-identifier`` - a station identifier the source publishes (a
  USGS county FIPS code, a PEGELONLINE water ``shortname``) equals an
  identifier the place is registered with - deterministic;
* ``published-coordinates`` - the station's published point lies inside the
  place boundary, with a replayable ``contains`` receipt and the boundary's
  geometry version - deterministic geometry;
* ``published-geometry`` - a water body's published geometry (named dataset
  vintage) lies in or crosses the place boundary - the only way water bodies
  reach places;
* ``river-name`` - a station's published river name equals a river place's
  name, used only when no identifier matched - **lower-evidence**.

Every proposal carries its method, evidence (the source revision, the place
revision and geometry version, receipts) and confidence, and is proposed,
reviewed (accepted or rejected with a reason), or reverted; each decision is an
entity identity decision in :class:`src.kb.entity_history.EntityHistoryStore`.
Subjects without an accepted match stay visible as unmatched.
"""

from __future__ import annotations

import itertools
import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.water_records import (
    IDENTITY_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    WaterError,
    authorize,
    canonical,
    digest,
)
from src.kb.water_store import WaterStore, table_exists

METHODS = ("published-identifier", "published-coordinates", "published-geometry", "river-name")
EVIDENCE_CLASS = {"published-identifier": "deterministic", "published-coordinates": "deterministic-geometry",
                  "published-geometry": "deterministic-geometry", "river-name": "lower-evidence"}
RELATIONS = ("located-in", "on-river", "intersects")
GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate"}
OWN_PLACE_TYPES = ("water-station-location",)
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS water_identity_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, subject_key TEXT NOT NULL, subject_kind TEXT NOT NULL,
  subject_record_id TEXT NOT NULL, subject_revision_id TEXT NOT NULL, place_id TEXT NOT NULL,
  place_revision_id TEXT NOT NULL, geometry_id TEXT, relation TEXT NOT NULL, method TEXT NOT NULL,
  confidence TEXT NOT NULL, evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT,
  subject_entity TEXT NOT NULL, place_entity TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  history_json TEXT NOT NULL, PRIMARY KEY(namespace, match_id)
);
"""


def entity_id(key: str, prefix: str = "ent-water-") -> str:
    return prefix + re.sub(r"[^a-z0-9]+", "-", key.casefold()).strip("-")


# ------------------------------------------------------------------ places and geometry


def place_view(conn: Any, namespace: str, place_id: str) -> dict[str, Any] | None:
    """A place with its current boundary polygon (if any), that geometry's version, names and registered ids."""
    from src.kb.geospatial import GeospatialStore

    if not table_exists(conn, "geospatial_places"):
        return None
    row = conn.execute("SELECT namespace FROM geospatial_places WHERE place_id=? AND namespace IN (?, 'global')",
                       [place_id, namespace]).fetchone()
    if row is None:
        return None
    geo = GeospatialStore(conn, initialize=False)
    place = geo.place(row[0], place_id, scopes={"knowledge:geospatial:read"})
    polygons = [g for g in geo.geometries(row[0], place_id, scopes={"knowledge:geospatial:read"},
                                          include_disputed=False)
                if g and g["geometry"]["type"] in {"Polygon", "MultiPolygon"}]
    boundary = polygons[0] if polygons else None
    return {"place_id": place_id, "namespace": row[0], "name": place["canonical_name"],
            "place_type": place["place_type"], "revision_id": place["revision_id"],
            "names": sorted({place["canonical_name"].casefold(), *(str(n.get("value") or "").casefold()
                                                                   for n in place["names"])}),
            "source_ids": {str(k): str(v) for k, v in place["source_ids"].items()},
            "geometry": boundary["geometry"] if boundary else None,
            "geometry_version": None if boundary is None else {
                "geometry_id": boundary["geometry_id"], "content_hash": boundary["content_hash"],
                "observed_at_ms": boundary["observed_at_ms"], "valid_from_ms": boundary["valid_from_ms"],
                "valid_to_ms": boundary["valid_to_ms"], "generation": boundary["generation"]}}


def _vertices(geometry: Mapping[str, Any]) -> list[list[float]]:
    kind, coords = geometry["type"], geometry["coordinates"]
    if kind == "Point":
        return [coords]
    if kind == "LineString":
        return list(coords)
    if kind in {"MultiLineString", "Polygon"}:
        return [p for part in coords for p in part]
    return [p for polygon in coords for ring in polygon for p in ring]


def _segments(geometry: Mapping[str, Any]) -> list[tuple[list[float], list[float]]]:
    kind, coords = geometry["type"], geometry["coordinates"]
    lines = ([coords] if kind == "LineString" else list(coords) if kind in {"MultiLineString", "Polygon"}
             else [ring for polygon in coords for ring in polygon] if kind == "MultiPolygon" else [])
    return [(a, b) for line in lines for a, b in itertools.pairwise(line)]


def geometry_relation(boundary: Mapping[str, Any], geometry: Mapping[str, Any]) -> dict[str, Any]:
    """How a published water-body geometry relates to a place boundary: within, crossing or outside."""
    from src.kb.geospatial import _contains, _segments_intersect

    vertices = _vertices(geometry)
    inside = [v for v in vertices if _contains(boundary, v, 0)]
    crossing = any(_segments_intersect(a, b, c, d, 0) for a, b in _segments(geometry)
                   for c, d in _segments(boundary))
    relation = ("within" if len(inside) == len(vertices) and not crossing else
                "crosses" if inside or crossing else "outside")
    return {"relation": relation, "vertices": len(vertices), "vertices_inside": len(inside),
            "first_inside": inside[0] if inside else None, "boundary_crossed": crossing}


class WaterIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = WaterStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "water_identity_matches")

    # ------------------------------------------------------------------ subjects

    def subjects(self, namespace: str) -> dict[str, dict[str, Any]]:
        """Current published stations and water bodies (removed ones are not matched), by subject key."""
        result = {}
        for record in self.store.records(namespace):
            if record["record_type"] not in {"station", "water_body"}:
                continue
            revision = self.store.current(namespace, record["record_id"])
            if revision is None or revision["event"] == "removed":
                continue
            result[record["subject_key"]] = {"record": record, "revision": revision,
                                             "published": revision["statement"]["as_published"],
                                             "kind": record["record_type"]}
        return result

    def places(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_places"):
            return []
        rows = self.conn.execute(
            "SELECT place_id FROM geospatial_places WHERE namespace=? ORDER BY place_id", [namespace]).fetchall()
        views = [place_view(self.conn, namespace, r[0]) for r in rows]
        return [v for v in views if v and v["place_type"] not in OWN_PLACE_TYPES]

    def _candidates(self, subject: Mapping[str, Any], place: Mapping[str, Any], principal_id: str
                    ) -> list[tuple[str, str, str, dict[str, Any]]]:
        """(relation, method, confidence, evidence) candidates of one subject against one place."""
        from src.kb.geospatial import GeospatialStore

        published, found = subject["published"], []
        if subject["kind"] == "station":
            ids = {(i["scheme"], str(i["value"])) for i in published.get("identifiers") or []}
            shared = sorted((s, v) for s, v in ids if place["source_ids"].get(s) == v)
            if shared:
                found.append(("located-in", "published-identifier", "high",
                              {"identifiers": [{"scheme": s, "value": v} for s, v in shared]}))
            river = published.get("river") or {}
            if river.get("shortname") and place["source_ids"].get("pegelonline-water") == river["shortname"]:
                found.append(("on-river", "published-identifier", "high",
                              {"identifiers": [{"scheme": "pegelonline-water", "value": river["shortname"]}]}))
            elif river.get("longname") and place["place_type"] == "river" and \
                    river["longname"].casefold() in place["names"]:
                found.append(("on-river", "river-name", "low",
                              {"river_name": river["longname"], "place_names": place["names"],
                               "note": "name equality only; no published river identifier matched"}))
            location = published.get("location")
            if location and place["geometry"]:
                point = [location["longitude"], location["latitude"]]
                receipt = GeospatialStore(self.conn, initialize=False).relation(
                    place["namespace"], "contains", place["geometry_version"]["geometry_id"], point,
                    scopes=GEO_SCOPES, principal_id=principal_id)
                if receipt["result"].get("contains"):
                    found.append(("located-in", "published-coordinates", "high",
                                  {"point": point, "crs": location.get("crs"), "receipt_id": receipt["receipt_id"]}))
        elif published.get("geometry") and place["geometry"]:
            relation = geometry_relation(place["geometry"], published["geometry"])
            if relation["relation"] != "outside":
                evidence = {**relation, "geometry_vintage": published.get("geometry_vintage")}
                if relation["first_inside"]:
                    evidence["receipt_id"] = GeospatialStore(self.conn, initialize=False).relation(
                        place["namespace"], "contains", place["geometry_version"]["geometry_id"],
                        relation["first_inside"], scopes=GEO_SCOPES, principal_id=principal_id)["receipt_id"]
                found.append(("intersects", "published-geometry",
                              "high" if relation["relation"] == "within" else "medium", evidence))
        return found

    # ------------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Offer every candidate station/water-body-to-place match for review; accept nothing."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        subjects, places = self.subjects(namespace), self.places(namespace)
        created = []
        for key, subject in sorted(subjects.items()):
            for place in places:
                candidates = self._candidates(subject, place, principal_id)
                identified = {r for r, m, _, _ in candidates if m == "published-identifier"}
                for relation, method, confidence, detail in candidates:
                    if method == "river-name" and relation in identified:
                        continue  # identifiers before names
                    change = self._offer(namespace, key, subject, place, relation, method, confidence, detail,
                                         principal_id)
                    if change:
                        created.append(change)
        matches = self.matches(namespace, scopes=scopes)
        matched = {m["subject_key"] for m in matches}
        return {"contract": IDENTITY_CONTRACT, "proposed": sorted(created), "matches": matches,
                "unmatched": [{"subject_key": k, "kind": s["kind"], "name": s["record"]["subject_name"],
                               "reason": "no published identifier, coordinate or geometry reaches a registered place"}
                              for k, s in sorted(subjects.items()) if k not in matched],
                "policy": "every proposal needs a reviewer; identifiers are used before names; nothing is merged"}

    def _offer(self, namespace, key, subject, place, relation, method, confidence, detail, principal_id):
        from src.kb.entities import register_canonical_entity

        match_id = "water-idm:" + digest([namespace, key, place["place_id"], relation, method])[:24]
        revision = subject["revision"]
        evidence = {**detail, "method": method, "evidence_class": EVIDENCE_CLASS[method],
                    "subject": {"subject_key": key, "record_id": subject["record"]["record_id"],
                                "revision_id": revision["revision_id"], "source_url":
                                    revision["statement"]["source"]["url"]},
                    "place": {"place_id": place["place_id"], "name": place["name"], "revision_id":
                              place["revision_id"], "geometry_version": place["geometry_version"]},
                    "note": "a reviewed match connects a station or water body to a place; it merges nothing"}
        row = self.conn.execute("SELECT state, subject_revision_id FROM water_identity_matches WHERE namespace=? AND "
                                "match_id=?", [namespace, match_id]).fetchone()
        if row is not None:
            if row[0] == "proposed" and row[1] != revision["revision_id"]:
                self.conn.execute("UPDATE water_identity_matches SET subject_revision_id=?, evidence_json=? WHERE "
                                  "namespace=? AND match_id=?",
                                  [revision["revision_id"], canonical(evidence), namespace, match_id])
            return None
        subject_entity = register_canonical_entity(self.conn, entity_id(key), subject["record"]["subject_name"] or key,
                                                   "water-body" if subject["kind"] == "water_body" else "station")
        place_entity = register_canonical_entity(self.conn, entity_id(place["place_id"], "ent-place-"),
                                                 place["name"], "place")
        now = self.now()
        self.conn.execute(
            "INSERT INTO water_identity_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, match_id, key, subject["kind"], subject["record"]["record_id"], revision["revision_id"],
             place["place_id"], place["revision_id"], (place["geometry_version"] or {}).get("geometry_id"), relation,
             method, confidence, canonical(evidence), "proposed", None, subject_entity, place_entity, principal_id, now,
             canonical([{"state": "proposed", "by": principal_id, "at_ms": now, "method": method}])])
        return match_id

    # ------------------------------------------------------------------ reviews

    _COLUMNS = ("match_id, subject_key, subject_kind, subject_record_id, subject_revision_id, place_id, "
                "place_revision_id, geometry_id, relation, method, confidence, evidence_json, state, decision_id, "
                "subject_entity, place_entity, created_by, created_at_ms, history_json")

    def _row(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {self._COLUMNS} FROM water_identity_matches WHERE namespace=? AND "
                                "match_id=?", [namespace, match_id]).fetchone() if self._ready() else None
        if row is None:
            raise WaterError("not_found", "water identity match is not visible in this namespace")
        names = [c.strip() for c in self._COLUMNS.split(",")]
        value = dict(zip(names, row))
        history = json.loads(value.pop("history_json"))
        evidence = json.loads(value.pop("evidence_json"))
        reviewed = [h for h in history if h["state"] in {"accepted", "rejected", "reverted"}]
        current = self.store.current(namespace, value["subject_record_id"])
        return {"contract": IDENTITY_CONTRACT, "namespace": namespace, **value,
                "evidence_class": EVIDENCE_CLASS[value["method"]], "evidence": evidence, "history": history,
                "reviewer": reviewed[-1]["by"] if reviewed and value["state"] != "proposed" else None,
                "reviewed_at_ms": reviewed[-1]["at_ms"] if reviewed and value["state"] != "proposed" else None,
                "subject_current_revision_id": current["revision_id"] if current else None,
                "subject_revised_since": bool(current and current["revision_id"] != value["subject_revision_id"]),
                "notice": "a match connects a station or water body to a place; it never merges records"}

    def matches(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                subject_key: str | None = None, place_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM water_identity_matches WHERE namespace=? AND (? IS NULL OR state=?) AND "
            "(? IS NULL OR subject_key=?) AND (? IS NULL OR place_id=?) ORDER BY match_id",
            [namespace, state, state, subject_key, subject_key, place_id, place_id]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise WaterError("invalid_decision", "accept or reject with a reason")
        match = self._row(namespace, match_id)
        if match["state"] not in {"proposed", "reverted"}:
            raise WaterError("invalid_state", f"match is {match['state']}; revert it before re-reviewing")
        for entity, key in ((match["subject_entity"], match["subject_key"]), (match["place_entity"], match["place_id"])):
            self.history.register_entity(namespace, entity, [key], principal_id=principal_id,
                                         scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [match["subject_entity"],
                                                                         match["place_entity"]],
            {"match_id": match_id, "relation": match["relation"], "method": match["method"],
             "confidence": match["confidence"], "evidence": match["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "environment.water", "records": [match["subject_revision_id"],
                                                                         match["place_revision_id"]]},
             "policy": {"merge": False, "note": "identity decision only; records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"water-identity:{namespace}:{match_id}:{len(match['history'])}")
        return self._transition(namespace, match, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise WaterError("invalid_decision", "a revert needs a reason")
        match = self._row(namespace, match_id)
        if match["state"] not in {"accepted", "rejected"}:
            raise WaterError("invalid_state", "only an accepted or rejected match can be reverted")
        undo = self.history.undo(namespace, match["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, match, "reverted", undo["decision_id"], principal_id, reason.strip())

    def _transition(self, namespace, match, state, decision_id, principal_id, reason):
        history = match["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                        "decision_id": decision_id}]
        self.conn.execute("UPDATE water_identity_matches SET state=?, decision_id=?, history_json=? WHERE "
                          "namespace=? AND match_id=?",
                          [state, decision_id, canonical(history), namespace, match["match_id"]])
        return self._row(namespace, match["match_id"])

    # ------------------------------------------------------------------ resolution

    def accepted(self, namespace: str, *, subject_key: str | None = None,
                 place_id: str | None = None) -> list[dict[str, Any]]:
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM water_identity_matches WHERE namespace=? AND state='accepted' AND "
            "(? IS NULL OR subject_key=?) AND (? IS NULL OR place_id=?) ORDER BY match_id",
            [namespace, subject_key, subject_key, place_id, place_id]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def generation(self, namespace: str) -> int:
        if not self._ready():
            return 0
        rows = self.conn.execute("SELECT history_json FROM water_identity_matches WHERE namespace=?",
                                 [namespace]).fetchall()
        return sum(len(json.loads(r[0])) - 1 for r in rows)  # every review and revert moves the generation


__all__ = ["EVIDENCE_CLASS", "METHODS", "RELATIONS", "WaterIdentity", "entity_id", "geometry_relation",
           "place_view"]
