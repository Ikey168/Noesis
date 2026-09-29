"""Bounded native OGC WFS 2.0.0 GetFeature adapter for source packs.

The adapter speaks the pinned WFS operation rather than treating the service
as generic REST JSON: every request carries the declared version, type name,
output format, ``SRSNAME`` and a stable ``SORTBY`` so ``COUNT``/``STARTINDEX``
paging is reproducible.  Paging stops on the provider's ``numberMatched``;
``next`` links are recorded but not trusted (some servers emit one past the
last page).  Service ``ExceptionReport`` envelopes, repeated pages and a
collection that changes size mid-run surface as runtime failures, so the
existing retry, quarantine and checkpoint machinery applies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from typing import Any

from src.ingestion.geojson_features import (
    AXIS_ORDERS,
    FeatureBudget,
    decode_feature_collection,
)
from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
WFS_VERSION = "2.0.0"
OUTPUT_FORMATS = frozenset({"application/json"})
_RETRYABLE_EXCEPTIONS = frozenset({"OperationProcessingFailed", "NoApplicableCode"})


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_wfs_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return the pinned WFS contract a source declares, or raise."""

    wfs = dict(source.get("wfs") or {})
    required = ("version", "type_names", "output_format", "srs_name", "axis_order",
                "sort_by", "page_size")
    missing = [key for key in required if wfs.get(key) in (None, "")]
    if missing:
        raise SourcePackError(
            "invalid_mapping", "WFS source must pin " + ", ".join(sorted(missing))
        )
    if wfs["version"] != WFS_VERSION:
        raise SourcePackError("invalid_mapping", "only WFS 2.0.0 is implemented")
    if wfs["output_format"] not in OUTPUT_FORMATS:
        raise SourcePackError(
            "invalid_mapping", "WFS output format must be a pinned GeoJSON encoding"
        )
    if wfs["axis_order"] not in AXIS_ORDERS:
        raise SourcePackError("invalid_mapping", "WFS axis order must be declared")
    if not 1 <= int(wfs["page_size"]) <= 10_000:
        raise SourcePackError("unbounded_source", "WFS page size is outside bounds")
    return wfs


def _cursor(value: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).decode()


