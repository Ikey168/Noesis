"""Versioned places, geometry, geocoding, and reproducible spatial relations."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

PLACE_CONTRACT = "noesis-geospatial-place-v1"
GEOMETRY_CONTRACT = "noesis-geospatial-geometry-v1"
RESOLUTION_CONTRACT = "noesis-geocode-resolution-v1"
SPATIAL_CONTRACT = "noesis-spatial-result-v1"
READ_SCOPE = "knowledge:geospatial:read"
WRITE_SCOPE = "knowledge:geospatial:write"
REVIEW_SCOPE = "knowledge:geospatial:review"
CALCULATE_SCOPE = "knowledge:geospatial:calculate"

_DDL = """
CREATE TABLE IF NOT EXISTS geospatial_places (
  place_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, place_key TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, UNIQUE(namespace,place_key)
);
CREATE TABLE IF NOT EXISTS geospatial_place_revisions (
  revision_id TEXT PRIMARY KEY, place_id TEXT NOT NULL, namespace TEXT NOT NULL,
  revision BIGINT NOT NULL, predecessor_revision_id TEXT, canonical_name TEXT NOT NULL,
  place_type TEXT NOT NULL, names_json TEXT NOT NULL, parent_ids_json TEXT NOT NULL,
  source_ids_json TEXT NOT NULL, generation BIGINT NOT NULL, valid_from_ms BIGINT,
  valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL,
  policy_json TEXT NOT NULL, provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  input_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL, UNIQUE(place_id,revision)
);
CREATE TABLE IF NOT EXISTS geospatial_place_current (
  place_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, revision BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS geospatial_geometries (
  geometry_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, place_id TEXT,
  geometry_type TEXT NOT NULL, coordinates_json TEXT NOT NULL, crs TEXT NOT NULL,
  precision_m DOUBLE NOT NULL, simplified_from TEXT, disputed BOOLEAN NOT NULL,
  admin_hierarchy_json TEXT NOT NULL, source_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
  generation BIGINT NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL, policy_json TEXT NOT NULL,
  principal_id TEXT NOT NULL, content_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS geocode_resolutions (
  resolution_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, mention TEXT NOT NULL,
  context_json TEXT NOT NULL, candidates_json TEXT NOT NULL, status TEXT NOT NULL,
  selected_place_id TEXT, confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL,
  method_json TEXT NOT NULL, input_hash TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS geocode_reviews (
  review_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, resolution_id TEXT NOT NULL,
  revision BIGINT NOT NULL, decision TEXT NOT NULL, selected_place_id TEXT,
  reason TEXT NOT NULL, predecessor_review_id TEXT, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(resolution_id,revision)
);
CREATE TABLE IF NOT EXISTS spatial_receipts (
  receipt_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  request_json TEXT NOT NULL, result_json TEXT NOT NULL, input_ids_json TEXT NOT NULL,
  algorithm TEXT NOT NULL, calculation_hash TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS geospatial_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_geospatial_place_name
  ON geospatial_place_revisions(namespace,canonical_name);
CREATE INDEX IF NOT EXISTS idx_geospatial_geometry_place
  ON geospatial_geometries(namespace,place_id,valid_from_ms);
"""


class GeospatialError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise GeospatialError("unauthorized", f"missing required scope {required}")


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).strip()


def _cursor(payload: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(_canonical(payload).encode()).decode().rstrip("=")


def _uncursor(value: str) -> dict[str, Any]:
    try:
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except Exception as exc:
        raise GeospatialError("invalid_cursor", "spatial cursor is malformed") from exc


def _point(value: Sequence[Any]) -> tuple[float, float]:
    if len(value) < 2:
        raise GeospatialError(
            "invalid_geometry", "coordinate needs longitude and latitude"
        )
    lon, lat = float(value[0]), float(value[1])
    if (
        not math.isfinite(lon)
        or not math.isfinite(lat)
        or not -180 <= lon <= 180
        or not -90 <= lat <= 90
    ):
        raise GeospatialError("invalid_geometry", "WGS84 coordinates are out of range")
    return lon, lat


def _validate_geometry(geometry: Mapping[str, Any]) -> tuple[str, Any]:
    kind = str(geometry.get("type") or "")
    coordinates = geometry.get("coordinates")
    if kind == "Point":
        return kind, list(_point(coordinates or []))
    if kind == "LineString":
        points = [list(_point(item)) for item in coordinates or []]
        if len(points) < 2:
            raise GeospatialError("invalid_geometry", "line needs at least two points")
        return kind, points
    if kind == "Polygon":
        rings = []
        for raw_ring in coordinates or []:
            ring = [list(_point(item)) for item in raw_ring]
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise GeospatialError(
                    "invalid_geometry", "polygon rings must be closed"
                )
            rings.append(ring)
        if not rings:
            raise GeospatialError("invalid_geometry", "polygon needs an exterior ring")
        return kind, rings
    raise GeospatialError(
        "invalid_geometry", "only Point, LineString, and Polygon are supported"
    )


def _longitude_delta(left: float, right: float) -> float:
    return (right - left + 540) % 360 - 180


def _distance_m(left: Sequence[Any], right: Sequence[Any]) -> float:
    lon1, lat1 = _point(left)
    lon2, lat2 = _point(right)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(_longitude_delta(lon1, lon2))
    dp = p2 - p1
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6_371_008.8 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def _unwrap(
    points: Sequence[Sequence[Any]], reference: float
) -> list[tuple[float, float]]:
    values = []
    for point in points:
        lon, lat = _point(point)
        while lon - reference > 180:
            lon -= 360
        while lon - reference < -180:
            lon += 360
        values.append((lon, lat))
    return values


def _point_in_ring(
    point: Sequence[Any], ring: Sequence[Sequence[Any]], tolerance: float = 0
) -> bool:
    x, y = _point(point)
    unwrapped = _unwrap(ring, x)
    inside = False
    for index, (x1, y1) in enumerate(unwrapped):
        x2, y2 = unwrapped[(index + 1) % len(unwrapped)]
        if min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance:
            cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
            if (
                abs(cross) <= tolerance
                and min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance
            ):
                return True
        if (y1 > y) != (y2 > y):
            intersection = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection:
                inside = not inside
    return inside


def _contains(
    geometry: Mapping[str, Any], point: Sequence[Any], tolerance_m: float
) -> bool:
    kind, coordinates = _validate_geometry(geometry)
    if kind == "Point":
        return _distance_m(coordinates, point) <= tolerance_m
    if kind == "Polygon":
        tolerance_deg = max(tolerance_m, 0) / 111_320
        return _point_in_ring(point, coordinates[0], tolerance_deg) and not any(
            _point_in_ring(point, ring, tolerance_deg) for ring in coordinates[1:]
        )
    return any(_distance_m(item, point) <= tolerance_m for item in coordinates)


def _orientation(a, b, c) -> float:
    return (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])


def _segments_intersect(a, b, c, d, tolerance: float) -> bool:
    values = [
        _orientation(a, b, c),
        _orientation(a, b, d),
        _orientation(c, d, a),
        _orientation(c, d, b),
    ]
    return values[0] * values[1] <= tolerance and values[2] * values[3] <= tolerance


def _segments(
    geometry: Mapping[str, Any],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    kind, coordinates = _validate_geometry(geometry)
    if kind == "Point":
        return []
    lines = [coordinates] if kind == "LineString" else coordinates
    return [
        (tuple(line[i]), tuple(line[i + 1]))
        for line in lines
        for i in range(len(line) - 1)
    ]


def _intersects(
    left: Mapping[str, Any], right: Mapping[str, Any], tolerance_m: float
) -> bool:
    lk, lc = _validate_geometry(left)
    rk, rc = _validate_geometry(right)
    if lk == "Point":
        return _contains(right, lc, tolerance_m)
    if rk == "Point":
        return _contains(left, rc, tolerance_m)
    tolerance = max(tolerance_m, 0) / 111_320
    return any(
        _segments_intersect(a, b, c, d, tolerance)
        for a, b in _segments(left)
        for c, d in _segments(right)
    )


def _points(geometry: Mapping[str, Any]) -> list[list[float]]:
    kind, coordinates = _validate_geometry(geometry)
    if kind == "Point":
        return [coordinates]
    if kind == "LineString":
        return coordinates
    return [point for ring in coordinates for point in ring]


class GeospatialStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
            self._bootstrap_gazetteer()

    def _bootstrap_gazetteer(self) -> None:
        from src.analytics.geospatial import GAZETTEER

        for name, (lat, lon, country) in GAZETTEER.items():
            self._register_place(
                "global",
                name,
                "country"
                if name
                in {
                    "germany",
                    "france",
                    "spain",
                    "italy",
                    "united kingdom",
                    "united states",
                    "china",
                    "ukraine",
                }
                else "settlement",
                names=[{"value": name, "language": "und", "kind": "canonical"}],
                source_ids={"builtin": name},
                parent_ids=[],
                principal_id="system",
                place_key=f"builtin:{name}",
                context={
                    "generation": 0,
                    "valid_from_ms": None,
                    "valid_to_ms": None,
                    "observed_at_ms": 0,
                    "producer": {"name": "noesis-static-gazetteer", "version": "1"},
                    "policy": {"offline": True},
                    "provenance": {"source": "src.analytics.geospatial.GAZETTEER"},
                },
                geometry={"type": "Point", "coordinates": [lon, lat]},
            )

    def _audit(
        self, namespace, operation, object_id, principal_id, detail, now
    ) -> None:
        audit_id = (
            "geospatial-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO geospatial_audit VALUES (?,?,?,?,?,?,?)",
            [
                audit_id,
                namespace,
                operation,
                object_id,
                principal_id,
                _canonical(detail),
                now,
            ],
        )

    def register_place(
        self,
        namespace: str,
        canonical_name: str,
        place_type: str,
        *,
        names: Sequence[Mapping[str, Any]],
        source_ids: Mapping[str, str],
        parent_ids: Sequence[str],
        principal_id: str,
        scopes: set[str],
        place_key: str | None = None,
        geometry: Mapping[str, Any] | None = None,
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        now = self.now()
        key = place_key or _digest([canonical_name.casefold(), dict(source_ids)])
        prior = self.conn.execute(
            "SELECT r.observed_at_ms FROM geospatial_places p "
            "JOIN geospatial_place_current c USING(place_id) "
            "JOIN geospatial_place_revisions r USING(revision_id) "
            "WHERE p.namespace=? AND p.place_key=?",
            [namespace, key],
        ).fetchone()
        context = {
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms
                if observed_at_ms is not None
                else prior[0]
                if prior
                else now
            ),
            "producer": dict(
                producer or {"name": "noesis-geospatial", "version": "1.0.0"}
            ),
            "policy": dict(policy or {"ambiguity": "preserve-v1"}),
            "provenance": dict(provenance or {}),
        }
        self.conn.execute("BEGIN")
        try:
            result = self._register_place(
                namespace,
                canonical_name,
                place_type,
                names=names,
                source_ids=source_ids,
                parent_ids=parent_ids,
                principal_id=principal_id,
                place_key=key,
                context=context,
                geometry=geometry,
            )
            if not result.get("idempotent"):
                self._audit(
                    namespace,
                    "register-place",
                    result["place_id"],
                    principal_id,
                    {},
                    now,
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def _register_place(
        self,
        namespace,
        canonical_name,
        place_type,
        *,
        names,
        source_ids,
        parent_ids,
        principal_id,
        place_key,
        context,
        geometry=None,
    ):
        if not canonical_name.strip() or not place_type.strip():
            raise GeospatialError(
                "invalid_place", "canonical name and place type are required"
            )
        normalized_names = [dict(item) for item in names] or [
            {"value": canonical_name, "kind": "canonical", "language": "und"}
        ]
        if any(not str(item.get("value") or "").strip() for item in normalized_names):
            raise GeospatialError("invalid_place", "every name needs a value")
        key = place_key or _digest([canonical_name.casefold(), dict(source_ids)])
        place_id = "place:" + _digest([namespace, key])[:24]
        stable = {
            "canonical_name": canonical_name.strip(),
            "place_type": place_type,
            "names": normalized_names,
            "parent_ids": sorted(set(parent_ids)),
            "source_ids": dict(sorted(source_ids.items())),
            **context,
        }
        input_hash = _digest(stable)
        existing = self.conn.execute(
            "SELECT revision_id FROM geospatial_place_revisions WHERE place_id=? AND input_hash=?",
            [place_id, input_hash],
        ).fetchone()
        key_row = self.conn.execute(
            "SELECT place_id FROM geospatial_places WHERE namespace=? AND place_key=?",
            [namespace, key],
        ).fetchone()
        if key_row and key_row[0] != place_id:
            raise GeospatialError(
                "place_conflict", "place key resolves to a different identity"
            )
        if existing:
            return {
                **self.place(namespace, place_id, scopes={READ_SCOPE}),
                "idempotent": True,
            }
        if key_row:
            raise GeospatialError(
                "place_conflict",
                "place key was reused with different semantics; revise the place",
            )
        now = self.now()
        revision_id = "place-revision:" + input_hash[:24]
        self.conn.execute(
            "INSERT INTO geospatial_places VALUES (?,?,?,?,?)",
            [place_id, namespace, key, principal_id, now],
        )
        self.conn.execute(
            "INSERT INTO geospatial_place_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                revision_id,
                place_id,
                namespace,
                1,
                None,
                stable["canonical_name"],
                place_type,
                _canonical(normalized_names),
                _canonical(stable["parent_ids"]),
                _canonical(stable["source_ids"]),
                context["generation"],
                context["valid_from_ms"],
                context["valid_to_ms"],
                context["observed_at_ms"],
                _canonical(context["producer"]),
                _canonical(context["policy"]),
                _canonical(context["provenance"]),
                principal_id,
                input_hash,
                now,
            ],
        )
        self.conn.execute(
            "INSERT INTO geospatial_place_current VALUES (?,?,?)",
            [place_id, revision_id, 1],
        )
        if geometry:
            self._store_geometry(
                namespace,
                geometry,
                place_id=place_id,
                crs="EPSG:4326",
                precision_m=1000,
                simplified_from=None,
                disputed=False,
                admin_hierarchy=stable["parent_ids"],
                source=context["provenance"],
                evidence=[],
                principal_id=principal_id,
                context=context,
            )
        return self.place(namespace, place_id, scopes={READ_SCOPE})

    def place(
        self,
        namespace: str,
        place_id: str,
        *,
        scopes: set[str],
        revision: int | None = None,
        include_history: bool = False,
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT revision_id,revision,predecessor_revision_id,canonical_name,place_type,names_json,parent_ids_json,source_ids_json,generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_json,provenance_json,principal_id,input_hash,created_at_ms FROM geospatial_place_revisions WHERE namespace=? AND place_id=? AND (? IS NULL OR revision=?) ORDER BY revision DESC",
            [namespace, place_id, revision, revision],
        ).fetchall()
        if not rows:
            return None

        def render(row):
            return {
                "contract": PLACE_CONTRACT,
                "place_id": place_id,
                "namespace": namespace,
                "revision_id": row[0],
                "revision": int(row[1]),
                "predecessor_revision_id": row[2],
                "canonical_name": row[3],
                "place_type": row[4],
                "names": _load(row[5], []),
                "parent_ids": _load(row[6], []),
                "source_ids": _load(row[7], {}),
                "generation": int(row[8]),
                "valid_from_ms": row[9],
                "valid_to_ms": row[10],
                "observed_at_ms": int(row[11]),
                "producer": _load(row[12], {}),
                "policy": _load(row[13], {}),
                "provenance": _load(row[14], {}),
                "principal_id": row[15],
                "input_hash": row[16],
                "created_at_ms": int(row[17]),
            }

        result = render(rows[0])
        if include_history:
            result["history"] = [render(row) for row in rows]
        return result

    def revise_place(
        self,
        namespace: str,
        place_id: str,
        expected_revision: int,
        patch: Mapping[str, Any],
        *,
        reason: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        prior = self.place(namespace, place_id, scopes={READ_SCOPE})
        if not prior:
            raise GeospatialError("not_found", "place does not exist")
        if prior["revision"] != expected_revision:
            raise GeospatialError("revision_conflict", "place revision changed")
        candidate = {
            key: patch[key] if key in patch else prior[key]
            for key in (
                "canonical_name",
                "place_type",
                "names",
                "parent_ids",
                "source_ids",
                "generation",
                "valid_from_ms",
                "valid_to_ms",
                "producer",
                "policy",
                "provenance",
            )
        }
        candidate["observed_at_ms"] = self.now()
        stable = {key: candidate[key] for key in candidate}
        input_hash = _digest(stable)
        revision_id = "place-revision:" + input_hash[:24]
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO geospatial_place_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    place_id,
                    namespace,
                    prior["revision"] + 1,
                    prior["revision_id"],
                    candidate["canonical_name"],
                    candidate["place_type"],
                    _canonical(candidate["names"]),
                    _canonical(sorted(set(candidate["parent_ids"]))),
                    _canonical(dict(sorted(candidate["source_ids"].items()))),
                    candidate["generation"],
                    candidate["valid_from_ms"],
                    candidate["valid_to_ms"],
                    candidate["observed_at_ms"],
                    _canonical(candidate["producer"]),
                    _canonical(candidate["policy"]),
                    _canonical(candidate["provenance"]),
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO geospatial_place_current VALUES (?,?,?)",
                [place_id, revision_id, prior["revision"] + 1],
            )
            self._audit(
                namespace,
                "revise-place",
                revision_id,
                principal_id,
                {"reason": reason},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.place(namespace, place_id, scopes={READ_SCOPE})

    def import_projected_geometry(self, namespace, geometry, *, source_crs, **kwargs):
        """Transform an external geometry offline and retain its source coordinates."""
        _require(kwargs.get("scopes", set()), WRITE_SCOPE)
        from src.integrations.spatial import transform_geometry
        transformed = transform_geometry(geometry, source_crs)
        source = dict(kwargs.pop("source", {}))
        source["coordinate_transform"] = transformed
        kwargs.pop("crs", None)
        return self.store_geometry(namespace, transformed["result"]["geometry"],
                                   crs="EPSG:4326", source=source, **kwargs)

    def store_geometry(
        self,
        namespace: str,
        geometry: Mapping[str, Any],
        *,
        place_id: str | None,
        crs: str,
        precision_m: float,
        simplified_from: str | None,
        disputed: bool,
        admin_hierarchy: Sequence[str],
        source: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if crs != "EPSG:4326":
            raise GeospatialError(
                "unsupported_crs", "only explicit EPSG:4326 is supported offline"
            )
        if not math.isfinite(float(precision_m)) or float(precision_m) < 0:
            raise GeospatialError(
                "invalid_precision", "precision must be a finite non-negative distance"
            )
        if place_id and not self.place(namespace, place_id, scopes={READ_SCOPE}):
            raise GeospatialError(
                "not_found", "geometry place does not exist in namespace"
            )
        context = {
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else self.now()
            ),
            "producer": dict(
                producer or {"name": "noesis-geospatial", "version": "1.0.0"}
            ),
            "policy": dict(policy or {"crs": "explicit-v1"}),
        }
        self.conn.execute("BEGIN")
        try:
            result = self._store_geometry(
                namespace,
                geometry,
                place_id=place_id,
                crs=crs,
                precision_m=precision_m,
                simplified_from=simplified_from,
                disputed=disputed,
                admin_hierarchy=admin_hierarchy,
                source=source,
                evidence=evidence,
                principal_id=principal_id,
                context=context,
            )
            if not result.get("idempotent"):
                self._audit(
                    namespace,
                    "store-geometry",
                    result["geometry_id"],
                    principal_id,
                    {},
                    result["created_at_ms"],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def _store_geometry(
        self,
        namespace,
        geometry,
        *,
        place_id,
        crs,
        precision_m,
        simplified_from,
        disputed,
        admin_hierarchy,
        source,
        evidence,
        principal_id,
        context,
    ):
        kind, coordinates = _validate_geometry(geometry)
        stable = {
            "namespace": namespace,
            "place_id": place_id,
            "geometry": {"type": kind, "coordinates": coordinates},
            "crs": crs,
            "precision_m": float(precision_m),
            "simplified_from": simplified_from,
            "disputed": bool(disputed),
            "admin_hierarchy": list(admin_hierarchy),
            "source": dict(source),
            "evidence": [dict(item) for item in evidence],
            **context,
        }
        content_hash = _digest(
            {key: value for key, value in stable.items() if key != "observed_at_ms"}
        )
        geometry_id = "geometry:" + content_hash[:24]
        existing = self.conn.execute(
            "SELECT created_at_ms FROM geospatial_geometries WHERE geometry_id=?",
            [geometry_id],
        ).fetchone()
        now = int(existing[0]) if existing else self.now()
        if existing:
            return {
                **self.geometry(namespace, geometry_id, scopes={READ_SCOPE}),
                "idempotent": True,
            }
        self.conn.execute(
            "INSERT INTO geospatial_geometries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                geometry_id,
                namespace,
                place_id,
                kind,
                _canonical(coordinates),
                crs,
                float(precision_m),
                simplified_from,
                bool(disputed),
                _canonical(list(admin_hierarchy)),
                _canonical(dict(source)),
                _canonical([dict(item) for item in evidence]),
                context["generation"],
                context["valid_from_ms"],
                context["valid_to_ms"],
                context["observed_at_ms"],
                _canonical(context["producer"]),
                _canonical(context["policy"]),
                principal_id,
                content_hash,
                now,
            ],
        )
        return {
            "contract": GEOMETRY_CONTRACT,
            "geometry_id": geometry_id,
            **stable,
            "content_hash": content_hash,
            "principal_id": principal_id,
            "created_at_ms": now,
            "idempotent": False,
        }

    def geometry(
        self, namespace: str, geometry_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT place_id,geometry_type,coordinates_json,crs,precision_m,simplified_from,disputed,admin_hierarchy_json,source_json,evidence_json,generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_json,principal_id,content_hash,created_at_ms FROM geospatial_geometries WHERE namespace=? AND geometry_id=?",
            [namespace, geometry_id],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": GEOMETRY_CONTRACT,
            "geometry_id": geometry_id,
            "namespace": namespace,
            "place_id": row[0],
            "geometry": {"type": row[1], "coordinates": _load(row[2], [])},
            "crs": row[3],
            "precision_m": float(row[4]),
            "simplified_from": row[5],
            "disputed": bool(row[6]),
            "admin_hierarchy": _load(row[7], []),
            "source": _load(row[8], {}),
            "evidence": _load(row[9], []),
            "generation": int(row[10]),
            "valid_from_ms": row[11],
            "valid_to_ms": row[12],
            "observed_at_ms": int(row[13]),
            "producer": _load(row[14], {}),
            "policy": _load(row[15], {}),
            "principal_id": row[16],
            "content_hash": row[17],
            "created_at_ms": int(row[18]),
        }

    def geometries(
        self,
        namespace: str,
        place_id: str,
        *,
        scopes: set[str],
        as_of_ms: int | None = None,
        include_disputed: bool = True,
    ) -> list[dict[str, Any]]:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT geometry_id FROM geospatial_geometries WHERE namespace=? AND place_id=? AND (? IS NULL OR ((valid_from_ms IS NULL OR valid_from_ms<=?) AND (valid_to_ms IS NULL OR valid_to_ms>?))) AND (? OR NOT disputed) ORDER BY observed_at_ms DESC,geometry_id",
            [namespace, place_id, as_of_ms, as_of_ms, as_of_ms, include_disputed],
        ).fetchall()
        return [self.geometry(namespace, row[0], scopes=scopes) for row in rows]

    def simplify(
        self,
        namespace: str,
        geometry_id: str,
        tolerance_m: float,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        original = self.geometry(namespace, geometry_id, scopes={READ_SCOPE})
        if not original:
            raise GeospatialError("not_found", "geometry does not exist")
        kind, coordinates = _validate_geometry(original["geometry"])
        if kind == "Point":
            reduced = coordinates
        else:

            def reduce_line(line):
                kept = [line[0]]
                for point in line[1:-1]:
                    if _distance_m(kept[-1], point) >= tolerance_m:
                        kept.append(point)
                kept.append(line[-1])
                return kept

            reduced = (
                reduce_line(coordinates)
                if kind == "LineString"
                else [reduce_line(ring) for ring in coordinates]
            )
            if kind == "Polygon" and any(len(ring) < 4 for ring in reduced):
                raise GeospatialError(
                    "invalid_simplification", "tolerance collapses a polygon ring"
                )
        return self.store_geometry(
            namespace,
            {"type": kind, "coordinates": reduced},
            place_id=original["place_id"],
            crs=original["crs"],
            precision_m=max(original["precision_m"], float(tolerance_m)),
            simplified_from=geometry_id,
            disputed=original["disputed"],
            admin_hierarchy=original["admin_hierarchy"],
            source=original["source"],
            evidence=original["evidence"],
            principal_id=principal_id,
            scopes=scopes,
            generation=original["generation"],
            valid_from_ms=original["valid_from_ms"],
            valid_to_ms=original["valid_to_ms"],
            producer=original["producer"],
            policy=original["policy"],
        )

    def resolve(
        self,
        namespace: str,
        mention: str,
        *,
        scopes: set[str],
        context: Mapping[str, Any] | None = None,
        coordinate_hint: Sequence[Any] | None = None,
        as_of_ms: int | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        if not mention.strip():
            raise GeospatialError("invalid_mention", "location mention is required")
        rows = self.conn.execute(
            "SELECT DISTINCT place_id,namespace FROM geospatial_place_revisions WHERE namespace IN (?, 'global') ORDER BY place_id",
            [namespace],
        ).fetchall()
        folded = _fold(mention)
        candidates = []
        for place_id, place_namespace in rows:
            place = self.place(place_namespace, place_id, scopes={READ_SCOPE})
            matched = []
            for name in place["names"]:
                value = str(name.get("value") or "")
                active = as_of_ms is None or (
                    (
                        name.get("valid_from_ms") is None
                        or name["valid_from_ms"] <= as_of_ms
                    )
                    and (
                        name.get("valid_to_ms") is None
                        or name["valid_to_ms"] > as_of_ms
                    )
                )
                if active and (
                    _fold(value) == folded
                    or folded in _fold(value)
                    or _fold(value) in folded
                ):
                    matched.append(name)
            if not matched:
                continue
            exact = any(_fold(str(item["value"])) == folded for item in matched)
            confidence = 0.9 if exact else 0.65
            reasons = ["exact-name" if exact else "partial-name"]
            distance = None
            if coordinate_hint:
                geometries = self.geometries(
                    place_namespace,
                    place_id,
                    scopes={READ_SCOPE},
                    include_disputed=True,
                )
                points = [
                    item for item in geometries if item["geometry"]["type"] == "Point"
                ]
                if points:
                    distance = min(
                        _distance_m(coordinate_hint, item["geometry"]["coordinates"])
                        for item in points
                    )
                    confidence *= 1 / (1 + distance / 100_000)
                    reasons.append("coordinate-hint")
            candidates.append(
                {
                    "place_id": place_id,
                    "namespace": place_namespace,
                    "canonical_name": place["canonical_name"],
                    "matched_names": matched,
                    "confidence": round(confidence, 6),
                    "distance_m": None if distance is None else round(distance, 3),
                    "reasons": reasons,
                }
            )
        candidates.sort(key=lambda item: (-item["confidence"], item["place_id"]))
        candidates = candidates[: min(max(limit, 1), 50)]
        request = {
            "namespace": namespace,
            "mention": mention,
            "context": dict(context or {}),
            "coordinate_hint": list(coordinate_hint) if coordinate_hint else None,
            "as_of_ms": as_of_ms,
            "limit": limit,
        }
        input_hash = _digest(request)
        resolution_id = "geocode-resolution:" + input_hash[:24]
        selected = (
            candidates[0]["place_id"]
            if len(candidates) == 1 and candidates[0]["confidence"] >= 0.85
            else None
        )
        status = "resolved" if selected else "ambiguous" if candidates else "unresolved"
        result = {
            "contract": RESOLUTION_CONTRACT,
            "resolution_id": resolution_id,
            "namespace": namespace,
            "mention": mention,
            "context": dict(context or {}),
            "candidates": candidates,
            "status": status,
            "selected_place_id": selected,
            "confidence": candidates[0]["confidence"] if candidates else 0.0,
            "evidence": [],
            "method": {
                "name": "offline-gazetteer",
                "version": "1",
                "transliteration": "NFKD",
            },
            "input_hash": input_hash,
        }
        return result

    def save_resolution(
        self, result: Mapping[str, Any], *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        now = self.now()
        existing = self.conn.execute(
            "SELECT created_at_ms FROM geocode_resolutions WHERE resolution_id=?",
            [result["resolution_id"]],
        ).fetchone()
        if not existing:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "INSERT INTO geocode_resolutions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        result["resolution_id"],
                        result["namespace"],
                        result["mention"],
                        _canonical(result["context"]),
                        _canonical(result["candidates"]),
                        result["status"],
                        result["selected_place_id"],
                        result["confidence"],
                        _canonical(result["evidence"]),
                        _canonical(result["method"]),
                        result["input_hash"],
                        principal_id,
                        now,
                    ],
                )
                self._audit(
                    result["namespace"],
                    "save-resolution",
                    result["resolution_id"],
                    principal_id,
                    {},
                    now,
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {
            **dict(result),
            "principal_id": principal_id,
            "created_at_ms": int(existing[0]) if existing else now,
            "idempotent": bool(existing),
        }

    def review(
        self,
        namespace: str,
        resolution_id: str,
        decision: str,
        *,
        selected_place_id: str | None,
        reason: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, REVIEW_SCOPE)
        row = self.conn.execute(
            "SELECT candidates_json FROM geocode_resolutions WHERE namespace=? AND resolution_id=?",
            [namespace, resolution_id],
        ).fetchone()
        if not row or decision not in {"accept", "reject", "defer"}:
            raise GeospatialError(
                "invalid_review", "saved resolution and supported decision are required"
            )
        candidates = {item["place_id"] for item in _load(row[0], [])}
        if decision == "accept" and selected_place_id not in candidates:
            raise GeospatialError(
                "invalid_review", "accepted place must be a retained candidate"
            )
        prior = self.conn.execute(
            "SELECT review_id,revision,decision,selected_place_id,reason,predecessor_review_id,"
            "principal_id,created_at_ms FROM geocode_reviews WHERE resolution_id=? "
            "ORDER BY revision DESC LIMIT 1",
            [resolution_id],
        ).fetchone()
        if prior and (prior[2], prior[3], prior[4]) == (
            decision,
            selected_place_id,
            reason,
        ):
            return {
                "review_id": prior[0],
                "resolution_id": resolution_id,
                "revision": int(prior[1]),
                "decision": prior[2],
                "selected_place_id": prior[3],
                "reason": prior[4],
                "predecessor_review_id": prior[5],
                "principal_id": prior[6],
                "created_at_ms": int(prior[7]),
                "idempotent": True,
            }
        revision = int(prior[1]) + 1 if prior else 1
        stable = [
            resolution_id,
            revision,
            decision,
            selected_place_id,
            reason,
            prior[0] if prior else None,
        ]
        review_id = "geocode-review:" + _digest(stable)[:24]
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO geocode_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    review_id,
                    namespace,
                    resolution_id,
                    revision,
                    decision,
                    selected_place_id,
                    reason,
                    prior[0] if prior else None,
                    principal_id,
                    now,
                ],
            )
            self._audit(
                namespace,
                "review-resolution",
                review_id,
                principal_id,
                {"decision": decision},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "review_id": review_id,
            "resolution_id": resolution_id,
            "revision": revision,
            "decision": decision,
            "selected_place_id": selected_place_id,
            "reason": reason,
            "predecessor_review_id": prior[0] if prior else None,
            "principal_id": principal_id,
            "created_at_ms": now,
        }

    def relation(
        self,
        namespace: str,
        operation: str,
        left_geometry_id: str,
        right: Any,
        *,
        scopes: set[str],
        principal_id: str,
        tolerance_m: float = 0,
        backend: str = "stdlib",
    ) -> dict[str, Any]:
        _require(scopes, CALCULATE_SCOPE)
        left = self.geometry(namespace, left_geometry_id, scopes={READ_SCOPE})
        if not left or left["crs"] != "EPSG:4326":
            raise GeospatialError("not_found", "left WGS84 geometry does not exist")
        if backend not in {"stdlib", "shapely"}:
            raise GeospatialError("invalid_backend", "unknown geometry backend")
        if backend == "shapely":
            if tolerance_m != 0 or operation not in {"contains", "intersects", "covers"}:
                raise GeospatialError("unsupported_operation", "Shapely adapter supports exact topology only")
            from src.integrations.spatial import topology
            if operation in {"contains", "covers"}:
                other_geometry = {"type": "Point", "coordinates": list(_point(right))}
                input_ids = [left_geometry_id]
            else:
                other = self.geometry(namespace, str(right), scopes={READ_SCOPE})
                if not other:
                    raise GeospatialError("not_found", "right geometry does not exist")
                other_geometry = other["geometry"]
                input_ids = [left_geometry_id, str(right)]
            evaluated = topology(operation, left["geometry"], other_geometry)
            return self._receipt(namespace, operation,
                {"operation": operation, "left_geometry_id": left_geometry_id, "right": right,
                 "tolerance_m": 0, "crs": "EPSG:4326", "backend": evaluated["producer"]},
                evaluated["result"], input_ids, principal_id,
                algorithm="shapely-" + evaluated["producer"]["version"])
        if operation in {"contains", "proximity"}:
            point = list(_point(right))
            if operation == "contains":
                value = _contains(left["geometry"], point, tolerance_m)
                result = {"contains": value}
            else:
                distance = min(
                    _distance_m(item, point) for item in _points(left["geometry"])
                )
                result = {
                    "distance_m": round(distance, 6),
                    "within_tolerance": distance <= tolerance_m,
                }
            input_ids = [left_geometry_id]
        elif operation == "intersects":
            other = self.geometry(namespace, str(right), scopes={READ_SCOPE})
            if not other:
                raise GeospatialError("not_found", "right geometry does not exist")
            result = {
                "intersects": _intersects(
                    left["geometry"], other["geometry"], tolerance_m
                )
            }
            input_ids = [left_geometry_id, str(right)]
        elif operation == "route":
            points = _points(left["geometry"])
            result = {
                "length_m": round(
                    sum(
                        _distance_m(points[i], points[i + 1])
                        for i in range(len(points) - 1)
                    ),
                    6,
                ),
                "segments": max(0, len(points) - 1),
            }
            input_ids = [left_geometry_id]
        else:
            raise GeospatialError("invalid_operation", "unsupported spatial operation")
        request = {
            "operation": operation,
            "left_geometry_id": left_geometry_id,
            "right": right,
            "tolerance_m": float(tolerance_m),
            "crs": "EPSG:4326",
        }
        return self._receipt(
            namespace, operation, request, result, input_ids, principal_id
        )

    def _receipt(self, namespace, operation, request, result, input_ids, principal_id, algorithm="wgs84-stdlib-v1"):
        stable = [
            namespace,
            operation,
            request,
            result,
            sorted(input_ids),
            algorithm,
        ]
        calculation_hash = _digest(stable)
        receipt_id = "spatial-receipt:" + calculation_hash[:24]
        existing = self.conn.execute(
            "SELECT principal_id,created_at_ms FROM spatial_receipts WHERE receipt_id=?",
            [receipt_id],
        ).fetchone()
        now = int(existing[1]) if existing else self.now()
        if not existing:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "INSERT INTO spatial_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        receipt_id,
                        namespace,
                        operation,
                        _canonical(request),
                        _canonical(result),
                        _canonical(sorted(input_ids)),
                        algorithm,
                        calculation_hash,
                        principal_id,
                        now,
                    ],
                )
                self._audit(
                    namespace,
                    "spatial-calculate",
                    receipt_id,
                    principal_id,
                    {"operation": operation},
                    now,
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {
            "contract": SPATIAL_CONTRACT,
            "receipt_id": receipt_id,
            "namespace": namespace,
            "operation": operation,
            "request": request,
            "result": result,
            "input_ids": sorted(input_ids),
            "algorithm": algorithm,
            "calculation_hash": calculation_hash,
            "principal_id": existing[0] if existing else principal_id,
            "created_at_ms": now,
            "idempotent": bool(existing),
        }

    def replay(
        self, namespace: str, receipt_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT operation,request_json,result_json,input_ids_json,algorithm,calculation_hash FROM spatial_receipts WHERE namespace=? AND receipt_id=?",
            [namespace, receipt_id],
        ).fetchone()
        if not row:
            raise GeospatialError("not_found", "spatial receipt does not exist")
        replayed = _digest(
            [
                namespace,
                row[0],
                _load(row[1], {}),
                _load(row[2], {}),
                _load(row[3], []),
                row[4],
            ]
        )
        return {
            "receipt_id": receipt_id,
            "stored_hash": row[5],
            "replayed_hash": replayed,
            "deterministic": replayed == row[5],
            "input_ids": _load(row[3], []),
        }

    def search(
        self,
        namespace: str,
        *,
        scopes: set[str],
        bbox: Sequence[Any] | None = None,
        center: Sequence[Any] | None = None,
        radius_m: float | None = None,
        contains_point: Sequence[Any] | None = None,
        as_of_ms: int | None = None,
        include_disputed: bool = True,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        filters = {
            "namespace": namespace,
            "bbox": list(bbox) if bbox else None,
            "center": list(center) if center else None,
            "radius_m": radius_m,
            "contains_point": list(contains_point) if contains_point else None,
            "as_of_ms": as_of_ms,
            "include_disputed": include_disputed,
        }
        offset = 0
        if cursor:
            decoded = _uncursor(cursor)
            if decoded.get("filters_hash") != _digest(filters):
                raise GeospatialError(
                    "cursor_mismatch", "cursor belongs to different filters"
                )
            offset = int(decoded.get("offset", 0))
        if bbox:
            if len(bbox) != 4:
                raise GeospatialError(
                    "invalid_bbox", "bbox needs west,south,east,north"
                )
            west, south = _point([bbox[0], bbox[1]])
            east, north = _point([bbox[2], bbox[3]])
            if south > north:
                raise GeospatialError("invalid_bbox", "bbox south exceeds north")
        else:
            west = south = east = north = None
        if center:
            center = list(_point(center))
            if radius_m is None or radius_m < 0:
                raise GeospatialError(
                    "invalid_radius", "center requires a non-negative radius"
                )
        rows = self.conn.execute(
            "SELECT geometry_id FROM geospatial_geometries WHERE namespace IN (?, 'global') AND (? IS NULL OR ((valid_from_ms IS NULL OR valid_from_ms<=?) AND (valid_to_ms IS NULL OR valid_to_ms>?))) AND (? OR NOT disputed) ORDER BY geometry_id",
            [namespace, as_of_ms, as_of_ms, as_of_ms, include_disputed],
        ).fetchall()
        items = []
        for (geometry_id,) in rows:
            row = self.conn.execute(
                "SELECT namespace FROM geospatial_geometries WHERE geometry_id=?",
                [geometry_id],
            ).fetchone()
            geometry = self.geometry(row[0], geometry_id, scopes={READ_SCOPE})
            points = _points(geometry["geometry"])
            if bbox:

                def lon_ok(lon):
                    return (
                        west <= lon <= east
                        if west <= east
                        else lon >= west or lon <= east
                    )

                if not any(
                    lon_ok(point[0]) and south <= point[1] <= north for point in points
                ):
                    continue
            if (
                center
                and min(_distance_m(center, point) for point in points) > radius_m
            ):
                continue
            if contains_point and not _contains(
                geometry["geometry"], contains_point, geometry["precision_m"]
            ):
                continue
            items.append(geometry)
        page_limit = min(max(int(limit), 1), 200)
        page = items[offset : offset + page_limit]
        next_cursor = (
            _cursor({"filters_hash": _digest(filters), "offset": offset + page_limit})
            if offset + page_limit < len(items)
            else None
        )
        return {
            "contract": SPATIAL_CONTRACT,
            "operation": "search",
            "items": page,
            "count": len(page),
            "next_cursor": next_cursor,
            "filters_hash": _digest(filters),
        }

    def event_map(
        self,
        namespace: str,
        *,
        scopes: set[str],
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        exists = self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='event_model_revisions'"
        ).fetchone()
        if not exists:
            return {
                "contract": SPATIAL_CONTRACT,
                "operation": "event-map",
                "items": [],
                "count": 0,
            }
        rows = self.conn.execute(
            "SELECT event_id,attributes_json FROM event_model_revisions WHERE namespace=? "
            "QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY revision DESC)=1 "
            "ORDER BY event_id LIMIT ?",
            [namespace, min(max(limit, 1), 500)],
        ).fetchall()
        items = []
        for event_id, raw in rows:
            attributes = _load(raw, {})
            location = attributes.get("location")
            interval = attributes.get("time") or {}
            event_start, event_end = interval.get("start_ms"), interval.get("end_ms")
            if start_ms is not None and event_end is not None and event_end < start_ms:
                continue
            if end_ms is not None and event_start is not None and event_start > end_ms:
                continue
            if isinstance(location, dict) and (
                location.get("coordinates") or location.get("place_id")
            ):
                items.append(
                    {
                        "event_id": event_id,
                        "location": location,
                        "start_ms": event_start,
                        "end_ms": event_end,
                    }
                )
        return {
            "contract": SPATIAL_CONTRACT,
            "operation": "event-map",
            "items": items,
            "count": len(items),
        }
