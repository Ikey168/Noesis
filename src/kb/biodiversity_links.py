"""Occurrences to places and datasets, datasets and assessments to cited publications (#2220, BD07 #2522).

Exact references only, each link citing the source revision it came from:

* **Places.** An occurrence with published coordinates is projected through the
  geospatial owner (:class:`src.kb.geospatial.GeospatialStore`) as a point
  place at its published precision (``precision_m`` = the published coordinate
  uncertainty or the publisher's stated generalisation), and its relation to
  each boundary place is computed with a replayable ``contains`` receipt:
  ``within`` only when the point is inside and farther from the boundary than
  its uncertainty, ``uncertain`` when the uncertainty reaches the boundary or
  is not published. A generalised record links only to places whose extent is
  at or above the stated generalisation; without a stated precision - and for
  records without coordinates - it links only by published country code to
  places registered for exactly that code (``source_ids`` ``iso3166-1``).
  Locality text is never geocoded. This reuses the place model the
  environment dossiers use (:mod:`src.kb.environment_places`); no spatial
  store is added.
* **Datasets.** An occurrence links to the dataset record with its exact
  ``datasetKey``; a dataset not acquired stays an unresolved reference.
* **Publications.** A dataset's bibliographic citations and an assessment's
  citation link to Science paper documents whose metadata states the same DOI
  (as the Astronomy citations do); anything else stays an ``unresolved``
  citation with its text.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.biodiversity_records import (
    LINK_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    authorize,
    canonical,
    digest,
)
from src.kb.biodiversity_store import BiodiversityStore, table_exists

GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate"}
PRODUCER = {"name": "noesis-environment-biodiversity", "version": "1.0.0"}
CODE_SCHEMES = ("iso3166-1", "iso3166", "iso3166-1-alpha2")
_DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
_DDL = """
CREATE TABLE IF NOT EXISTS biodiversity_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL, basis TEXT NOT NULL,
  detail_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def normalize_doi(value: Any) -> str | None:
    match = _DOI.search(str(value or ""))
    return match.group(0).rstrip(".,;").lower() if match else None


# ------------------------------------------------------------------ geometry helpers


def _meters(origin_lat: float, a: list[float], b: list[float]) -> tuple[float, float]:
    scale = math.cos(math.radians(origin_lat))
    return (b[0] - a[0]) * 111_320.0 * scale, (b[1] - a[1]) * 110_574.0


def _rings(geometry: Mapping[str, Any]) -> list[list[list[float]]]:
    if geometry["type"] == "Polygon":
        return list(geometry["coordinates"])
    if geometry["type"] == "MultiPolygon":
        return [ring for polygon in geometry["coordinates"] for ring in polygon]
    return []


def boundary_distance_m(geometry: Mapping[str, Any], point: list[float]) -> float:
    """Shortest distance (local equirectangular metres) from the point to the polygon boundary."""
    best = math.inf
    for ring in _rings(geometry):
        for a, b in zip(ring, ring[1:], strict=False):
            ax, ay = _meters(point[1], point, a)
            bx, by = _meters(point[1], point, b)
            dx, dy = bx - ax, by - ay
            length = dx * dx + dy * dy
            t = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def extent_m(geometry: Mapping[str, Any]) -> float:
    """The place's smaller bounding-box side in metres (its 'level' for generalised records)."""
    points = [p for ring in _rings(geometry) for p in ring]
    if not points:
        return 0.0
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    mid = (min(lats) + max(lats)) / 2
    width, height = _meters(mid, [min(lons), min(lats)], [max(lons), max(lats)])
    return min(abs(width), abs(height))


def contains(geometry: Mapping[str, Any], point: list[float]) -> bool:
    from src.kb.geospatial import _contains

    return bool(_contains(geometry, point, 0))


