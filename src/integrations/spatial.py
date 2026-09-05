"""Offline coordinate transforms and topology operations with version receipts."""

import math
from .common import IntegrationError, receipt


def _validate_coordinates(geometry):
    dimensions = {
        "Point": 0,
        "LineString": 1,
        "Polygon": 2,
        "MultiPoint": 1,
        "MultiLineString": 2,
        "MultiPolygon": 3,
    }
    if geometry.get("type") not in dimensions:
        raise IntegrationError("unsupported_geometry", "Unsupported geometry type")
    pending = [(geometry.get("coordinates"), dimensions[geometry["type"]])]
    points = 0
    while pending:
        coords, depth = pending.pop()
        if not isinstance(coords, (list, tuple)) or not coords:
            raise IntegrationError(
                "invalid_geometry", "Nonempty coordinate arrays required"
            )
        if depth == 0:
            points += 1
            if len(coords) != 2 or any(
                type(v) not in (float, int) or not math.isfinite(v) for v in coords
            ):
                raise IntegrationError(
                    "invalid_geometry", "Finite two-dimensional coordinates required"
                )
        else:
            if len(pending) + len(coords) > 100_000:
                raise IntegrationError(
                    "input_limit", "Geometry exceeds coordinate budget"
                )
            pending.extend((item, depth - 1) for item in coords)
        if points > 100_000:
            raise IntegrationError("input_limit", "Geometry exceeds coordinate budget")


def transform_geometry(geometry, source_crs, target_crs="EPSG:4326"):
    _validate_coordinates(geometry)
    import pyproj
    from pyproj.transformer import TransformerGroup

    if pyproj.network.is_network_enabled():
        raise IntegrationError(
            "network_enabled", "Disable PROJ network access for reproducible transforms"
        )
    source, target = pyproj.CRS(source_crs), pyproj.CRS(target_crs)
    group = TransformerGroup(source, target, always_xy=True, allow_ballpark=False)
    if not group.transformers or not group.best_available:
        raise IntegrationError(
            "transform_unavailable",
            "Required transformation or grid is unavailable locally",
        )
    transformer = group.transformers[0]
    count = 0

    def convert(coords, depth=0):
        nonlocal count
        if depth > 3 or not isinstance(coords, (list, tuple)) or not coords:
            raise IntegrationError(
                "invalid_geometry", "Coordinates must be nonempty arrays"
            )
        if isinstance(coords[0], (float, int)):
            count += 1
            if (
                count > 100_000
                or len(coords) != 2
                or not all(math.isfinite(float(v)) for v in coords)
            ):
                raise IntegrationError(
                    "invalid_geometry",
                    "Only bounded finite 2D coordinates are supported",
                )
            x, y = transformer.transform(*coords, errcheck=True)
            if not math.isfinite(x) or not math.isfinite(y):
                raise IntegrationError(
                    "transform_failed", "Nonfinite transformed coordinates"
                )
            return [x, y]
        return [convert(c, depth + 1) for c in coords]

    if geometry.get("type") not in {
        "Point",
        "LineString",
        "Polygon",
        "MultiPoint",
        "MultiLineString",
        "MultiPolygon",
    }:
        raise IntegrationError("unsupported_geometry", "Unsupported geometry type")
    result = {
        "geometry": {
            "type": geometry["type"],
            "coordinates": convert(geometry["coordinates"]),
        },
        "source_crs": source.to_string(),
        "target_crs": target.to_string(),
        "axis_order": "x,y",
        "pipeline": transformer.definition,
        "accuracy_m": transformer.accuracy if transformer.accuracy >= 0 else None,
        "area_of_use": str(transformer.area_of_use),
        "proj_version": pyproj.proj_version_str,
        "proj_database": pyproj.database.get_database_metadata("EPSG.VERSION"),
    }
    return receipt(
        "pyproj",
        "pyproj",
        {"geometry": geometry, "source_crs": source_crs, "target_crs": target_crs},
        result,
    )


def topology(operation, left, right=None):
    from shapely.geometry import shape
    from shapely.validation import explain_validity
    import shapely

    for geometry in (left, right):
        if geometry is not None:
            _validate_coordinates(geometry)
    shapes = [shape(g) for g in (left, right) if g is not None]
    for g in shapes:
        if g.is_empty or not g.is_valid:
            raise IntegrationError("invalid_geometry", explain_validity(g))
        # Geographic dateline wrapping needs a separate normalization policy.
        if g.bounds[2] - g.bounds[0] > 180:
            raise IntegrationError(
                "unsupported_dateline",
                "Unwrap dateline geometry explicitly before topology operations",
            )
    if len(shapes) != 2 or operation not in {"contains", "covers", "intersects"}:
        raise IntegrationError(
            "unsupported_operation",
            "Supported topology operations: contains, covers, intersects",
        )
    result = {
        operation: bool(getattr(shapes[0], operation)(shapes[1])),
        "geos_version": shapely.geos_version_string,
        "semantics": "planar topology in supplied coordinates; no metric distances",
    }
    return receipt(
        "shapely",
        "shapely",
        {"operation": operation, "left": left, "right": right},
        result,
    )
