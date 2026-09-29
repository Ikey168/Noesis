"""Place-centred environmental dossiers over the geospatial owners (E08).

A dossier answers, for one resolved place and an as-of time:

* which stations lie **within** the boundary feature containing the place
  (``GeospatialFeatureStore.within`` over the ``environment-stations``
  collection, with its replayable receipt) and their series, split by kind;
* which **facilities are near** the place (``GeospatialStore.relation``
  proximity receipts) with releases, permits, ETS records and operator links;
* which **grid events** belong to the bidding zone containing the place
  (zone containment receipt; zones are declared in
  ``config/environment/bidding_zones.json``);
* which **Umweltatlas layers** contain the place (exact ring parity over the
  pinned feature revisions); and
* which **model** and **forecast** series cover the place's grid cell.

Every item lists its source, kind and as-of basis. Values and records come
from the vintages and revisions valid at ``as_of_ms``; those are pinned, so a
later vintage makes the dossier *stale* (never silently updated) and
:meth:`EnvironmentDossiers.replay` recomputes every spatial answer from the
pinned geometry. A section without data returns an explicit coverage gap.
Nothing here infers attribution, projections or compliance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.kb.environment_records import DOSSIER_CONTRACT, READ_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.environment_store import (
    GEO_SCOPES,
    STATION_COLLECTION,
    EnvironmentStore,
    EnvironmentStoreError,
    authorize,
)

ZONES_PATH = Path(__file__).resolve().parents[2] / "config/environment/bidding_zones.json"
BOUNDARY_COLLECTION = "alkis_bezirke:bezirksgrenzen"
LAYER_COLLECTIONS = ("ua_umweltzone:umweltzone", "ua_stratlaerm_2022:laerm_lden", "ua_klimaanalyse_2022:klimafunktion")
NOT_IMPLEMENTED = {"copernicus-cams": "not implemented: ADS account, key and licence acceptance unverified"}
_DDL = """
CREATE TABLE IF NOT EXISTS environment_dossiers(
 dossier_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, request_hash TEXT NOT NULL,
 place_id TEXT, as_of_ms BIGINT, content_json TEXT NOT NULL, pins_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL);
