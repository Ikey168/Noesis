"""Offline coordinate transforms and topology operations with version receipts."""

import math

from .common import IntegrationError, digest, receipt


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


STDLIB_UTM_PRODUCER = {"backend": "noesis-stdlib-utm", "version": "1.0.0"}
_GRS80 = (6_378_137.0, 1 / 298.257222101)
_WGS84 = (6_378_137.0, 1 / 298.257223563)
_CRS_ALIASES = {"CRS84": "EPSG:4326", "OGC:CRS84": "EPSG:4326"}


def _epsg_code(crs):
    """Return an EPSG integer from ``EPSG:n`` or an OGC URN, else ``None``."""

    text = _CRS_ALIASES.get(str(crs).strip(), str(crs).strip())
    for prefix in ("EPSG:", "urn:ogc:def:crs:EPSG::", "urn:ogc:def:crs:EPSG:"):
        if text.startswith(prefix):
            tail = text[len(prefix):].rsplit(":", 1)[-1]
            return int(tail) if tail.isdigit() else None
    return None


def _utm_definition(code):
    """ETRS89/UTM (GRS80) and WGS 84/UTM zones the stdlib fallback supports."""

    if code is not None and 25828 <= code <= 25838:
        return {"zone": code - 25800, "south": False, "ellipsoid": _GRS80,
                "datum": "ETRS89"}
    if code is not None and (32601 <= code <= 32660 or 32701 <= code <= 32760):
        return {"zone": code % 100, "south": code >= 32701, "ellipsoid": _WGS84,
                "datum": "WGS 84"}
    return None


def _utm_inverse(easting, northing, definition):
    """Karney (2011) sixth-order Krüger series inverse transverse Mercator."""

    a, f = definition["ellipsoid"]
    n = f / (2 - f)
    n2, n3, n4, n5, n6 = n**2, n**3, n**4, n**5, n**6
    big_a = a / (1 + n) * (1 + n2 / 4 + n4 / 64 + n6 / 256)
    beta = (
        n / 2 - 2 * n2 / 3 + 37 * n3 / 96 - n4 / 360 - 81 * n5 / 512
        + 96199 * n6 / 604800,
        n2 / 48 + n3 / 15 - 437 * n4 / 1440 + 46 * n5 / 105 - 1118711 * n6 / 3870720,
        17 * n3 / 480 - 37 * n4 / 840 - 209 * n5 / 4480 + 5569 * n6 / 90720,
        4397 * n4 / 161280 - 11 * n5 / 504 - 830251 * n6 / 7257600,
        4583 * n5 / 161280 - 108847 * n6 / 3991680,
        20648693 * n6 / 638668800,
    )
    k0 = 0.9996
    xi = (northing - (10_000_000.0 if definition["south"] else 0.0)) / (k0 * big_a)
    eta = (easting - 500_000.0) / (k0 * big_a)
    xi_p, eta_p = xi, eta
    for j, coefficient in enumerate(beta, start=1):
        xi_p -= coefficient * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        eta_p -= coefficient * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    tau_p = math.sin(xi_p) / math.sqrt(math.sinh(eta_p) ** 2 + math.cos(xi_p) ** 2)
    e2 = f * (2 - f)
    e = math.sqrt(e2)
    tau = tau_p
    for _ in range(8):
        sigma = math.sinh(e * math.atanh(e * tau / math.sqrt(1 + tau * tau)))
        tau_i = tau * math.sqrt(1 + sigma * sigma) - sigma * math.sqrt(1 + tau * tau)
        step = ((tau_p - tau_i) / math.sqrt(1 + tau_i * tau_i)
                * (1 + (1 - e2) * tau * tau) / ((1 - e2) * math.sqrt(1 + tau * tau)))
        tau += step
        if abs(step) < 1e-14:
            break
    central = definition["zone"] * 6 - 183
    longitude = central + math.degrees(math.atan2(math.sinh(eta_p), math.cos(xi_p)))
    return [longitude, math.degrees(math.atan(tau))]


