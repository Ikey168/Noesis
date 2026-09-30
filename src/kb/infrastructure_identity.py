"""Reviewable identity and citation links for infrastructure assets (CI07-CI09; #2377, #2379, #2382).

**Asset reconciliation (CI07).** The same physical asset reported by GPPD,
GEM, OSM, EIA and ENTSOG is linked by *matches*. Each source's records stay
intact; nothing is merged or rewritten.

* An *identifier match* needs an explicit published cross-reference. Either
  one record carries the other's primary identifier (a GEM ``WRI: DEU…``
  reference, an OSM ``ref:gppd`` tag), or two records from different datasets
  share a published identifier (WEPP id, Wikidata QID). It is active at once
  (``accepted`` by the identifier rule) and stays rejectable and reversible.
  A GEM unit citing a GPPD plant is ``component_of`` that plant, never the
  same asset.
* A *proximity candidate* needs the same asset class, a distance within the
  class threshold (measured by
  :meth:`src.kb.geospatial.GeospatialStore.relation`, receipted) and a name
  token overlap. It carries the distance and the score and is never
  auto-accepted.

Reviews are append-only rows (accept, reject, revert). Only accepted
``same_asset`` matches group assets in answers, and conflicting status or
capacity across the grouped sources is shown side by side
(:mod:`src.kb.infrastructure_queries`).

**Operators and owners (CI08).** Owner, operator and parent assertions are
grouped into *parties* by their published name (``infrastructure:party:…``
keys). Parties are offered to Corporate Ownership legal entities through the
shared :class:`src.kb.ownership_identity.OwnershipIdentityService` state
machine:

* a published identifier (LEI, company number, Wikidata QID) equal to an
  entity identifier is ``cross-referenced-identifier``;
* an equal normalized name in the same country is ``name-jurisdiction``;
* otherwise a name match is the never-acceptable ``similar-name``.

Names from ``canonical_entities`` aliases are weak ``name-jurisdiction``
candidates. Assertions are never rewritten to the matched name, and shares
stay as published per source.

**Citation links (CI09).** An asset links to:

* Energy Systems series, only by a shared published identifier (the EIA plant
  code);
* legal works, only by a permit, docket or regulation the publisher cites
  (exact identifier lookup in :class:`src.kb.legal.LegalStore`, read-only);
* Climate and Environment facilities, only by a shared identifier.

Each link stores the citing revision. Name or proximity overlap creates a
review *candidate* only. When an optional pack is not installed, its link step
is skipped with a reason.
"""

from __future__ import annotations

import json
import re

from src.kb.infrastructure_assets import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    InfrastructureError,
    InfrastructureStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

MATCH_CONTRACT = "noesis-infrastructure-asset-match-v1"
LINK_CONTRACT = "noesis-infrastructure-link-v1"
PARTY_PREFIX = "infrastructure:party:"
GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:calculate"}
PRIMARY_SCHEMES = {"gppd": "gppd_idnr", "osm": "osm_element", "entsog": "entsog_point_key"}
# Published identifiers that name one physical asset (a generic OSM ``ref`` does not).
LINKING_SCHEMES = {"gppd_idnr", "wepp_id", "wikidata", "eia_plant_code", "gem_unit_id", "gem_project_id",
                   "gem_terminal_id", "gem_location_id", "entsog_point_key", "osm_element"}
THRESHOLDS_M = {"power_plant": 2000, "generating_unit": 1000, "lng_terminal": 3000, "pipeline": 5000,
                "transmission_line": 5000, "substation": 1000, "mine": 3000}
MIN_NAME_OVERLAP = 0.5
COUNTRIES = {"DEU": "DE", "GERMANY": "DE", "DE": "DE", "USA": "US", "UNITED STATES": "US", "US": "US",
             "FRA": "FR", "FRANCE": "FR", "POLAND": "PL", "POL": "PL"}