def _uncursor(value: str | None) -> dict[str, Any]:
    if value is None:
        return {"start": 0, "matched": None, "first": None}
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value.encode()))
        return {
            "start": int(decoded["start"]),
            "matched": None if decoded.get("matched") is None else int(decoded["matched"]),
            "first": decoded.get("first"),
            "scope": decoded.get("scope"),
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise SourcePackError("cursor_drift", "WFS cursor is not a valid checkpoint") from exc


def service_exception(raw: bytes) -> SourcePackError:
    """Map an OWS ExceptionReport envelope to a classified runtime error."""

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return SourcePackError("schema_drift", "WFS returned an unparseable XML body")
    if not root.tag.endswith("ExceptionReport"):
        return SourcePackError("schema_drift", "WFS returned XML that is not GeoJSON")
    codes = sorted(
        {
            str(item.attrib.get("exceptionCode") or "unknown")
            for item in root.iter()
            if item.tag.endswith("}Exception") or item.tag == "Exception"
        }
    ) or ["unknown"]
    code = "source_unavailable" if set(codes) <= _RETRYABLE_EXCEPTIONS else "schema_drift"
    return SourcePackError(code, "WFS service exception: " + ",".join(codes))


class WfsFeatureAdapter:
    """Read-only WFS 2.0.0 GetFeature pages for one pinned feature type."""

    accepts_transport = True

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.wfs = validate_wfs_declaration(self.source)
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
            "wfs": dict(self.wfs),
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def scope(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        """The collection scope a run covers; only an unbounded scope is complete."""

        bbox = parameters.get("bbox")
        return {
            "endpoint": self.source["endpoint"],
            "type_names": self.wfs["type_names"],
            "srs_name": self.wfs["srs_name"],
            "sort_by": self.wfs["sort_by"],
            "bbox": None if bbox is None else [float(value) for value in bbox],
        }

    def request_parameters(
        self, request: Mapping[str, Any], *, start: int, count: int
    ) -> dict[str, Any]:
        parameters = dict(request.get("parameters") or {})
        if set(parameters) - {"bbox"}:
            raise SourcePackError(
                "parameter_forbidden", "WFS runs accept only an explicit bbox parameter"
            )
        params = {
            "SERVICE": "WFS",
            "VERSION": self.wfs["version"],
            "REQUEST": "GetFeature",
            "TYPENAMES": self.wfs["type_names"],
            "OUTPUTFORMAT": self.wfs["output_format"],
            "SRSNAME": self.wfs["srs_name"],
            "SORTBY": self.wfs["sort_by"],
            "COUNT": count,
            "STARTINDEX": start,
        }
        bbox = parameters.get("bbox")
        if bbox is not None:
            if (
                not isinstance(bbox, (list, tuple))
                or len(bbox) != 4
                or float(bbox[0]) >= float(bbox[2])
                or float(bbox[1]) >= float(bbox[3])
            ):
                raise SourcePackError(
                    "parameter_forbidden", "bbox needs minx,miny,maxx,maxy in source CRS"
                )
            params["BBOX"] = ",".join(str(float(value)) for value in bbox) + "," + str(
                self.wfs["srs_name"]
            )
        return params

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        operation = str(request.get("operation") or "")
        if operation not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        state = _uncursor(cursor)
        scope = self.scope(dict(request.get("parameters") or {}))
        scope_hash = _digest(scope)
        if state.get("scope") not in (None, scope_hash):
            raise SourcePackError(
                "cursor_drift", "WFS cursor belongs to a different collection scope"
            )
        count = max(1, min(int(request.get("limit", 100)), int(self.wfs["page_size"])))
        params = self.request_parameters(request, start=state["start"], count=count)
        response = self.transport(
            url=self.definition["endpoint"],
            params=params,
            headers={"Accept": "application/json, application/xml;q=0.5"},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        status = int(response.get("status", 200))
        headers = {
            str(key).casefold(): value
            for key, value in dict(response.get("headers") or {}).items()
        }
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "source quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", "source rejected configured authentication"
            )
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if status < 200 or status >= 300:
            if raw.lstrip().startswith(b"<"):
                raise service_exception(raw)
            raise SourcePackError("source_unavailable", f"source returned HTTP {status}")
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "source response exceeds its byte limit"
            )
        if raw.lstrip().startswith(b"<"):
            raise service_exception(raw)
        geospatial = dict(self.source.get("geospatial") or {})
        decoded = decode_feature_collection(
            raw,
            provider=self.source["publisher"],
            collection=self.wfs["type_names"],
            source_crs=self.wfs["srs_name"],
            axis_order=self.wfs["axis_order"],
            id_property=self.wfs.get("id_property"),
            title_property=geospatial.get("title_property"),
            budget=FeatureBudget(
                max_features=count,
                max_bytes=int(self.definition["limits"]["max_bytes"]),
            ),
            source_url=self.definition["endpoint"],
        )
        metadata = decoded["metadata"]
        returned = len(decoded["records"]) + len(decoded["rejections"])
        try:
            matched = int(metadata["number_matched"])
            declared_returned = int(metadata["number_returned"])
        except (TypeError, ValueError) as exc:
            raise SourcePackError(
                "schema_drift", "WFS response lacks numberMatched/numberReturned"
            ) from exc
        if declared_returned != returned:
            raise SourcePackError(
                "schema_drift", "WFS numberReturned disagrees with the features array"
            )
        if state["matched"] is not None and state["matched"] != matched:
            raise SourcePackError(
                "schema_drift", "WFS collection changed size while paging"
            )
        feature_ids = [item["feature"]["native_id"] for item in decoded["records"]]
        first = feature_ids[0] if feature_ids else None
        if state["first"] is not None and first == state["first"]:
            raise SourcePackError("schema_drift", "WFS repeated the previous page")
        next_start = state["start"] + returned
        next_cursor = (
            _cursor({"start": next_start, "matched": matched, "first": first,
                     "scope": scope_hash})
            if returned > 0 and next_start < matched
            else None
        )
        page = {
            "start_index": state["start"],
            "number_matched": matched,
            "number_returned": returned,
            "provider_timestamp": metadata["provider_timestamp"],
            "response_sha256": decoded["source_sha256"],
            "scope": scope,
            "scope_hash": scope_hash,
            "complete_scope": scope["bbox"] is None,
            "final_page": next_cursor is None,
            "declared_next": any(link["rel"] == "next" for link in metadata["links"]),
        }
        records = [
            {**record, "feature_page": page} for record in decoded["records"]
        ] + [{**record, "feature_page": page} for record in decoded["rejections"]]
        return RuntimePage(
            tuple(records),
            next_cursor,
            len(raw),
            receipt={"status": status, **{key: page[key] for key in page if key != "scope"}},
        )


def fixture_transport(pages: list[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay captured native envelopes by ``STARTINDEX`` for offline conformance.

    Each page is ``{"start_index": int, "status": int, "body": <json|str>}``.
    The transport refuses requests whose pinned parameters differ from the
    capture so fixture replay cannot drift from the declared contract.
    """

    by_start = {int(page["start_index"]): page for page in pages}

    def transport(*, url, params, headers, timeout):
        del url, headers, timeout
        page = by_start.get(int(params["STARTINDEX"]))
        if page is None:
            raise SourcePackError(
                "fixture_missing", "no captured page for the requested STARTINDEX"
            )
        pinned = dict(page.get("params") or {})
        mismatched = sorted(
            key for key, value in pinned.items() if str(params.get(key)) != str(value)
        )
        if mismatched:
            raise SourcePackError(
                "schema_drift", "fixture request differs from capture: " + ",".join(mismatched)
            )
        body = page["body"]
        content = body.encode() if isinstance(body, str) else json.dumps(
            body, ensure_ascii=False
        ).encode()
        return {"status": int(page.get("status", 200)), "headers": {}, "content": content}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Decode a captured native fixture through the real adapter, page by page.

    Used by offline conformance so the pinned expected output is produced by
    the same WFS/GeoJSON code path as a live run, not by pre-normalized data.
    """

    from src.ingestion.geojson_features import GeoJsonFeatureAdapter

    transport = fixture_transport(list(fixture["native_pages"]))
    builder = WfsFeatureAdapter if source["connector"] == "wfs" else GeoJsonFeatureAdapter
    adapter = builder(source, transport=transport)
    operation = sorted(source["operations"])[0]
    limit = int(source["budgets"]["max_results"])
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page(
            {"operation": operation, "parameters": {}, "limit": limit - len(records)},
            cursor=cursor,
        )
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