def _stdlib_transform(geometry, source_crs, target_crs):
    """Deterministic UTM→geographic fallback used only when pyproj is absent."""

    if _epsg_code(target_crs) != 4326:
        raise IntegrationError(
            "transform_unavailable",
            "Install the optional pyproj dependency for this target CRS",
        )
    code = _epsg_code(source_crs)
    definition = _utm_definition(code)
    if code == 4326:
        definition = None
    elif definition is None:
        raise IntegrationError(
            "transform_unavailable",
            "Install the optional pyproj dependency for this source CRS",
        )
    count = 0

    def convert(coords, depth=0):
        nonlocal count
        if depth > 3 or not isinstance(coords, (list, tuple)) or not coords:
            raise IntegrationError(
                "invalid_geometry", "Coordinates must be nonempty arrays"
            )
        if isinstance(coords[0], (float, int)):
            count += 1
            if count > 100_000 or len(coords) != 2:
                raise IntegrationError(
                    "invalid_geometry",
                    "Only bounded finite 2D coordinates are supported",
                )
            x, y = float(coords[0]), float(coords[1])
            if definition is None:
                if not (-180 <= x <= 180 and -90 <= y <= 90):
                    raise IntegrationError(
                        "invalid_geometry",
                        "Geographic coordinates require longitude/latitude bounds",
                    )
                return [x, y]
            if not (0 < x < 1_000_000 and 0 <= y <= 10_000_000):
                raise IntegrationError(
                    "transform_failed", "Coordinate lies outside the UTM grid"
                )
            lon, lat = _utm_inverse(x, y, definition)
            if not (math.isfinite(lon) and math.isfinite(lat)):
                raise IntegrationError(
                    "transform_failed", "Nonfinite transformed coordinates"
                )
            return [round(lon, 10), round(lat, 10)]
        return [convert(c, depth + 1) for c in coords]

    result = {
        "geometry": {"type": geometry["type"], "coordinates": convert(geometry["coordinates"])},
        "source_crs": f"EPSG:{code}",
        "target_crs": "EPSG:4326",
        "axis_order": "x,y",
        "pipeline": (
            "identity" if definition is None else
            f"inverse transverse Mercator, UTM zone {definition['zone']}"
            f"{'S' if definition['south'] else 'N'}, Kruger series order 6"
        ),
        # ETRS89 to WGS 84 is the EPSG:1149 null transformation (1 m accuracy).
        "accuracy_m": 0.0 if definition is None or definition["datum"] == "WGS 84" else 1.0,
        "datum_transformation": (
            None if definition is None or definition["datum"] == "WGS 84"
            else "EPSG:1149 ETRS89 to WGS 84 null transformation"
        ),
        "network_enabled": False,
        "grids": [],
    }
    core = {
        "producer": dict(STDLIB_UTM_PRODUCER),
        "request": {"geometry": geometry, "source_crs": source_crs, "target_crs": target_crs},
        "result": result,
    }
    return {**core, "sha256": digest(core)}


def transform_capability():
    """Report which coordinate-transform backend is available offline."""

    try:
        import pyproj  # noqa: F401
    except ImportError:
        return {
            "backend": STDLIB_UTM_PRODUCER["backend"],
            "pyproj_available": False,
            "supported_sources": ["EPSG:4326", "EPSG:25828-25838", "EPSG:32601-32660",
                                  "EPSG:32701-32760"],
            "action": "Install the optional pyproj dependency for other CRSs",
        }
    return {"backend": "pyproj", "pyproj_available": True, "supported_sources": ["*"],
            "action": None}


def transform_geometry(geometry, source_crs, target_crs="EPSG:4326"):
    _validate_coordinates(geometry)
    try:
        import pyproj
    except ImportError:
        return _stdlib_transform(geometry, source_crs, target_crs)
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
