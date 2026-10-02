"""Stations and water bodies to places and rivers through reviewable identity (#2582, WA06 #2612).

Stations and water bodies stay separate subjects; they are connected to
geospatial places (districts, rivers, basins) only by **reviewed match
proposals**, exactly as the biodiversity taxon identities are
(:mod:`src.kb.biodiversity_identity`). Nothing is merged and nothing is
accepted automatically. Published identifiers are used before names, and
geometry comes only from what a source published:

* ``published-river-identifier`` - the station's published river/water
  identifier (PEGELONLINE water shortname, USGS hydrologic unit code) equals
  an identifier the river place was registered for - deterministic;
* ``published-water-body-code`` - a place registered for the water body's EU
  code - deterministic;
* ``published-coordinates-within`` - the station's published coordinates lie
  inside the place boundary, computed through the geospatial owner with a
  replayable ``contains`` receipt naming the geometry version - deterministic;
* ``published-geometry-within`` / ``published-geometry-intersects`` - every /
  some vertex of the water body's *published* geometry lies inside the place
  boundary (receipts per vertex) - deterministic / lower-evidence;
* ``river-name`` - the published river name equals the river place's name,
  proposed only when no identifier is published - lower-evidence.

A water body without a published geometry or code-registered place stays
**unmatched**, and so does any station no rule reaches; unmatched subjects are
listed on every proposal run. Accepting, rejecting or reverting records the
reviewer, time and an entity identity decision in
:class:`src.kb.entity_history.EntityHistoryStore`.
"""

from __future__ import annotations

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

METHODS = ("published-river-identifier", "published-water-body-code", "published-coordinates-within",
           "published-geometry-within", "published-geometry-intersects", "river-name")
EVIDENCE_CLASS = {"published-river-identifier": "deterministic", "published-water-body-code": "deterministic",
                  "published-coordinates-within": "deterministic", "published-geometry-within": "deterministic",
                  "published-geometry-intersects": "lower-evidence", "river-name": "lower-evidence"}
RELATIONS = {"published-river-identifier": "on-river", "river-name": "on-river",
             "published-water-body-code": "identified-by", "published-coordinates-within": "located-in",
             "published-geometry-within": "located-in", "published-geometry-intersects": "intersects"}
GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate"}
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
WATER_PLACE_TYPES = ("water-station",)
_DDL = """
CREATE TABLE IF NOT EXISTS water_identity_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, subject_key TEXT NOT NULL, subject_kind TEXT NOT NULL,
  place_id TEXT NOT NULL, subject_entity TEXT NOT NULL, place_entity TEXT NOT NULL, relation TEXT NOT NULL,
  method TEXT NOT NULL, evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL, PRIMARY KEY(namespace, match_id)
);
"""


def entity_id(key: str) -> str:
    return "ent-water-" + re.sub(r"[^a-z0-9]+", "-", key.casefold()).strip("-")


def _fold(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def place_view(conn: Any, namespace: str, place_id: str) -> dict[str, Any] | None:
    """A place with its current boundary geometry (id = geometry version), names and registered identifiers."""
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
            "names": sorted({_fold(n.get("value")) for n in place.get("names") or []} | {_fold(
                place["canonical_name"])}),
            "identifiers": {str(k): str(v) for k, v in dict(place["source_ids"]).items()},
            "geometry": boundary["geometry"] if boundary else None,
            "geometry_id": boundary["geometry_id"] if boundary else None,
            "geometry_observed_at_ms": boundary.get("observed_at_ms") if boundary else None}


def contains(geometry: Mapping[str, Any], point: list[float]) -> bool:
    from src.kb.geospatial import _contains

    return bool(_contains(geometry, point, 0))


