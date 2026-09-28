"""Housing records projected onto places and answered as of a date (#1912, U07).

An address or parcel is a Geospatial place, reached through the reviewable
place-resolution flow (``record_geospatial_resolution`` /
``review_geospatial_resolution``) or named directly by place id; a bare point
is accepted as well. A district is its ALKIS boundary feature
(``alkis_bezirke:bezirksgrenzen``, matched by the published Bezirk code).
Containment uses the existing spatial operations only - ``calculate-spatial-relation``
(:meth:`src.kb.geospatial.GeospatialStore.relation`) against the geometry a
housing record references, and ``features-within``/point-in-feature
(:meth:`src.kb.geospatial_features.GeospatialFeatureStore.containing`) for the
district - so every membership has a spatial receipt; no spatial index or
store is introduced.

As of a date the dossier selects, per source:

* the land-value-zone revision whose valuation date is the latest not after
  the date (earlier valuation dates listed as prior revisions, undated
  readings listed as undated);
* each development plan's stage current on that date (the latest dated stage
  not after it) with its stage history;
* the Wohnlage category and the rent-index edition valid then, and the
  edition's cells whose published Wohnlage equals the place's category;
* the district's permit and completion statistics and the Land's statistics
  and Destatis indicator vintages published on or before the date.

A point on a zone boundary or in no published zone is reported as such; two
sources that disagree are listed side by side. Nothing is interpolated,
averaged or chosen between zones, cells, sources or editions, and nothing in
the dossier is a valuation or advice. Transit stops may be listed as
accessibility context and carry no valuation meaning.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from src.ingestion.housing_sources import (
    NO_SURFACE,
    PLAN_STAGES,
    REVIEW_BOUNDARY,
    plan_key,
    reporting_area,
)
from src.kb.housing import (
    READ_SCOPE,
    HousingError,
    HousingStore,
    authorize,
    digest,
    iso_day,
    require_scope,
    table_exists,
)

DOSSIER_CONTRACT = "noesis-housing-dossier-v1"
GEO_READ = "knowledge:geospatial:read"
GEO_CALCULATE = "knowledge:geospatial:calculate"
TRANSIT_READ = "knowledge:transit:read"
NEWS_READ = "knowledge:read"
DISTRICT_COLLECTION = "alkis_bezirke:bezirksgrenzen"
DISTRICT_CODE_PROPERTY = "gem"
DISTRICT_NAME_PROPERTY = "namgem"
LAND_CODE = "11"  # Berlin, AGS Land code
MAX_PARCEL_VERTICES = 64
TRANSIT_RADIUS_DEG = 0.005
_STAGE_ORDER = {stage: index for index, stage in enumerate(PLAN_STAGES)}


def _end_of_day_ms(day: str) -> int:
    moment = datetime.combine(
        date.fromisoformat(day), datetime.max.time(), tzinfo=timezone.utc
    )
    return int(moment.timestamp() * 1000)


def _bbox(geometry: Mapping[str, Any]) -> tuple[float, float, float, float]:
    kind, coordinates = geometry["type"], geometry["coordinates"]
    if kind == "Point":
        points = [coordinates]
    elif kind == "Polygon":
        points = [p for ring in coordinates for p in ring]
    elif kind == "MultiPolygon":
        points = [p for polygon in coordinates for ring in polygon for p in ring]
    else:
        points = list(coordinates)
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _vertices(geometry: Mapping[str, Any]) -> list[list[float]]:
    kind, coordinates = geometry["type"], geometry["coordinates"]
    if kind == "Point":
        return [list(coordinates)]
    if kind == "Polygon":
        rings = coordinates[:1]
    elif kind == "MultiPolygon":
        rings = [polygon[0] for polygon in coordinates]
    else:
        raise HousingError(
            "unsupported_geometry", f"a {kind} place cannot be located in zones"
        )
    unique = []
    for ring in rings:
        for vertex in ring[:-1]:
            if list(vertex) not in unique:
                unique.append(list(vertex))
    return unique


class HousingPlaces:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.geospatial import GeospatialStore
        from src.kb.geospatial_features import GeospatialFeatureStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = HousingStore(conn, initialize=initialize, now=self.now)
        self.geo = GeospatialStore(conn, initialize=initialize, now=self.now)
        self.features = GeospatialFeatureStore(
            conn, initialize=initialize, now=self.now
        )

    # ------------------------------------------------------------------ inputs

    def _resolution(self, geo_namespace: str, resolution_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT status, selected_place_id, candidates_json, mention FROM geocode_resolutions "
            "WHERE namespace=? AND resolution_id=?",
            [geo_namespace, resolution_id],
        ).fetchone()
        if row is None:
            raise HousingError(
                "not_found", "place resolution is not recorded in this namespace"
            )
        review = self.conn.execute(
            "SELECT decision, selected_place_id, review_id, principal_id FROM geocode_reviews "
            "WHERE resolution_id=? ORDER BY revision DESC LIMIT 1",
            [resolution_id],
        ).fetchone()
        candidates = [c["place_id"] for c in json.loads(row[2] or "[]")]
        base = {
            "resolution_id": resolution_id,
            "mention": row[3],
            "status": row[0],
            "candidates": candidates,
        }
        if review and review[0] == "accept":
            return {
                **base,
                "place_id": review[1],
                "basis": "accepted review",
                "review_id": review[2],
                "reviewed_by": review[3],
            }
        if review and review[0] == "reject":
            return {
                **base,
                "place_id": None,
                "basis": "rejected review",
                "review_id": review[2],
            }
        if row[0] == "resolved" and row[1]:
            return {
                **base,
                "place_id": row[1],
                "basis": "single high-confidence candidate (not yet reviewed)",
            }
        return {
            **base,
            "place_id": None,
            "basis": f"resolution is {row[0]}; review it to select a place",
        }

    def locate(
        self,
        geo_namespace: str,
        *,
        scopes: set[str],
        place_id: str | None = None,
        resolution_id: str | None = None,
        point: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        """The WGS84 points a place stands for: an address point, or a parcel's outline vertices."""
        if sum(v is not None for v in (place_id, resolution_id, point)) != 1:
            raise HousingError(
                "invalid_request",
                "give exactly one of place_id, resolution_id or point",
            )
        resolution = None
        if resolution_id is not None:
            resolution = self._resolution(geo_namespace, resolution_id)
            if not resolution["place_id"]:
                return {
                    "status": "place_unresolved",
                    "resolution": resolution,
                    "points": [],
                }
            place_id = resolution["place_id"]
        if point is not None:
            try:
                lon, lat = float(point[0]), float(point[1])
            except (TypeError, ValueError, IndexError) as exc:
                raise HousingError(
                    "invalid_point", "point is [longitude, latitude]"
                ) from exc
            if len(point) != 2 or not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise HousingError(
                    "invalid_point", "point is [longitude, latitude] in WGS84"
                )
            return {
                "status": "located",
                "kind": "point",
                "points": [[lon, lat]],
                "place": None,
                "resolution": None,
            }
        place = self.geo.place(geo_namespace, str(place_id), scopes=scopes | {GEO_READ})
        if place is None:
            raise HousingError("not_found", "place is not registered in this namespace")
        geometries = [
            g
            for g in self.geo.geometries(
                geo_namespace, str(place_id), scopes=scopes | {GEO_READ}
            )
            if g["geometry"]["type"] in {"Point", "Polygon", "MultiPolygon"}
        ]
        if not geometries:
            return {
                "status": "place_without_geometry",
                "place": place,
                "resolution": resolution,
                "points": [],
            }
        geometry = next(
            (g for g in geometries if g["geometry"]["type"] == "Point"), geometries[0]
        )
        points = _vertices(geometry["geometry"])
        if len(points) > MAX_PARCEL_VERTICES:
            raise HousingError(
                "input_limit",
                f"a parcel outline is checked at up to {MAX_PARCEL_VERTICES} vertices",
            )
        kind = "point" if geometry["geometry"]["type"] == "Point" else "parcel"
        return {
            "status": "located",
            "kind": kind,
            "points": points,
            "place": {
                k: place[k]
                for k in ("place_id", "canonical_name", "place_type", "revision_id")
            },
            "geometry_id": geometry["geometry_id"],
            "resolution": resolution,
            "note": None
            if kind == "point"
            else "a parcel is located by its outline vertices; a zone lying wholly "
            "inside the parcel without touching a vertex is not detected",
        }

    # ------------------------------------------------------------------ spatial

    def _contains(
        self, geo_namespace, geometry_id, points, principal_id, scopes
    ) -> dict[str, Any]:
        """calculate-spatial-relation (contains) for each point against one pinned geometry, with receipts."""
        loaded = self.features._geometries([geometry_id])[geometry_id]["geometry"]
        west, south, east, north = _bbox(loaded)
        hits, receipts = [], []
        for point in points:
            if not (west <= point[0] <= east and south <= point[1] <= north):
                continue
            receipt = self.geo.relation(
                geo_namespace,
                "contains",
                geometry_id,
                list(point),
                scopes=scopes | {GEO_CALCULATE, GEO_READ},
                principal_id=principal_id,
            )
            receipts.append(receipt["receipt_id"])
            if receipt["result"]["contains"]:
                hits.append(list(point))
        return {"contains": bool(hits), "points": hits, "receipt_ids": receipts}

    @staticmethod
    def _geometry_namespace(conn, geometry_id: str) -> str | None:
        row = conn.execute(
            "SELECT namespace FROM geospatial_geometries WHERE geometry_id=?",
            [geometry_id],
        ).fetchone()
        return row[0] if row else None

    def _members(self, records, points, principal_id, scopes):
        members, receipts = [], []
        self._unchecked = []
        for record in records:
            geometry_id = record.get("geometry_id")
            geo_namespace = (
                self._geometry_namespace(self.conn, geometry_id)
                if geometry_id
                else None
            )
            if geo_namespace is None:
                # Never read as "outside": a record whose geometry is not stored is reported as unchecked.
                self._unchecked.append(
                    {
                        "record_id": record.get("record_id"),
                        "reason": "geometry_not_stored",
                    }
                )
                continue
            found = self._contains(
                geo_namespace, geometry_id, points, principal_id, scopes
            )
            receipts += found["receipt_ids"]
            if found["contains"]:
                members.append((record, found["points"]))
        return members, receipts

    # ------------------------------------------------------------------ sections

    def _land_values(
        self, namespace, points, day, principal_id, scopes
    ) -> dict[str, Any]:
        rows = self.store.records("land_value_revision", namespace)
        dated = [
            r for r in rows if r.get("valuation_date") and r["valuation_date"] <= day
        ]
        latest: dict[tuple, dict[str, Any]] = {}
        for row in dated:
            key = (
                row["source_id"],
                row["source_revision"]["collection"],
                row["zone_id"],
            )
            if (
                key not in latest
                or row["valuation_date"] > latest[key]["valuation_date"]
            ):
                latest[key] = row
        members, receipts = self._members(
            list(latest.values()), points, principal_id, scopes
        )
        zones = []
        for record, hit_points in sorted(
            members, key=lambda m: (m[0]["zone_id"], m[0]["source_id"])
        ):
            history = [
                r
                for r in rows
                if r["source_id"] == record["source_id"]
                and r["zone_id"] == record["zone_id"]
                and r["source_revision"]["collection"]
                == record["source_revision"]["collection"]
            ]
            zones.append(
                {
                    "zone_id": record["zone_id"],
                    "zone_name": record.get("zone_name"),
                    "source_id": record["source_id"],
                    "selected": record,
                    "selection_basis": f"latest valuation date not after {day}",
                    "prior_revisions": sorted(
                        (
                            r
                            for r in history
                            if r.get("valuation_date")
                            and r["valuation_date"] < record["valuation_date"]
                        ),
                        key=lambda r: r["valuation_date"],
                        reverse=True,
                    ),
                    "later_revisions": sorted(
                        (
                            r
                            for r in history
                            if r.get("valuation_date") and r["valuation_date"] > day
                        ),
                        key=lambda r: r["valuation_date"],
                    ),
                    "undated_readings": [
                        r for r in history if not r.get("valuation_date")
                    ],
                    "points_inside": hit_points,
                }
            )
        zone_ids = {z["zone_id"] for z in zones}
        status = (
            "not_determined"
            if not zones and self._unchecked
            else "outside_published_zones"
            if not zones
            else "single_zone"
            if len(zone_ids) == 1
            else "several_zones"
        )
        return {
            "status": status,
            "note": {
                "outside_published_zones": "the place lies in no published land-value zone for this date; no "
                "value is inferred from neighbouring zones",
                "several_zones": "the place touches several zones (a shared boundary, an overlap or a parcel "
                "spanning zones); each zone is listed, none is chosen",
                "single_zone": None,
                "not_determined": "no checked zone contains the place, but some zones could not be checked "
                "(see unchecked_records); the place is not reported as outside",
            }[status],
            "zones": zones,
            "receipt_ids": receipts,
            "unchecked_records": self._unchecked,
        }

    def _plans(self, namespace, points, day, principal_id, scopes) -> dict[str, Any]:
        rows = self.store.records("plan_stage", namespace)
        by_plan: dict[tuple, list[dict[str, Any]]] = {}
        for row in rows:
            by_plan.setdefault(
                (
                    row["source_id"],
                    row["source_revision"]["collection"],
                    row["source_revision"]["feature_id"],
                ),
                [],
            ).append(row)
        candidates = []
        for stages in by_plan.values():
            newest = max(stages, key=lambda r: r["sequence"])
            candidates.append({**newest, "_stages": stages})
        members, receipts = self._members(candidates, points, principal_id, scopes)
        plans = []
        for record, _hit in sorted(members, key=lambda m: m[0]["plan_id"]):
            stages = sorted(
                record.pop("_stages"),
                key=lambda r: (
                    r.get("stage_date") or "9999",
                    _STAGE_ORDER.get(r["stage"], 99),
                ),
            )
            dated = [
                s for s in stages if s.get("stage_date") and s["stage_date"] <= day
            ]
            current = (
                max(
                    dated,
                    key=lambda s: (s["stage_date"], _STAGE_ORDER.get(s["stage"], 99)),
                )
                if dated
                else None
            )
            plans.append(
                {
                    "plan_id": record["plan_id"],
                    "district": record.get("district"),
                    "source_id": record["source_id"],
                    "stage_on_date": current,
                    "status": "stage_selected"
                    if current
                    else (
                        "stage_date_unknown"
                        if any(not s.get("stage_date") for s in stages)
                        else "no_stage_on_or_before_date"
                    ),
                    "selection_basis": f"latest dated stage not after {day}",
                    "stage_history": stages,
                    "later_stages": [
                        s
                        for s in stages
                        if s.get("stage_date") and s["stage_date"] > day
                    ],
                }
            )
        return {
            "plans": plans,
            "receipt_ids": receipts,
            "unchecked_records": self._unchecked,
        }

    def _area(self, namespace, points, day, principal_id, scopes) -> dict[str, Any]:
        rows = [
            r
            for r in self.store.records("area_category", namespace)
            if (r["edition"].get("valid_from") or "9999") <= day
        ]
        latest: dict[tuple, str] = {}
        for row in rows:
            key = (row["source_id"],)
            latest[key] = max(latest.get(key, ""), row["edition"]["valid_from"])
        current = [
            r for r in rows if r["edition"]["valid_from"] == latest[(r["source_id"],)]
        ]
        members, receipts = self._members(current, points, principal_id, scopes)
        categories = [
            {
                "category": r["category"],
                "edition": r["edition"],
                "area_id": r["area_id"],
                "record": r,
            }
            for r, _ in sorted(members, key=lambda m: m[0]["area_id"])
        ]
        labels = sorted({c["category"] for c in categories})
        return {
            "status": "none"
            if not labels
            else "single"
            if len(labels) == 1
            else "several",
            "categories": categories,
            "receipt_ids": receipts,
            "unchecked_records": self._unchecked,
            "note": "source-labelled categories; not a value or score",
        }

    def _rent_index(self, namespace, day, area: Mapping[str, Any]) -> dict[str, Any]:
        editions = [
            e
            for e in self.store.editions(namespace)
            if (e.get("valid_from") or "9999") <= day
        ]
        if not editions:
            return {"status": "no_edition_valid_on_date", "editions": []}
        out = []
        by_source: dict[str, dict[str, Any]] = {}
        for edition in editions:
            if (
                edition["source_id"] not in by_source
                or edition["valid_from"] > by_source[edition["source_id"]]["valid_from"]
            ):
                by_source[edition["source_id"]] = edition
        labels = {c["category"].casefold(): c for c in area["categories"]}
        for edition in by_source.values():
            cells = self.store.records(
                "rent_index_cell", namespace, edition_id=edition["edition_id"]
            )
            matched = [
                c
                for c in cells
                if str((c.get("dimensions") or {}).get("wohnlage") or "").casefold()
                in labels
            ]
            area_editions = {c["edition"]["edition_id"] for c in area["categories"]}
            out.append(
                {
                    "edition": edition,
                    "selection_basis": f"the edition valid from the latest date not after {day}",
                    "cells_for_place": sorted(matched, key=lambda c: c["cell_key"]),
                    "match_basis": "the cell's published Wohnlage equals the place's source-labelled Wohnlage category"
                    if labels
                    else None,
                    "status": "cells_matched"
                    if matched
                    else "wohnlage_unknown"
                    if not labels
                    else "no_matching_cell",
                    "wohnlage_edition_matches": edition["edition_id"] in area_editions
                    if labels
                    else None,
                    "note": "all cells of the edition for the place's Wohnlage are listed; dwelling age and size are "
                    "not known here, so no single cell is chosen",
                }
            )
        return {"status": "edition_selected", "editions": out}

    def _district(self, geo_namespace, points, principal_id, scopes) -> dict[str, Any]:
        found = self.features.containing(
            geo_namespace,
            collection=DISTRICT_COLLECTION,
            point=points[0],
            principal_id=principal_id,
            scopes=scopes | {GEO_CALCULATE, GEO_READ},
        )
        districts = []
        for member in found["members"]:
            feature = self.features.feature(
                geo_namespace,
                member["feature_id"],
                scopes=scopes | {GEO_READ},
                include_history=False,
            )
            properties = feature["current"]["properties"]
            districts.append(
                {
                    "feature_id": member["feature_id"],
                    "revision_id": member["revision_id"],
                    "name": properties.get(DISTRICT_NAME_PROPERTY),
                    "code": reporting_area(
                        "berlin-bezirk", properties.get(DISTRICT_CODE_PROPERTY)
                    ),
                    "match_basis": f"published Bezirk code equals the ALKIS feature property {DISTRICT_CODE_PROPERTY!r}",
                }
            )
        return {
            "status": found["status"],
            "districts": districts,
            "receipt_id": found["receipt"]["receipt_id"],
            "coverage": found["coverage"]["completeness"],
        }

    def _statistics(self, namespace, scheme, code, day) -> list[dict[str, Any]]:
        rows = [
            r
            for r in self.store.statistics(namespace, scheme=scheme, code=code)
            if r["vintage"] <= day
        ]
        latest: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = (row["source_id"], row["statistic"], row["measure"], row["period"])
            if key not in latest or row["vintage"] > latest[key]["vintage"]:
                latest[key] = row
        return [
            {
                **row,
                "earlier_vintages": sorted(
                    r["vintage"]
                    for r in rows
                    if r["entity_key"] != row["entity_key"]
                    and (r["source_id"], r["statistic"], r["measure"], r["period"])
                    == key
                ),
                "selection_basis": f"the latest vintage published on or before {day}",
            }
            for key, row in sorted(latest.items())
        ]

    def _indicators(self, namespace, geography, day) -> list[dict[str, Any]]:
        out = []
        cutoff = _end_of_day_ms(day)
        for series_id in sorted(
            {
                v["series_id"]
                for v in self.store.indicator_vintages(namespace, geography=geography)
            }
        ):
            out.append(
                self.store.indicator_values(namespace, series_id, as_of_ms=cutoff)
            )
        return out

    @staticmethod
    def _disagreements(statistics, indicators) -> list[dict[str, Any]]:
        readings: dict[tuple, list[dict[str, Any]]] = {}
        for row in statistics:
            readings.setdefault((row["measure"], row["period"]), []).append(
                {
                    "source_id": row["source_id"],
                    "provider": row["provider"],
                    "value": row["value"],
                    "unit": row["unit"],
                    "vintage": row["vintage"],
                    "record_id": row["record_id"],
                }
            )
        for series in indicators:
            if series["status"] != "selected":
                continue
            vintage = series["vintage"]
            for observation in series["observations"]:
                if observation["value"] is None:
                    continue
                value = observation["value"]
                text = str(int(value)) if float(value).is_integer() else str(value)
                readings.setdefault(
                    (vintage["measure"], observation["period"]), []
                ).append(
                    {
                        "source_id": vintage["source_id"],
                        "provider": vintage["provider"],
                        "value": text,
                        "unit": vintage["unit"],
                        "vintage": vintage["published_on"],
                        "record_id": vintage["record_id"],
                    }
                )
        out = []
        for (measure, period), items in sorted(readings.items()):
            providers = {i["provider"] for i in items}
            if len(providers) > 1 and len({i["value"] for i in items}) > 1:
                out.append(
                    {
                        "measure": measure,
                        "period": period,
                        "readings": items,
                        "note": "sources disagree; both readings are kept side by side and none is chosen",
                    }
                )
        return out

    # ------------------------------------------------------------------ dossier

    def dossier(
        self,
        namespace: str,
        *,
        as_of: str,
        principal_id: str,
        scopes: Iterable[str],
        geo_namespace: str = "global",
        place_id: str | None = None,
        resolution_id: str | None = None,
        point: Sequence[float] | None = None,
        district_code: str | None = None,
        include_transit_feed: str | None = None,
        include_news: bool = False,
    ) -> dict[str, Any]:
        """The cited housing dossier of an address, parcel, point or district as of a date."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, GEO_CALCULATE)
        require_scope(scopes, GEO_READ)
        day = iso_day(as_of)
        base = {
            "contract": DOSSIER_CONTRACT,
            "namespace": namespace,
            "as_of": day,
            "review_boundary": REVIEW_BOUNDARY,
            "value_surface": NO_SURFACE,
        }
        if district_code is not None:
            if any(v is not None for v in (place_id, resolution_id, point)):
                raise HousingError(
                    "invalid_request", "a district dossier takes only the district code"
                )
            return self._district_dossier(
                namespace, geo_namespace, district_code, day, base, scopes
            )
        located = self.locate(
            geo_namespace,
            scopes=scopes,
            place_id=place_id,
            resolution_id=resolution_id,
            point=point,
        )
        if located["status"] != "located":
            return {**base, "status": located["status"], "input": located}
        points = located["points"]
        land = self._land_values(namespace, points, day, principal_id, scopes)
        plans = self._plans(namespace, points, day, principal_id, scopes)
        area = self._area(namespace, points, day, principal_id, scopes)
        rent_index = self._rent_index(namespace, day, area)
        district = self._district(geo_namespace, points, principal_id, scopes)
        district_statistics = [
            {
                **d,
                "statistics": self._statistics(
                    namespace, "berlin-bezirk", d["code"], day
                ),
            }
            for d in district["districts"]
            if d["code"]
        ]
        land_statistics = self._statistics(namespace, "ags", LAND_CODE, day)
        indicators = self._indicators(namespace, LAND_CODE, day)
        answer = {
            **base,
            "status": "answered",
            "input": {
                k: located.get(k)
                for k in ("kind", "points", "place", "resolution", "note")
            },
            "land_value": land,
            "development_plans": plans,
            "residential_area": area,
            "rent_index": rent_index,
            "district": {**district, "districts": district_statistics},
            "land": {
                "code": LAND_CODE,
                "scheme": "ags",
                "level_note": "Land Berlin figures are listed at their own level; nothing is apportioned to the "
                "district or place",
                "statistics": land_statistics,
                "indicators": indicators,
                "disagreements": self._disagreements(land_statistics, indicators),
            },
        }
        answer["links"] = self._links(namespace, rent_index, plans, scopes)
        if include_transit_feed:
            answer["transit_context"] = self._transit(
                geo_namespace, include_transit_feed, points[0], scopes
            )
        if include_news:
            answer["news_context"] = self._news(namespace, rent_index, plans, scopes)
        answer["receipt"] = self._receipt(answer)
        return answer

    def _district_dossier(
        self, namespace, geo_namespace, district_code, day, base, scopes
    ):
        code = reporting_area("berlin-bezirk", district_code)
        rows = (
            self.conn.execute(
                "SELECT f.feature_id, r.properties_json, r.revision_id FROM geospatial_features f JOIN "
                "geospatial_feature_current c ON c.feature_id=f.feature_id JOIN geospatial_feature_revisions r ON "
                "r.revision_id=c.revision_id WHERE f.namespace IN (?, 'global') AND f.collection=? AND "
                "c.lifecycle='active' ORDER BY f.feature_id",
                [geo_namespace, DISTRICT_COLLECTION],
            ).fetchall()
            if table_exists(self.conn, "geospatial_features")
            else []
        )
        districts = [
            {
                "feature_id": r[0],
                "revision_id": r[2],
                "name": json.loads(r[1]).get(DISTRICT_NAME_PROPERTY),
                "code": code,
                "match_basis": f"published Bezirk code equals the ALKIS feature property "
                f"{DISTRICT_CODE_PROPERTY!r}",
            }
            for r in rows
            if reporting_area(
                "berlin-bezirk", json.loads(r[1]).get(DISTRICT_CODE_PROPERTY)
            )
            == code
        ]
        names = {d["name"].casefold() for d in districts if d["name"]}
        plans = []
        by_plan: dict[str, list[dict[str, Any]]] = {}
        for row in self.store.records("plan_stage", namespace):
            if str(row.get("district") or "").casefold() in names:
                by_plan.setdefault(plan_key(row["plan_id"]), []).append(row)
        for stages in by_plan.values():
            stages.sort(
                key=lambda r: (
                    r.get("stage_date") or "9999",
                    _STAGE_ORDER.get(r["stage"], 99),
                )
            )
            dated = [
                s for s in stages if s.get("stage_date") and s["stage_date"] <= day
            ]
            plans.append(
                {
                    "plan_id": stages[0]["plan_id"],
                    "stage_history": stages,
                    "stage_on_date": dated[-1] if dated else None,
                    "match_basis": "the plan's published district name equals the ALKIS district name",
                }
            )
        land_statistics = self._statistics(namespace, "ags", LAND_CODE, day)
        indicators = self._indicators(namespace, LAND_CODE, day)
        answer = {
            **base,
            "status": "answered" if districts else "district_not_found",
            "input": {"kind": "district", "district_code": code},
            "district": {
                "districts": [
                    {
                        **d,
                        "statistics": self._statistics(
                            namespace, "berlin-bezirk", code, day
                        ),
                    }
                    for d in districts
                ]
            },
            "development_plans": {"plans": sorted(plans, key=lambda p: p["plan_id"])},
            "land_value": {
                "status": "not_answered_for_districts",
                "note": "land values are answered per address or parcel; a district spans many zones "
                "and no district value is formed",
            },
            "rent_index": self._rent_index(namespace, day, {"categories": []}),
            "land": {
                "code": LAND_CODE,
                "scheme": "ags",
                "statistics": land_statistics,
                "indicators": indicators,
                "disagreements": self._disagreements(land_statistics, indicators),
            },
        }
        answer["links"] = self._links(
            namespace, answer["rent_index"], answer["development_plans"], scopes
        )
        answer["receipt"] = self._receipt(answer)
        return answer

    def _links(self, namespace, rent_index, plans, scopes) -> list[dict[str, Any]]:
        from src.kb.housing_links import LINKING_STATES, HousingLinks

        links = HousingLinks(self.conn, initialize=False)
        if not links.ready():
            return []
        subjects = [
            ("rent-index-edition", e["edition"]["edition_id"])
            for e in rent_index.get("editions", [])
        ]
        subjects += [("plan", plan_key(p["plan_id"])) for p in plans.get("plans", [])]
        current = links.current_record_ids(namespace)
        out = []
        for kind, subject_id in subjects:
            out += [
                link
                for link in links.links(
                    namespace,
                    scopes=scopes,
                    subject_kind=kind,
                    subject_id=subject_id,
                    states=(*LINKING_STATES, "unresolved"),
                )
                if link.get("invalid")
                or link["basis"] != "explicit_citation"
                or link["source_record_id"] in current
            ]
        return out

    def _transit(self, geo_namespace, feed, point, scopes) -> dict[str, Any]:
        from src.kb.transit import TransitError, TransitStore

        require_scope(scopes, TRANSIT_READ)
        if not table_exists(self.conn, "transit_feed_versions"):
            return {
                "status": "transit_feed_not_acquired",
                "stops": [],
                "note": "accessibility context only",
            }
        bbox = [
            point[0] - TRANSIT_RADIUS_DEG,
            point[1] - TRANSIT_RADIUS_DEG,
            point[0] + TRANSIT_RADIUS_DEG,
            point[1] + TRANSIT_RADIUS_DEG,
        ]
        try:
            found = TransitStore(self.conn, initialize=False).stops_in_bbox(
                geo_namespace, feed, bbox, scopes=scopes
            )
        except TransitError as exc:
            return {
                "status": exc.code,
                "stops": [],
                "note": "accessibility context only",
            }
        return {
            **found,
            "bbox": bbox,
            "note": "stops near the place as accessibility context only; they carry no valuation meaning",
        }

    def _news(self, namespace, rent_index, plans, scopes) -> list[dict[str, Any]]:
        from src.kb.housing_links import HousingLinks

        require_scope(scopes, NEWS_READ)
        terms = [
            e["edition"]["edition"]
            for e in rent_index.get("editions", [])
            if e["edition"].get("edition")
        ]
        terms += [p["plan_id"] for p in plans.get("plans", [])]
        return HousingLinks(self.conn, initialize=False).news_context(terms)

    @staticmethod
    def _receipt(answer: Mapping[str, Any]) -> dict[str, Any]:
        """A digest over every record, source revision and spatial receipt the answer used."""

        def ids(value):
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if (
                        key
                        in {
                            "record_id",
                            "receipt_id",
                            "feature_revision_id",
                            "source_revision_id",
                        }
                        and item
                    ):
                        yield f"{key}={item}"
                    elif key == "receipt_ids":
                        yield from (f"receipt_id={i}" for i in item)
                    else:
                        yield from ids(item)
            elif isinstance(value, list):
                for item in value:
                    yield from ids(item)

        used = sorted(set(ids({k: v for k, v in answer.items() if k != "receipt"})))
        request = {k: answer[k] for k in ("namespace", "as_of")} | {
            "input": answer.get("input")
        }
        return {
            "contract": "noesis-housing-dossier-receipt-v1",
            "request": request,
            "used": used,
            "dossier_digest": digest([request, used]),
        }

    def replay(
        self, receipt: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Recompute a dossier from its receipt's request; reproduced only when the same records are selected."""
        request = dict(receipt.get("request") or {})
        given = dict(request.get("input") or {})
        if not request.get("namespace") or not request.get("as_of") or not given:
            raise HousingError(
                "invalid_receipt", "a dossier receipt carries its request"
            )
        kwargs: dict[str, Any] = {}
        if given.get("kind") == "district":
            kwargs["district_code"] = given.get("district_code")
        elif (given.get("resolution") or {}).get("resolution_id"):
            kwargs["resolution_id"] = given["resolution"]["resolution_id"]
        elif given.get("place"):
            kwargs["place_id"] = given["place"]["place_id"]
        elif given.get("points"):
            kwargs["point"] = given["points"][0]
        else:
            raise HousingError(
                "invalid_receipt", "the receipt names no place, point or district"
            )
        answer = self.dossier(
            request["namespace"],
            as_of=request["as_of"],
            principal_id=principal_id,
            scopes=scopes,
            **kwargs,
        )
        again = answer["receipt"]
        return {
            "status": "reproduced"
            if again["dossier_digest"] == receipt.get("dossier_digest")
            else "changed",
            "receipt": again,
            "added": sorted(set(again["used"]) - set(receipt.get("used") or [])),
            "removed": sorted(set(receipt.get("used") or []) - set(again["used"])),
        }

    # ------------------------------------------------------------------ comparison

    def compare_zone(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        zone_id: str | None = None,
        feature_id: str | None = None,
    ) -> dict[str, Any]:
        """One zone's values across valuation dates and sources, side by side; no change is computed."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if bool(zone_id) == bool(feature_id):
            raise HousingError(
                "invalid_request", "give exactly one of zone_id or feature_id"
            )
        rows = self.store.land_value_history(
            namespace, zone_id=zone_id, feature_id=feature_id
        )
        corrections = self.store.records(
            "land_value_revision",
            namespace,
            current_only=False,
            zone_id=zone_id,
            feature_id=feature_id,
        )
        by_date: dict[str | None, list[dict[str, Any]]] = {}
        for row in rows:
            by_date.setdefault(row.get("valuation_date"), []).append(row)
        return {
            "zone_id": zone_id or (rows[0]["zone_id"] if rows else None),
            "valuation_dates": [
                {
                    "valuation_date": day,
                    "readings": [
                        {
                            k: r.get(k)
                            for k in (
                                "record_id",
                                "source_id",
                                "provider",
                                "value_text",
                                "value",
                                "currency",
                                "unit",
                                "use_type",
                                "qualifiers",
                                "valuation_date_basis",
                                "source_revision",
                            )
                        }
                        for r in sorted(items, key=lambda r: r["source_id"])
                    ],
                    "sources_disagree": len({r["value"] for r in items}) > 1,
                }
                for day, items in sorted(by_date.items(), key=lambda kv: kv[0] or "")
            ],
            "corrections": [r for r in corrections if r.get("change") == "correction"],
            "note": "values side by side by valuation date and source; no change, trend or interpolated value is "
            "computed",
        }
