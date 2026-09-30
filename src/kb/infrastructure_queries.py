"""Which assets exist in a place or under an operator as of a date, with status history (CI10, #2386).

An answer is **per source, side by side**. The assets grouped by accepted
``same_asset`` matches (:mod:`src.kb.infrastructure_identity`) each keep their
own view:

* status as of the date: the latest status revision whose effective date (or,
  when the publisher states none, its first publication) is not after the
  date; an optional ``known_by`` cutoff limits the answer to what was
  published by then;
* the full status history;
* capacity per metric and direction as of the date;
* the owner and operator assertions of the revision published by the date;
* a citation (source, release, as-of basis, licence constraints, receipt).

Where the grouped sources disagree on status or capacity, every value is
listed in ``disagreements``; nothing is resolved or averaged. GEM units that
cite a plant are listed as its ``components``, never summed.

The identity matches an answer used (accepted reconciliation matches and
accepted operator matches) are listed with pending candidates beside them. A
place outside the selected coverage is ``not_covered``. An empty covered
answer is "none on record", never "none exist". Assets published without
coordinates are listed as ``not_located`` and never placed.

Answers export as ``noesis-evidence-bundle-v1`` bundles. No vulnerability,
criticality or valuation output exists.
"""

from __future__ import annotations

from src.ingestion.infrastructure_sources import BOUNDED_COVERAGE, LIVE_VERIFICATION
from src.kb.infrastructure_assets import (
    READ_SCOPE,
    InfrastructureError,
    InfrastructureStore,
    authorize,
    digest,
    iso,
    ms,
    table_exists,
)

CONTRACT = "noesis-infrastructure-answer-v1"
NOTICE = ("Assets as published per source and release, side by side; disagreements are listed, never resolved. "
          "Only attributes the publishers release: no vulnerability, criticality or valuation.")
NONE_ON_RECORD = "none on record (an empty answer inside the selected coverage is not evidence that none exist)"
GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:calculate"}


def _bbox_of(geometry):
    coords = geometry["coordinates"]
    kind = geometry["type"]
    flat = ([coords] if kind == "Point" else coords if kind == "LineString" else
            [p for part in coords for p in part])
    lons, lats = [p[0] for p in flat], [p[1] for p in flat]
    return [min(lons), min(lats), max(lons), max(lats)]


def _vertices(geometry):
    coords = geometry["coordinates"]
    kind = geometry["type"]
    return [coords] if kind == "Point" else coords if kind == "LineString" else [p for part in coords for p in part]