PARTY_ID_SCHEMES = {"lei", "gb-coh", "sec-cik", "wikidata", "de-hrb", "eia_utility_id", "entsog_operator_key"}
_DDL = """
CREATE TABLE IF NOT EXISTS infra_asset_matches(
 match_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, left_asset TEXT NOT NULL, right_asset TEXT NOT NULL,
 relation TEXT NOT NULL, basis TEXT NOT NULL, score DOUBLE, distance_m DOUBLE, evidence_json TEXT NOT NULL,
 proposed_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS infra_match_reviews(
 review_id TEXT PRIMARY KEY, match_id TEXT NOT NULL, namespace TEXT NOT NULL, revision BIGINT NOT NULL,
 state TEXT NOT NULL, reason TEXT NOT NULL, reviewer TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 UNIQUE(match_id, revision));
CREATE TABLE IF NOT EXISTS infra_links(
 link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, asset_id TEXT NOT NULL, revision_id TEXT NOT NULL,
 relation TEXT NOT NULL, target_owner TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT,
 basis TEXT NOT NULL, citation_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS infra_link_reviews(
 review_id TEXT PRIMARY KEY, link_id TEXT NOT NULL, namespace TEXT NOT NULL, revision BIGINT NOT NULL,
 state TEXT NOT NULL, reason TEXT NOT NULL, reviewer TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 UNIQUE(link_id, revision));
"""
_REFUSED_BASES = ("name similarity", "co-location", "proximity", "keyword", "correlation")


def tokens(name):
    return set(re.findall(r"[a-z0-9]+", str(name or "").casefold()))


def name_overlap(left, right):
    a, b = tokens(left), tokens(right)
    return round(len(a & b) / len(a | b), 3) if a and b else 0.0


def country_code(value):
    return COUNTRIES.get(str(value or "").strip().upper())


def party_key(name):
    from src.kb.entities import normalize_surface

    slug = re.sub(r"[^a-z0-9]+", "-", normalize_surface(str(name))).strip("-")
    return PARTY_PREFIX + (slug or digest(name)[:12])


def _representative(geometry):
    coords = geometry["coordinates"]
    kind = geometry["type"]
    flat = ([coords] if kind == "Point" else coords if kind == "LineString" else
            [p for part in coords for p in part])
    return [round(sum(p[0] for p in flat) / len(flat), 7), round(sum(p[1] for p in flat) / len(flat), 7)]


