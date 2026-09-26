"""Bounded native Open Icecat and EPREL acquisition for the Products pack.

Both providers are read one explicitly selected model at a time: a source
declares a bounded ``product.selection`` list and each runtime page fetches one
selector, so request, byte, page and result budgets from the source-pack
runtime apply unchanged and the cursor is simply the selector index.

Per-model coverage outcomes are *not* failures: a model the provider does not
know (``not_found``) or one outside the Open Icecat catalogue
(``outside_open_catalogue``) is recorded in the page receipt and the run
continues. Authentication failure, throttling, service errors and schema drift
raise :class:`~src.ingestion.source_packs.SourcePackError` so the runtime's
retry, quarantine and checkpoint handling applies.

Records carry a provider-neutral ``product_record`` (contract
``noesis-product-record-v1``) that keeps native identifiers, names, values,
units and JSON-pointer locators. Neither provider's content is independent
testing: Icecat is brand-authorised/editorial content and EPREL is supplier
registration data, and the records say so.

The native field names below were pinned against the providers' public
documentation and must be re-validated with a live bounded run (see
``scripts/products_live_check.py``); fixtures in ``tests/fixtures/source_packs``
are authored envelopes, not captures.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-product-record-v1"
PRODUCT_CONNECTORS = frozenset({"icecat", "eprel"})
MAX_SELECTION = 50
ASSERTION_KINDS = {
    "icecat": "brand-authorised-content",
    "eprel": "supplier-registration",
}
# Provider documentation locations recorded in the source contract.
PROVIDER_CONTRACTS = {
    "icecat": {
        "documentation": "https://icecat.com/structured-data-content-users/",
        "access": "Open Icecat JSON product API (live.icecat.biz/api) by brand + product code or GTIN",
        "authentication": "Open Icecat shop name; Full Icecat content requires a paid account and is out of scope",
        "catalogue_boundary": "Only Open Icecat (sponsoring brands) is claimed; restricted products are reported as outside_open_catalogue",
        "formats": ["json"],
        "status": "unverified-live",
    },
    "eprel": {
        "documentation": "https://eprel.ec.europa.eu/screen/requestpublicapikey",
        "access": "EPREL public API /api/products/{productGroup}/{registrationNumber}",
        "authentication": "x-api-key issued through the EPREL public API request process (operator step)",
        "catalogue_boundary": "Public registration data for the pinned product group only; supplier-only endpoints are never used",
        "formats": ["json"],
        "status": "unverified-live",
    },
}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def gtin_state(value: Any) -> dict[str, Any]:
    """Validate a GTIN while keeping the exact original string (leading zeros)."""

    text = "" if value is None else str(value).strip()
    if not text:
        return {"value": None, "state": "absent"}
    if not re.fullmatch(r"\d{8}|\d{12}|\d{13}|\d{14}", text):
        return {"value": text, "state": "invalid_format"}
    digits = [int(char) for char in text]
    body, check = digits[:-1], digits[-1]
    total = sum(digit * (3 if index % 2 == 0 else 1) for index, digit in enumerate(reversed(body)))
    valid = (10 - total % 10) % 10 == check
    return {"value": text, "state": "valid" if valid else "invalid_checksum"}


def _decimal_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return str(number.normalize()) if number == number.to_integral_value() else str(number)


def product_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    product = dict(source.get("product") or {})
    selection = product.get("selection")
    if not isinstance(selection, list) or not 1 <= len(selection) <= MAX_SELECTION:
        raise SourcePackError(
            "unbounded_source", f"product sources need an explicit selection of 1-{MAX_SELECTION} models"
        )
    for item in selection:
        if not isinstance(item, Mapping) or not item:
            raise SourcePackError("invalid_mapping", "each product selector must be an object")
        keys = set(item) - {"label"}
        if keys not in ({"brand", "product_code"}, {"gtin"}, {"registration_number"}):
            raise SourcePackError(
                "invalid_mapping",
                "selectors use brand+product_code, gtin or registration_number",
            )
    category = dict(product.get("category") or {})
    if not category.get("label"):
        raise SourcePackError("invalid_mapping", "product sources pin a category label")
    return product


class _ProductAdapter:
    accepts_transport = True
    provider = ""

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.product = product_declaration(self.source)
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
            "product": {
                "provider": self.provider,
                "category": self.product["category"],
                "selection_size": len(self.product["selection"]),
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _selection_scope(self) -> dict[str, Any]:
        return {"endpoint": self.source["endpoint"], "selection": self.product["selection"],
                "category": self.product["category"]}

    def request(self, selector: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, str]]:
        raise NotImplementedError

    def records(self, payload: Any, selector: Mapping[str, Any], raw: bytes) -> list[dict[str, Any]]:
        raise NotImplementedError

    def classify(self, status: int, payload: Any, raw: bytes, selector: Mapping[str, Any]) -> str | None:
        """Return a per-model outcome (``not_found``/``outside_open_catalogue``) or raise."""
        raise NotImplementedError

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        operation = str(request.get("operation") or "")
        if operation not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError(
                "parameter_forbidden", "product runs use the pinned selection, not ad-hoc parameters"
            )
        selection = self.product["selection"]
        try:
            index = 0 if cursor is None else int(json.loads(cursor)["index"])
            scope = None if cursor is None else json.loads(cursor)["scope"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SourcePackError("cursor_drift", "product cursor is not a valid checkpoint") from exc
        scope_hash = _digest(self._selection_scope())
        if scope not in (None, scope_hash):
            raise SourcePackError("cursor_drift", "product cursor belongs to a different selection")
        if index >= len(selection):
            return RuntimePage((), None, 0, receipt={"status": 200, "selection_index": index})
        selector = dict(selection[index])
        url, params, headers = self.request(selector)
        response = self.transport(
            url=url, params=params, headers=headers,
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        status = int(response.get("status", 200))
        response_headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            raise SourcePackError(
                "rate_limited", "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(response_headers.get("retry-after")),
            )
        if status in {401, 403} and self.provider == "eprel":
            raise SourcePackError(
                "authentication_failed", "EPREL rejected the API key (missing, unapproved or revoked)"
            )
        try:
            payload = json.loads(raw) if raw.strip() else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        outcome = self.classify(status, payload, raw, selector)
        page_info = {
            "selection_index": index,
            "selector": selector,
            "selection_size": len(selection),
            "scope_hash": scope_hash,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "http_status": status,
            "final_page": index + 1 >= len(selection),
        }
        records: list[dict[str, Any]] = []
        if outcome is None:
            if not isinstance(payload, Mapping):
                raise SourcePackError("schema_drift", f"{self.provider} returned a non-JSON product body")
            records = [
                {**item, "product_page": page_info}
                for item in self.records(payload, selector, raw)
            ]
        next_cursor = (
            json.dumps({"index": index + 1, "scope": scope_hash}, sort_keys=True)
            if index + 1 < len(selection)
            else None
        )
        return RuntimePage(
            tuple(records), next_cursor, len(raw),
            receipt={"status": status, **page_info,
                     "model_outcome": outcome or "returned",
                     "quota_remaining": response_headers.get("x-ratelimit-remaining")},
        )


def _pointer(*parts: Any) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)


# ----------------------------------------------------------------- Open Icecat

# Feature names are the provider's English feature labels; the mapping is part
# of the pinned source contract and extends only by explicit review.
ICECAT_FEATURES = {
    "display diagonal": ("diagonal", None),
    "display resolution": ("resolution", None),
    "width (with stand)": ("width", "with-stand"),
    "height (with stand)": ("height", "with-stand"),
    "depth (with stand)": ("depth", "with-stand"),
    "width (without stand)": ("width", "without-stand"),
    "height (without stand)": ("height", "without-stand"),
    "depth (without stand)": ("depth", "without-stand"),
    "power consumption (typical)": ("on_mode_power", "typical"),
    "energy efficiency class (sdr)": ("energy_class", "sdr"),
    "energy efficiency class (hdr)": ("energy_class", "hdr"),
    "power consumption (sdr) per 1000 hours": ("energy_consumption_1000h", "sdr"),
    "power consumption (hdr) per 1000 hours": ("energy_consumption_1000h", "hdr"),
}
_ICECAT_UNITS = {'"': "in", "inch": "in", "cm": "cm", "mm": "mm", "w": "W", "kwh": "kWh"}


def _icecat_unit(feature: Mapping[str, Any]) -> str | None:
    measure = dict(feature.get("Measure") or {})
    sign = (measure.get("Sign") or dict(measure.get("Signs") or {}).get("_") or feature.get("Sign") or "")
    return _ICECAT_UNITS.get(str(sign).strip().casefold()) if sign else None


class IcecatProductAdapter(_ProductAdapter):
    provider = "icecat"

    def request(self, selector):
        params = {
            "lang": str(self.product.get("language") or "EN"),
            "shopname": str(self.product.get("shopname") or "openicecat-live"),
            "content": "",
        }
        if "gtin" in selector:
            params["GTIN"] = selector["gtin"]
        else:
            params["Brand"] = selector["brand"]
            params["ProductCode"] = selector["product_code"]
        headers = {"Accept": "application/json"}
        if self.secret:
            headers["api-token"] = self.secret
        return self.source["endpoint"], params, headers

    def classify(self, status, payload, raw, selector):
        message = str((payload or {}).get("Message") or (payload or {}).get("message") or "") if isinstance(payload, Mapping) else ""
        lowered = message.casefold()
        if "full icecat" in lowered or "restricted" in lowered or "not allowed" in lowered:
            return "outside_open_catalogue"
        if status == 404 or "not present" in lowered or "not found" in lowered:
            return "not_found"
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", "Icecat rejected the configured access")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"Icecat returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"Icecat returned HTTP {status}: {message[:120]}")
        if not isinstance(payload, Mapping) or str(payload.get("msg") or "").upper() != "OK" or not isinstance(payload.get("data"), Mapping):
            raise SourcePackError("schema_drift", "Icecat response lacks msg=OK and a data object")
        return None

    def records(self, payload, selector, raw):
        data = payload["data"]
        info = data.get("GeneralInfo")
        if not isinstance(info, Mapping) or info.get("IcecatId") in (None, ""):
            raise SourcePackError("schema_drift", "Icecat product lacks GeneralInfo.IcecatId")
        icecat_id = str(info["IcecatId"])
        category = dict(info.get("Category") or {})
        category_id = str(category.get("CategoryID") or "")
        pinned = str(self.product["category"].get("provider_category_id") or "")
        attributes: list[dict[str, Any]] = []
        for group_index, group in enumerate(data.get("FeaturesGroups") or []):
            for feature_index, feature in enumerate(group.get("Features") or []):
                definition = dict(feature.get("Feature") or {})
                name = str(dict(definition.get("Name") or {}).get("Value") or "")
                mapped = ICECAT_FEATURES.get(name.casefold())
                if not mapped:
                    continue
                attribute, mode = mapped
                attributes.append({
                    "attribute": attribute,
                    "mode": mode,
                    "native_name": name,
                    "native_feature_id": str(definition.get("ID") or ""),
                    "native_value": feature.get("Value"),
                    "presentation_value": feature.get("PresentationValue"),
                    "native_unit": _icecat_unit(definition),
                    "locator": {"json_pointer": _pointer("data", "FeaturesGroups", group_index, "Features", feature_index)},
                })
        documents = []
        for index, item in enumerate(data.get("Multimedia") or []):
            if not isinstance(item, Mapping) or not item.get("URL"):
                continue
            documents.append({
                "kind": "datasheet" if "sheet" in str(item.get("Description") or item.get("Type") or "").casefold()
                or str(item.get("Type") or "").casefold() == "leaflet" else "manufacturer-document",
                "url": str(item["URL"]),
                "media_type": item.get("ContentType"),
                "language": item.get("Language") or self.product.get("language"),
                "declared_size": item.get("Size"),
                "declared_updated": item.get("Updated"),
                "title": item.get("Description"),
                "locator": {"json_pointer": _pointer("data", "Multimedia", index)},
            })
        gtins = info.get("GTIN") or []
        record = {
            "contract": RECORD_CONTRACT,
            "provider": "icecat",
            "assertion_kind": ASSERTION_KINDS["icecat"],
            "provider_record_id": icecat_id,
            "provider_revision": str(info.get("ProductDataUpdated") or info.get("Updated") or "") or None,
            "category": {
                "label": self.product["category"]["label"],
                "provider_category_id": category_id or None,
                "provider_category_name": dict(category.get("Name") or {}).get("Value"),
                "in_pinned_category": bool(pinned) and category_id == pinned,
            },
            "brand": str(info.get("Brand") or dict(info.get("BrandInfo") or {}).get("BrandName") or "") or None,
            "designation": str(info.get("BrandPartCode") or "") or None,
            "title": info.get("Title"),
            "family": dict(info.get("ProductFamily") or {}).get("Value"),
            "series": dict(info.get("ProductSeries") or {}).get("Value"),
            "identifiers": {
                "icecat_id": icecat_id,
                "mpn": str(info.get("BrandPartCode") or "") or None,
                "gtin": [gtin_state(value) for value in (gtins if isinstance(gtins, list) else [gtins])],
            },
            "market": {"region": self.product.get("market"), "language": self.product.get("language")},
            "lifecycle_claims": {"release_date": info.get("ReleaseDate"), "end_of_life": info.get("EndOfLifeDate")},
            "attributes": attributes,
            "documents": documents,
            "record_status": "published",
            "selector": selector,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
        }
        return [{
            "id": f"icecat:{icecat_id}",
            "title": str(info.get("Title") or f"Icecat {icecat_id}"),
            "language": str(self.product.get("language") or "EN").lower(),
            "updated_at": record["provider_revision"],
            "url": f"https://icecat.biz/p/{quote(str(record['brand'] or 'product').lower())}/{quote(str(record['designation'] or icecat_id).lower())}/{icecat_id}.html",
            "status": "active",
            "product_record": record,
        }]


# ----------------------------------------------------------------------- EPREL

EPREL_FIELDS = {
    "diagonalCm": ("diagonal", None, "cm"),
    "diagonalInch": ("diagonal", None, "in"),
    "resolutionHorizontalPixels": ("resolution_horizontal", None, "px"),
    "resolutionVerticalPixels": ("resolution_vertical", None, "px"),
    "powerOnModeSDR": ("on_mode_power", "sdr", "W"),
    "powerOnModeHDR": ("on_mode_power", "hdr", "W"),
    "energyConsumption1000hSDR": ("energy_consumption_1000h", "sdr", "kWh"),
    "energyConsumption1000hHDR": ("energy_consumption_1000h", "hdr", "kWh"),
    "energyClassSDR": ("energy_class", "sdr", None),
    "energyClassHDR": ("energy_class", "hdr", None),
}
EPREL_WITHDRAWN = frozenset({"WITHDRAWN", "DELETED", "REMOVED"})


def _eprel_date(value: Any) -> str | None:
    if isinstance(value, list) and len(value) >= 3:
        return f"{int(value[0]):04d}-{int(value[1]):02d}-{int(value[2]):02d}"
    return str(value) if value else None


class EprelProductAdapter(_ProductAdapter):
    provider = "eprel"

    def request(self, selector):
        if "registration_number" not in selector:
            raise SourcePackError("invalid_mapping", "EPREL selectors use registration_number")
        group = str(self.product["category"].get("product_group") or "")
        if not re.fullmatch(r"[a-z0-9]+", group):
            raise SourcePackError("invalid_mapping", "EPREL sources pin a product group")
        number = str(selector["registration_number"])
        if not re.fullmatch(r"\d{1,12}", number):
            raise SourcePackError("invalid_mapping", "EPREL registration numbers are numeric")
        headers = {"Accept": "application/json"}
        if self.secret:
            headers["x-api-key"] = self.secret
        return f"{self.source['endpoint'].rstrip('/')}/{group}/{number}", {}, headers

    def classify(self, status, payload, raw, selector):
        if status == 404:
            return "not_found"
        if status >= 500:
            raise SourcePackError("source_unavailable", f"EPREL returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"EPREL returned HTTP {status}")
        if not isinstance(payload, Mapping) or not payload.get("eprelRegistrationNumber"):
            raise SourcePackError("schema_drift", "EPREL product lacks eprelRegistrationNumber")
        return None

    def records(self, payload, selector, raw):
        number = str(payload["eprelRegistrationNumber"])
        group = str(self.product["category"]["product_group"])
        native_group = str(payload.get("productGroup") or "")
        attributes = []
        for field, (attribute, mode, unit) in EPREL_FIELDS.items():
            if field not in payload:
                continue
            attributes.append({
                "attribute": attribute, "mode": mode, "native_name": field,
                "native_value": payload[field], "native_unit": unit,
                "label_scheme": payload.get("implementingAct"),
                "locator": {"json_pointer": _pointer(field)},
            })
        status = str(payload.get("status") or "").upper()
        base = f"{self.source['endpoint'].rstrip('/')}/{group}/{number}"
        language = str(self.product.get("language") or "EN")
        documents = [
            {"kind": "product-information-sheet", "url": f"{base}/fiches?language={language}",
             "media_type": "application/pdf", "language": language,
             "locator": {"json_pointer": "/eprelRegistrationNumber"}},
            {"kind": "energy-label", "url": f"{base}/labels?format=PDF",
             "media_type": "application/pdf", "language": None,
             "locator": {"json_pointer": "/energyLabelId"}},
        ]
        record = {
            "contract": RECORD_CONTRACT,
            "provider": "eprel",
            "assertion_kind": ASSERTION_KINDS["eprel"],
            "provider_record_id": number,
            "provider_revision": (f"version {payload.get('versionNumber')}" if payload.get("versionNumber") is not None else None),
            "category": {
                "label": self.product["category"]["label"],
                "provider_category_id": native_group or None,
                "provider_category_name": native_group or None,
                "in_pinned_category": native_group == group,
            },
            "brand": str(payload.get("supplierOrTrademark") or "") or None,
            "designation": str(payload.get("modelIdentifier") or "") or None,
            "title": f"{payload.get('supplierOrTrademark') or ''} {payload.get('modelIdentifier') or ''}".strip() or None,
            "family": None,
            "series": None,
            "identifiers": {"eprel_registration": number, "mpn": str(payload.get("modelIdentifier") or "") or None,
                            "gtin": []},
            "market": {"region": self.product.get("market"),
                       "placement_countries": sorted({str(item.get("country")) for item in payload.get("placementCountries") or []
                                                      if isinstance(item, Mapping) and item.get("country")})},
            "lifecycle_claims": {"on_market_start": _eprel_date(payload.get("onMarketStartDate")),
                                 "on_market_end": _eprel_date(payload.get("onMarketEndDate"))},
            "label_scheme": payload.get("implementingAct"),
            "attributes": attributes,
            "documents": documents,
            "record_status": "withdrawn" if status in EPREL_WITHDRAWN else "published",
            "native_status": status or None,
            "selector": selector,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
        }
        return [{
            "id": f"eprel:{group}:{number}",
            "title": f"{record['title'] or 'EPREL model'} (EPREL {number})",
            "language": "en",
            # versionNumber is an ordinal, not a time; it stays in provider_revision.
            "published_at": record["lifecycle_claims"]["on_market_start"],
            "url": f"https://eprel.ec.europa.eu/screen/product/{group}/{number}",
            "status": "withdrawn" if record["record_status"] == "withdrawn" else "active",
            "product_record": record,
        }]


ADAPTERS = {"icecat": IcecatProductAdapter, "eprel": EprelProductAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored native envelopes keyed by the selector they answer."""

    by_key = {_digest(page["selector"]): page for page in pages}

    def transport(*, url, params, headers, timeout):
        del timeout
        key = None
        for page in pages:
            selector = page["selector"]
            if ("registration_number" in selector and url.rstrip("/").endswith("/" + str(selector["registration_number"])))\
                    or ("gtin" in selector and params.get("GTIN") == selector["gtin"])\
                    or ("product_code" in selector and params.get("ProductCode") == selector["product_code"]
                        and params.get("Brand") == selector["brand"]):
                key = _digest(selector)
                break
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", "no captured page for this selector")
        if page.get("requires_secret") and not (headers.get("x-api-key") or headers.get("api-token")):
            return {"status": 401, "headers": {}, "content": b""}
        body = page.get("body")
        content = b"" if body is None else body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}), "content": content}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Decode an authored native fixture through the real adapter, page by page."""

    adapter = ADAPTERS[source["connector"]](
        source, transport=fixture_transport(list(fixture["native_pages"])),
        secret="fixture-credential" if dict(source.get("auth") or {}).get("kind") != "none" else None,
    )
    operation = min(source["operations"])
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": operation, "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