"""


class EnvironmentDossiers:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.store = EnvironmentStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------- zones

    def ensure_zones(self, namespace, *, principal_id):
        """Register the declared bidding-zone outlines as places in ``namespace`` (idempotent)."""

        declared = json.loads(ZONES_PATH.read_text())
        zones = []
        for zone in declared["zones"]:
            key = f"environment:bidding-zone:{zone['code']}"
            row = self.conn.execute("SELECT place_id FROM geospatial_places WHERE namespace=? AND place_key=?",
                                    [namespace, key]).fetchone()
            if row is None:
                place_id = self.store.geo.register_place(
                    namespace, f"Bidding zone {zone['name']}", "bidding-zone",
                    names=[{"value": zone["name"], "language": "und", "kind": "canonical"},
                           {"value": zone["code"], "language": "und", "kind": "identifier"}],
                    source_ids={"eic": zone["code"]}, parent_ids=[], principal_id=principal_id, scopes=GEO_SCOPES,
                    place_key=key, observed_at_ms=0, provenance={"source": "config/environment/bidding_zones.json"},
                )["place_id"]
            else:
                place_id = row[0]
            geometry = self.store.geo.store_geometry(
                namespace, zone["geometry"], place_id=place_id, crs="EPSG:4326", precision_m=float(zone["precision_m"]),
                simplified_from=None, disputed=False, admin_hierarchy=[],
                source={"kind": "authored-coarse-outline", "config": "config/environment/bidding_zones.json",
                        "note": declared["note"]},
                evidence=[], principal_id=principal_id, scopes=GEO_SCOPES, observed_at_ms=0)
            zones.append({**{k: zone[k] for k in ("code", "name", "members", "precision_m")}, "place_id": place_id,
                          "geometry_id": geometry["geometry_id"]})
        return zones

    # ------------------------------------------------------------- helpers

    def _resolve(self, namespace, *, place_id, mention, as_of_ms):
        geo = self.store.geo
        if place_id is None:
            resolution = geo.resolve(namespace, mention, scopes={"knowledge:geospatial:read"}, as_of_ms=as_of_ms)
            if resolution["status"] != "resolved":
                return None, resolution
            place_id = resolution["selected_place_id"]
            place_namespace = next(c["namespace"] for c in resolution["candidates"] if c["place_id"] == place_id)
        else:
            resolution = None
            row = self.conn.execute("SELECT namespace FROM geospatial_places WHERE place_id=? AND namespace IN (?, 'global')",
                                    [place_id, namespace]).fetchone()
            if row is None:
                raise EnvironmentStoreError("not_found", "place is not visible in this namespace")
            place_namespace = row[0]
        place = geo.place(place_namespace, place_id, scopes={"knowledge:geospatial:read"})
        points = [g for g in geo.geometries(place_namespace, place_id, scopes={"knowledge:geospatial:read"},
                                            as_of_ms=as_of_ms, include_disputed=False)
                  if g["geometry"]["type"] == "Point"]
        return {"place": place, "namespace": place_namespace, "point": points[0] if points else None}, resolution

    def _polygon_features(self, namespace, collection):
        return self.conn.execute(
            "SELECT f.feature_id, f.provider, f.native_id, r.revision_id, r.title, r.geometry_id, r.properties_json, "
            "r.observed_at_ms FROM geospatial_features f JOIN geospatial_feature_current c ON c.feature_id=f.feature_id "
            "JOIN geospatial_feature_revisions r ON r.revision_id=c.revision_id WHERE f.namespace IN (?, 'global') "
            "AND f.collection=? AND c.lifecycle='active' AND r.geometry_type IN ('Polygon','MultiPolygon') "
            "ORDER BY f.native_id", [namespace, collection]).fetchall()

    def _contains(self, geometry_id, coordinates):
        from src.kb.geospatial_features import GeospatialFeatureStore

        loaded = self.store.features._geometries([geometry_id])[geometry_id]
        candidate = {"geometry_id": "__place__", "geometry": {"type": "Point", "coordinates": coordinates}}
        return bool(GeospatialFeatureStore._membership(loaded, [candidate]))

    def _series_summary(self, namespace, rid, *, scopes, as_of_ms, max_values):
        series = self.store.series(namespace, rid, scopes=scopes, as_of_ms=as_of_ms)
        vintage = series["vintage"]
        values = series["values"][-max_values:] if max_values else series["values"]
        return {
            "record_id": rid, "title": series["title"], "provider": series["provider"], "kind": series["kind"],
            "source_url": series["source_url"], "indicator": series["indicator"], "unit": series["unit"],
            "interval": series["interval"], "aggregation": series["aggregation"], "model": series["model"],
            "status_basis": series["status_basis"],
            "as_of": None if vintage is None else {
                "vintage_id": vintage["vintage_id"], "release_at_ms": vintage["release_at_ms"],
                "release_at_basis": vintage["release_at_basis"], "retrieved_at_ms": vintage["retrieved_at_ms"],
                "status": vintage["status"]},
            "values": values, "kind_notice": series["kind_notice"], "unknowns": series["unknowns"],
        }, (None if vintage is None else {"kind": "vintage", "id": vintage["vintage_id"], "record_id": rid})

    def _series_at(self, namespace, ref):
        rows = self.conn.execute(
            "SELECT r.record_id, v.content_json FROM environment_records r JOIN environment_record_current c USING(record_id) "
            "JOIN environment_record_revisions v ON v.revision_id=c.revision_id WHERE r.namespace=? "
            "AND r.record_type='observation_series' ORDER BY r.record_id", [namespace]).fetchall()
        return [rid for rid, content in rows if json.loads(content)["location"].get("ref") == ref]

    # ------------------------------------------------------------- build

    def build(self, namespace, request_key, *, principal_id, scopes, place_id=None, mention=None, as_of_ms=None,
              radius_m=5000, boundary_collection=BOUNDARY_COLLECTION, layer_collections=LAYER_COLLECTIONS,
              max_values=24):
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if bool(place_id) == bool(mention):
            raise EnvironmentStoreError("invalid_request", "give exactly one of place_id or mention")
        if not 0 < float(radius_m) <= 50_000:
            raise EnvironmentStoreError("invalid_request", "radius_m must be within (0, 50000]")
        request = {"namespace": namespace, "place_id": place_id, "mention": mention, "as_of_ms": as_of_ms,
                   "radius_m": radius_m, "boundary_collection": boundary_collection,
                   "layer_collections": list(layer_collections), "max_values": max_values}
        dossier_id = "env-dossier:" + digest([namespace, principal_id, request_key])[:24]
        prior = self.conn.execute("SELECT request_hash FROM environment_dossiers WHERE dossier_id=?", [dossier_id]).fetchone()
        if prior:
            if prior[0] != digest(request):
                raise EnvironmentStoreError("idempotency_conflict", "request key was used for another dossier request")
            return self.inspect(namespace, dossier_id, scopes=scopes, principal_id=principal_id)
        as_of = int(as_of_ms if as_of_ms is not None else self.now())
        located, resolution = self._resolve(namespace, place_id=place_id, mention=mention, as_of_ms=as_of)
        if located is None:
            return {"contract": DOSSIER_CONTRACT, "status": "place_unresolved", "resolution": resolution,
                    "note": "ambiguous or unknown places are not guessed; resolve or pass place_id"}
        read = set(scopes) | {READ_SCOPE}
        pins, receipts, gaps = [], [], []
        point = located["point"]
        place = located["place"]
        sections: dict[str, Any] = {"observations": [], "model_output": [], "forecasts": [], "grid": [],
                                    "facilities": [], "layers": []}
        if point is None:
            gaps.append({"section": "all", "reason": "the place has no point geometry valid at the as-of time"})
            return self._save(namespace, dossier_id, principal_id, request, place, None, as_of, sections, pins,
                              receipts, gaps, resolution)
        coordinates = point["geometry"]["coordinates"]
        pins.append({"kind": "place-geometry", "id": point["geometry_id"]})

        # Stations within the boundary feature that contains the place.
        boundary = next(({"feature_id": f[0], "title": f[4], "revision_id": f[3], "geometry_id": f[5]}
                         for f in self._polygon_features(namespace, boundary_collection)
                         if f[5] and self._contains(f[5], coordinates)), None)
        if boundary is None:
            gaps.append({"section": "observations", "reason": f"no active boundary in {boundary_collection} contains the place"})
        else:
            pins.append({"kind": "feature-revision", "id": boundary["revision_id"], "feature_id": boundary["feature_id"]})
            within = self.store.features.within(namespace, collection=STATION_COLLECTION,
                                                boundary_feature_id=boundary["feature_id"], principal_id=principal_id,
                                                scopes=GEO_SCOPES, limit=5000)
            receipts.append({"operation": "points_within", "receipt_id": within["receipt"]["receipt_id"],
                             "boundary": boundary, "status": within["status"],
                             "coverage": within["coverage"]["completeness"]})
            for member in within["members"]:
                provider = member["provenance"].get("provider") or member["feature_id"].split(":")[0]
                rid = self.store.find(namespace, "station", provider, member["native_id"])
                if rid is None:
                    continue
                try:
                    station = self.store.record(namespace, rid, scopes=read, as_of_ms=as_of)
                except EnvironmentStoreError:
                    continue  # the station was not yet acquired at the as-of time
                pins.append({"kind": "record-revision", "id": station["revision_id"], "record_id": rid})
                entry = {"station": {"record_id": rid, "title": station["content"]["title"], "provider": provider,
                                     "identifiers": station["content"]["identifiers"], "coordinates": member["coordinates"],
                                     "source_url": station["content"]["source_url"],
                                     "as_of": {"revision_id": station["revision_id"], "observed_at_ms": station["observed_at_ms"]}},
                         "links": self.store.links(namespace, scopes=read, record=rid), "series": []}
                for series_id in self._series_at(namespace, f"{provider}:{member['native_id']}"):
                    try:
                        summary, pin = self._series_summary(namespace, series_id, scopes=read, as_of_ms=as_of,
                                                            max_values=max_values)
                    except EnvironmentStoreError:
                        continue  # no revision existed at the as-of time
                    if pin:
                        pins.append(pin)
                    entry["series"].append(summary)
                sections["observations"].append(entry)
            if not sections["observations"]:
                gaps.append({"section": "observations",
                             "reason": f"no selected station lies within {boundary['title']} (bounded selection; coverage {within['coverage']['completeness']})"})

        # Model output and forecasts for the grid cell(s) covering the place.
        for rid, content in self._grid_series(namespace):
            location = content["location"]
            geometry_id = self._place_point(namespace, rid)
            if geometry_id is None:
                continue
            tolerance = float((content.get("model") or {}).get("grid_resolution_m") or 0) or 1000.0
            relation = self.store.geo.relation(namespace, "proximity", geometry_id, coordinates, scopes=GEO_SCOPES,
                                               principal_id=principal_id, tolerance_m=tolerance)
            if not relation["result"]["within_tolerance"]:
                continue
            receipts.append({"operation": "grid-cell-proximity", "receipt_id": relation["receipt_id"],
                             "distance_m": relation["result"]["distance_m"], "tolerance_m": tolerance, "record_id": rid})
            try:
                summary, pin = self._series_summary(namespace, rid, scopes=read, as_of_ms=as_of, max_values=max_values)
            except EnvironmentStoreError:
                continue
            summary["grid_cell"] = {"ref": location["ref"], "distance_m": relation["result"]["distance_m"]}
            if pin:
                pins.append(pin)
            sections["model_output" if summary["kind"] == "model" else "forecasts"].append(summary)
        if not sections["model_output"]:
            gaps.append({"section": "model_output", "reason": "no reanalysis/model series covers the place's grid cell"})
        if not sections["forecasts"]:
            gaps.append({"section": "forecasts", "reason": "no forecast series covers the place's grid cell"})

        # Grid events for the bidding zone containing the place.
        zones = self.ensure_zones(namespace, principal_id=principal_id)
        zone = None
        for candidate in zones:
            relation = self.store.geo.relation(namespace, "contains", candidate["geometry_id"], coordinates,
                                               scopes=GEO_SCOPES, principal_id=principal_id)
            if relation["result"]["contains"]:
                zone = candidate
                receipts.append({"operation": "zone-contains", "receipt_id": relation["receipt_id"], "zone": candidate["code"],
                                 "basis": "authored coarse outline; precision_m=" + str(candidate["precision_m"])})
                break
        if zone is None:
            gaps.append({"section": "grid", "reason": "no declared bidding-zone outline contains the place"})
        else:
            sections["grid"] = self._grid_events(namespace, zone, scopes=read, as_of_ms=as_of, pins=pins,
                                                 max_values=max_values)
            if not sections["grid"]:
                gaps.append({"section": "grid", "reason": f"no grid records acquired for zone {zone['name']}"})

        # Facilities near the place.
        sections["facilities"] = self._facilities(namespace, coordinates, radius_m=radius_m, scopes=read, as_of_ms=as_of,
                                                  principal_id=principal_id, pins=pins, receipts=receipts,
                                                  max_values=max_values)
        if not sections["facilities"]:
            gaps.append({"section": "facilities", "reason": f"no acquired facility within {radius_m} m"})

        # Umweltatlas layers containing the place.
        for collection in layer_collections:
            for feature in self._polygon_features(namespace, collection):
                if feature[5] and self._contains(feature[5], coordinates):
                    pins.append({"kind": "feature-revision", "id": feature[3], "feature_id": feature[0]})
                    sections["layers"].append({"collection": collection, "feature_id": feature[0], "provider": feature[1],
                                               "native_id": feature[2], "title": feature[4],
                                               "properties": json.loads(feature[6]), "kind": "map-layer",
                                               "as_of": {"revision_id": feature[3], "observed_at_ms": int(feature[7])},
                                               "method": "exact ring parity over the pinned WGS84 geometry"})
        if not sections["layers"]:
            gaps.append({"section": "layers", "reason": "no acquired Umweltatlas layer feature contains the place"})
        return self._save(namespace, dossier_id, principal_id, request, place, coordinates, as_of, sections, pins,
                          receipts, gaps, resolution, zone=zone, boundary=boundary)

    def _grid_series(self, namespace):
        rows = self.conn.execute(
            "SELECT r.record_id, v.content_json FROM environment_records r JOIN environment_record_current c USING(record_id) "
            "JOIN environment_record_revisions v ON v.revision_id=c.revision_id WHERE r.namespace=? "
            "AND r.record_type='observation_series' ORDER BY r.record_id", [namespace]).fetchall()
        return [(rid, json.loads(content)) for rid, content in rows
                if json.loads(content)["location"]["kind"] == "grid-cell"]

    def _place_point(self, namespace, rid):
        row = self.conn.execute("SELECT geometry_id FROM environment_record_revisions WHERE record_id=? AND geometry_id "
                                "IS NOT NULL ORDER BY revision DESC LIMIT 1", [rid]).fetchone()
        if row:
            return row[0]
        place = self.conn.execute("SELECT place_id FROM environment_record_revisions WHERE record_id=? AND place_id IS NOT NULL "
                                  "ORDER BY revision DESC LIMIT 1", [rid]).fetchone()
        if not place:
            return None
        geometries = self.store.geo.geometries(namespace, place[0], scopes={"knowledge:geospatial:read"})
        return geometries[0]["geometry_id"] if geometries else None

    def _grid_events(self, namespace, zone, *, scopes, as_of_ms, pins, max_values):
        codes = {zone["code"], zone["name"], *zone["members"]}
        result = []
        for record in self.store.records(namespace, scopes=scopes, record_type="grid_event"):
            try:
                current = self.store.record(namespace, record["record_id"], scopes=scopes, as_of_ms=as_of_ms)
            except EnvironmentStoreError:
                continue
            content = current["content"]
            area = content["bidding_zone"]
            if area.get("code") not in codes and area.get("within") not in codes:
                continue
            pins.append({"kind": "record-revision", "id": current["revision_id"], "record_id": record["record_id"]})
            item = {"record_id": record["record_id"], "title": content["title"], "provider": content["provider"],
                    "event_type": content["event_type"], "kind": content["kind"], "area": area,
                    "zone_match": "zone" if area.get("code") in {zone["code"], zone["name"]} else "member area of zone",
                    "source_url": content["source_url"], "resolution": content.get("resolution"),
                    "production_type": content.get("production_type"), "unit": content["unit"],
                    "document": content.get("document"),
                    "as_of": {"revision_id": current["revision_id"], "observed_at_ms": current["observed_at_ms"]}}
            if content["event_type"] == "unavailability":
                item["unavailability"] = content["unavailability"]
                item["points"] = content.get("points")
            else:
                summary, pin = self._series_summary(namespace, record["record_id"], scopes=scopes, as_of_ms=as_of_ms,
                                                    max_values=max_values)
                item.update(values=summary["values"], vintage=summary["as_of"])
                if pin:
                    pins.append(pin)
            if content["kind"] == "forecast":
                item["kind_notice"] = "forecast values; never an observation"
            result.append(item)
        return result

    def _facilities(self, namespace, coordinates, *, radius_m, scopes, as_of_ms, principal_id, pins, receipts, max_values):
        from src.kb.environment_identity import operator_view

        result = []
        for record in self.store.records(namespace, scopes=scopes, record_type="facility"):
            geometry_id = self._place_point(namespace, record["record_id"])
            if geometry_id is None:
                continue
            relation = self.store.geo.relation(namespace, "proximity", geometry_id, coordinates, scopes=GEO_SCOPES,
                                               principal_id=principal_id, tolerance_m=float(radius_m))
            if not relation["result"]["within_tolerance"]:
                continue
            try:
                current = self.store.record(namespace, record["record_id"], scopes=scopes, as_of_ms=as_of_ms)
            except EnvironmentStoreError:
                continue
            receipts.append({"operation": "facility-proximity", "receipt_id": relation["receipt_id"],
                             "record_id": record["record_id"], "distance_m": relation["result"]["distance_m"]})
            pins.append({"kind": "record-revision", "id": current["revision_id"], "record_id": record["record_id"]})
            content = current["content"]
            linked = []
            for link in self.store.links(namespace, scopes=scopes, record=record["record_id"]):
                other = next(r for r in link["records"] if r != record["record_id"])
                other_record = self.store.record(namespace, other, scopes=scopes)
                series = []
                for series_id in self._series_at(namespace, f"{other_record['provider']}:{other_record['native_id']}"):
                    try:
                        summary, pin = self._series_summary(namespace, series_id, scopes=scopes, as_of_ms=as_of_ms,
                                                            max_values=max_values)
                    except EnvironmentStoreError:
                        continue
                    if pin:
                        pins.append(pin)
                    series.append(summary)
                pins.append({"kind": "record-revision", "id": other_record["revision_id"], "record_id": other})
                linked.append({"link": link, "record_id": other, "provider": other_record["provider"],
                               "title": other_record["content"]["title"], "permits": other_record["content"].get("permits"),
                               "operator": operator_view(self.conn, namespace, other, other_record["content"]["operator"]),
                               "series": series})
            releases = []
            for series_id in self._series_at(namespace, f"{content['provider']}:{content['native_id']}"):
                try:
                    summary, pin = self._series_summary(namespace, series_id, scopes=scopes, as_of_ms=as_of_ms,
                                                        max_values=max_values)
                except EnvironmentStoreError:
                    continue
                if pin:
                    pins.append(pin)
                releases.append(summary)
            result.append({"record_id": record["record_id"], "title": content["title"], "provider": content["provider"],
                           "source_url": content["source_url"], "distance_m": relation["result"]["distance_m"],
                           "activities": content.get("activities"), "permits": content.get("permits"),
                           "operator": operator_view(self.conn, namespace, record["record_id"], content["operator"]),
                           "releases": releases, "linked_records": linked, "kind": "observation (reported)",
                           "as_of": {"revision_id": current["revision_id"], "observed_at_ms": current["observed_at_ms"]},
                           "notice": "reported data as published; no compliance determination is made"})
        return sorted(result, key=lambda item: item["distance_m"])

    def _save(self, namespace, dossier_id, principal_id, request, place, coordinates, as_of, sections, pins, receipts,
              gaps, resolution, zone=None, boundary=None):
        providers = sorted({item["provider"] for section in ("model_output", "forecasts", "grid", "facilities")
                            for item in sections[section]}
                           | {s["provider"] for entry in sections["observations"] for s in entry["series"]})
        content = {
            "contract": DOSSIER_CONTRACT, "dossier_id": dossier_id, "namespace": namespace,
            "place": {"place_id": place["place_id"], "canonical_name": place["canonical_name"],
                      "place_type": place["place_type"], "coordinates": coordinates, "revision_id": place["revision_id"]},
            "resolution": None if resolution is None else {"resolution_id": resolution["resolution_id"],
                                                           "status": resolution["status"]},
            "as_of_ms": as_of, "boundary": boundary, "bidding_zone": zone, "sections": sections,
            "coverage_gaps": gaps, "not_implemented": NOT_IMPLEMENTED,
            "providers": {p: self.store.provider_state(namespace, p) for p in providers},
            "receipts": receipts,
            "status": "coverage_gap" if not any(sections.values()) else "complete" if not gaps else "partial",
            "boundaries": ["observations, model output and forecasts are listed separately and never mixed",
                           "no attribution, projection or compliance determination is inferred",
                           "thresholds are not asserted by this pack"],
            "evidence_kind": "as acquired (see provider execution: injected fixtures are not live coverage)",
        }
        content_hash = digest(content)
        pins = sorted({canonical(p): p for p in pins}.values(), key=canonical)
        self.conn.execute("INSERT INTO environment_dossiers VALUES (?,?,?,?,?,?,?,?,?,?)",
                          [dossier_id, namespace, principal_id, digest(request), place["place_id"], as_of,
                           canonical(content), canonical(pins), content_hash, self.now()])
        return {**content, "pins": pins, "content_hash": content_hash, "stale": False, "stale_reasons": []}

    # ------------------------------------------------------------- reads

    def _row(self, namespace, dossier_id, principal_id, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        row = self.conn.execute("SELECT owner, content_json, pins_json, content_hash, created_at_ms FROM environment_dossiers "
                                "WHERE dossier_id=? AND namespace=?", [dossier_id, namespace]).fetchone()
        if row is None or (row[0] != principal_id and "operator" not in set(scopes)):
            raise EnvironmentStoreError("not_found", "dossier is not visible")
        return row

    def staleness(self, namespace, pins, *, created_at_ms=None):
        reasons = []
        for pin in pins:
            if pin["kind"] == "vintage":
                newer = self.conn.execute(
                    "SELECT vintage_id FROM environment_vintages WHERE record_id=? AND sequence > (SELECT sequence FROM "
                    "environment_vintages WHERE vintage_id=?) ORDER BY sequence", [pin["record_id"], pin["id"]]).fetchall()
                if newer:
                    reasons.append({"pin": pin, "reason": "newer vintage", "newer": [r[0] for r in newer]})
            elif pin["kind"] == "record-revision":
                current = self.conn.execute("SELECT revision_id FROM environment_record_current WHERE record_id=?",
                                            [pin["record_id"]]).fetchone()
                if current and current[0] != pin["id"]:
                    reasons.append({"pin": pin, "reason": "newer record revision", "newer": [current[0]]})
            elif pin["kind"] == "feature-revision":
                current = self.conn.execute("SELECT revision_id FROM geospatial_feature_current WHERE feature_id=?",
                                            [pin["feature_id"]]).fetchone()
                if current and current[0] != pin["id"]:
                    reasons.append({"pin": pin, "reason": "newer feature revision", "newer": [current[0]]})
        return reasons

    def inspect(self, namespace, dossier_id, *, scopes, principal_id):
        row = self._row(namespace, dossier_id, principal_id, scopes)
        content, pins = json.loads(row[1]), json.loads(row[2])
        reasons = self.staleness(namespace, pins, created_at_ms=int(row[4]))
        return {**content, "pins": pins, "content_hash": row[3], "stale": bool(reasons), "stale_reasons": reasons,
                "recompute": "build a new dossier (new request_key) to adopt newer vintages" if reasons else None}

    def replay(self, namespace, dossier_id, *, scopes, principal_id):
        """Recompute every spatial receipt from pinned geometry and verify the stored content hash."""

        row = self._row(namespace, dossier_id, principal_id, scopes)
        content, pins = json.loads(row[1]), json.loads(row[2])
        checks = []
        for receipt in content["receipts"]:
            if receipt["operation"] == "points_within":
                replay = self.store.features.replay_within(namespace, receipt["receipt_id"], scopes=GEO_SCOPES)
            else:
                replay = self.store.geo.replay(namespace, receipt["receipt_id"], scopes=GEO_SCOPES)
            checks.append({"receipt_id": receipt["receipt_id"], "operation": receipt["operation"],
                           "deterministic": replay["deterministic"]})
        missing = []
        for pin in pins:
            table, column = {"vintage": ("environment_vintages", "vintage_id"),
                             "record-revision": ("environment_record_revisions", "revision_id"),
                             "feature-revision": ("geospatial_feature_revisions", "revision_id"),
                             "place-geometry": ("geospatial_geometries", "geometry_id")}[pin["kind"]]
            if not self.conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", [pin["id"]]).fetchone():
                missing.append(pin)
        return {"dossier_id": dossier_id, "content_hash_verified": digest(content) == row[3], "receipts": checks,
                "pins_retained": not missing, "missing_pins": missing,
                "deterministic": all(c["deterministic"] for c in checks) and not missing and digest(content) == row[3]}

    def export(self, namespace, dossier_id, *, scopes, principal_id):
        """A cited Markdown rendering plus the structured dossier; kinds and as-of are shown for every item."""

        dossier = self.inspect(namespace, dossier_id, scopes=scopes, principal_id=principal_id)
        lines = [f"# Environmental dossier: {dossier['place']['canonical_name']}",
                 f"As of {_iso(dossier['as_of_ms'])}. Status: {dossier['status']}. Stale: {dossier['stale']}.",
                 "Observations, model output and forecasts are listed separately. No attribution, projection or "
                 "compliance determination is made.", ""]
        citations = []

        def cite(url):
            if url not in citations:
                citations.append(url)
            return f"[{citations.index(url) + 1}]"

        lines.append("## Observations")
        for entry in dossier["sections"]["observations"]:
            station = entry["station"]
            lines.append(f"- Station {station['title']} ({station['provider']}) {cite(station['source_url'])}")
            for series in entry["series"]:
                lines.append(f"  - {series['indicator']['code']} [{series['kind']}], {series['unit']}, "
                             f"vintage {series['as_of']['vintage_id'] if series['as_of'] else 'none'} "
                             f"({series['as_of']['status'] if series['as_of'] else 'n/a'}) {cite(series['source_url'])}")
        for section, heading in (("model_output", "Model output (reanalysis)"), ("forecasts", "Forecasts")):
            lines.append(f"## {heading}")
            for series in dossier["sections"][section]:
                model = series["model"] or {}
                lines.append(f"- {series['title']} [{series['kind']}] model {model.get('name')}"
                             f"{', issued ' + str(model.get('issue_time')) if series['kind'] == 'forecast' else ''} "
                             f"{cite(series['source_url'])}")
        lines.append("## Grid (bidding zone " + str((dossier["bidding_zone"] or {}).get("name")) + ")")
        for event in dossier["sections"]["grid"]:
            extra = ""
            if event["event_type"] == "unavailability":
                u = event["unavailability"]
                extra = f" {u['kind']} {u['start']}–{u['end']}, reason: {'; '.join(r['text'] or r['code'] for r in u['reason'])}"
            lines.append(f"- {event['title']} [{event['kind']}]{extra} {cite(event['source_url'])}")
        lines.append("## Facilities")
        for facility in dossier["sections"]["facilities"]:
            lines.append(f"- {facility['title']} ({facility['distance_m']:.0f} m) operator: "
                         f"{facility['operator']['source_name']} {cite(facility['source_url'])}")
            for linked in facility["linked_records"]:
                lines.append(f"  - linked {linked['provider']} record {linked['title']} {cite(facility['source_url'])}")
        lines.append("## Layers")
        for layer in dossier["sections"]["layers"]:
            lines.append(f"- {layer['collection']}: {layer['title']} (map layer)")
        lines.append("## Coverage gaps")
        for gap in dossier["coverage_gaps"]:
            lines.append(f"- {gap['section']}: {gap['reason']}")
        for provider, reason in dossier["not_implemented"].items():
            lines.append(f"- {provider}: {reason}")
        lines.append("")
        lines.extend(f"[{i + 1}] {url}" for i, url in enumerate(citations))
        markdown = "\n".join(lines) + "\n"
        return {"contract": "noesis-environment-dossier-export-v1", "dossier": dossier, "markdown": markdown,
                "citations": citations, "sha256": digest([dossier["content_hash"], markdown])}


def _iso(value):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()

