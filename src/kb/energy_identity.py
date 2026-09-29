"""Reviewable identity for zones, balancing areas, countries and plants (EN08).

Each source names its subjects in its own scheme: ENTSO-E bidding zones by
EIC code, EIA balancing areas by respondent code, Ember countries by ISO
alpha-3, Eurostat by its geo code, Energy-Charts by a lower-case country code,
plants and units by EIA plant/generator ids or ENTSO-E production-unit mRIDs.
This module proposes **candidate matches** from those subjects to the owners
that already hold identity; it never merges anything by itself:

* **Places** (countries, bidding zones, balancing areas) go through
  :class:`src.kb.geospatial.GeospatialStore`: countries are resolved by name
  from the declared code table (``config/energy/areas.json``) with a saved,
  reviewable geocode resolution; zones and balancing areas are registered as
  places (``bidding-zone`` / ``balancing-area``) with their published code as
  a source id and the matched countries as parents.
* **Plants and units** go to ``canonical_entities`` (``src/kb/entities.py``):
  by the published identifier (identifier-based canonical ids such as
  ``ent-eia-plant-99901``) or by an exact alias of the published name, and an
  accepted match is recorded as an ``entity_history`` identity decision
  (``match``; a rejection as ``non-match``; a revert undoes the decision).

Every match stores its method, evidence and review state (``candidate``,
``accepted``, ``rejected``, ``reverted``); reviews are append-only rows.
Ambiguous or unmatched subjects are listed in the proposal result, never
silently merged. Only accepted matches connect subjects in queries.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.kb.energy_records import READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.energy_store import EnergyStore, EnergyStoreError, authorize

CONTRACT = "noesis-energy-identity-match-v1"
AREAS = Path(__file__).resolve().parents[2] / "config/energy/areas.json"
GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:review"}
ENTITY_SCOPES = {"knowledge:entity-history:read", "knowledge:entity-history:write",
                 "knowledge:entity-history:review", "knowledge:entity-history:execute"}
STATES = ("candidate", "accepted", "rejected", "reverted", "ambiguous")
_DDL = """
CREATE TABLE IF NOT EXISTS energy_identity_matches(
 match_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_scheme TEXT NOT NULL,
 subject_code TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, method TEXT NOT NULL,
 evidence_json TEXT NOT NULL, proposed_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS energy_identity_reviews(
 review_id TEXT PRIMARY KEY, match_id TEXT NOT NULL, namespace TEXT NOT NULL, revision BIGINT NOT NULL,
 state TEXT NOT NULL, reason TEXT NOT NULL, reviewer TEXT NOT NULL, owner_ref_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, UNIQUE(match_id, revision));
"""
_COUNTRY_SCHEMES = {"iso3166-alpha2": "iso2", "iso3166-alpha3": "iso3", "eurostat-geo": "eurostat",
                    "energy-charts-country": "energy_charts"}


def area_table():
    return json.loads(AREAS.read_text())


def _country(scheme, code):
    field = _COUNTRY_SCHEMES.get(scheme)
    if field is None:
        return None
    return next((c for c in area_table()["countries"] if c.get(field) == code), None)


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


class EnergyIdentity:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.geospatial import GeospatialStore

        self.conn = conn
        self.store = EnergyStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.geo = GeospatialStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------- proposals

    def _insert(self, namespace, subject, target_kind, target_id, method, evidence, principal_id):
        subject = {k: subject[k] for k in ("kind", "scheme", "code")}
        match_id = "energy-match:" + digest([namespace, subject["scheme"], subject["code"], target_kind, target_id])[:24]
        self.conn.execute("INSERT INTO energy_identity_matches VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [match_id, namespace, subject["kind"], subject["scheme"], subject["code"], target_kind,
                           target_id, method, canonical(evidence), principal_id, self.now()])
        return self.match(namespace, match_id)

    def _country_place(self, namespace, country, principal_id):
        resolution = self.geo.resolve(namespace, country["place_name"], scopes=GEO_SCOPES,
                                      context={"energy_code_table": "config/energy/areas.json", "iso2": country["iso2"]})
        saved = self.geo.save_resolution(resolution, principal_id=principal_id, scopes=GEO_SCOPES)
        return saved

    def _area_place(self, namespace, kind, code, name, parents, principal_id):
        place = self.geo.register_place(
            namespace, f"{'Bidding zone' if kind == 'bidding-zone' else 'Balancing area'} {name} ({code})", kind,
            names=[{"value": f"{name} ({code})", "language": "und", "kind": "canonical"}],
            source_ids={"eic" if kind == "bidding-zone" else "eia_ba": code}, parent_ids=sorted(parents),
            principal_id=principal_id, scopes=GEO_SCOPES, place_key=f"energy:{kind}:{code}",
            producer={"name": "noesis-energy-pack", "version": "1.0.0"},
            provenance={"source": "config/energy/areas.json"})
        return place["place_id"]

    def propose(self, namespace, *, principal_id, scopes):
        """Candidate matches for every stored subject; ambiguous and unmatched subjects are reported."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        table = area_table()
        proposed, ambiguous, unmatched = [], [], []
        country_places = {}
        for country in table["countries"]:
            saved = self._country_place(namespace, country, principal_id)
            country_places[country["iso2"]] = saved
        for subject in self.store.subjects(namespace, scopes=scopes):
            kind, scheme, code = subject["kind"], subject["scheme"], subject["code"]
            if kind == "country":
                country = _country(scheme, code)
                if country is None:
                    unmatched.append({**subject, "reason": "code not in the declared code table"})
                    continue
                saved = country_places[country["iso2"]]
                if saved["status"] == "ambiguous":
                    ambiguous.append({**subject, "reason": "place resolution is ambiguous",
                                      "candidates": [c["place_id"] for c in saved["candidates"]],
                                      "resolution_id": saved["resolution_id"]})
                    self._insert(namespace, subject, "place", "ambiguous:" + saved["resolution_id"],
                                 "declared code table + geospatial place resolution",
                                 {"resolution_id": saved["resolution_id"], "status": "ambiguous"}, principal_id)
                    continue
                if saved["selected_place_id"] is None:
                    unmatched.append({**subject, "reason": f"no place resolves '{country['place_name']}'",
                                      "resolution_id": saved["resolution_id"]})
                    continue
                proposed.append(self._insert(
                    namespace, subject, "place", saved["selected_place_id"],
                    "declared code table + geospatial place resolution (exact name)",
                    {"code_table": {k: country[k] for k in ("iso2", "iso3", "eurostat", "energy_charts")},
                     "resolution_id": saved["resolution_id"], "place_name": country["place_name"],
                     "confidence": saved["confidence"]}, principal_id))
            elif kind in {"bidding-zone", "balancing-area"}:
                if kind == "bidding-zone":
                    entry = next((z for z in table["bidding_zones"] if z["eic"] == code), None)
                    members = (entry or {}).get("countries") or []
                else:
                    entry = next((b for b in table["balancing_areas"] if b["code"] == code), None)
                    members = [entry["country"]] if entry else []
                if entry is None:
                    unmatched.append({**subject, "reason": "code not in the declared code table"})
                    continue
                parents = [country_places[m]["selected_place_id"] for m in members
                           if m in country_places and country_places[m]["selected_place_id"]]
                place_id = self._area_place(namespace, kind, code, entry["name"], parents, principal_id)
                proposed.append(self._insert(
                    namespace, subject, "place", place_id, f"declared {'EIC' if kind == 'bidding-zone' else 'EIA'} code table",
                    {"code_table_entry": entry, "parent_place_ids": parents,
                     "unresolved_members": sorted(set(members) - {m for m in members if country_places.get(m, {}).get("selected_place_id")})},
                    principal_id))
            elif kind in {"plant", "unit"}:
                found = self._plant_candidates(subject)
                if not found:
                    unmatched.append({**subject, "reason": "no identifier-based or alias candidate"})
                for target, method, evidence in found:
                    proposed.append(self._insert(namespace, subject, "entity", target, method, evidence, principal_id))
                if len({t for t, _, _ in found}) > 1:
                    ambiguous.append({**subject, "reason": "more than one entity candidate; review each",
                                      "candidates": sorted({t for t, _, _ in found})})
            else:
                unmatched.append({**subject, "reason": f"no identity rule for {kind}"})
        return {"contract": CONTRACT, "namespace": namespace, "proposed": proposed, "ambiguous": ambiguous,
                "unmatched": unmatched,
                "notice": "candidates only; nothing is merged until a reviewer accepts a match"}

    def _plant_candidates(self, subject):
        from src.kb.entities import normalize_surface

        scheme, code = subject["scheme"], subject["code"]
        found = []
        if scheme in {"eia-generator", "eia-plant"}:
            plant = code.split(":", 1)[0]
            found.append((f"ent-eia-plant-{plant.lower()}", "published EIA plant id (identifier-based canonical id)",
                          {"eia_plant_id": plant, "subject": code}))
        elif scheme == "entsoe-unit":
            found.append((f"ent-entsoe-unit-{code.lower()}", "published ENTSO-E production unit mRID (identifier-based)",
                          {"entsoe_unit_mrid": code}))
        name = subject.get("name")
        if name and _table(self.conn, "entity_aliases"):
            from src.kb.entities import resolve

            match = resolve(self.conn, name)
            if match is not None:
                found.append((match["canonical_id"], "canonical_entities alias (exact normalised published name; review required)",
                              {"surface_form": normalize_surface(name), "alias_method": match["method"],
                               "preferred_name": match["preferred_name"]}))
        return found

    # ---------------------------------------------------------------- reviews

    def _reviews(self, match_id):
        return [{"review_id": r[0], "revision": int(r[1]), "state": r[2], "reason": r[3], "reviewer": r[4],
                 "owner_ref": json.loads(r[5]), "created_at_ms": r[6]}
                for r in self.conn.execute("SELECT review_id, revision, state, reason, reviewer, owner_ref_json, "
                                           "created_at_ms FROM energy_identity_reviews WHERE match_id=? ORDER BY revision",
                                           [match_id]).fetchall()]

    def match(self, namespace, match_id):
        row = self.conn.execute("SELECT match_id, subject_kind, subject_scheme, subject_code, target_kind, target_id, "
                                "method, evidence_json, proposed_by, created_at_ms FROM energy_identity_matches "
                                "WHERE namespace=? AND match_id=?", [namespace, match_id]).fetchone()
        if row is None:
            raise EnergyStoreError("match_not_found", "identity match is unavailable")
        reviews = self._reviews(match_id)
        state = reviews[-1]["state"] if reviews else ("ambiguous" if row[5].startswith("ambiguous:") else "candidate")
        return {"contract": CONTRACT, "match_id": row[0],
                "subject": {"kind": row[1], "scheme": row[2], "code": row[3]},
                "target": {"kind": row[4], "id": row[5]}, "method": row[6], "evidence": json.loads(row[7]),
                "proposed_by": row[8], "created_at_ms": row[9], "state": state, "reviews": reviews}

    def matches(self, namespace, *, scopes, state=None, subject_code=None):
        authorize(namespace, scopes, READ_SCOPE)
        if not _table(self.conn, "energy_identity_matches"):
            return []
        ids = [r[0] for r in self.conn.execute(
            "SELECT match_id FROM energy_identity_matches WHERE namespace=? AND (? IS NULL OR subject_code=?) "
            "ORDER BY subject_scheme, subject_code, match_id", [namespace, subject_code, subject_code]).fetchall()]
        result = [self.match(namespace, m) for m in ids]
        return [m for m in result if state is None or m["state"] == state]

    def review(self, namespace, match_id, decision, reason, *, principal_id, scopes):
        """Accept, reject or revert a match; the decision is also recorded with the owning store."""

        from src.kb.entity_history import EntityHistoryStore

        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject", "revert"} or not str(reason or "").strip():
            raise EnergyStoreError("invalid_review", "decision is accept, reject or revert, with a reason")
        current = self.match(namespace, match_id)
        if current["state"] == "ambiguous":
            raise EnergyStoreError("ambiguous", "an ambiguous resolution is resolved in the geospatial owner first")
        if current["proposed_by"] == principal_id and decision == "accept":
            raise EnergyStoreError("self_review", "a match is accepted by a principal other than its proposer")
        if decision == "revert" and current["state"] != "accepted":
            raise EnergyStoreError("invalid_review", "only an accepted match can be reverted")
        owner_ref = {}
        target = current["target"]
        if target["kind"] == "place" and current["evidence"].get("resolution_id"):
            geo_decision = {"accept": "accept", "reject": "reject", "revert": "defer"}[decision]
            review = self.geo.review(namespace, current["evidence"]["resolution_id"], geo_decision,
                                     selected_place_id=target["id"] if geo_decision == "accept" else None,
                                     reason=f"energy identity {decision}: {reason}", principal_id=principal_id,
                                     scopes=GEO_SCOPES)
            owner_ref = {"owner": "geospatial", "review_id": review["review_id"]}
        elif target["kind"] == "entity":
            history = EntityHistoryStore(self.conn, now=self.now)
            if decision == "revert":
                prior = next(r for r in reversed(current["reviews"]) if r["state"] == "accepted")
                undone = history.undo(namespace, prior["owner_ref"]["decision_id"], reviewer_id=principal_id,
                                      principal_id=principal_id, scopes=ENTITY_SCOPES)
                owner_ref = {"owner": "entity_history", "decision_id": undone["decision_id"],
                             "undoes": prior["owner_ref"]["decision_id"]}
            else:
                if target["id"].startswith("ent-") and decision == "accept":
                    from src.kb.entities import register_canonical_entity

                    register_canonical_entity(self.conn, target["id"], current["evidence"].get("preferred_name")
                                              or current["subject"]["code"], "power_plant")
                alias = f"{current['subject']['scheme']}:{current['subject']['code']}"
                history.register_entity(namespace, target["id"], [alias], principal_id=principal_id, scopes=ENTITY_SCOPES)
                made = history.decide(namespace, "match" if decision == "accept" else "non-match", [target["id"]],
                                      {"subject": alias, "method": current["method"], "reason": reason,
                                       "provenance": {"energy_match_id": match_id}},
                                      reviewer_id=principal_id, principal_id=principal_id, scopes=ENTITY_SCOPES,
                                      event_key=f"energy-identity:{match_id}:{len(current['reviews']) + 1}")
                owner_ref = {"owner": "entity_history", "decision_id": made["decision_id"]}
        revision = len(current["reviews"]) + 1
        state = {"accept": "accepted", "reject": "rejected", "revert": "reverted"}[decision]
        review_id = "energy-review:" + digest([match_id, revision, state])[:24]
        self.conn.execute("INSERT INTO energy_identity_reviews VALUES (?,?,?,?,?,?,?,?,?)",
                          [review_id, match_id, namespace, revision, state, reason, principal_id, canonical(owner_ref),
                           int(self.now())])
        return self.match(namespace, match_id)

    # ---------------------------------------------------------------- reading

    def connected(self, namespace, scheme, code, *, scopes):
        """Subjects reachable from one code through accepted matches, plus related (not identical) areas.

        ``same`` lists subjects accepted as the same place/entity, each with the
        match that connected it; ``related`` lists zones or areas whose place
        lies in (or contains) that place — shown beside, never merged.
        """

        authorize(namespace, scopes, READ_SCOPE)
        accepted = [m for m in self.matches(namespace, scopes=scopes) if m["state"] == "accepted"]
        start = {"scheme": scheme, "code": code}
        targets = {m["target"]["id"] for m in accepted if m["subject"]["scheme"] == scheme and m["subject"]["code"] == code}
        same = [{"subject": start, "match": None, "basis": "the requested code"}]
        for m in accepted:
            if m["target"]["id"] in targets and (m["subject"]["scheme"], m["subject"]["code"]) != (scheme, code):
                same.append({"subject": m["subject"], "match": {k: m[k] for k in ("match_id", "method", "state")},
                             "basis": f"accepted match to {m['target']['id']}"})
        related = []
        for m in accepted:
            if m["target"]["kind"] != "place" or m["target"]["id"] in targets:
                continue
            place = self.geo.place(namespace, m["target"]["id"], scopes=GEO_SCOPES) or {}
            parents = set(place.get("parent_ids") or [])
            if parents & targets:
                related.append({"subject": m["subject"], "relation": f"{place.get('place_type')} declaring the requested place as a member (not identical)",
                                "match": {"match_id": m["match_id"], "method": m["method"]}})
        own = [self.geo.place(namespace, t, scopes=GEO_SCOPES) for t in targets if not t.startswith(("ent-", "ambiguous:"))]
        for place in own:
            for parent in (place or {}).get("parent_ids") or []:
                for m in accepted:
                    if m["target"]["id"] == parent:
                        related.append({"subject": m["subject"], "relation": "declared member place of the requested area (not identical)",
                                        "match": {"match_id": m["match_id"], "method": m["method"]}})
        return {"requested": start, "targets": sorted(targets), "same": same, "related": related}