def _intersects(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _inside_bbox(point, box):
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def coverage_for(box):
    areas = [a for a in BOUNDED_COVERAGE["areas"] if _intersects(a["bbox"], box)]
    contained = [a for a in areas if a["bbox"][0] <= box[0] and a["bbox"][1] <= box[1] and a["bbox"][2] >= box[2]
                 and a["bbox"][3] >= box[3]]
    status = "not_covered" if not areas else "covered" if contained else "partial"
    return {"status": status, "areas": [a["id"] for a in areas],
            "sources": sorted({s for a in areas for s in a["sources"]}),
            "live_verification": {p: LIVE_VERIFICATION[p]["status"] for p in sorted({s for a in areas
                                                                                   for s in a["sources"]})},
            "basis": "selected bounded coverage (docs/development/infrastructure-evidence/source-audit.md)"}


class InfrastructureQueries:
    def __init__(self, conn, *, now=None):
        from src.kb.infrastructure_identity import InfrastructureIdentity

        self.conn = conn
        self.store = InfrastructureStore(conn, initialize=False, now=now)
        self.identity = InfrastructureIdentity(conn, initialize=False, now=now)
        self.now = self.store.now

    # ----------------------------------------------------------------- views

    def asset_view(self, namespace, aid, *, scopes, as_of_ms=None, known_by_ms=None):
        """One source's view of one asset as of a date, with its histories and citation."""

        revisions = [r for r in self.store.revisions(namespace, aid, scopes=scopes)
                     if known_by_ms is None or r["published_at_ms"] <= known_by_ms]
        if not revisions:
            return None
        chosen = [r for r in revisions if as_of_ms is None or r["published_at_ms"] <= as_of_ms]
        revision = max(chosen, key=lambda r: (r["published_at_ms"], r["sequence"])) if chosen else revisions[0]
        record = revision["record"]
        known = {r["revision_id"] for r in revisions}
        history = [s for s in self.store.status_history(namespace, aid, scopes=scopes) if s["revision_id"] in known]

        def effective(item):
            return ms(item["effective_date"]) if item["effective_date"] else item["published_at_ms"]

        in_force = [s for s in history if as_of_ms is None or effective(s) <= as_of_ms]
        status = max(in_force, key=lambda s: (effective(s), s["sequence"])) if in_force else None
        capacities, later = {}, {}
        for item in self.store.capacity_history(namespace, aid, scopes=scopes):
            if item["revision_id"] not in known:
                continue
            when = ms(item["effective_date"]) if item["effective_date"] else item["published_at_ms"]
            key = (item["metric"], item["direction"], item["period"])
            if as_of_ms is not None and when > as_of_ms:
                if not item["effective_date"] and (key not in later or (when, item["sequence"]) < later[key][0]):
                    later[key] = ((when, item["sequence"]), {**item, "selection_basis": (
                        "first publication, after the as-of date (the publisher states no effective date)")})
                continue
            if key not in capacities or (when, item["sequence"]) >= capacities[key][0]:
                capacities[key] = ((when, item["sequence"]), {**item, "selection_basis": (
                    "effective date" if item["effective_date"] else "publication date")})
        for key, value in later.items():
            capacities.setdefault(key, value)
        citation = self.store.citation(revision)
        return {
            "asset_id": aid, "provider": record["provider"], "dataset": record["dataset"],
            "native_id": record["native_id"], "asset_class": record["asset_class"], "name": record["name"],
            "country": record["country"], "identifiers": record["identifiers"], "geometry_id": revision["geometry_id"],
            "revision_id": revision["revision_id"],
            "revision_basis": ("latest revision published by the as-of date" if chosen else
                               "first revision (published after the as-of date)"),
            "status": None if status is None else {
                "published": status["published"], "normalized": status["normalized"],
                "effective_date": status["effective_date"], "effective_basis": status["effective_basis"],
                "status_revision_id": status["status_revision_id"],
                "selection_basis": "effective date" if status["effective_date"] else "first publication"},
            "status_note": None if status else ("no status published by this source" if not history else
                                                "no status in force by the as-of date"),
            "status_history": [{k: s[k] for k in ("sequence", "published", "normalized", "effective_date",
                                                   "effective_basis", "revision_id")} | {
                "published_at": iso(s["published_at_ms"])} for s in history],
            "capacities": [{k: item[k] for k in ("metric", "direction", "period", "value", "unit", "effective_date",
                                                 "estimate", "capacity_revision_id", "revision_id", "selection_basis")}
                           for _, (_, item) in sorted(capacities.items(), key=lambda kv: tuple(str(x) for x in kv[0]))],
            "owners": self.store.owner_assertions(namespace, revision["revision_id"], scopes=scopes),
            "estimates": record["estimates"], "unknowns": record["unknowns"], "citation": citation,
        }

    def _group(self, namespace, aid, *, scopes, as_of_ms, known_by_ms, seen):
        cluster = self.identity.cluster(namespace, aid, scopes=scopes) if table_exists(
            self.conn, "infra_asset_matches") else {"same": [aid], "matches": [], "components": [], "part_of": []}
        seen |= set(cluster["same"])
        sources = [v for v in (self.asset_view(namespace, a, scopes=scopes, as_of_ms=as_of_ms, known_by_ms=known_by_ms)
                               for a in cluster["same"]) if v]
        statuses = {v["provider"]: v["status"]["normalized"] for v in sources
                    if v["status"] and v["status"]["normalized"] != "unknown"}
        disagreements = {}
        if len(set(statuses.values())) > 1:
            disagreements["status"] = [{"provider": v["provider"], "asset_id": v["asset_id"],
                                        "published": v["status"]["published"], "normalized": v["status"]["normalized"],
                                        "revision_id": v["revision_id"]} for v in sources
                                       if v["status"] and v["status"]["normalized"] != "unknown"]
        by_metric = {}
        for view in sources:
            for capacity in view["capacities"]:
                by_metric.setdefault((capacity["metric"], capacity["direction"]), []).append(
                    {"provider": view["provider"], "asset_id": view["asset_id"], "value": capacity["value"],
                     "unit": capacity["unit"], "estimate": capacity["estimate"], "revision_id": view["revision_id"]})
        conflicts = [{"metric": metric, "direction": direction, "values": values}
                     for (metric, direction), values in sorted(by_metric.items(), key=lambda kv: str(kv[0]))
                     if len({(v["value"], v["unit"]) for v in values}) > 1]
        if conflicts:
            disagreements["capacity"] = conflicts
        pending = []
        if table_exists(self.conn, "infra_asset_matches"):
            for member in cluster["same"]:
                pending += [{k: m[k] for k in ("match_id", "left_asset", "right_asset", "relation", "basis", "score",
                                                "distance_m", "state")}
                            for m in self.identity.asset_matches(namespace, scopes=scopes, asset_id=member)
                            if m["state"] == "candidate"]
        components = [v for v in (self.asset_view(namespace, m["left_asset"], scopes=scopes, as_of_ms=as_of_ms,
                                                  known_by_ms=known_by_ms) for m in cluster["components"]) if v]
        return {"group_id": "infra-group:" + digest(sorted(cluster["same"]))[:16], "sources": sources,
                "disagreements": disagreements,
                "components": components,
                "identity": {"used": [{k: m[k] for k in ("match_id", "left_asset", "right_asset", "relation", "basis",
                                                         "state")} for m in cluster["matches"] + cluster["components"]],
                             "pending_candidates": sorted({p["match_id"]: p for p in pending}.values(),
                                                          key=lambda p: p["match_id"])}}

    def _answer(self, namespace, asset_ids, *, scopes, as_of_ms, known_by_ms, query):
        seen, groups = set(), []
        component_ids = set()
        if table_exists(self.conn, "infra_asset_matches"):
            component_ids = {m["left_asset"] for m in self.identity.asset_matches(namespace, scopes=scopes,
                                                                                  state="accepted")
                             if m["relation"] == "component_of"}
        for aid in sorted(asset_ids):
            if aid in seen:
                continue
            group = self._group(namespace, aid, scopes=scopes, as_of_ms=as_of_ms, known_by_ms=known_by_ms, seen=seen)
            if aid in component_ids and not group["components"] and len(group["sources"]) == 1:
                group["component_of"] = [m["right_asset"] for m in self.identity.asset_matches(
                    namespace, scopes=scopes, state="accepted", asset_id=aid) if m["relation"] == "component_of"]
            if group["sources"]:
                groups.append(group)
        return {"contract": CONTRACT, "query": query, "as_of": iso(as_of_ms), "known_by": iso(known_by_ms),
                "assets": groups, "status": "found" if groups else NONE_ON_RECORD, "notice": NOTICE}

    # --------------------------------------------------------------- queries

    def _area(self, namespace, *, place_id, geometry_id, place_name, bbox, scopes):
        from src.kb.geospatial import GeospatialStore

        geo = GeospatialStore(self.conn, initialize=False, now=self.now)
        if bbox is not None:
            box = [float(v) for v in bbox]
            return {"kind": "bbox", "bbox": box, "geometry_id": None, "label": f"bbox {box}"}
        if place_name is not None:
            resolved = geo.resolve(namespace, place_name, scopes=GEO_SCOPES)
            places = [c for c in resolved.get("candidates") or []]
            if len(places) != 1:
                raise InfrastructureError("place_ambiguous" if places else "place_not_found",
                                          f"{place_name!r} resolves to {len(places)} places; give a place_id")
            place_id = places[0]["place_id"]
        if place_id is not None:
            geometries = [g for g in geo.geometries(namespace, place_id, scopes=GEO_SCOPES)
                          if g["geometry"]["type"] in {"Polygon", "MultiPolygon"}]
            if not geometries:
                raise InfrastructureError("place_has_no_area", "the place has no polygon geometry; give a bbox")
            geometry_id = geometries[0]["geometry_id"]
        geometry = geo.geometry(namespace, geometry_id, scopes=GEO_SCOPES) if geometry_id else None
        if geometry is None or geometry["geometry"]["type"] not in {"Polygon", "MultiPolygon"}:
            raise InfrastructureError("place_has_no_area", "give a polygon place, polygon geometry or bbox")
        shape = geometry["geometry"]
        rings = shape["coordinates"] if shape["type"] == "Polygon" else [r for poly in shape["coordinates"] for r in poly]
        points = [p for ring in rings for p in ring]
        box = [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points),
               max(p[1] for p in points)]
        return {"kind": "geometry", "bbox": box, "geometry_id": geometry_id, "place_id": place_id,
                "label": place_name or place_id or geometry_id}

    def assets_in_place(self, namespace, *, scopes, principal_id, place_id=None, geometry_id=None, place_name=None,
                        bbox=None, as_of=None, known_by=None, asset_classes=None):
        """Assets located in a place (polygon or bbox) with status, capacity and owners as of a date."""

        from src.kb.geospatial import GeospatialStore

        authorize(namespace, scopes, READ_SCOPE)
        area = self._area(namespace, place_id=place_id, geometry_id=geometry_id, place_name=place_name, bbox=bbox,
                          scopes=scopes)
        coverage = coverage_for(area["bbox"])
        query = {"kind": "place", "area": area, "asset_classes": sorted(asset_classes or [])}
        if coverage["status"] == "not_covered":
            return {"contract": CONTRACT, "query": query, "as_of": as_of, "coverage": coverage, "assets": [],
                    "not_located": [], "status": "not_covered",
                    "notice": "the area lies outside the selected coverage; nothing is claimed about it"}
        geo = GeospatialStore(self.conn, initialize=False, now=self.now)
        inside, not_located, receipts = [], [], []
        latest = self.identity.latest(namespace, scopes=scopes) if self.store.ready() else {}
        for aid, (asset, revision) in sorted(latest.items()):
            if asset_classes and asset["asset_class"] not in set(asset_classes):
                continue
            record = revision["record"]
            if record["geometry"] is None:
                if record["provider"] in coverage["sources"]:
                    not_located.append({"asset_id": aid, "provider": record["provider"], "name": record["name"],
                                        "reason": "published without coordinates; never placed or geocoded"})
                continue
            vertices = _vertices(record["geometry"])
            if not any(_inside_bbox(p, area["bbox"]) for p in vertices):
                continue
            if area["kind"] == "bbox":
                inside.append(aid)
                continue
            hit = False
            for vertex in [p for p in vertices if _inside_bbox(p, area["bbox"])]:
                # A point asset is inside when the polygon contains it; a line or outline when any vertex is.
                result = geo.relation(namespace, "contains", area["geometry_id"], vertex, scopes=GEO_SCOPES,
                                      principal_id=principal_id)
                receipts.append(result["receipt_id"])
                if result["result"]["contains"]:
                    hit = True
                    break
            if not hit and record["geometry"]["type"] != "Point":
                result = geo.relation(namespace, "intersects", area["geometry_id"], revision["geometry_id"],
                                      scopes=GEO_SCOPES, principal_id=principal_id)
                receipts.append(result["receipt_id"])
                hit = bool(result["result"]["intersects"])
            if hit:
                inside.append(aid)
        answer = self._answer(namespace, inside, scopes=scopes, as_of_ms=ms(as_of), known_by_ms=ms(known_by),
                              query=query)
        return {**answer, "coverage": coverage, "not_located": not_located, "spatial_receipts": sorted(set(receipts))}

    def assets_of_operator(self, namespace, operator, *, scopes, as_of=None, known_by=None):
        """Assets whose owner/operator/parent assertions name the operator, through accepted operator matches.

        ``operator`` is a Corporate Ownership record key or entity id (reached through accepted matches only),
        or a published name / ``infrastructure:party:`` key (the published string, not an identity).
        """

        from src.kb.infrastructure_identity import PARTY_PREFIX, party_key

        authorize(namespace, scopes, READ_SCOPE)
        if str(operator).startswith(PARTY_PREFIX) or not (":" in str(operator) or str(operator).startswith("ent-")):
            key = operator if str(operator).startswith(PARTY_PREFIX) else party_key(operator)
            parties = [{"party_key": key, "basis": "published name (not an identity resolution)", "candidate": None}]
        else:
            parties = self.identity.operator_parties(namespace, operator, scopes=scopes)
        keys = {p["party_key"] for p in parties}
        known = {p["party_key"]: p for p in self.identity.parties(namespace, scopes=scopes)} if self.store.ready() else {}
        asset_ids, roles = set(), []
        for key in sorted(keys):
            for occurrence in (known.get(key) or {}).get("occurrences", []):
                asset_ids.add(occurrence["asset_id"])
                roles.append({"party_key": key, **occurrence})
        pending = []
        for key in sorted(set(known) & keys):
            pending += [{k: c[k] for k in ("candidate_id", "left_key", "right_key", "basis", "state")}
                        for c in self.identity.operator_candidates(namespace, scopes=scopes, party=key)
                        if c["state"] == "proposed"]
        answer = self._answer(namespace, asset_ids, scopes=scopes, as_of_ms=ms(as_of), known_by_ms=ms(known_by),
                              query={"kind": "operator", "operator": operator})
        status = answer["status"]
        if not parties:
            status = "unmatched operator: no accepted operator match reaches this entity; " + NONE_ON_RECORD
        return {**answer, "status": status, "operator_matches": parties, "pending_operator_candidates": pending,
                "roles": roles,
                "coverage": {"basis": "operator answers cover only the selected sources and areas",
                             "live_verification": {p: v["status"] for p, v in LIVE_VERIFICATION.items()}}}

    def asset_history(self, namespace, aid, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        revisions = self.store.revisions(namespace, aid, scopes=scopes)
        if not revisions:
            raise InfrastructureError("not_found", "asset is unavailable")
        return {"contract": CONTRACT, "asset_id": aid,
                "revisions": [{"revision_id": r["revision_id"], "sequence": r["sequence"], "release": r["record"]["release"],
                               "published_at": iso(r["published_at_ms"]), "revision_of": r["revision_of"],
                               "citation": self.store.citation(r)} for r in revisions],
                "status_history": self.store.status_history(namespace, aid, scopes=scopes),
                "capacity_history": self.store.capacity_history(namespace, aid, scopes=scopes),
                "owner_assertions": {r["revision_id"]: self.store.owner_assertions(namespace, r["revision_id"],
                                                                                   scopes=scopes) for r in revisions},
                "notice": NOTICE}

    # --------------------------------------------------------- evidence bundle

    def export_bundle(self, answer, *, created_at_ms=0):
        """A ``noesis-evidence-bundle-v1``: one evidence object per cited asset revision."""

        from src.evidence_bundle.builder import EvidenceBundleBuilder

        builder = EvidenceBundleBuilder("receipt", {"operation": "infrastructure-assets", "query": answer.get("query"),
                                                    "as_of": answer.get("as_of")},
                                        created_at_ms=created_at_ms, as_of_ms=ms(answer.get("as_of")))
        refs = []
        for group in answer.get("assets") or []:
            for view in group["sources"] + group["components"]:
                citation = view["citation"]
                object_id = f"infrastructure:{citation['revision_id']}"
                builder.add_object("evidence", {
                    "kind": "infrastructure-asset-revision", "asset_id": view["asset_id"], "name": view["name"],
                    "status": view["status"], "capacities": view["capacities"], "owners": view["owners"],
                    "locator": {"cited": True, "document_id": view["asset_id"], "revision_id": citation["revision_id"],
                                "url": citation["source_url"]},
                    "citation": citation}, object_id=object_id)
                refs.append(object_id)
                builder.add_external_reference(f"source:{view['asset_id']}", citation["source_url"], required=False)
        root = {k: v for k, v in answer.items() if k != "assets"}
        root["groups"] = [{"group_id": g["group_id"], "disagreements": g["disagreements"], "identity": g["identity"]}
                          for g in answer.get("assets") or []]
        builder.add_object("receipt", root, object_id=f"{CONTRACT}:{digest(refs)[:16]}", references=refs, root=True)
        if not answer.get("assets"):
            builder.add_omission(str(answer.get("status") or NONE_ON_RECORD))
        for item in answer.get("not_located") or []:
            builder.add_omission(f"not located: {item['asset_id']} ({item['reason']})")
        return builder.build()
