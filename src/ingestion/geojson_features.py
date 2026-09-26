"""Bounded GeoJSON FeatureCollection decoding for geospatial source packs.

The decoder turns one native FeatureCollection into runtime records without
losing source meaning.  Every feature keeps its provider, collection, native
identifier, properties, and source-CRS geometry.  Features that cannot keep a
stable identity or a supported geometry become explicit *rejection* records,
which the source-pack runtime quarantines instead of silently dropping them.

Budgets are enforced before any geometry is normalized: bytes are checked on
the raw payload, nesting depth on the parsed structure, and feature and
coordinate counts while walking the collection.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.ingestion.source_packs import SourcePackError

FEATURE_RECORD_CONTRACT = "noesis-geospatial-feature-record-v1"
TARGET_SCHEMA = "noesis-geospatial-feature-v1"
SUPPORTED_GEOMETRIES = ("Point", "LineString", "Polygon", "MultiPolygon")
AXIS_ORDERS = ("east_north", "north_east")
IMPORT_ROOT_ENV = "NOESIS_GEOSPATIAL_IMPORT_ROOT"
_GEOMETRY_DEPTH = {"Point": 0, "LineString": 1, "Polygon": 2, "MultiPolygon": 3}
_KNOWN_GEOMETRIES = frozenset(
    {*SUPPORTED_GEOMETRIES, "MultiPoint", "MultiLineString", "GeometryCollection"}
)


@dataclass(frozen=True)
class FeatureBudget:
    max_features: int = 10_000
    max_bytes: int = 20_000_000
    max_coordinates: int = 500_000
    max_depth: int = 32

    def __post_init__(self) -> None:
        for name, value, ceiling in (
            ("max_features", self.max_features, 100_000),
            ("max_bytes", self.max_bytes, 100_000_000),
            ("max_coordinates", self.max_coordinates, 5_000_000),
            ("max_depth", self.max_depth, 64),
        ):
            if not isinstance(value, int) or not 1 <= value <= ceiling:
                raise SourcePackError(
                    "unbounded_run", f"feature budget {name} is outside supported bounds"
                )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def feature_key(provider: str, collection: str, native_id: str) -> str:
    """Stable feature identity: provider + collection + native identifier."""

    return "geofeature:" + _digest([provider, collection, native_id])[:24]


def _depth(value: Any, limit: int) -> int:
    deepest, pending = 0, [(value, 1)]
    while pending:
        item, depth = pending.pop()
        deepest = max(deepest, depth)
        if deepest > limit:
            raise SourcePackError(
                "budget_exhausted", "feature collection exceeds its nesting budget"
            )
        if isinstance(item, Mapping):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return deepest


def _count_positions(coordinates: Any, depth: int) -> int:
    """Validate nesting for the declared geometry type and count positions."""

    if depth == 0:
        if (
            not isinstance(coordinates, list)
            or len(coordinates) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in coordinates
            )
        ):
            raise ValueError("positions must be finite two-dimensional numbers")
        return 1
    if not isinstance(coordinates, list) or not coordinates:
        raise ValueError("coordinate arrays must be non-empty")
    return sum(_count_positions(item, depth - 1) for item in coordinates)


def _swap_axes(coordinates: Any, depth: int) -> Any:
    if depth == 0:
        return [coordinates[1], coordinates[0]]
    return [_swap_axes(item, depth - 1) for item in coordinates]


def _rings_closed(kind: str, coordinates: Any) -> bool:
    polygons = (
        [coordinates] if kind == "Polygon" else coordinates if kind == "MultiPolygon" else []
    )
    return all(
        len(ring) >= 4 and ring[0] == ring[-1] for polygon in polygons for ring in polygon
    )


def _rejection(
    code: str,
    message: str,
    *,
    provider: str,
    collection: str,
    index: int,
    native_id: Any,
    feature: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "id": f"rejected:{collection}:{index}",
        "rejection": {
            "code": code,
            "message": message,
            "provider": provider,
            "collection": collection,
            "feature_index": index,
            "native_id": None if native_id is None else str(native_id),
        },
        "native_feature": dict(feature),
    }


def decode_feature_collection(
    raw: bytes | str | Mapping[str, Any],
    *,
    provider: str,
    collection: str,
    source_crs: str,
    axis_order: str = "east_north",
    id_property: str | None = None,
    title_property: str | None = None,
    budget: FeatureBudget | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Decode one FeatureCollection into runtime records plus explicit rejections.

    ``raw`` should be the exact response bytes so their hash can be preserved;
    a parsed mapping is accepted for local imports and is hashed canonically.
    """

    limits = budget or FeatureBudget()
    if axis_order not in AXIS_ORDERS:
        raise SourcePackError("invalid_mapping", "axis_order must be east_north or north_east")
    if not str(provider).strip() or not str(collection).strip() or not str(source_crs).strip():
        raise SourcePackError(
            "invalid_mapping", "provider, collection and source CRS must be explicit"
        )
    if isinstance(raw, Mapping):
        payload_bytes = _canonical(raw).encode()
        payload: Any = json.loads(payload_bytes)
    else:
        payload_bytes = raw.encode() if isinstance(raw, str) else bytes(raw)
        if len(payload_bytes) > limits.max_bytes:
            raise SourcePackError(
                "response_too_large", "feature collection exceeds its byte budget"
            )
        try:
            payload = json.loads(payload_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise SourcePackError(
                "schema_drift", "feature collection is not valid JSON"
            ) from exc
    if len(payload_bytes) > limits.max_bytes:
        raise SourcePackError(
            "response_too_large", "feature collection exceeds its byte budget"
        )
    _depth(payload, limits.max_depth)
    if not isinstance(payload, Mapping) or payload.get("type") != "FeatureCollection":
        raise SourcePackError("schema_drift", "response is not a GeoJSON FeatureCollection")
    features = payload.get("features")
    if not isinstance(features, list):
        raise SourcePackError("schema_drift", "FeatureCollection has no features array")
    if len(features) > limits.max_features:
        raise SourcePackError(
            "budget_exhausted", "feature collection exceeds its feature budget"
        )
    source_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    declared_crs = (
        dict(payload.get("crs") or {}).get("properties", {}).get("name")
        if isinstance(payload.get("crs"), Mapping)
        else None
    )
    metadata = {
        "number_matched": payload.get("numberMatched", payload.get("totalFeatures")),
        "number_returned": payload.get("numberReturned", len(features)),
        "provider_timestamp": payload.get("timeStamp"),
        "declared_crs": declared_crs,
        "bbox": payload.get("bbox"),
        "links": [
            {"rel": link.get("rel"), "href": link.get("href")}
            for link in payload.get("links") or []
            if isinstance(link, Mapping)
        ],
    }
    records: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    positions = 0
    for index, feature in enumerate(features):
        if not isinstance(feature, Mapping) or feature.get("type") != "Feature":
            rejections.append(
                _rejection(
                    "invalid_feature",
                    "collection member is not a GeoJSON Feature",
                    provider=provider,
                    collection=collection,
                    index=index,
                    native_id=None,
                    feature=feature if isinstance(feature, Mapping) else {"value": feature},
                )
            )
            continue
        properties = feature.get("properties")
        properties = dict(properties) if isinstance(properties, Mapping) else {}
        native_id = properties.get(id_property) if id_property else feature.get("id")
        common = {
            "provider": provider,
            "collection": collection,
            "index": index,
            "native_id": native_id,
            "feature": feature,
        }
        if native_id is None or str(native_id).strip() == "":
            rejections.append(
                _rejection(
                    "missing_id",
                    "feature has no native identifier; no stable identity is invented",
                    **common,
                )
            )
            continue
        native_id = str(native_id)
        if native_id in seen:
            rejections.append(
                _rejection(
                    "duplicate_id",
                    f"native identifier repeats feature {seen[native_id]} in one collection",
                    **common,
                )
            )
            continue
        seen[native_id] = index
        geometry = feature.get("geometry")
        if geometry is None:
            rejections.append(
                _rejection("null_geometry", "feature has no geometry", **common)
            )
            continue
        kind = str(dict(geometry).get("type") or "") if isinstance(geometry, Mapping) else ""
        if kind not in SUPPORTED_GEOMETRIES:
            rejections.append(
                _rejection(
                    "unsupported_geometry",
                    f"geometry type {kind or 'unknown'!r} is not supported"
                    + ("" if kind in _KNOWN_GEOMETRIES else " (unrecognized)"),
                    **common,
                )
            )
            continue
        coordinates = geometry.get("coordinates")
        try:
            count = _count_positions(coordinates, _GEOMETRY_DEPTH[kind])
            if kind == "LineString" and len(coordinates or []) < 2:
                raise ValueError("line needs at least two positions")
            if not _rings_closed(kind, coordinates):
                raise ValueError("polygon rings must be closed with at least four positions")
        except ValueError as exc:
            rejections.append(_rejection("invalid_geometry", str(exc), **common))
            continue
        positions += count
        if positions > limits.max_coordinates:
            raise SourcePackError(
                "budget_exhausted", "feature collection exceeds its coordinate budget"
            )
        normalized_coordinates = (
            coordinates
            if axis_order == "east_north"
            else _swap_axes(coordinates, _GEOMETRY_DEPTH[kind])
        )
        title = (
            properties.get(title_property) if title_property else None
        ) or f"{collection} {native_id}"
        # Explicit provider tombstones travel with the record so both the
        # document lifecycle and the spatial projection can honour them.
        lifecycle = {
            key: feature[key] for key in ("deleted", "status") if feature.get(key) is not None
        }
        records.append(
            {
                "id": feature_key(provider, collection, native_id),
                "title": str(title),
                "content": _canonical(properties),
                "language": "und",
                **lifecycle,
                "feature": {
                    "contract": FEATURE_RECORD_CONTRACT,
                    "provider": provider,
                    "collection": collection,
                    "native_id": native_id,
                    "properties": properties,
                    "geometry": {"type": kind, "coordinates": normalized_coordinates},
                    "source_geometry": {"type": kind, "coordinates": coordinates},
                    "source_crs": source_crs,
                    "declared_crs": declared_crs,
                    "axis_order": axis_order,
                    "source_url": source_url,
                    "source_sha256": source_sha256,
                },
            }
        )
    return {
        "contract": "noesis-geospatial-feature-collection-v1",
        "provider": provider,
        "collection": collection,
        "source_crs": source_crs,
        "axis_order": axis_order,
        "source_sha256": source_sha256,
        "source_bytes": len(payload_bytes),
        "metadata": metadata,
        "records": records,
        "rejections": rejections,
        "positions": positions,
    }


class GeoJsonFeatureAdapter:
    """Fetch one bounded GeoJSON FeatureCollection over the runtime HTTPS policy.

    A static FeatureCollection has no paging contract, so a run is a single
    page.  The collection is complete only when the source declares
    ``geospatial.snapshot = "complete"``; otherwise absence never implies
    deletion.
    """

    accepts_transport = True

    def __init__(self, source: Mapping[str, Any], *, transport=None, secret=None) -> None:
        from src.ingestion.source_pack_runtime import ADAPTER_CONTRACT, HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        geospatial = dict(self.source.get("geospatial") or {})
        for key in ("collection", "source_crs", "axis_order"):
            if not geospatial.get(key):
                raise SourcePackError(
                    "invalid_mapping", f"GeoJSON source must declare geospatial.{key}"
                )
        self.geospatial = geospatial
        if transport is None:
            from functools import partial

            transport = partial(
                HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
            )
        self.transport = transport
        self.secret = secret
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if request.get("parameters"):
            raise SourcePackError(
                "parameter_forbidden", "static GeoJSON sources accept no parameters"
            )
        if cursor is not None:
            raise SourcePackError("cursor_drift", "static GeoJSON sources have one page")
        response = self.transport(
            url=self.definition["endpoint"],
            params={},
            headers={"Accept": "application/geo+json, application/json;q=0.9"},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        status = int(response.get("status", 200))
        if status == 429:
            headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
            raise SourcePackError(
                "rate_limited",
                "source quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", "source rejected configured authentication"
            )
        if status < 200 or status >= 300:
            raise SourcePackError("source_unavailable", f"source returned HTTP {status}")
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        decoded = decode_feature_collection(
            raw,
            provider=self.source["publisher"],
            collection=self.geospatial["collection"],
            source_crs=self.geospatial["source_crs"],
            axis_order=self.geospatial["axis_order"],
            id_property=self.geospatial.get("id_property"),
            title_property=self.geospatial.get("title_property"),
            budget=FeatureBudget(
                max_features=int(self.definition["limits"]["max_results"]),
                max_bytes=int(self.definition["limits"]["max_bytes"]),
            ),
            source_url=self.definition["endpoint"],
        )
        page = {
            "start_index": 0,
            "number_matched": len(decoded["records"]) + len(decoded["rejections"]),
            "number_returned": len(decoded["records"]) + len(decoded["rejections"]),
            "provider_timestamp": decoded["metadata"]["provider_timestamp"],
            "response_sha256": decoded["source_sha256"],
            "scope": {"endpoint": self.definition["endpoint"], "bbox": None},
            "scope_hash": _digest({"endpoint": self.definition["endpoint"], "bbox": None}),
            "complete_scope": self.geospatial.get("snapshot") == "complete",
            "final_page": True,
            "declared_next": False,
        }
        records = [{**item, "feature_page": page} for item in decoded["records"]]
        records += [{**item, "feature_page": page} for item in decoded["rejections"]]
        return RuntimePage(
            tuple(records),
            None,
            len(raw),
            receipt={"status": status, **{k: v for k, v in page.items() if k != "scope"}},
        )


def load_local_feature_collection(
    path: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str] | None = None,
    max_bytes: int = 20_000_000,
) -> bytes:
    """Read a local FeatureCollection only from the configured import root.

    Local imports never read arbitrary server paths: the file must resolve
    inside ``root`` (or ``NOESIS_GEOSPATIAL_IMPORT_ROOT``) and stay within the
    byte budget.  Authorization and namespace ownership are enforced by the
    caller that projects the features.
    """

    configured = root if root is not None else os.environ.get(IMPORT_ROOT_ENV)
    if not configured:
        raise SourcePackError(
            "import_root_unconfigured",
            f"set {IMPORT_ROOT_ENV} to allow local geospatial imports",
        )
    base = Path(configured).resolve()
    target = (base / path).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise SourcePackError(
            "unsafe_import_path", "local import escapes the configured import root"
        ) from exc
    if not target.is_file():
        raise SourcePackError("not_found", "local feature collection does not exist")
    with target.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise SourcePackError(
            "response_too_large", "local feature collection exceeds its byte budget"
        )
    return content