def vertices(geometry: Mapping[str, Any]) -> list[list[float]]:
    kind, coords = geometry["type"], geometry["coordinates"]
    if kind == "LineString":
        return [list(p) for p in coords]
    if kind in {"MultiLineString", "Polygon"}:
        return [list(p) for part in coords for p in part]
    if kind == "MultiPolygon":
        return [list(p) for poly in coords for ring in poly for p in ring]
    return []


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

    def subjects(self, namespace: str, *, as_of_ms: int | None = None) -> dict[str, dict[str, Any]]:
        """Current station and water-body statements, keyed by subject key (removed ones excluded)."""
        result = {}
        for record in self.store.records(namespace):
            if record["record_type"] not in {"station", "water_body"}:
                continue
            revision = self.store.current(namespace, record["record_id"], as_of_ms=as_of_ms)
            if revision is None or revision["event"] == "removed":
                continue
            result[record["subject_key"]] = {"record": record, "revision": revision,
                                             "published": revision["statement"]["as_published"],
                                             "kind": record["record_type"]}
        return result

    def places(self, namespace: str, place_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if place_ids is None:
            if not table_exists(self.conn, "geospatial_places"):
                return []
            place_ids = [r[0] for r in self.conn.execute(
                "SELECT place_id FROM geospatial_places WHERE namespace=? ORDER BY place_id", [namespace]).fetchall()]
        views = [place_view(self.conn, namespace, pid) for pid in place_ids]
        return [v for v in views if v and v["place_type"] not in WATER_PLACE_TYPES]

    # ------------------------------------------------------------------ proposals

    def _candidates(self, namespace, subject, place, *, principal_id) -> list[tuple[str, dict[str, Any]]]:
        from src.kb.geospatial import GeospatialStore

        published, revision = subject["published"], subject["revision"]
        cite = {"subject_revision_id": revision["revision_id"], "place_revision_id": place["revision_id"],
                "source_url": revision["statement"]["source"]["url"]}
        found = []
        if subject["kind"] == "station":
            river = published.get("river") or {}
            identifier, scheme = river.get("identifier"), river.get("scheme")
            if identifier and place["identifiers"].get(scheme) == identifier:
                found.append(("published-river-identifier", {**cite, "scheme": scheme, "identifier": identifier}))
            elif not identifier and river.get("name") and place["place_type"] == "river" and \
                    _fold(river["name"]) in place["names"]:
                found.append(("river-name", {**cite, "river_name": river["name"],
                                             "note": "no river identifier is published; name equality only"}))
            location = published.get("location") or {}
            if place["geometry"] and location.get("latitude") is not None:
                point = [location["longitude"], location["latitude"]]
                if contains(place["geometry"], point):
                    receipt = GeospatialStore(self.conn, initialize=False).relation(
                        place["namespace"], "contains", place["geometry_id"], point, scopes=GEO_SCOPES,
                        principal_id=principal_id)
                    found.append(("published-coordinates-within", {
                        **cite, "point": point, "geometry_id": place["geometry_id"],
                        "receipt_id": receipt["receipt_id"], "basis": location.get("basis")}))
        else:
            code = published["eu_code"]
            if code in {v for k, v in place["identifiers"].items() if k in {"eu-water-body-code", "wfd-code"}}:
                found.append(("published-water-body-code", {**cite, "eu_code": code}))
            geometry = published.get("geometry")
            if geometry and place["geometry"]:
                points = vertices(geometry)
                inside = [p for p in points if contains(place["geometry"], p)]
                if inside:
                    receipts = [GeospatialStore(self.conn, initialize=False).relation(
                        place["namespace"], "contains", place["geometry_id"], p, scopes=GEO_SCOPES,
                        principal_id=principal_id)["receipt_id"] for p in points]
                    method = "published-geometry-within" if len(inside) == len(points) else \
                        "published-geometry-intersects"
                    found.append((method, {**cite, "geometry_id": place["geometry_id"],
                                           "published_geometry": geometry["source"], "vertices": len(points),
                                           "vertices_inside": len(inside), "receipt_ids": receipts}))
        return found

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                place_ids: Iterable[str] | None = None) -> dict[str, Any]:
        """Offer station/water-body to place and river candidates for review; accept nothing."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        subjects = self.subjects(namespace)
        places = self.places(namespace, place_ids)
        offered, reached = [], set()
        for key, subject in sorted(subjects.items()):
            for place in places:
                for method, evidence in self._candidates(namespace, subject, place, principal_id=principal_id):
                    reached.add(key)
                    offered.append(self._offer(namespace, key, subject, place, method, evidence,
                                               principal_id=principal_id))
        return {"contract": IDENTITY_CONTRACT, "proposed": sorted(o["match_id"] for o in offered if o["change"]),
                "places": [p["place_id"] for p in places],
                "unmatched": sorted(set(subjects) - reached),
                "matches": self.matches(namespace, scopes=scopes),
                "policy": "every proposal needs a reviewer; identifiers before names; geometry only as published"}

    def _offer(self, namespace, key, subject, place, method, evidence, *, principal_id) -> dict[str, Any]:
        from src.kb.entities import register_canonical_entity

        match_id = "water-idm:" + digest([namespace, key, place["place_id"], method])[:24]
        row = self.conn.execute("SELECT state, evidence_json FROM water_identity_matches WHERE namespace=? AND "
                                "match_id=?", [namespace, match_id]).fetchone()
        evidence = {**evidence, "method": method, "evidence_class": EVIDENCE_CLASS[method],
                    "note": "a reviewed match connects the record to the place; nothing is merged"}
        if row is not None:
            if row[0] == "proposed" and json.loads(row[1]) != evidence:
                self.conn.execute("UPDATE water_identity_matches SET evidence_json=? WHERE namespace=? AND "
                                  "match_id=?", [canonical(evidence), namespace, match_id])
                return {"match_id": match_id, "change": "updated"}
            return {"match_id": match_id, "change": None}
        name = subject["published"].get("name") or key
        entities = [register_canonical_entity(self.conn, entity_id(key), name, f"water-{subject['kind']}"),
                    register_canonical_entity(self.conn, entity_id("place:" + place["place_id"]), place["name"],
                                              "place")]
        now = self.now()
        self.conn.execute(
            "INSERT INTO water_identity_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, match_id, key, subject["kind"], place["place_id"], entities[0], entities[1],
             RELATIONS[method], method, canonical(evidence), "proposed", None, principal_id, now,
             canonical([{"state": "proposed", "by": principal_id, "at_ms": now, "method": method}])])
        return {"match_id": match_id, "change": "created"}

    # ------------------------------------------------------------------ reviews

    def _row(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT match_id, subject_key, subject_kind, place_id, subject_entity, place_entity, relation, method, "
            "evidence_json, state, decision_id, created_by, created_at_ms, history_json FROM water_identity_matches "
            "WHERE namespace=? AND match_id=?", [namespace, match_id]).fetchone() if self._ready() else None
        if row is None:
            raise WaterError("not_found", "water identity match is not visible in this namespace")
        history = json.loads(row[13])
        reviewed = [h for h in history if h["state"] in {"accepted", "rejected", "reverted"}]
        return {"contract": IDENTITY_CONTRACT, "namespace": namespace,
                **dict(zip(("match_id", "subject_key", "subject_kind", "place_id", "subject_entity", "place_entity",
                            "relation", "method"), row[:8])),
                "evidence_class": EVIDENCE_CLASS[row[7]], "evidence": json.loads(row[8]), "state": row[9],
                "decision_id": row[10], "created_by": row[11], "created_at_ms": row[12], "history": history,
                "reviewer": reviewed[-1]["by"] if reviewed and row[9] != "proposed" else None,
                "reviewed_at_ms": reviewed[-1]["at_ms"] if reviewed and row[9] != "proposed" else None}

    def matches(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                subject_key: str | None = None, place_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM water_identity_matches WHERE namespace=? AND (? IS NULL OR state=?) AND "
            "(? IS NULL OR subject_key=?) AND (? IS NULL OR place_id=?) ORDER BY subject_key, place_id, match_id",
            [namespace, state, state, subject_key, subject_key, place_id, place_id]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def accepted(self, namespace: str, *, subject_key: str | None = None,
                 place_id: str | None = None) -> list[dict[str, Any]]:
        """Accepted matches (no scope check: callers authorize first)."""
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM water_identity_matches WHERE namespace=? AND state='accepted' AND "
            "(? IS NULL OR subject_key=?) AND (? IS NULL OR place_id=?) ORDER BY subject_key, place_id, match_id",
            [namespace, subject_key, subject_key, place_id, place_id]).fetchall()
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
        self.history.register_entity(namespace, match["subject_entity"], [match["subject_key"]],
                                     principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        self.history.register_entity(namespace, match["place_entity"], ["place:" + match["place_id"]],
                                     principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match",
            [match["subject_entity"], match["place_entity"]],
            {"match_id": match_id, "method": match["method"], "relation": match["relation"],
             "evidence": match["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "environment.water", "records": [match["subject_key"], match["place_id"]]},
             "policy": {"merge": False, "note": "identity decision only; the record and the place stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"water-identity:{namespace}:{match_id}:{len(match['history'])}")
        state = "accepted" if decision == "accept" else "rejected"
        return self._transition(namespace, match, state, recorded["decision_id"], principal_id, reason.strip())

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

    def find(self, namespace: str, query: str) -> list[str]:
        """Subject keys for ``provider:id``, a native id, a station number, an EU code or an exact name."""
        text = str(query or "").strip()
        if not text:
            raise WaterError("invalid_request", "give a station id or number, an EU water-body code or a name")
        records = [r for r in self.store.records(namespace) if r["record_type"] in {"station", "water_body"}]
        if any(r["subject_key"] == text for r in records):
            return [text]
        direct = sorted({r["subject_key"] for r in records if r["record_key"] == text})
        if direct:
            return direct
        numbers = []
        for record in records:
            if record["record_type"] == "station":
                current = self.store.current(namespace, record["record_id"])
                if current and current["statement"]["as_published"].get("number") == text:
                    numbers.append(record["subject_key"])
        if numbers:
            return sorted(numbers)
        return sorted({r["subject_key"] for r in records if _fold(r["subject_name"]) == _fold(text)})


__all__ = ["EVIDENCE_CLASS", "METHODS", "RELATIONS", "WaterIdentity", "contains", "entity_id", "place_view",
           "vertices"]
