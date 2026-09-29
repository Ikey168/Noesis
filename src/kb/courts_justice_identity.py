"""Courts, organisational parties and statistic places through reviewable identity (#2218, CJ07).

* **Courts** map to canonical court entities by their CourtListener court id
  (``courts:court:courtlistener:<id>``), with the published court resource URL
  as the cited identifier. Nothing is matched by court name.
* **Organisational parties** (the CJ01 minimisation decision keeps only these
  by name) are *proposed* into the shared reviewable state machine
  (:class:`src.kb.ownership_identity.OwnershipIdentityService`, whose accepted
  and reverted decisions are :class:`src.kb.entity_history.EntityHistoryStore`
  decisions): ``exact-identifier`` when a party carries a published identifier
  an ownership record also carries, ``name-jurisdiction`` against US ownership
  legal entities with an equal normalized name (low evidence), ``similar-name``
  against a canonical entity alias (shown, never acceptable). Nothing is
  accepted automatically.
* **Natural persons** have no party key and are never proposed, matched or
  aggregated across dockets.
* **Reporting areas** (FBI state and agency ORI, police.uk force
  neighbourhoods, Eurostat GEO codes) resolve to :mod:`src.kb.geospatial`
  places only by an exact published code in a place's ``source_ids``; one
  place is ``resolved`` (the mapping source cited), several stay an
  ``ambiguous`` review candidate, none is ``unresolved``. A reviewer accepts or
  rejects any resolution; a rejected one is never used. No new entity or
  spatial store is added.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from src.kb.courts_justice import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    CourtsJusticeError,
    authorize,
    canonical,
    digest,
    load,
    table_exists,
)

# Published code scheme -> the geospatial place ``source_ids`` key that carries it.
PLACE_ID_KEYS = {"us-state": "us-state", "fbi-ori": "fbi-ori", "police-uk-neighbourhood": "police-uk-neighbourhood",
                 "police-uk-polygon": "police-uk-polygon", "eurostat-geo": "nuts"}
_DDL = """
CREATE TABLE IF NOT EXISTS justice_place_resolutions (
  resolution_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, place_scheme TEXT NOT NULL, place_code TEXT NOT NULL,
  geo_namespace TEXT NOT NULL, candidates_json TEXT NOT NULL, status TEXT NOT NULL, selected_place_id TEXT,
  mapping_source_json TEXT NOT NULL, review_state TEXT NOT NULL, history_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
"""


def _norm(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def entity_for(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


class CourtsIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)

    def courts(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        ids: set[str] = set()
        for table in ("legal_docket_revisions", "legal_opinion_revisions"):
            if table_exists(self.conn, table):
                ids |= {r[0] for r in self.conn.execute(f"SELECT DISTINCT court_id FROM {table} WHERE namespace=? "
                                                        "AND court_id IS NOT NULL", [namespace]).fetchall()}
        return [{"court_id": court_id, "court_key": f"courts:court:courtlistener:{court_id}",
                 "entity_id": entity_for(f"courts:court:courtlistener:{court_id}"),
                 "published_identifiers": [{"scheme": "courtlistener-court-id", "value": court_id},
                                           {"scheme": "courtlistener-court-url",
                                            "value": f"https://www.courtlistener.com/api/rest/v4/courts/{court_id}/"}],
                 "basis": "mapped by the CourtListener court id as published; never by name"}
                for court_id in sorted(ids)]

    def parties(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Organisational parties of the current docket revisions (natural persons are never listed)."""
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "legal_docket_parties"):
            return []
        rows = self.conn.execute(
            "SELECT r.record_key, p.party_key, p.name_as_published, p.roles_json, r.revision_id FROM "
            "legal_docket_parties p JOIN legal_docket_revisions r USING(revision_id) WHERE r.namespace=? AND "
            "p.party_type='organisation' AND r.revision_no=(SELECT max(revision_no) FROM legal_docket_revisions x "
            "WHERE x.namespace=r.namespace AND x.record_key=r.record_key) ORDER BY p.party_key",
            [namespace]).fetchall()
        return [{"party_key": r[1], "docket_key": r[0], "name_as_published": r[2], "roles": load(r[3], []),
                 "revision_id": r[4], "entity_id": entity_for(r[1]), "identifiers": []} for r in rows]

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                ownership_namespace: str | None = None, canonical_names: bool = True) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        offered = []
        entities = []
        if ownership_namespace and table_exists(self.conn, "ownership_records"):
            entities = self.service._entities(ownership_namespace, principal_id, scopes)
        for party in self.parties(namespace, scopes=scopes):
            evidence_left = {"record_key": party["party_key"], "name_as_published": party["name_as_published"],
                             "roles": party["roles"], "docket": party["docket_key"],
                             "revision_id": party["revision_id"]}
            for entity in entities:
                body = entity["record"]
                right = {"record_key": body["record_key"], "provider": body["source"]["provider"],
                         "revision": entity["revision"]}
                shared = [i for i in body.get("identifiers") or [] if any(
                    i["scheme"] == p["scheme"] and str(i["value"]) == str(p["value"]) for p in party["identifiers"])]
                if shared:
                    offered.append(self._offer(namespace, party, body["record_key"], body.get(
                        "canonical_entity_id") or entity_for(body["record_key"]), "exact-identifier", {
                        "identifiers": shared, "left": evidence_left, "right": right}, principal_id, scopes))
                elif _norm(body.get("name")) == _norm(party["name_as_published"]) and str(
                        body.get("jurisdiction") or "").split("-")[0].upper() == "US":
                    offered.append(self._offer(namespace, party, body["record_key"], body.get(
                        "canonical_entity_id") or entity_for(body["record_key"]), "name-jurisdiction", {
                        "normalized_name": _norm(body.get("name")), "country": "US", "left": evidence_left,
                        "right": right, "note": "a name alone is low evidence; a reviewer decides"},
                        principal_id, scopes))
            if canonical_names and table_exists(self.conn, "entity_aliases"):
                from src.kb.entities import resolve

                found = resolve(self.conn, party["name_as_published"])
                if found:
                    key = f"canonical:{found['canonical_id']}"
                    offered.append(self._offer(namespace, party, key, found["canonical_id"], "similar-name", {
                        "left": evidence_left, "right": {"record_key": key, "canonical_id": found["canonical_id"]},
                        "note": "a name alone is never an identity"}, principal_id, scopes))
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes),
                "natural_persons": "never proposed, matched or aggregated (CJ01 minimisation decision)"}

    def _offer(self, namespace, party, right_key, right_entity, basis, evidence, principal_id, scopes):
        return self.service.offer(namespace, left_key=party["party_key"], right_key=right_key,
                                  left_entity=party["entity_id"], right_entity=right_entity, basis=basis,
                                  evidence=[{**evidence, "method": basis}], principal_id=principal_id, scopes=scopes)

    @staticmethod
    def view(candidate: dict[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        return {"candidate_id": candidate["candidate_id"], "state": candidate["state"],
                "method": candidate["basis"], "confidence": candidate["confidence"],
                "records": [candidate["left_key"], candidate["right_key"]],
                "entities": [candidate["left_entity"], candidate["right_entity"]],
                "decision_id": candidate["decision_id"],
                "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
                "reason": last.get("reason"), "evidence": candidate["evidence"], "history": candidate["history"],
                "notice": "a reviewable identity decision for an organisational party; records are never merged"}

    def candidates(self, namespace: str, *, scopes: Iterable[str], party_key: str | None = None
                   ) -> list[dict[str, Any]]:
        rows = [c for c in self.service.candidates(namespace, scopes=scopes, record_key=party_key)
                if c["left_key"].startswith("courts:party:") or c["right_key"].startswith("courts:party:")]
        return [self.view(c) for c in rows]

    def _own(self, namespace, candidate_id, scopes):
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise CourtsJusticeError("not_found", "no court party identity candidate with that id")

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

    def accepted_parties(self, namespace: str, entity: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Party keys linked to an entity (canonical entity id or record key) by an accepted, unreverted match."""
        out = []
        for view in self.candidates(namespace, scopes=scopes):
            if view["state"] != "accepted":
                continue
            (left, right), (left_entity, right_entity) = view["records"], view["entities"]
            for party, other, other_entity in ((left, right, right_entity), (right, left, left_entity)):
                if party.startswith("courts:party:") and entity in {other, other_entity}:
                    docket_id = party.split(":")[3]
                    out.append({"candidate_id": view["candidate_id"], "party_key": party,
                                "docket_key": f"courts:docket:courtlistener:{docket_id}", "method": view["method"],
                                "reviewer": view["reviewer"], "decision_id": view["decision_id"]})
        return out


class JusticePlaces:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _places(self, geo_namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        from src.kb.geospatial import READ_SCOPE as GEO_READ
        from src.kb.geospatial import GeospatialStore

        geo = GeospatialStore(self.conn, initialize=False)
        rows = self.conn.execute("SELECT DISTINCT place_id, namespace FROM geospatial_place_revisions WHERE "
                                 "namespace IN (?, 'global') ORDER BY place_id", [geo_namespace]).fetchall()
        return [p for p in (geo.place(ns, pid, scopes={GEO_READ}) for pid, ns in rows) if p]

    def resolve(self, namespace: str, *, geo_namespace: str, principal_id: str, scopes: Iterable[str]
                ) -> dict[str, Any]:
        """Resolve every statistic reporting area by exact published code; idempotent per code and place set."""
        from src.kb.justice_statistics import JusticeStatisticsStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if "knowledge:geospatial:read" not in scopes:
            raise CourtsJusticeError("unauthorized", "knowledge:geospatial:read is required to resolve places")
        places = self._places(geo_namespace)
        out = []
        for scheme, code, label in JusticeStatisticsStore(self.conn, initialize=False).place_codes(namespace):
            key = PLACE_ID_KEYS.get(scheme, scheme)
            matches = [p for p in places if str(dict(p["source_ids"]).get(key) or "") == code]
            candidates = [{"place_id": p["place_id"], "namespace": p["namespace"], "revision_id": p["revision_id"],
                           "canonical_name": p["canonical_name"], "source_ids": p["source_ids"]} for p in matches]
            status = "resolved" if len(matches) == 1 else "ambiguous" if matches else "unresolved"
            mapping = {"method": "exact published code", "code_scheme": scheme, "place_source_id_key": key,
                       "code": code, "label_as_published": label,
                       "place_revisions": [c["revision_id"] for c in candidates]}
            resolution_id = "justice-place:" + digest([namespace, scheme, code, geo_namespace, candidates])[:24]
            exists = self.conn.execute("SELECT 1 FROM justice_place_resolutions WHERE resolution_id=?",
                                       [resolution_id]).fetchone()
            if not exists:
                self.conn.execute("INSERT INTO justice_place_resolutions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  [resolution_id, namespace, scheme, code, geo_namespace, canonical(candidates),
                                   status, matches[0]["place_id"] if status == "resolved" else None,
                                   canonical(mapping), "unreviewed" if status != "unresolved" else "none",
                                   canonical([{"state": status, "by": principal_id, "at_ms": self.now()}]),
                                   self.now()])
            out.append(self.resolution(namespace, resolution_id))
        return {"resolutions": out, "notice": "codes are matched exactly; ambiguous mappings stay review candidates"}

    def resolution(self, namespace: str, resolution_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT resolution_id, place_scheme, place_code, geo_namespace, candidates_json, "
                                "status, selected_place_id, mapping_source_json, review_state, history_json FROM "
                                "justice_place_resolutions WHERE namespace=? AND resolution_id=?",
                                [namespace, resolution_id]).fetchone()
        if row is None:
            raise CourtsJusticeError("not_found", "no place resolution with that id")
        return {"resolution_id": row[0], "scheme": row[1], "code": row[2], "geo_namespace": row[3],
                "candidates": load(row[4], []), "status": row[5], "selected_place_id": row[6],
                "mapping_source": load(row[7], {}), "review_state": row[8], "history": load(row[9], [])}

    def resolutions(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "justice_place_resolutions"):
            return []
        rows = self.conn.execute("SELECT resolution_id FROM justice_place_resolutions WHERE namespace=? ORDER BY "
                                 "place_scheme, place_code, created_at_ms", [namespace]).fetchall()
        return [self.resolution(namespace, r[0]) for r in rows]

    def review(self, namespace: str, resolution_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str], place_id: str | None = None) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        item = self.resolution(namespace, resolution_id)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise CourtsJusticeError("invalid_decision", "accept or reject with a reason")
        selected = item["selected_place_id"]
        if decision == "accept":
            ids = {c["place_id"] for c in item["candidates"]}
            selected = place_id or selected
            if selected not in ids:
                raise CourtsJusticeError("invalid_decision", "accept one of the candidate places")
        history = item["history"] + [{"state": f"{decision}ed" if decision == "reject" else "accepted",
                                      "by": principal_id, "reason": reason.strip(), "place_id": selected,
                                      "at_ms": self.now()}]
        self.conn.execute("UPDATE justice_place_resolutions SET review_state=?, selected_place_id=?, history_json=? "
                          "WHERE resolution_id=?", ["accepted" if decision == "accept" else "rejected",
                                                    selected if decision == "accept" else None, canonical(history),
                                                    resolution_id])
        return self.resolution(namespace, resolution_id)

    def codes_for_place(self, namespace: str, place_id: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Published codes mapped to a place by a resolved (not rejected) or accepted resolution."""
        out = []
        for item in self.resolutions(namespace, scopes=scopes):
            usable = item["review_state"] == "accepted" or (item["status"] == "resolved"
                                                            and item["review_state"] != "rejected")
            if usable and item["selected_place_id"] == place_id:
                out.append({"scheme": item["scheme"], "code": item["code"], "resolution_id": item["resolution_id"],
                            "status": item["status"], "review_state": item["review_state"],
                            "mapping_source": item["mapping_source"]})
        return out