def place_view(conn: Any, namespace: str, place_id: str) -> dict[str, Any] | None:
    """A place with its boundary geometry (if any) and the exact codes it was registered for."""
    from src.kb.geospatial import GeospatialStore

    row = conn.execute("SELECT namespace FROM geospatial_places WHERE place_id=? AND namespace IN (?, 'global')",
                       [place_id, namespace]).fetchone()
    if row is None:
        return None
    geo = GeospatialStore(conn, initialize=False)
    place = geo.place(row[0], place_id, scopes={"knowledge:geospatial:read"})
    polygons = [g for g in geo.geometries(row[0], place_id, scopes={"knowledge:geospatial:read"},
                                          include_disputed=False)
                if g and g["geometry"]["type"] in {"Polygon", "MultiPolygon"}]
    codes = sorted({str(v).upper() for k, v in place["source_ids"].items() if k in CODE_SCHEMES})
    boundary = polygons[0] if polygons else None
    return {"place_id": place_id, "namespace": row[0], "name": place["canonical_name"],
            "revision_id": place["revision_id"], "codes": codes,
            "geometry": boundary["geometry"] if boundary else None,
            "geometry_id": boundary["geometry_id"] if boundary else None,
            "extent_m": round(extent_m(boundary["geometry"]), 1) if boundary else None}


def classify(published: Mapping[str, Any], place: Mapping[str, Any]) -> dict[str, Any]:
    """The relation of one occurrence (as published) to one place; never inferred beyond the published precision."""
    flags = published.get("generalisation") or {}
    coords = published.get("coordinates")
    code = (published.get("country_code") or "").upper()
    by_code = {"relation": "code", "basis": f"published country code {code} equals the place's registered code",
               "code": code} if code and code in place["codes"] else None
    if coords is None:
        reason = ("coordinates withheld by the publisher" if flags.get("coordinates_withheld")
                  else "no coordinates published")
        return by_code or {"relation": "not-linked", "basis": f"{reason}; locality text is never geocoded"}
    if place["geometry"] is None:
        return by_code or {"relation": "not-linked", "basis": "the place has no boundary geometry or matching code"}
    if flags.get("generalised"):
        precision = flags.get("precision_m")
        if precision is None:
            return by_code or {"relation": "not-linked", "basis": "generalised without a stated precision; linked "
                                                                  "only by published country code"}
        if place["extent_m"] < precision:
            return by_code or {"relation": "below-generalisation",
                               "basis": f"generalised to {precision:g} m; the place ({place['extent_m']:g} m) is "
                                        "below that level, so no link is made"}
    point = [coords["longitude"], coords["latitude"]]
    inside = contains(place["geometry"], point)
    distance = boundary_distance_m(place["geometry"], point)
    uncertainty = published.get("coordinate_uncertainty_m")
    radius = max([v for v in (uncertainty, flags.get("precision_m")) if v is not None], default=None)
    detail = {"inside": inside, "distance_to_boundary_m": round(distance, 1), "uncertainty_m": uncertainty,
              "generalisation_m": flags.get("precision_m")}
    if radius is None:
        relation = "uncertain" if inside else "outside"
        basis = ("inside, but coordinateUncertaintyInMeters is not published" if inside
                 else "outside the boundary")
    elif inside and distance >= radius:
        relation, basis = "within", "inside and farther from the boundary than the published uncertainty"
    elif inside or distance <= radius:
        relation, basis = "uncertain", "the published uncertainty reaches the place boundary"
    else:
        relation, basis = "outside", "outside the boundary beyond the published uncertainty"
    return {"relation": relation, "basis": basis, **detail}


class BiodiversityLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = BiodiversityStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "biodiversity_links")

    def _insert(self, namespace, record_id, revision_id, target_kind, target_id, relation, basis, detail,
                principal_id) -> bool:
        link_id = "biodiversity-link:" + digest([namespace, revision_id, target_kind, target_id])[:24]
        inserted = self.conn.execute(
            "INSERT INTO biodiversity_links VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING link_id",
            [namespace, link_id, record_id, revision_id, target_kind, target_id, relation, basis, canonical(detail),
             principal_id, self.now()]).fetchall()
        return bool(inserted)

    def _current_occurrences(self, namespace):
        for record in self.store.records(namespace, record_type="occurrence"):
            revision = self.store.current(namespace, record["record_id"])
            if revision and revision["event"] == "published":
                yield record, revision

    def _project_point(self, namespace, record, revision, principal_id) -> dict[str, Any] | None:
        """The occurrence point as a geospatial place at its published precision (idempotent per revision)."""
        from src.kb.geospatial import GeospatialStore

        published = revision["statement"]["as_published"]
        coords = published.get("coordinates")
        if coords is None:
            return None
        geo = GeospatialStore(self.conn, initialize=False)
        key = f"biodiversity-occurrence:{revision['revision_id']}"
        row = self.conn.execute("SELECT place_id FROM geospatial_places WHERE namespace=? AND place_key=?",
                                [namespace, key]).fetchone()
        precision = max([v for v in (published.get("coordinate_uncertainty_m"),
                                     (published.get("generalisation") or {}).get("precision_m")) if v is not None],
                        default=0.0)
        if row:
            return {"place_id": row[0]}
        name = f"GBIF occurrence {published['gbif_id']}"
        place = geo.register_place(
            namespace, name, "biodiversity-occurrence", names=[{"value": name, "language": "und", "kind": "canonical"}],
            source_ids={"gbif-occurrence": published["gbif_id"]}, parent_ids=[], principal_id=principal_id,
            scopes=GEO_SCOPES, place_key=key, observed_at_ms=revision["observed_at_ms"], producer=PRODUCER,
            provenance={"record_id": record["record_id"], "revision_id": revision["revision_id"]})
        stored = geo.store_geometry(
            namespace, {"type": "Point", "coordinates": [coords["longitude"], coords["latitude"]]},
            place_id=place["place_id"], crs="EPSG:4326", precision_m=float(precision), simplified_from=None,
            disputed=False, admin_hierarchy=[], source={"kind": "biodiversity-occurrence",
                                                        "revision_id": revision["revision_id"]},
            evidence=[{"kind": "provider-record", "source_url": revision["statement"]["source"]["url"]}],
            principal_id=principal_id, scopes=GEO_SCOPES, observed_at_ms=revision["observed_at_ms"],
            producer=PRODUCER, policy={"crs": "explicit-v1", "precision": "published uncertainty or generalisation; "
                                                                        "never refined"})
        return {"place_id": place["place_id"], "geometry_id": stored["geometry_id"]}

    def link_places(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                    place_ids: Iterable[str] | None = None) -> dict[str, Any]:
        """Link current occurrences to boundary or code places and to their datasets; returns counts per relation."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        if place_ids is None:
            place_ids = [r[0] for r in self.conn.execute(
                "SELECT place_id FROM geospatial_places WHERE namespace=? AND place_type<>'biodiversity-occurrence' "
                "ORDER BY place_id", [namespace]).fetchall()]
        places = [p for p in (place_view(self.conn, namespace, pid) for pid in place_ids)
                  if p and (p["geometry"] or p["codes"])]
        geo = GeospatialStore(self.conn, initialize=False)
        counts: dict[str, int] = {}
        datasets = {r["record_key"]: r for r in self.store.records(namespace, record_type="dataset")}
        for record, revision in self._current_occurrences(namespace):
            published = revision["statement"]["as_published"]
            projected = self._project_point(namespace, record, revision, principal_id)
            for place in places:
                result = classify(published, place)
                counts[result["relation"]] = counts.get(result["relation"], 0) + 1
                if result["relation"] not in {"within", "uncertain", "code"}:
                    continue
                if result["relation"] != "code":
                    receipt = geo.relation(place["namespace"], "contains", place["geometry_id"],
                                           [published["coordinates"]["longitude"],
                                            published["coordinates"]["latitude"]],
                                           scopes=GEO_SCOPES, principal_id=principal_id)
                    result = {**result, "receipt_id": receipt["receipt_id"]}
                self._insert(namespace, record["record_id"], revision["revision_id"], "place", place["place_id"],
                             result["relation"], result["basis"],
                             {**result, "place_revision_id": place["revision_id"], "projected": projected},
                             principal_id)
            dataset = datasets.get(published["dataset_key"])
            self._insert(namespace, record["record_id"], revision["revision_id"], "dataset",
                         dataset["record_id"] if dataset else published["dataset_key"],
                         "published-by" if dataset else "unresolved",
                         "exact datasetKey" if dataset else "dataset metadata not acquired; kept as an unresolved "
                                                            "reference", {"dataset_key": published["dataset_key"]},
                         principal_id)
        return {"contract": LINK_CONTRACT, "places": [p["place_id"] for p in places], "relations": counts,
                "links": self.links(namespace, scopes=scopes)}

    # ------------------------------------------------------------------ citations

    def _papers(self) -> dict[str, dict[str, Any]]:
        tables = {r[0] for r in self.conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name IN "
                                                  "('documents', 'document_revision_records')").fetchall()}
        if len(tables) < 2:
            return {}
        index = {}
        for document_id, title, metadata in self.conn.execute(
                "SELECT document_id, title, metadata FROM documents WHERE source_type='paper' ORDER BY document_id"
        ).fetchall():
            revision = self.conn.execute(
                "SELECT revision_id FROM document_revision_records WHERE document_id=? AND committed_watermark IS "
                "NOT NULL ORDER BY revision DESC LIMIT 1", [document_id]).fetchone()
            meta = json.loads(metadata) if isinstance(metadata, str) and metadata else dict(metadata or {})
            doi = normalize_doi(meta.get("doi"))
            if doi and revision:
                index[doi] = {"document_id": document_id, "revision_id": revision[0], "title": title}
        return index

    def link_citations(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Datasets' bibliographic citations and assessments' citations to papers by exact DOI; others unresolved."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        papers = self._papers()
        resolved = unresolved = 0
        for record_type in ("dataset", "conservation_assessment"):
            for record in self.store.records(namespace, record_type=record_type):
                revision = self.store.current(namespace, record["record_id"])
                published = revision["statement"]["as_published"]
                if record_type == "dataset":
                    refs = [{"text": r.get("text"), "doi": normalize_doi(r.get("identifier")) or normalize_doi(
                        r.get("text"))} for r in published.get("cited_references") or []]
                else:
                    refs = [{"text": published.get("citation"), "doi": normalize_doi(published.get("citation"))}] \
                        if published.get("citation") else []
                for ref in refs:
                    paper = papers.get(ref["doi"]) if ref["doi"] else None
                    target = paper["document_id"] if paper else (ref["doi"] or digest(ref["text"])[:16])
                    if paper:
                        resolved += 1
                    else:
                        unresolved += 1
                    self._insert(namespace, record["record_id"], revision["revision_id"],
                                 "publication" if paper else "unresolved-citation", target,
                                 "cites" if paper else "unresolved",
                                 "exact DOI stated by the source and by the paper's metadata" if paper else
                                 ("DOI stated but no paper with that DOI is held" if ref["doi"] else
                                  "no DOI or exact identifier; kept as an unresolved citation"),
                                 {"cited_text": ref["text"], "doi": ref["doi"],
                                  "paper_revision_id": (paper or {}).get("revision_id")}, principal_id)
        return {"contract": LINK_CONTRACT, "resolved": resolved, "unresolved": unresolved,
                "links": [link for link in self.links(namespace, scopes=scopes)
                          if link["target_kind"] in {"publication", "unresolved-citation"}]}

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, *, scopes: Iterable[str], record_id: str | None = None,
              target_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT link_id, record_id, revision_id, target_kind, target_id, relation, basis, detail_json, created_by "
            "FROM biodiversity_links WHERE namespace=? AND (? IS NULL OR record_id=?) AND (? IS NULL OR target_id=?) "
            "ORDER BY record_id, target_kind, target_id, link_id",
            [namespace, record_id, record_id, target_id, target_id]).fetchall()
        return [{"contract": LINK_CONTRACT, **dict(zip(("link_id", "record_id", "revision_id", "target_kind",
                                                        "target_id", "relation", "basis"), r[:7])),
                 "detail": json.loads(r[7]), "created_by": r[8]} for r in rows]

    def generation(self, namespace: str) -> int:
        if not self._ready():
            return 0
        return int(self.conn.execute("SELECT count(*) FROM biodiversity_links WHERE namespace=?",
                                     [namespace]).fetchone()[0])


__all__ = ["BiodiversityLinks", "boundary_distance_m", "classify", "extent_m", "normalize_doi", "place_view"]
