"""Affected places and cross-source event correspondences through reviewable identity (NH08, #2341).

Two publishers describing one physical event (USGS, EMSC and GDACS for one
earthquake; NHC and GDACS for one storm; EFFIS and GDACS for one fire) stay two
records. This module only *links* them:

* **Candidates carry their method.** ``shared_identifier`` (a published ID of
  one record appears among the other's published IDs, e.g. GDACS ``sourceid``
  = a USGS network ID or an NHC storm ID), ``glide`` (the same GLIDE number)
  or ``proximity`` (earthquakes whose published origin times and epicentres lie
  within declared tolerances, measured through the geospatial owner's
  ``proximity`` relation with a replayable spatial receipt). Each candidate pins
  both record revisions, its evidence and a confidence.
* **Review is someone else's.** A different principal with
  ``knowledge:hazards:review`` accepts or rejects a candidate with a reason; the
  decision is an entity-identity ``match`` / ``non-match`` in
  :class:`~src.kb.entity_history.EntityHistoryStore` with ``merge: false``, and a
  revert undoes it there. Accepting never replaces, averages or merges either
  publisher's parameters.
* **Unmatched stays visible** as unmatched.
* **Places come from published geometry only.** An event's published geometry
  (never buffered or modelled) is related to ``geospatial`` places through
  ``contains`` / ``intersects`` relations; each link records the place boundary
  it used (geometry id, generation, validity and content hash) and the spatial
  receipt. No new spatial or entity store is added.
"""

from __future__ import annotations

import json

from src.kb.hazards_records import READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.hazards_store import GEO_SCOPES, HazardStore, HazardStoreError, authorize, ms

CONTRACT = "noesis-hazard-correspondence-v1"
PLACE_CONTRACT = "noesis-hazard-place-link-v1"
PROXIMITY_SECONDS = 60
PROXIMITY_METRES = 100_000.0
_ENTITY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                  "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS hazard_correspondences(
 correspondence_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, left_record TEXT NOT NULL, right_record TEXT NOT NULL,
 left_revision TEXT NOT NULL, right_revision TEXT NOT NULL, method TEXT NOT NULL, evidence_json TEXT NOT NULL,
 confidence DOUBLE NOT NULL, proposed_by TEXT NOT NULL, proposed_at_ms BIGINT NOT NULL,
 UNIQUE(namespace, left_record, right_record, method));
CREATE TABLE IF NOT EXISTS hazard_correspondence_reviews(
 review_id TEXT PRIMARY KEY, correspondence_id TEXT NOT NULL, seq BIGINT NOT NULL, state TEXT NOT NULL,
 principal_id TEXT NOT NULL, reason TEXT NOT NULL, entity_decision_id TEXT, created_at_ms BIGINT NOT NULL,
 UNIQUE(correspondence_id, seq));