class InfrastructureIdentity:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.store = InfrastructureStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _ready(self):
        return table_exists(self.conn, "infra_asset_matches")

    # ------------------------------------------------------------------ assets

    def latest(self, namespace, *, scopes):
        """asset_id -> (asset row, latest revision) for every stored asset."""

        result = {}
        for asset in self.store.assets(namespace, scopes=scopes):
            revisions = self.store.revisions(namespace, asset["asset_id"], scopes=scopes)
            if revisions:
                result[asset["asset_id"]] = (asset, revisions[-1])
        return result

    def _insert_match(self, namespace, left, right, relation, basis, score, distance, evidence, principal_id):
        match_id = "infra-match:" + digest([namespace, left, right, relation])[:24]
        exists = self.conn.execute("SELECT 1 FROM infra_asset_matches WHERE match_id=?", [match_id]).fetchone()
        if exists:
            return None
        self.conn.execute("INSERT INTO infra_asset_matches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          [match_id, namespace, left, right, relation, basis, score, distance, canonical(evidence),
                           principal_id, self.now()])
        return match_id

    def propose_asset_matches(self, namespace, *, principal_id, scopes):
        """Identifier matches (active, reviewable) and proximity+name+class candidates (review required)."""

        from src.kb.geospatial import GeospatialStore

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        assets = self.latest(namespace, scopes=scopes)
        index = {}
        for aid, (asset, revision) in assets.items():
            for identifier in revision["record"]["identifiers"]:
                if identifier["scheme"] in LINKING_SCHEMES:
                    index.setdefault((identifier["scheme"], identifier["value"].strip().upper()), []).append(aid)
        identifier_pairs = {}
        for (scheme, value), holders in sorted(index.items()):
            for i, left in enumerate(holders):
                for right in holders[i + 1:]:
                    a, b = assets[left], assets[right]
                    if a[0]["dataset"] == b[0]["dataset"]:
                        continue  # co-published units referencing one plant are not the same asset
                    identifier_pairs.setdefault(tuple(sorted((left, right))), []).append(
                        {"scheme": scheme, "value": value,
                         "primary_of": [x[0]["asset_id"] for x in (a, b)
                                        if PRIMARY_SCHEMES.get(x[0]["provider"]) == scheme
                                        or x[0]["native_id"].upper() == value]})
        created = []
        for (left, right), shared in sorted(identifier_pairs.items()):
            classes = {assets[left][0]["asset_class"], assets[right][0]["asset_class"]}
            if len(classes) == 1:
                relation, (src_id, dst_id) = "same_asset", (left, right)
            elif classes == {"generating_unit", "power_plant"}:
                relation = "component_of"
                src_id, dst_id = (left, right) if assets[left][0]["asset_class"] == "generating_unit" else (right, left)
            else:
                continue
            evidence = {"kind": "published-identifier", "shared": shared,
                        "revisions": {aid: assets[aid][1]["revision_id"] for aid in (left, right)}}
            match_id = self._insert_match(namespace, src_id, dst_id, relation, "identifier", 1.0, None, evidence,
                                          principal_id)
            if match_id:
                self._review(namespace, match_id, "accepted", "explicit published cross-reference (identifier rule)",
                             "identifier-rule")
                created.append(match_id)
        geo = GeospatialStore(self.conn, initialize=False, now=self.now)
        items = sorted(assets.items())
        for i, (left, (la, lr)) in enumerate(items):
            for right, (ra, rr) in items[i + 1:]:
                if la["provider"] == ra["provider"] or la["asset_class"] != ra["asset_class"]:
                    continue
                if tuple(sorted((left, right))) in identifier_pairs:
                    continue
                threshold = THRESHOLDS_M.get(la["asset_class"])
                if threshold is None or not lr["geometry_id"] or not rr["geometry_id"]:
                    continue
                overlap = name_overlap(lr["record"]["name"], rr["record"]["name"])
                if overlap < MIN_NAME_OVERLAP:
                    continue
                relation = geo.relation(namespace, "proximity", lr["geometry_id"],
                                        _representative(rr["record"]["geometry"]), scopes=GEO_SCOPES,
                                        principal_id=principal_id, tolerance_m=threshold)
                distance = float(relation["result"]["distance_m"])
                if distance > threshold:
                    continue
                score = round(0.5 * overlap + 0.5 * (1 - distance / threshold), 3)
                evidence = {"kind": "proximity-name-class", "distance_m": distance, "threshold_m": threshold,
                            "name_overlap": overlap, "names": [lr["record"]["name"], rr["record"]["name"]],
                            "asset_class": la["asset_class"], "spatial_receipt_id": relation.get("receipt_id"),
                            "revisions": {left: lr["revision_id"], right: rr["revision_id"]},
                            "note": "a review candidate only; never merged or accepted automatically"}
                match_id = self._insert_match(namespace, left, right, "same_asset", "proximity-name-class", score,
                                              distance, evidence, principal_id)
                if match_id:
                    created.append(match_id)
        return {"proposed": created, "matches": self.asset_matches(namespace, scopes=scopes)}

    def _review(self, namespace, match_id, state, reason, reviewer):
        revision = self.conn.execute("SELECT count(*) FROM infra_match_reviews WHERE match_id=?",
                                     [match_id]).fetchone()[0] + 1
        self.conn.execute("INSERT INTO infra_match_reviews VALUES (?,?,?,?,?,?,?,?)",
                          ["infra-match-review:" + digest([match_id, revision, state])[:24], match_id, namespace,
                           revision, state, reason, reviewer, self.now()])

    def asset_match(self, namespace, match_id):
        row = self.conn.execute(
            "SELECT match_id, left_asset, right_asset, relation, basis, score, distance_m, evidence_json, proposed_by, "
            "created_at_ms FROM infra_asset_matches WHERE namespace=? AND match_id=?", [namespace, match_id]).fetchone()
        if row is None:
            raise InfrastructureError("not_found", "asset match is unavailable")
        reviews = [{"revision": int(r[0]), "state": r[1], "reason": r[2], "reviewer": r[3], "created_at_ms": r[4]}
                   for r in self.conn.execute("SELECT revision, state, reason, reviewer, created_at_ms FROM "
                                              "infra_match_reviews WHERE match_id=? ORDER BY revision",
                                              [match_id]).fetchall()]
        return {"contract": MATCH_CONTRACT, "match_id": row[0], "left_asset": row[1], "right_asset": row[2],
                "relation": row[3], "basis": row[4], "score": row[5], "distance_m": row[6],
                "evidence": json.loads(row[7]), "proposed_by": row[8], "created_at_ms": row[9],
                "state": reviews[-1]["state"] if reviews else "candidate", "reviews": reviews}

    def asset_matches(self, namespace, *, scopes, state=None, asset_id=None):
        authorize(namespace, scopes, READ_SCOPE)
        if not self._ready():
            return []
        ids = [r[0] for r in self.conn.execute(
            "SELECT match_id FROM infra_asset_matches WHERE namespace=? AND (? IS NULL OR left_asset=? OR right_asset=?) "
            "ORDER BY match_id", [namespace, asset_id, asset_id, asset_id]).fetchall()]
        matches = [self.asset_match(namespace, m) for m in ids]
        return [m for m in matches if state is None or m["state"] == state]

    def review_asset_match(self, namespace, match_id, decision, reason, *, principal_id, scopes):
        """Accept, reject or revert a match; decisions are appended, never overwritten."""

        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject", "revert"} or not str(reason or "").strip():
            raise InfrastructureError("invalid_review", "decision is accept, reject or revert, with a reason")
        current = self.asset_match(namespace, match_id)
        allowed = {"accept": {"candidate", "rejected", "reverted"}, "reject": {"candidate", "accepted"},
                   "revert": {"accepted", "rejected"}}[decision]
        if current["state"] not in allowed:
            raise InfrastructureError("invalid_state", f"a {current['state']} match cannot be {decision}ed")
        if decision == "accept" and current["proposed_by"] == principal_id:
            raise InfrastructureError("self_review", "a candidate is accepted by a principal other than its proposer")
        state = {"accept": "accepted", "reject": "rejected", "revert": "reverted"}[decision]
        self._review(namespace, match_id, state, reason.strip(), principal_id)
        return self.asset_match(namespace, match_id)

    def cluster(self, namespace, aid, *, scopes):
        """Assets accepted as the same physical asset as ``aid`` and the accepted components of any of them."""

        accepted = self.asset_matches(namespace, scopes=scopes, state="accepted")
        same, used = {aid}, []
        changed = True
        while changed:
            changed = False
            for match in accepted:
                if match["relation"] != "same_asset":
                    continue
                pair = {match["left_asset"], match["right_asset"]}
                if pair & same and not pair <= same:
                    same |= pair
                    changed = True
                if pair & same and match not in used:
                    used.append(match)
        components, parents = [], []
        for match in accepted:
            if match["relation"] != "component_of":
                continue
            if match["right_asset"] in same:
                components.append(match)
            elif match["left_asset"] in same:
                parents.append(match)
        return {"same": sorted(same), "matches": used, "components": components, "part_of": parents}

    # ---------------------------------------------------------------- parties

    def parties(self, namespace, *, scopes):
        """Owner, operator and parent assertions grouped by published name (never resolved here)."""

        grouped = {}
        for aid, (asset, revision) in self.latest(namespace, scopes=scopes).items():
            record = revision["record"]
            for owner in record["owners"]:
                key = party_key(owner["name"])
                party = grouped.setdefault(key, {"party_key": key, "names": set(), "identifiers": set(),
                                                 "countries": set(), "occurrences": []})
                party["names"].add(owner["name"])
                party["identifiers"] |= {(i["scheme"], i["value"]) for i in owner["identifiers"]}
                if country_code(record["country"]):
                    party["countries"].add(country_code(record["country"]))
                party["occurrences"].append({"asset_id": aid, "provider": asset["provider"], "role": owner["role"],
                                             "name": owner["name"], "share": owner["share"],
                                             "share_text": owner["share_text"], "revision_id": revision["revision_id"]})
        return [{**p, "names": sorted(p["names"]), "countries": sorted(p["countries"]),
                 "identifiers": [{"scheme": s, "value": v} for s, v in sorted(p["identifiers"])]}
                for _, p in sorted(grouped.items())]

    def propose_operator_matches(self, namespace, *, ownership_namespace, principal_id, scopes):
        """Offer parties to Corporate Ownership entities and canonical entities through the shared state machine."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not table_exists(self.conn, "ownership_records"):
            return {"skipped": "Corporate Ownership is not installed; operators stay published strings",
                    "unmatched": [p["party_key"] for p in self.parties(namespace, scopes=scopes)], "offered": []}
        from src.kb.entities import normalize_surface
        from src.kb.ownership_identity import OwnershipIdentityService
        from src.kb.ownership_store import OwnershipStore

        entities = [e for e in OwnershipStore(self.conn, initialize=False).records(
            ownership_namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",))
            if not e.get("redacted")]
        service = OwnershipIdentityService(self.conn, now=self.now)
        offered, unmatched = [], []
        for party in self.parties(namespace, scopes=scopes):
            ours = {(i["scheme"].lower(), i["value"].strip().upper()) for i in party["identifiers"]
                    if i["scheme"].lower() in PARTY_ID_SCHEMES}
            names = {normalize_surface(n) for n in party["names"]}
            found = False
            for entity in entities:
                body = entity["record"]
                theirs = {(i["scheme"].lower(), str(i.get("value") or "").strip().upper())
                          for i in body.get("identifiers") or []}
                shared = sorted(ours & theirs)
                jurisdiction = str(body.get("jurisdiction") or "").split("-")[0].upper()
                if shared:
                    basis = "cross-referenced-identifier"
                elif normalize_surface(body.get("name") or "") in names:
                    basis = ("name-jurisdiction" if jurisdiction and jurisdiction in party["countries"]
                             else "similar-name")
                else:
                    continue
                found = True
                evidence = {"kind": "infrastructure-party", "shared_identifiers": [
                    {"scheme": s, "value": v} for s, v in shared], "names": party["names"],
                    "countries": party["countries"], "entity_name": body.get("name"),
                    "entity_jurisdiction": body.get("jurisdiction"), "ownership_namespace": ownership_namespace,
                    "occurrences": party["occurrences"],
                    "note": "assertions keep the published name and share; nothing is rewritten"}
                offered.append({**service.offer(
                    namespace, left_key=party["party_key"], right_key=body["record_key"],
                    left_entity=party["party_key"], right_entity=body.get("canonical_entity_id") or body["record_key"],
                    basis=basis, evidence=[evidence], principal_id=principal_id, scopes=scopes),
                    "party_key": party["party_key"], "basis": basis, "record_key": body["record_key"]})
            if table_exists(self.conn, "entity_aliases"):
                from src.kb.entities import resolve

                for name in party["names"]:
                    hit = resolve(self.conn, name)
                    if hit is None:
                        continue
                    found = True
                    offered.append({**service.offer(
                        namespace, left_key=party["party_key"], right_key=f"canonical:{hit['canonical_id']}",
                        left_entity=party["party_key"], right_entity=hit["canonical_id"], basis="name-jurisdiction",
                        evidence=[{"kind": "canonical-alias", "name": name, "preferred_name": hit["preferred_name"],
                                   "note": "canonical alias table (no jurisdiction): a weak signal needing review"}],
                        principal_id=principal_id, scopes=scopes), "party_key": party["party_key"],
                        "basis": "name-jurisdiction", "record_key": f"canonical:{hit['canonical_id']}"})
            if not found:
                unmatched.append(party["party_key"])
        return {"offered": offered, "unmatched": unmatched,
                "candidates": self.operator_candidates(namespace, scopes=scopes)}

    def operator_candidates(self, namespace, *, scopes, state=None, party=None):
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return []
        from src.kb.ownership_identity import OwnershipIdentityService

        service = OwnershipIdentityService(self.conn, now=self.now, initialize=False)
        rows = service.candidates(namespace, scopes=set(scopes) | {"knowledge:ownership:read"}, state=state,
                                  record_key=party)
        return [c for c in rows if c["left_key"].startswith(PARTY_PREFIX) or c["right_key"].startswith(PARTY_PREFIX)]

    def _own_candidate(self, namespace, candidate_id, scopes):
        found = [c for c in self.operator_candidates(namespace, scopes=scopes) if c["candidate_id"] == candidate_id]
        if not found:
            raise InfrastructureError("not_found", "operator candidate is not an infrastructure party candidate")
        return found[0]

    def review_operator_match(self, namespace, candidate_id, decision, reason, *, principal_id, scopes):
        """Accept or reject (entity identity decision), or revert; the published assertions are untouched."""

        from src.kb.ownership_identity import OwnershipIdentityService

        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        self._own_candidate(namespace, candidate_id, scopes)
        service = OwnershipIdentityService(self.conn, now=self.now, initialize=False)
        ownership_scopes = set(scopes) | {"knowledge:ownership:review"}
        if decision == "revert":
            return service.revert(namespace, candidate_id, reason, principal_id=principal_id, scopes=ownership_scopes)
        return service.review(namespace, candidate_id, decision, reason, principal_id=principal_id,
                              scopes=ownership_scopes)

    def operator_parties(self, namespace, target, *, scopes):
        """Party keys accepted as ``target`` (an ownership record key or entity id), or the party key itself."""

        if str(target).startswith(PARTY_PREFIX):
            return [{"party_key": target, "basis": "published name (not an identity resolution)", "candidate": None}]
        result = []
        for candidate in self.operator_candidates(namespace, scopes=scopes, state="accepted"):
            other_key = candidate["right_key"] if candidate["left_key"].startswith(PARTY_PREFIX) else candidate["left_key"]
            other_entity = (candidate["right_entity"] if candidate["left_key"].startswith(PARTY_PREFIX)
                            else candidate["left_entity"])
            party = candidate["left_key"] if candidate["left_key"].startswith(PARTY_PREFIX) else candidate["right_key"]
            if target in {other_key, other_entity}:
                result.append({"party_key": party, "basis": f"accepted {candidate['basis']} match",
                               "candidate": {k: candidate[k] for k in ("candidate_id", "basis", "state",
                                                                         "decision_id")}})
        return result

    # ------------------------------------------------------------------ links

    def _insert_link(self, namespace, aid, revision_id, relation, owner, kind, target, basis, citation, principal_id,
                     state):
        link_id = "infra-link:" + digest([namespace, aid, relation, owner, kind, target, citation.get("locator")])[:24]
        if not self.conn.execute("SELECT 1 FROM infra_links WHERE link_id=?", [link_id]).fetchone():
            self.conn.execute("INSERT INTO infra_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              [link_id, namespace, aid, revision_id, relation, owner, kind, target, basis,
                               canonical(citation), principal_id, self.now()])
            self._link_review(namespace, link_id, state, basis, principal_id)
        return link_id

    def _link_review(self, namespace, link_id, state, reason, reviewer):
        revision = self.conn.execute("SELECT count(*) FROM infra_link_reviews WHERE link_id=?",
                                     [link_id]).fetchone()[0] + 1
        self.conn.execute("INSERT INTO infra_link_reviews VALUES (?,?,?,?,?,?,?,?)",
                          ["infra-link-review:" + digest([link_id, revision, state])[:24], link_id, namespace, revision,
                           state, reason, reviewer, self.now()])

    def _citation(self, record, revision_id, locator, basis):
        return {"source": record["attribution"], "source_url": record["source_url"], "locator": locator,
                "citing_revision_id": revision_id, "release": record["release"]["key"], "basis": basis}

    def link_energy(self, namespace, *, energy_namespace, principal_id, scopes):
        """Assets to Energy Systems plant/unit series by the published EIA plant code (read-only on energy)."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not table_exists(self.conn, "energy_series"):
            return {"linked": [], "candidates": [], "skipped": "Energy Systems is not installed"}
        from src.kb.energy_store import EnergyStore

        series = EnergyStore(self.conn, initialize=False).series(energy_namespace, scopes=scopes)
        linked, candidates = [], []
        for aid, (asset, revision) in sorted(self.latest(namespace, scopes=scopes).items()):
            record = revision["record"]
            codes = {i["value"] for i in record["identifiers"] if i["scheme"] == "eia_plant_code"}
            for item in series:
                subject = item["subject"]
                shared = subject["scheme"] in {"eia-plant", "eia-generator"} and subject["code"].split(":")[0] in codes
                if shared:
                    linked.append(self._insert_link(
                        namespace, aid, revision["revision_id"], "same-plant-series", "energy", "energy-series",
                        item["series_id"], "shared published identifier (EIA plant code)",
                        self._citation(record, revision["revision_id"],
                                       f"eia_plant_code={subject['code'].split(':')[0]}; series {item['series_id']}",
                                       "explicit identifier in both records"), principal_id, "linked"))
                elif subject["kind"] in {"plant", "unit"} and record["asset_class"] in {"power_plant",
                                                                                          "generating_unit"}:
                    name = (item.get("facets") or {}).get("plant_name") or subject.get("name")
                    if name and name_overlap(name, record["name"]) >= MIN_NAME_OVERLAP:
                        candidates.append(self._insert_link(
                            namespace, aid, revision["revision_id"], "same-plant-series", "energy", "energy-series",
                            item["series_id"], "name overlap only (review candidate)",
                            self._citation(record, revision["revision_id"], f"name {name!r}",
                                           "name overlap; not a citation"), principal_id, "candidate"))
        return {"linked": sorted(set(linked)), "candidates": sorted(set(candidates))}

    def link_legal(self, namespace, *, legal_namespace, principal_id, scopes):
        """Assets to legal works through permits, dockets or regulations the publisher cites (exact identifier)."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not table_exists(self.conn, "legal_works"):
            return {"linked": [], "unresolved": [], "skipped": "Legal is not installed"}
        from src.kb.legal import LegalStore

        legal = LegalStore(self.conn, initialize=False)
        linked, unresolved = [], []
        for aid, (_, revision) in sorted(self.latest(namespace, scopes=scopes).items()):
            record = revision["record"]
            for ref in record["cited_references"]:
                found = legal.lookup(legal_namespace, scopes=scopes, identifier=str(ref["identifier"]))
                works = found.get("works") or []
                citation = self._citation(record, revision["revision_id"],
                                          f"{ref['scheme']} {ref['identifier']} ({ref.get('locator') or 'record'})",
                                          "the publisher cites this identifier")
                if len(works) == 1:
                    linked.append(self._insert_link(namespace, aid, revision["revision_id"],
                                                    ref.get("relation") or "cites", "legal", "legal-work",
                                                    works[0]["work_id"], "explicit citation by identifier", citation,
                                                    principal_id, "linked"))
                else:
                    unresolved.append(self._insert_link(
                        namespace, aid, revision["revision_id"], ref.get("relation") or "cites", "legal",
                        "legal-work", None, f"cited identifier not resolved ({'ambiguous' if works else 'not acquired'})",
                        citation, principal_id, "unresolved"))
        return {"linked": sorted(set(linked)), "unresolved": sorted(set(unresolved))}

    def link_environment(self, namespace, *, environment_namespace, principal_id, scopes):
        """Assets to Climate and Environment facilities by a shared identifier; name overlap is a candidate."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not table_exists(self.conn, "environment_records"):
            return {"linked": [], "candidates": [], "skipped": "Climate and Environment is not installed"}
        from src.kb.environment_store import EnvironmentStore

        facilities = EnvironmentStore(self.conn, initialize=False).records(environment_namespace, scopes=scopes,
                                                                           record_type="facility")
        linked, candidates = [], []
        for aid, (_, revision) in sorted(self.latest(namespace, scopes=scopes).items()):
            record = revision["record"]
            ours = {(i["scheme"], i["value"]) for i in record["identifiers"] if i["scheme"] in LINKING_SCHEMES}
            for facility in facilities:
                content = facility["content"]
                theirs = {(str(k), str(v)) for k, v in (content.get("identifiers") or {}).items()}
                shared = sorted(ours & theirs)
                if shared:
                    linked.append(self._insert_link(
                        namespace, aid, revision["revision_id"], "same-facility", "climate-environment", "facility",
                        facility["record_id"], "shared published identifier",
                        self._citation(record, revision["revision_id"],
                                       "; ".join(f"{s}={v}" for s, v in shared) + f" (facility revision "
                                                                                    f"{facility['revision_id']})",
                                       "explicit identifier in both records"), principal_id, "linked"))
                elif name_overlap(content.get("title"), record["name"]) >= MIN_NAME_OVERLAP:
                    candidates.append(self._insert_link(
                        namespace, aid, revision["revision_id"], "same-facility", "climate-environment", "facility",
                        facility["record_id"], "name overlap only (review candidate)",
                        self._citation(record, revision["revision_id"], f"title {content.get('title')!r}",
                                       "name overlap; not a citation"), principal_id, "candidate"))
        return {"linked": sorted(set(linked)), "candidates": sorted(set(candidates))}

    def link_cited(self, namespace, asset_id, target, *, citation, principal_id, scopes):
        """A link another source explicitly states (citing source and locator are mandatory)."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not isinstance(citation, dict) or not str(citation.get("source") or "").strip() \
                or not str(citation.get("locator") or "").strip():
            raise InfrastructureError("citation_required", "a link names the citing source and a locator")
        if any(word in str(citation.get("basis") or "").casefold() for word in _REFUSED_BASES):
            raise InfrastructureError("inferred_link_refused", "links are never inferred from names or proximity; "
                                                               "propose a candidate instead")
        if not isinstance(target, dict) or not all(target.get(k) for k in ("owner", "kind", "id")):
            raise InfrastructureError("invalid_target", "target names owner, kind and id")
        revision = self.store.revision_as_of(namespace, asset_id, scopes=scopes)
        if revision is None:
            raise InfrastructureError("not_found", "asset is unavailable")
        cited = {k: citation.get(k) for k in ("source", "locator", "quote", "url", "basis") if citation.get(k)}
        cited["citing_revision_id"] = revision["revision_id"]
        return {"linked": self._insert_link(namespace, asset_id, revision["revision_id"], "cited", target["owner"],
                                            target["kind"], target["id"], "explicit citation", cited, principal_id,
                                            "linked")}

    def review_link(self, namespace, link_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        current = next((link for link in self.links(namespace, scopes=scopes)["links"] if link["link_id"] == link_id),
                       None)
        if current is None:
            raise InfrastructureError("not_found", "link is unavailable")
        if decision not in {"accept", "reject"} or current["state"] != "candidate" or not str(reason or "").strip():
            raise InfrastructureError("invalid_review", "only a candidate link is accepted or rejected, with a reason")
        self._link_review(namespace, link_id, "linked" if decision == "accept" else "rejected", reason.strip(),
                          principal_id)
        return next(link for link in self.links(namespace, scopes=scopes)["links"] if link["link_id"] == link_id)

    def links(self, namespace, asset_id=None, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "infra_links"):
            return {"links": [], "status": "no links on record"}
        rows = self.conn.execute(
            "SELECT l.link_id, l.asset_id, l.revision_id, l.relation, l.target_owner, l.target_kind, l.target_id, "
            "l.basis, l.citation_json, (SELECT state FROM infra_link_reviews r WHERE r.link_id=l.link_id ORDER BY "
            "revision DESC LIMIT 1) FROM infra_links l WHERE l.namespace=? AND (? IS NULL OR l.asset_id=?) "
            "ORDER BY l.link_id", [namespace, asset_id, asset_id]).fetchall()
        items = [{"contract": LINK_CONTRACT, "link_id": r[0], "asset_id": r[1], "citing_revision_id": r[2],
                  "relation": r[3], "target": {"owner": r[4], "kind": r[5], "id": r[6]}, "basis": r[7],
                  "citation": json.loads(r[8]), "state": r[9]} for r in rows]
        return {"links": items, "status": "linked" if items else "no links on record"}
