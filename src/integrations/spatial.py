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
    kind = geometry["type"]
    if kind == "LineString" and len(geometry["coordinates"]) < 2:
        raise IntegrationError("invalid_geometry", "Line needs at least two points")
    polygons = (
        [geometry["coordinates"]]
        if kind == "Polygon"
        else geometry["coordinates"]
        if kind == "MultiPolygon"
        else []
    )
    for polygon in polygons:
        for ring in polygon:
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise IntegrationError(
                    "invalid_geometry", "Polygon rings must be explicitly closed"
                )


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
            if source.is_geographic and not (
                -180 <= coords[0] <= 180 and -90 <= coords[1] <= 90
            ):
                raise IntegrationError(
                    "invalid_geometry",
                    "Geographic coordinates require longitude/latitude bounds",
                )
            try:
                x, y = transformer.transform(*coords, errcheck=True)
            except pyproj.exceptions.ProjError as exc:
                raise IntegrationError(
                    "transform_failed", "Coordinate transformation failed"
                ) from exc
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
        "proj_database_date": pyproj.database.get_database_metadata("EPSG.DATE"),
        "network_enabled": False,
        "grids": [
            {
                "short_name": grid.short_name,
                "available": grid.available,
                "url": grid.url,
            }
            for operation in transformer.operations
            for grid in operation.grids
        ],
    }
    return receipt(
        "pyproj",
        "pyproj",
        {"geometry": geometry, "source_crs": source_crs, "target_crs": target_crs},
        result,
    )


def topology(operation, left, right=None, *, crs="EPSG:4326"):
    import shapely
    from shapely.geometry import shape
    from shapely.validation import explain_validity

    for geometry in (left, right):
        if geometry is not None:
            _validate_coordinates(geometry)
    shapes = [shape(g) for g in (left, right) if g is not None]
    for g in shapes:
        if g.is_empty or not g.is_valid:
            raise IntegrationError("invalid_geometry", explain_validity(g))
        # Geographic dateline wrapping needs a separate normalization policy.
        if crs != "EPSG:4326":
            raise IntegrationError(
                "unsupported_crs",
                "Topology adapter requires explicit WGS84 coordinates",
            )
        if not (
            -180 <= g.bounds[0] <= g.bounds[2] <= 180
            and -90 <= g.bounds[1] <= g.bounds[3] <= 90
        ):
            raise IntegrationError(
                "invalid_geometry", "WGS84 coordinates are out of range"
            )
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
        {
            "operation": operation,
            "left": left,
            "right": right,
            "crs": crs,
            "repair_policy": "reject",
        },
        result,
    )


def simplify_geometry(geometry, tolerance_m, *, projected_crs):
    """Topology-preserving planar simplification in an explicit local metric CRS."""
    import json

    import pyproj
    import shapely
    from shapely.geometry import mapping, shape

    from .common import finite

    tolerance = finite(tolerance_m, "tolerance_m", 0, 10000)
    _validate_coordinates(geometry)
    original = shape(geometry)
    if shapely.get_num_coordinates(original) > 10000:
        raise IntegrationError(
            "input_limit", "Simplification supports at most 10000 coordinates"
        )
    if original.is_empty or not original.is_valid:
        raise IntegrationError(
            "invalid_geometry", "Invalid geometry; repair policy is reject"
        )
    if original.bounds[2] - original.bounds[0] > 180:
        raise IntegrationError(
            "unsupported_dateline", "Dateline normalization is not supported"
        )
    if not projected_crs:
        raise IntegrationError(
            "unsupported_crs", "Choose an explicit local projected metric CRS"
        )
    target = pyproj.CRS(projected_crs)
    if target.to_epsg() not in {25832, 25833}:
        raise IntegrationError(
            "unsupported_crs", "Supported metric scope is ETRS89/UTM zones 32N and 33N"
        )
    if (
        not target.is_projected
        or len(target.axis_info) != 2
        or any(axis.unit_conversion_factor != 1 for axis in target.axis_info)
    ):
        raise IntegrationError("unsupported_crs", "Projected metre axes are required")
    area = target.area_of_use
    xmin, ymin, xmax, ymax = original.bounds
    if area is None or not (
        area.west <= xmin <= xmax <= area.east
        and area.south <= ymin <= ymax <= area.north
    ):
        raise IntegrationError(
            "unsupported_crs", "Geometry lies outside declared projection area"
        )
    projected = transform_geometry(geometry, "EPSG:4326", target.to_string())
    source_shape = shape(projected["result"]["geometry"])
    effective_tolerance = tolerance
    for _ in range(8):
        reduced = shapely.simplify(
            source_shape, tolerance=effective_tolerance, preserve_topology=True
        )
        displacement = float(
            shapely.hausdorff_distance(source_shape, reduced, densify=0.25)
        )
        if displacement <= tolerance + 1e-9:
            break
        effective_tolerance /= 2
    else:
        raise IntegrationError(
            "invalid_simplification",
            "Simplification exceeds sampled displacement budget",
        )
    if reduced.is_empty or not reduced.is_valid:
        raise IntegrationError(
            "invalid_simplification", "Simplification lost valid topology"
        )
    reduced_geometry = json.loads(json.dumps(mapping(reduced)))
    restored = transform_geometry(reduced_geometry, target.to_string(), "EPSG:4326")
    accuracies = [run["result"]["accuracy_m"] for run in (projected, restored)]
    if any(value is None for value in accuracies):
        raise IntegrationError(
            "unknown_accuracy",
            "Transform accuracy is required for precision propagation",
        )
    return receipt(
        "shapely",
        "shapely",
        {
            "geometry": geometry,
            "source_crs": "EPSG:4326",
            "projected_crs": target.to_string(),
            "tolerance_m": tolerance,
            "repair_policy": "reject",
            "preserve_topology": True,
        },
        {
            "geometry": restored["result"]["geometry"],
            "geos_version": shapely.geos_version_string,
            "discrete_hausdorff_m": displacement,
            "hausdorff_densify": 0.25,
            "effective_tolerance_m": effective_tolerance,
            "transformation_accuracy_m": sum(accuracies),
            "forward_transform": projected,
            "inverse_transform": restored,
            "semantics": "Planar simplification tolerance in projected metres; not geodesic distance",
        },
    )