CREATE TABLE IF NOT EXISTS hazard_place_links(
 link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
 place_id TEXT NOT NULL, place_namespace TEXT NOT NULL, place_geometry_id TEXT NOT NULL, relation TEXT NOT NULL,
 boundary_json TEXT NOT NULL, receipt_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
"""


def _published_ids(record):
    """Identifiers a publisher states for its own record (never inferred)."""

    content, provider = record["content"], record["provider"]
    ids = dict(content.get("identifiers") or {})
    tokens = {f"{provider}:{content['native_id']}"}
    if provider == "usgs":
        tokens |= {f"usgs:{i}" for i in ids.get("ids") or []}
    elif provider == "gdacs" and ids.get("sourceid"):
        source = str(ids.get("source") or "").upper()
        owner = {"NEIC": "usgs", "USGS": "usgs", "NOAA": "nhc", "NHC": "nhc", "EFFIS": "effis", "EMSC": "emsc"}.get(source)
        if owner:
            tokens.add(f"{owner}:{ids['sourceid']}")
    return tokens


def _glide(record):
    return (record["content"].get("identifiers") or {}).get("glide") or None


class HazardIdentity:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.store = HazardStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ correspondences

    def _events(self, namespace, scopes):
        return [r for r in self.store.records(namespace, scopes=scopes, record_type="hazard_event")
                if (r["content"] or {}).get("status") not in {"deleted", "merged"}]

    def propose(self, namespace, *, principal_id, scopes):
        """Create correspondence candidates between different publishers' events; nothing is accepted."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        events = self._events(namespace, scopes)
        created = []
        for index, left in enumerate(events):
            for right in events[index + 1:]:
                if left["provider"] == right["provider"] or left["hazard_type"] != right["hazard_type"]:
                    continue
                a, b = sorted([left, right], key=lambda r: r["record_id"])
                for method, evidence, confidence in self._methods(namespace, a, b, principal_id=principal_id):
                    cid = "hazard-corr:" + digest([namespace, a["record_id"], b["record_id"], method])[:24]
                    inserted = self.conn.execute(
                        "INSERT INTO hazard_correspondences VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING "
                        "RETURNING correspondence_id",
                        [cid, namespace, a["record_id"], b["record_id"], a["revision_id"], b["revision_id"], method,
                         canonical(evidence), float(confidence), principal_id, self.now()]).fetchall()
                    if inserted:
                        created.append(cid)
        return {"contract": CONTRACT, "created": created,
                "correspondences": self.correspondences(namespace, scopes=scopes),
                "unmatched": self.unmatched(namespace, scopes=scopes),
                "policy": "candidates only; a different principal reviews each; parameters are never merged"}

    def _methods(self, namespace, a, b, *, principal_id):
        shared = sorted(_published_ids(a) & _published_ids(b))
        if shared:
            yield "shared_identifier", {"shared": shared, "left_ids": sorted(_published_ids(a)),
                                        "right_ids": sorted(_published_ids(b))}, 0.99
        if _glide(a) and _glide(a) == _glide(b):
            yield "glide", {"glide": _glide(a)}, 0.95
        if a["hazard_type"] != "earthquake":
            return
        ta, tb = ms(a["content"].get("event_time")), ms(b["content"].get("event_time"))
        ga, gb = a["content"].get("geometry"), b["content"].get("geometry")
        if None in (ta, tb) or not ga or not gb or ga["type"] != "Point" or gb["type"] != "Point" or not a["geometry_id"]:
            return
        seconds = abs(ta - tb) / 1000
        if seconds > PROXIMITY_SECONDS:
            return
        relation = self.store.geo.relation(namespace, "proximity", a["geometry_id"], gb["coordinates"], scopes=GEO_SCOPES,
                                           principal_id=principal_id, tolerance_m=PROXIMITY_METRES)
        if not relation["result"]["within_tolerance"]:
            return
        distance = float(relation["result"]["distance_m"])
        confidence = max(0.1, round(1 - 0.5 * seconds / PROXIMITY_SECONDS - 0.4 * distance / PROXIMITY_METRES, 3))
        yield "proximity", {"origin_time_difference_s": round(seconds, 3), "epicentre_distance_m": round(distance, 1),
                            "tolerances": {"seconds": PROXIMITY_SECONDS, "metres": PROXIMITY_METRES},
                            "spatial_receipt_id": relation.get("receipt_id")}, confidence

    def _reviews(self, cid):
        return [{"seq": int(r[0]), "state": r[1], "principal_id": r[2], "reason": r[3], "entity_decision_id": r[4],
                 "at_ms": int(r[5])} for r in self.conn.execute(
            "SELECT seq, state, principal_id, reason, entity_decision_id, created_at_ms FROM hazard_correspondence_reviews "
            "WHERE correspondence_id=? ORDER BY seq", [cid]).fetchall()]

    def correspondence(self, namespace, cid):
        row = self.conn.execute(
            "SELECT left_record, right_record, left_revision, right_revision, method, evidence_json, confidence, proposed_by, "
            "proposed_at_ms FROM hazard_correspondences WHERE namespace=? AND correspondence_id=?", [namespace, cid]).fetchone()
        if row is None:
            raise HazardStoreError("not_found", "correspondence is not visible in this namespace")
        reviews = self._reviews(cid)
        return {"contract": CONTRACT, "correspondence_id": cid, "left_record": row[0], "right_record": row[1],
                "left_revision": row[2], "right_revision": row[3], "method": row[4], "evidence": json.loads(row[5]),
                "confidence": row[6], "proposed_by": row[7], "proposed_at_ms": int(row[8]),
                "state": reviews[-1]["state"] if reviews else "proposed", "reviews": reviews,
                "merge": False, "note": "records stay separate; each publisher's parameters are shown as published"}

    def correspondences(self, namespace, *, scopes, record_id=None, state=None):
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT correspondence_id FROM hazard_correspondences WHERE namespace=? AND (? IS NULL OR left_record=? OR "
            "right_record=?) ORDER BY correspondence_id", [namespace, record_id, record_id, record_id]).fetchall()
        items = [self.correspondence(namespace, r[0]) for r in rows]
        return [i for i in items if state is None or i["state"] == state]

    def review(self, namespace, cid, decision, reason, *, principal_id, scopes):
        from src.kb.entity_history import EntityHistoryStore

        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise HazardStoreError("invalid_decision", "accept or reject with a reason")
        item = self.correspondence(namespace, cid)
        if item["state"] not in {"proposed", "reverted"}:
            raise HazardStoreError("invalid_state", f"correspondence is {item['state']}; revert it before re-reviewing")
        if item["proposed_by"] == principal_id:
            raise HazardStoreError("self_review", "the proposer cannot review their own correspondence")
        history = EntityHistoryStore(self.conn, now=self.now)
        entities = [f"hazard-record:{item['left_record']}", f"hazard-record:{item['right_record']}"]
        for entity, record in zip(entities, (item["left_record"], item["right_record"]), strict=True):
            history.register_entity(namespace, entity, [record], principal_id=principal_id, scopes=_ENTITY_SCOPES)
        seq = len(item["reviews"]) + 1
        recorded = history.decide(
            namespace, "match" if decision == "accept" else "non-match", entities,
            {"correspondence_id": cid, "method": item["method"], "evidence": item["evidence"], "reason": reason.strip(),
             "revisions": [item["left_revision"], item["right_revision"]],
             "policy": {"merge": False, "note": "the same physical event per a reviewer; parameters stay each publisher's"},
             "provenance": {"producer": "hazards.core"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_SCOPES,
            event_key=f"hazard-correspondence:{namespace}:{cid}:{seq}")
        return self._transition(cid, seq, "accepted" if decision == "accept" else "rejected", principal_id, reason,
                                recorded["decision_id"], namespace)

    def revert(self, namespace, cid, reason, *, principal_id, scopes):
        from src.kb.entity_history import EntityHistoryStore

        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise HazardStoreError("invalid_decision", "a revert needs a reason")
        item = self.correspondence(namespace, cid)
        if item["state"] not in {"accepted", "rejected"}:
            raise HazardStoreError("invalid_state", "only an accepted or rejected correspondence can be reverted")
        undo = EntityHistoryStore(self.conn, now=self.now).undo(
            namespace, item["reviews"][-1]["entity_decision_id"], reviewer_id=principal_id, principal_id=principal_id,
            scopes=_ENTITY_SCOPES)
        return self._transition(cid, len(item["reviews"]) + 1, "reverted", principal_id, reason, undo["decision_id"],
                                namespace)

    def _transition(self, cid, seq, state, principal_id, reason, decision_id, namespace):
        self.conn.execute("INSERT INTO hazard_correspondence_reviews VALUES (?,?,?,?,?,?,?,?)",
                          ["hazard-corr-review:" + digest([cid, seq])[:24], cid, seq, state, principal_id, reason.strip(),
                           decision_id, self.now()])
        return self.correspondence(namespace, cid)

    def accepted_for(self, namespace, record_id):
        """Records an accepted correspondence links to ``record_id`` (with the correspondence id)."""

        if not self._ready():
            return []
        result = []
        for row in self.conn.execute(
                "SELECT correspondence_id, left_record, right_record FROM hazard_correspondences WHERE namespace=? AND "
                "(left_record=? OR right_record=?) ORDER BY correspondence_id", [namespace, record_id, record_id]).fetchall():
            reviews = self._reviews(row[0])
            if reviews and reviews[-1]["state"] == "accepted":
                result.append((row[0], row[2] if row[1] == record_id else row[1]))
        return result

    def _ready(self):
        return bool(self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='hazard_correspondences'")
                    .fetchone())

    def unmatched(self, namespace, *, scopes, hazard_type=None):
        """Events with no accepted correspondence stay visible as unmatched (with any open candidates)."""

        result = []
        for record in self._events(namespace, scopes):
            if hazard_type and record["hazard_type"] != hazard_type:
                continue
            if self.accepted_for(namespace, record["record_id"]):
                continue
            open_ = [c["correspondence_id"] for c in self.correspondences(namespace, scopes=scopes,
                                                                             record_id=record["record_id"], state="proposed")]
            result.append({"record_id": record["record_id"], "provider": record["provider"],
                           "native_id": record["native_id"], "hazard_type": record["hazard_type"],
                           "state": "unmatched", "open_candidates": open_})
        return result

    # ------------------------------------------------------------------- places

    def resolve_places(self, namespace, record_id, *, principal_id, scopes, as_of_ms=None):
        """Relate the revision's *published* geometry to geospatial places; record the boundary vintage used."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        record = self.store.record(namespace, record_id, scopes=scopes, as_of_ms=as_of_ms)
        content = record["content"] or {}
        geometry = content.get("geometry")
        if not geometry or not record.get("geometry_id"):
            return {"contract": PLACE_CONTRACT, "record_id": record_id, "links": [], "state": "no published geometry",
                    "published_countries": content.get("countries") or []}
        from src.kb.geospatial import _points

        points = _points(geometry)
        lons, lats = [p[0] for p in points], [p[1] for p in points]
        found = self.store.geo.search(namespace, scopes={"knowledge:geospatial:read"},
                                      bbox=[min(lons) - 1, min(lats) - 1, max(lons) + 1, max(lats) + 1], limit=200)
        links = []
        for candidate in found["items"]:
            place_id = candidate.get("place_id")
            if not place_id or candidate["geometry"]["type"] not in {"Polygon", "MultiPolygon"}:
                continue
            place = self.store.geo.place(candidate["namespace"], place_id, scopes={"knowledge:geospatial:read"})
            if place is None or place["place_type"].startswith("hazard-"):
                continue
            relation, receipt = self._relate(namespace, record["geometry_id"], geometry, candidate, principal_id)
            if relation is None:
                continue
            boundary = {"geometry_id": candidate["geometry_id"], "generation": candidate["generation"],
                        "valid_from_ms": candidate["valid_from_ms"], "valid_to_ms": candidate["valid_to_ms"],
                        "observed_at_ms": candidate["observed_at_ms"], "content_hash": candidate["content_hash"],
                        "source": candidate["source"], "place_revision_id": place["revision_id"]}
            link_id = "hazard-place:" + digest([namespace, record["revision_id"], candidate["geometry_id"]])[:24]
            self.conn.execute("INSERT INTO hazard_place_links VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                              [link_id, namespace, record_id, record["revision_id"], place_id, candidate["namespace"],
                               candidate["geometry_id"], relation, canonical(boundary), receipt, self.now()])
            links.append({"link_id": link_id, "place_id": place_id, "place_name": place["canonical_name"],
                          "place_type": place["place_type"], "relation": relation, "boundary": boundary,
                          "spatial_receipt_id": receipt, "revision_id": record["revision_id"]})
        return {"contract": PLACE_CONTRACT, "record_id": record_id, "revision_id": record["revision_id"],
                "links": links, "state": "resolved" if links else "no place matched",
                "published_countries": content.get("countries") or [],
                "method": "published geometry only (no buffer, no modelled footprint)",
                "citation": record["citation"]}

    def _relate(self, namespace, event_geometry_id, geometry, candidate, principal_id):
        from src.kb.geospatial import _points

        geo = self.store.geo
        if candidate["namespace"] != namespace:
            return None, None  # relations run inside one namespace; global gazetteer points are not boundaries
        if geometry["type"] == "Point":
            result = geo.relation(namespace, "contains", candidate["geometry_id"], geometry["coordinates"],
                                  scopes=GEO_SCOPES, principal_id=principal_id)
            return ("contains" if result["result"]["contains"] else None), result.get("receipt_id")
        if geometry["type"] == "MultiPolygon" or candidate["geometry"]["type"] == "MultiPolygon":
            for point in _points(geometry):
                result = geo.relation(namespace, "contains", candidate["geometry_id"], point, scopes=GEO_SCOPES,
                                      principal_id=principal_id)
                if result["result"]["contains"]:
                    return "intersects", result.get("receipt_id")
            return None, None
        result = geo.relation(namespace, "intersects", event_geometry_id, candidate["geometry_id"], scopes=GEO_SCOPES,
                              principal_id=principal_id)
        if result["result"]["intersects"]:
            return "intersects", result.get("receipt_id")
        for left, point in ((candidate["geometry_id"], _points(geometry)[0]),
                            (event_geometry_id, _points(candidate["geometry"])[0])):
            inside = geo.relation(namespace, "contains", left, point, scopes=GEO_SCOPES, principal_id=principal_id)
            if inside["result"]["contains"]:
                return "intersects", inside.get("receipt_id")
        return None, None

    def place_links(self, namespace, *, place_id=None, record_id=None):
        rows = self.conn.execute(
            "SELECT link_id, record_id, revision_id, place_id, place_geometry_id, relation, boundary_json, receipt_id "
            "FROM hazard_place_links WHERE namespace=? AND (? IS NULL OR place_id=?) AND (? IS NULL OR record_id=?) "
            "ORDER BY link_id", [namespace, place_id, place_id, record_id, record_id]).fetchall()
        return [{"link_id": r[0], "record_id": r[1], "revision_id": r[2], "place_id": r[3], "place_geometry_id": r[4],
                 "relation": r[5], "boundary": json.loads(r[6]), "spatial_receipt_id": r[7]} for r in rows]


def side_by_side(store, namespace, record_id, *, scopes, as_of_ms=None, basis="publisher"):
    """Each accepted correspondent's revision in force at the same as-of, as published (never combined)."""

    identity = HazardIdentity(store.conn, initialize=False, now=store.now)
    result = []
    for cid, other in identity.accepted_for(namespace, record_id):
        view = store.record(namespace, other, scopes=scopes, as_of_ms=as_of_ms, basis=basis)
        item = identity.correspondence(namespace, cid)
        result.append({"correspondence_id": cid, "method": item["method"], "record_id": other,
                       "provider": view["provider"], "native_id": view["native_id"],
                       "revision_id": view.get("revision_id"),
                       "parameters": (view.get("content") or {}).get("parameters"),
                       "citation": view.get("citation"),
                       "note": "shown beside, never merged or averaged"})
    return result

