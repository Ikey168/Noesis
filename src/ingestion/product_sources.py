"""Bounded native Open Icecat and EPREL acquisition for the Products pack, and safety notices.

The second half of this module holds the Products ``safety`` feature's notice
connectors (EU Safety Gate, CPSC, NHTSA, RASFF; ``noesis-product-safety-notice-v1``),
see the section "Product safety notices and recalls" below.

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
PRODUCT_CONNECTORS = frozenset({"icecat", "eprel"})  # display models; notice connectors: SAFETY_CONNECTORS
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


FIXTURE_SECRET = "fixture-credential"
ADAPTERS: dict[str, Any] = {"icecat": IcecatProductAdapter, "eprel": EprelProductAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored native envelopes keyed by the selector (products) or request (safety notices) they answer."""

    if pages and all("request" in page for page in pages):
        return safety_fixture_transport(pages)
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


# ======================================================================
# Product safety notices and recalls (Products ``safety`` feature, #1916)
# ======================================================================
#
# Four native connectors read explicitly selected notices from the sources
# audited in R01 (``docs/roadmaps/products-safety-source-audit.md``): EU Safety
# Gate alerts (R03, #1959), CPSC recalls and NHTSA recall campaigns (R04,
# #1972) and RASFF notifications (R05, #1979). Each page answers one selector
# of a bounded selection (1-50) and yields ``noesis-product-safety-notice-v1``
# statements: what one authority published about one notice, verbatim, with
# JSON-pointer locators into the notice object. Nothing here matches products,
# assesses risk or gives advice. Fetch-time values never enter a statement, so
# re-acquiring an unchanged notice yields an identical statement.

SAFETY_CONTRACT = "noesis-product-safety-notice-v1"
SAFETY_CONNECTORS = ("safety-gate", "cpsc", "nhtsa", "rasff")
NOTICE_TYPE_VALUES = (
    "alert",
    "recall",
    "warning",
    "withdrawal",
    "information",
    "border_rejection",
    "unknown",
)
SAFETY_FAILURE_CODES = (
    "authentication_failed",
    "rate_limited",
    "source_unavailable",
    "schema_drift",
    "response_too_large",
)
# provider -> (declared issuing authority, authority code, jurisdiction)
SAFETY_AUTHORITIES = {
    "safety-gate": ("European Commission (Safety Gate)", "eu-safety-gate", "EU"),
    "cpsc": ("U.S. Consumer Product Safety Commission", "us-cpsc", "US"),
    "nhtsa": ("National Highway Traffic Safety Administration", "us-nhtsa", "US"),
    "rasff": ("European Commission (RASFF)", "eu-rasff", "EU"),
}
# Provider-declared notice types -> the shared enumeration; anything else is ``unknown``.
_NOTICE_TYPES = {
    "safety-gate": {
        "alert notification": "alert",
        "notification for information": "information",
        "information notification": "information",
    },
    "cpsc": {"recall": "recall"},
    "nhtsa": {"recall": "recall"},
    "rasff": {
        "alert notification": "alert",
        "information notification for attention": "information",
        "information notification for follow-up": "information",
        "border rejection notification": "border_rejection",
        "news": "information",
    },
}
SAFETY_ID_PATTERNS = {
    "alert_number": re.compile(r"^[A-Z]{1,3}/\d{4,5}/\d{2}$"),
    "week": re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$"),
    "recall_number": re.compile(r"^\d{5}$"),
    "campaign_number": re.compile(r"^\d{2}[VETCIX]\d{6}$"),
    "notification_reference": re.compile(r"^\d{4}\.\d{4,5}$"),
}
_SAFETY_SELECTORS = {
    "safety-gate": ({"alert_number"}, {"week", "category"}),
    "cpsc": (
        {"recall_number"},
        {"manufacturer", "recall_date_start", "recall_date_end"},
    ),
    "nhtsa": ({"campaign_number"},),
    "rasff": ({"notification_reference"},),
}
MAX_WINDOW_DAYS = 366

# R01 access decisions (#1935). ``verify`` lists what must be checked against the
# live terms and a published response before a dated live run (R12, #2033).
SAFETY_PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "safety-gate": {
        "publisher": "European Commission (Safety Gate, ex RAPEX)",
        "documentation": "https://ec.europa.eu/safety-gate-alerts/screen/webReport",
        "access": "JSON alert view by alert number and weekly report by ISO week on the Safety Gate portal",
        "authentication": "none",
        "rate_limits": "not documented; one request per selector",
        "pagination": "one alert or one weekly report per page; a report over the result budget is refused",
        "identifiers": ["alert number (SR/00417/26 shape)", "notifying country"],
        "revision_semantics": "lastUpdateDate else publicationDate; follow-ups dated; earlier versions not "
        "retrievable, so each distinct payload observed is kept as a revision",
        "cadence": "weekly report plus continuous updates",
        "publishes": [
            "hazard",
            "risk level",
            "brand",
            "model",
            "batch",
            "barcode",
            "measures by whom",
            "notifying country",
            "publication and update dates",
        ],
        "terms_url": "https://ec.europa.eu/info/legal-notice_en",
        "verify": [
            "portal JSON paths and field names",
            "whether the portal JSON is a supported interface",
            "alert-number prefixes",
            "reuse notice wording",
        ],
        "status": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "cpsc": {
        "publisher": "U.S. Consumer Product Safety Commission",
        "documentation": "https://www.cpsc.gov/Recalls/CPSC-Recalls-Application-Program-Interface-API-Information",
        "access": "GET https://www.saferproducts.gov/RestWebServices/Recall?format=json with RecallNumber, or "
        "Manufacturer + RecallDateStart/RecallDateEnd",
        "authentication": "none",
        "rate_limits": "not documented",
        "pagination": "none; one JSON array per request",
        "identifiers": ["RecallNumber", "RecallID", "UPC"],
        "revision_semantics": "LastPublishDate; only the current version is served",
        "cadence": "continuous",
        "publishes": [
            "hazard",
            "injuries",
            "product names and models",
            "UPCs",
            "remedy and remedy options",
            "manufacturers, importers, distributors, retailers",
            "recall and publish dates",
        ],
        "terms_url": "https://www.cpsc.gov/About-CPSC/Agency-Reports/Privacy-and-FOIA",
        "verify": ["field casing", "rate limits", "attribution wording"],
        "status": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "nhtsa": {
        "publisher": "National Highway Traffic Safety Administration",
        "documentation": "https://www.nhtsa.gov/nhtsa-datasets-and-apis",
        "access": "GET https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=...; recallsByVehicle is a "
        "filtered view used for discovery only, never for acquisition",
        "authentication": "none",
        "rate_limits": "not documented",
        "pagination": "none; one campaign per request (one row per make/model/model year)",
        "identifiers": ["NHTSACampaignNumber", "NHTSAActionNumber"],
        "revision_semantics": "no update date; ReportReceivedDate (DD/MM/YYYY) only, so revisions are ordered by "
        "observation",
        "cadence": "continuous",
        "publishes": [
            "component",
            "summary",
            "consequence",
            "remedy",
            "make, model and model year",
            "manufacturer",
            "report received date",
        ],
        "terms_url": "https://www.nhtsa.gov/about-nhtsa/website-policies",
        "verify": [
            "ReportReceivedDate format",
            "response envelope (Count, Message, results)",
        ],
        "status": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "rasff": {
        "publisher": "European Commission (RASFF Window)",
        "documentation": "https://webgate.ec.europa.eu/rasff-window/screen/search",
        "access": "JSON notification view by reference on the RASFF Window",
        "authentication": "none",
        "rate_limits": "not documented",
        "pagination": "one notification per request",
        "identifiers": ["notification reference (2026.0457 shape)", "classification"],
        "revision_semantics": "lastUpdate else notificationDate; follow-ups dated; prior versions not retrievable",
        "cadence": "continuous",
        "publishes": [
            "hazard category and analytical result",
            "product category and name",
            "lots and best-before",
            "origin and distribution countries",
            "action taken",
            "risk decision",
            "notifying country",
        ],
        "terms_url": "https://ec.europa.eu/info/legal-notice_en",
        "verify": [
            "whether the RASFF Window JSON view is a supported public interface (else not-implemented)",
            "field names",
            "reuse notice wording",
        ],
        "status": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "baua": {
        "publisher": "Bundesanstalt für Arbeitsschutz und Arbeitsmedizin (BAuA)",
        "documentation": "https://www.baua.de/DE/Themen/Anwendungssichere-Chemikalien-und-Produkte/Produktsicherheit/"
        "Produktrueckrufe/",
        "access": "HTML pages only",
        "status": "not-implemented",
        "reason": "no documented API, feed or bulk export (verify whether an RSS feed exists); never scraped. "
        "German market-surveillance alerts reach Safety Gate, which is acquired instead",
        "verify": ["RSS or open-data availability"],
    },
    "gpsr": {
        "publisher": "Publications Office of the European Union (EUR-Lex / CELLAR)",
        "documentation": "https://eur-lex.europa.eu/eli/reg/2023/988",
        "access": "not a notice source: CELEX 32023R0988 and 32002R0178 are acquired through the legal-research "
        "CELLAR source cellar-product-safety-acts-eng and resolved by exact identifier",
        "status": "not-implemented",
        "reason": "the EUR-Lex HTML page is never fetched; the act lives in the Legal store",
        "verify": [],
    },
}


def safety_date(value: Any, *, day_first: bool = False) -> str | None:
    """A provider date as ISO ``YYYY-MM-DD`` (None when absent or unparseable); the raw text is kept beside it."""

    from datetime import date

    text = "" if value is None else str(value).strip()
    match = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?", text
    )
    if match:
        year, month, day = (int(part) for part in match.groups())
    else:
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if not match:
            return None
        first, second, year = (int(part) for part in match.groups())
        day, month = (first, second) if day_first else (second, first)
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def safety_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    connector = str(source.get("connector") or "")
    declared = dict(source.get("product_safety") or {})
    selection = declared.get("selection")
    if connector not in SAFETY_CONNECTORS:
        raise SourcePackError(
            "invalid_mapping", f"{connector!r} is not a product-safety connector"
        )
    if not isinstance(selection, list) or not 1 <= len(selection) <= MAX_SELECTION:
        raise SourcePackError(
            "unbounded_source",
            f"product-safety sources need an explicit selection of 1-{MAX_SELECTION} selectors",
        )
    shapes = _SAFETY_SELECTORS[connector]
    for item in selection:
        if not isinstance(item, Mapping) or (set(item) - {"label"}) not in shapes:
            raise SourcePackError(
                "invalid_mapping",
                f"{connector} selectors use "
                + " or ".join("+".join(sorted(s)) for s in shapes),
            )
        for key, pattern in SAFETY_ID_PATTERNS.items():
            if key in item and not pattern.fullmatch(str(item[key])):
                raise SourcePackError(
                    "invalid_mapping", f"{key} {item[key]!r} is not a valid identifier"
                )
        if "manufacturer" in item:
            start, end = (
                safety_date(item["recall_date_start"]),
                safety_date(item["recall_date_end"]),
            )
            if (
                not str(item["manufacturer"]).strip()
                or not start
                or not end
                or start > end
            ):
                raise SourcePackError(
                    "invalid_mapping",
                    "a manufacturer window needs a name and ISO start <= end",
                )
            from datetime import date

            if (
                date.fromisoformat(end) - date.fromisoformat(start)
            ).days > MAX_WINDOW_DAYS:
                raise SourcePackError(
                    "unbounded_source",
                    f"manufacturer windows are at most {MAX_WINDOW_DAYS} days",
                )
        if "category" in item and not str(item["category"]).strip():
            raise SourcePackError(
                "invalid_mapping", "a weekly-report selector pins a product category"
            )
    return {**declared, "selection": [dict(item) for item in selection]}


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (Mapping, list)):
        return None
    text = str(value).strip()
    return text or None


# Markers a provider uses for "not stated"; an identification field holding one is absent, never an identifier.
MISSING_MARKERS = frozenset(
    {"-", "--", "–", "n/a", "na", "n.a.", "not available", "unknown", "none", "null"}
)


def _ident(
    kind: str, value: Any, pointer: str, group: int, **extra: Any
) -> dict[str, Any] | None:
    text = _text(value)
    if text is None or text.casefold() in MISSING_MARKERS:
        return None
    item = {
        "kind": kind,
        "value": text,
        "group": group,
        "locator": {"json_pointer": pointer},
    }
    if kind == "gtin":
        item["gtin_state"] = gtin_state(text)["state"]
    item.update({k: v for k, v in extra.items() if v is not None})
    return item


def _split_field(
    kind: str, value: Any, pointer: str, group: int, separators: str
) -> list[dict[str, Any]]:
    """One identification per listed code; a multi-code field also keeps its verbatim text (never dropped)."""
    text = _text(value)
    if text is None:
        return []
    parts = [
        p.strip()
        for p in re.split(separators, text)
        if p.strip() and p.strip().casefold() not in MISSING_MARKERS
    ]
    if not parts:
        return []
    if parts == [text]:
        return [_ident(kind, text, pointer, group)]
    items = [_ident(f"{kind}_field", text, pointer, group)]
    items += [
        _ident(kind, part, pointer, group, part=index)
        for index, part in enumerate(parts)
    ]
    return items


def _as_objects(value: Any, key: str) -> list[Mapping[str, Any]]:
    """A provider list whose members may be objects or bare strings (or one object), as objects; nothing dropped."""
    items = value if isinstance(value, list) else [] if value is None else [value]
    return [
        item if isinstance(item, Mapping) else {key: item}
        for item in items
        if isinstance(item, Mapping) or _text(item)
    ]


def _enum(provider: str, raw: Any) -> dict[str, Any]:
    text = _text(raw)
    return {
        "declared": text,
        "value": _NOTICE_TYPES[provider].get((text or "").casefold(), "unknown"),
    }


def _country(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        code = _text(value.get("code"))
        return {
            "declared": _text(value.get("name")) or code,
            "code": code.upper()
            if code and re.fullmatch(r"[A-Za-z]{2}", code)
            else None,
        }
    return {"declared": _text(value), "code": None}


def _statement(provider: str, number: str, **fields: Any) -> dict[str, Any]:
    declared, code, jurisdiction = SAFETY_AUTHORITIES[provider]
    published, updated = fields.pop("published"), fields.pop("updated")
    statement = {
        "contract": SAFETY_CONTRACT,
        "provider": provider,
        "notice_number": number,
        "jurisdiction": jurisdiction,
        "issuing_authority": {"declared": declared, "value": code},
        "published": safety_date(published, day_first=fields.get("day_first", False)),
        "published_declared": _text(published),
        "updated": safety_date(updated, day_first=fields.get("day_first", False)),
        "updated_declared": _text(updated),
    }
    fields.pop("day_first", None)
    statement["revision_date"] = statement["updated"] or statement["published"]
    statement["order_basis"] = (
        "source-date"
        if statement["updated_declared"] or provider != "nhtsa"
        else "observation"
    )
    for key in (
        "identifications",
        "hazards",
        "corrective_actions",
        "parties",
        "followups",
        "compliance",
    ):
        statement[key] = [item for item in fields.pop(key, []) if item]
    statement.update(fields)
    return statement


def parse_safety_gate_alert(alert: Mapping[str, Any]) -> dict[str, Any]:
    number = _text(alert.get("reference"))
    if not number or not SAFETY_ID_PATTERNS["alert_number"].fullmatch(number):
        raise SourcePackError(
            "schema_drift", "Safety Gate alert lacks a valid reference"
        )
    product = alert.get("product")
    risk = alert.get("risk")
    if not isinstance(product, Mapping) or not isinstance(risk, Mapping):
        raise SourcePackError(
            "schema_drift", "Safety Gate alert lacks product or risk objects"
        )
    identifications = [
        _ident("brand", product.get("brand"), "/product/brand", 0),
        _ident("name", product.get("name"), "/product/name", 0),
        *_split_field(
            "model",
            product.get("typeNumberOfModel"),
            "/product/typeNumberOfModel",
            0,
            r"[,;]",
        ),
        *_split_field(
            "batch", product.get("batchNumber"), "/product/batchNumber", 0, r"[,;]"
        ),
        *_split_field(
            "gtin", product.get("barcode"), "/product/barcode", 0, r"[,;\s]+"
        ),
        _ident("description", product.get("description"), "/product/description", 0),
    ]
    types = risk.get("riskType")
    types = types if isinstance(types, list) else [types]
    hazards = [
        {
            "hazard_type": _text(kind),
            "risk_level": _text(risk.get("level")),
            "description": _text(risk.get("description")),
            "locator": {
                "json_pointer": f"/risk/riskType/{index}"
                if isinstance(risk.get("riskType"), list)
                else "/risk/riskType",
                "description_pointer": "/risk/description",
            },
        }
        for index, kind in enumerate(types)
        if _text(kind) or _text(risk.get("description"))
    ]
    actions = [
        {
            "text": _text(m.get("measureType")),
            "measure_type": _text(m.get("measureType")),
            "taken_by": _text(m.get("takenBy")),
            "category": _text(m.get("category")),
            "locator": {"json_pointer": f"/measures/{i}"},
        }
        for i, m in enumerate(_as_objects(alert.get("measures"), "measureType"))
        if isinstance(m, Mapping) and _text(m.get("measureType"))
    ]
    followups = [
        {
            "date": safety_date(f.get("date")),
            "date_declared": _text(f.get("date")),
            "country": _country(f.get("country")),
            "text": _text(f.get("text")),
            "measures": [
                _text(m.get("measureType"))
                for m in _as_objects(f.get("measures"), "measureType")
            ],
            "locator": {"json_pointer": f"/followUps/{i}"},
        }
        for i, f in enumerate(_as_objects(alert.get("followUps"), "text"))
        if isinstance(f, Mapping)
    ]
    compliance = (
        [
            {
                "field": "risk.compliance",
                "text": _text(risk.get("compliance")),
                "locator": {"json_pointer": "/risk/compliance"},
            }
        ]
        if _text(risk.get("compliance"))
        else []
    )
    return _statement(
        "safety-gate",
        number,
        published=alert.get("publicationDate"),
        updated=alert.get("lastUpdateDate"),
        notice_type=_enum("safety-gate", alert.get("notificationType")),
        notifying_country=_country(alert.get("notifyingCountry")),
        title=_text(product.get("name")),
        category={"declared": _text(product.get("category"))},
        origin=[_country(product.get("countryOfOrigin"))]
        if product.get("countryOfOrigin")
        else [],
        identifications=identifications,
        hazards=hazards,
        corrective_actions=actions,
        parties=[],
        followups=followups,
        compliance=compliance,
        flags={"counterfeit": product.get("counterfeit")}
        if "counterfeit" in product
        else {},
        url=_text(alert.get("url")),
    )


def parse_cpsc_recall(recall: Mapping[str, Any]) -> dict[str, Any]:
    number = _text(recall.get("RecallNumber"))
    if not number or not SAFETY_ID_PATTERNS["recall_number"].fullmatch(number):
        raise SourcePackError(
            "schema_drift", "CPSC recall lacks a five-digit RecallNumber"
        )
    identifications: list[Any] = []
    for i, item in enumerate(_as_objects(recall.get("Products"), "Name")):
        if not isinstance(item, Mapping):
            continue
        identifications += [
            _ident("name", item.get("Name"), f"/Products/{i}/Name", i),
            *_split_field(
                "model", item.get("Model"), f"/Products/{i}/Model", i, r"[,;]"
            ),
            _ident(
                "description", item.get("Description"), f"/Products/{i}/Description", i
            ),
            _ident("product_type", item.get("Type"), f"/Products/{i}/Type", i),
            _ident(
                "units", item.get("NumberOfUnits"), f"/Products/{i}/NumberOfUnits", i
            ),
        ]
    # UPCs are published per recall, not per product: group -1 is notice-level.
    for i, item in enumerate(_as_objects(recall.get("ProductUPCs"), "UPC")):
        if isinstance(item, Mapping):
            identifications.append(
                _ident("gtin", item.get("UPC"), f"/ProductUPCs/{i}/UPC", -1)
            )
    hazards = [
        {
            "hazard_type": _text(h.get("HazardType")),
            "risk_level": None,
            "description": _text(h.get("Name")),
            "locator": {"json_pointer": f"/Hazards/{i}"},
        }
        for i, h in enumerate(_as_objects(recall.get("Hazards"), "Name"))
        if isinstance(h, Mapping) and _text(h.get("Name"))
    ]
    hazards += [
        {
            "hazard_type": "injury-report",
            "risk_level": None,
            "description": _text(h.get("Name")),
            "locator": {"json_pointer": f"/Injuries/{i}"},
        }
        for i, h in enumerate(_as_objects(recall.get("Injuries"), "Name"))
        if isinstance(h, Mapping) and _text(h.get("Name"))
    ]
    options = [
        _text(o.get("Option"))
        for o in _as_objects(recall.get("RemedyOptions"), "Option")
        if isinstance(o, Mapping)
    ]
    actions = [
        {
            "text": _text(r.get("Name")),
            "measure_type": None,
            "taken_by": None,
            "remedy_type": sorted({o for o in options if o}),
            "locator": {"json_pointer": f"/Remedies/{i}"},
        }
        for i, r in enumerate(_as_objects(recall.get("Remedies"), "Name"))
        if isinstance(r, Mapping) and _text(r.get("Name"))
    ]
    parties = [
        {
            "role": role,
            "name": _text(p.get("Name")),
            "locator": {"json_pointer": f"/{field}/{i}"},
        }
        for field, role in (
            ("Manufacturers", "manufacturer"),
            ("Importers", "importer"),
            ("Distributors", "distributor"),
            ("Retailers", "retailer"),
        )
        for i, p in enumerate(_as_objects(recall.get(field), "Name"))
        if isinstance(p, Mapping) and _text(p.get("Name"))
    ]
    return _statement(
        "cpsc",
        number,
        published=recall.get("RecallDate"),
        updated=recall.get("LastPublishDate"),
        native_record_id=_text(recall.get("RecallID")),
        notice_type={"declared": "recall", "value": "recall"},
        notifying_country={"declared": "United States", "code": "US"},
        title=_text(recall.get("Title")),
        category={"declared": None},
        origin=[
            {"declared": _text(c.get("Country")), "code": None}
            for c in recall.get("ManufacturerCountries") or []
            if isinstance(c, Mapping) and _text(c.get("Country"))
        ],
        identifications=identifications,
        hazards=hazards,
        corrective_actions=actions,
        parties=parties,
        followups=[],
        compliance=[],
        consumer_contact=_text(recall.get("ConsumerContact")),
        url=_text(recall.get("URL")),
    )


def parse_nhtsa_campaign(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows or not all(isinstance(r, Mapping) for r in rows):
        raise SourcePackError("schema_drift", "NHTSA campaign has no result rows")
    numbers = {_text(r.get("NHTSACampaignNumber")) for r in rows}
    number = next(iter(numbers)) if len(numbers) == 1 else None
    if not number or not SAFETY_ID_PATTERNS["campaign_number"].fullmatch(number):
        raise SourcePackError(
            "schema_drift", "NHTSA rows lack one valid NHTSACampaignNumber"
        )
    first = rows[0]
    identifications: list[Any] = []
    for i, row in enumerate(rows):
        identifications += [
            _ident("brand", row.get("Make"), f"/results/{i}/Make", i, basis="make"),
            _ident("model", row.get("Model"), f"/results/{i}/Model", i),
            _ident("model_year", row.get("ModelYear"), f"/results/{i}/ModelYear", i),
            _ident("component", row.get("Component"), f"/results/{i}/Component", i),
        ]
    hazards = (
        [
            {
                "hazard_type": _text(first.get("Component")),
                "risk_level": None,
                "description": _text(first.get("Consequence")),
                "summary": _text(first.get("Summary")),
                "locator": {
                    "json_pointer": "/results/0/Consequence",
                    "summary_pointer": "/results/0/Summary",
                },
            }
        ]
        if any(_text(first.get(k)) for k in ("Component", "Consequence", "Summary"))
        else []
    )
    actions = (
        [
            {
                "text": _text(first.get("Remedy")),
                "measure_type": None,
                "taken_by": None,
                "remedy_type": sorted(
                    k
                    for k in ("parkIt", "parkOutSide", "overTheAirUpdate")
                    if first.get(k) is True
                ),
                "locator": {"json_pointer": "/results/0/Remedy"},
            }
        ]
        if _text(first.get("Remedy"))
        else []
    )
    parties = (
        [
            {
                "role": "manufacturer",
                "name": _text(first.get("Manufacturer")),
                "locator": {"json_pointer": "/results/0/Manufacturer"},
            }
        ]
        if _text(first.get("Manufacturer"))
        else []
    )
    return _statement(
        "nhtsa",
        number,
        published=first.get("ReportReceivedDate"),
        updated=None,
        day_first=True,
        native_record_id=_text(first.get("NHTSAActionNumber")),
        notice_type={"declared": "recall", "value": "recall"},
        notifying_country={"declared": "United States", "code": "US"},
        title=f"{_text(first.get('Make')) or ''} {_text(first.get('Component')) or ''}".strip()
        or None,
        category={"declared": "motor vehicles and equipment"},
        origin=[],
        identifications=identifications,
        hazards=hazards,
        corrective_actions=actions,
        parties=parties,
        followups=[],
        compliance=[],
        notes=_text(first.get("Notes")),
        url=f"https://www.nhtsa.gov/recalls?nhtsaId={number}",
    )


def parse_rasff_notification(item: Mapping[str, Any]) -> dict[str, Any]:
    number = _text(item.get("reference"))
    if not number or not SAFETY_ID_PATTERNS["notification_reference"].fullmatch(number):
        raise SourcePackError(
            "schema_drift", "RASFF notification lacks a valid reference"
        )
    product = item.get("product")
    if not isinstance(product, Mapping):
        raise SourcePackError(
            "schema_drift", "RASFF notification lacks a product object"
        )
    identifications = [
        _ident("brand", product.get("brand"), "/product/brand", 0),
        _ident("name", product.get("name"), "/product/name", 0),
        _ident("description", product.get("description"), "/product/description", 0),
    ]
    for i, batch in enumerate(_as_objects(product.get("batches"), "lot")):
        if isinstance(batch, Mapping):
            identifications += [
                _ident("batch", batch.get("lot"), f"/product/batches/{i}/lot", 0),
                _ident(
                    "best_before",
                    batch.get("bestBefore"),
                    f"/product/batches/{i}/bestBefore",
                    0,
                ),
            ]
    hazards = [
        {
            "hazard_type": _text(h.get("category")),
            "risk_level": _text(item.get("riskDecision")),
            "description": _text(h.get("name")),
            "analytical_result": _text(h.get("analyticalResult")),
            "locator": {"json_pointer": f"/hazards/{i}"},
        }
        for i, h in enumerate(_as_objects(item.get("hazards"), "name"))
        if isinstance(h, Mapping)
    ]
    actions = (
        [
            {
                "text": _text(item.get("actionTaken")),
                "measure_type": _text(item.get("actionTaken")),
                "taken_by": None,
                "distribution_status": _text(item.get("distributionStatus")),
                "locator": {"json_pointer": "/actionTaken"},
            }
        ]
        if _text(item.get("actionTaken"))
        else []
    )
    followups = [
        {
            "date": safety_date(f.get("date")),
            "date_declared": _text(f.get("date")),
            "country": _country(f.get("country")),
            "text": _text(f.get("text")),
            "measures": [m for m in [_text(f.get("actionTaken"))] if m],
            "locator": {"json_pointer": f"/followUps/{i}"},
        }
        for i, f in enumerate(_as_objects(item.get("followUps"), "text"))
        if isinstance(f, Mapping)
    ]
    return _statement(
        "rasff",
        number,
        published=item.get("notificationDate"),
        updated=item.get("lastUpdate"),
        notice_type=_enum("rasff", item.get("classification")),
        notifying_country=_country(item.get("notifyingCountry")),
        title=_text(item.get("subject")),
        category={"declared": _text(product.get("category"))},
        origin=[_country(c) for c in item.get("origin") or []],
        distribution=[_country(c) for c in item.get("distribution") or []],
        identifications=identifications,
        hazards=hazards,
        corrective_actions=actions,
        parties=[],
        followups=followups,
        compliance=[],
        url=_text(item.get("url")),
    )


class SafetyNoticeAdapter:
    """Fetch one selector of a bounded product-safety selection per page and emit its notice statements."""

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
        self.provider = str(self.source["connector"])
        self.declared = safety_declaration(self.source)
        if transport is None:
            from functools import partial

            # The runtime's default transport: same-host public redirects only, a byte ceiling, the source timeout.
            transport = partial(
                HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
            )
        self.transport = transport
        del secret  # every audited notice source is keyless; nothing is sent
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
            "product_safety": {
                "provider": self.provider,
                "selection_size": len(self.declared["selection"]),
                "record_contract": SAFETY_CONTRACT,
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def request(self, selector: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        base = self.source["endpoint"].rstrip("/")
        if self.provider == "safety-gate":
            if "alert_number" in selector:
                return f"{base}/alerts", {"reference": selector["alert_number"]}
            year, week = str(selector["week"]).split("-W")
            return f"{base}/weekly-reports/{year}/{int(week)}", {}
        if self.provider == "cpsc":
            if "recall_number" in selector:
                return base, {
                    "format": "json",
                    "RecallNumber": selector["recall_number"],
                }
            return base, {
                "format": "json",
                "Manufacturer": selector["manufacturer"],
                "RecallDateStart": safety_date(selector["recall_date_start"]),
                "RecallDateEnd": safety_date(selector["recall_date_end"]),
            }
        if self.provider == "nhtsa":
            return base, {"campaignNumber": selector["campaign_number"]}
        return base, {"reference": selector["notification_reference"]}

    def _get(self, url: str, params: Mapping[str, Any]) -> tuple[int, bytes]:
        from urllib.parse import urlsplit

        from src.ingestion.source_pack_runtime import _retry_after_ms

        response = self.transport(
            url=url,
            params=dict(params),
            headers={"Accept": "application/json"},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        if (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold() != (urlsplit(url).hostname or "").casefold():
            raise SourcePackError(
                "network_policy", "notice source was served from another host"
            )
        status = int(response.get("status", 200))
        headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "source response exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"source refused the request (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"source returned HTTP {status}"
            )
        if status >= 400 and status != 404:
            raise SourcePackError("schema_drift", f"source returned HTTP {status}")
        return status, raw

    def _statements(
        self, payload: Any, selector: Mapping[str, Any]
    ) -> tuple[list[tuple[str, dict]], int]:
        """(payload pointer, statement) pairs plus the count filtered out by the pinned category."""
        if self.provider == "safety-gate":
            if "alert_number" in selector:
                alert = payload.get("alert") if isinstance(payload, Mapping) else None
                if not isinstance(alert, Mapping):
                    raise SourcePackError(
                        "schema_drift", "Safety Gate response lacks an alert object"
                    )
                statement = parse_safety_gate_alert(alert)
                if statement["notice_number"] != selector["alert_number"]:
                    raise SourcePackError(
                        "schema_drift", "Safety Gate answered another alert number"
                    )
                return [("/alert", statement)], 0
            alerts = payload.get("alerts") if isinstance(payload, Mapping) else None
            if not isinstance(alerts, list):
                raise SourcePackError(
                    "schema_drift", "Safety Gate weekly report lacks an alerts list"
                )
            wanted = str(selector["category"]).casefold()
            kept = [
                (f"/alerts/{i}", parse_safety_gate_alert(a))
                for i, a in enumerate(alerts)
                if isinstance(a, Mapping)
                and str(dict(a.get("product") or {}).get("category") or "").casefold()
                == wanted
            ]
            return kept, len(alerts) - len(kept)
        if self.provider == "cpsc":
            if not isinstance(payload, list):
                raise SourcePackError(
                    "schema_drift", "CPSC response is not a JSON array"
                )
            pairs = [
                (f"/{i}", parse_cpsc_recall(r))
                for i, r in enumerate(payload)
                if isinstance(r, Mapping)
            ]
            if "recall_number" in selector and any(
                s["notice_number"] != selector["recall_number"] for _, s in pairs
            ):
                raise SourcePackError(
                    "schema_drift", "CPSC answered another recall number"
                )
            return pairs, 0
        if self.provider == "nhtsa":
            rows = payload.get("results") if isinstance(payload, Mapping) else None
            if not isinstance(rows, list):
                raise SourcePackError(
                    "schema_drift", "NHTSA response lacks a results list"
                )
            if not rows:
                return [], 0
            statement = parse_nhtsa_campaign(rows)
            if statement["notice_number"] != selector["campaign_number"]:
                raise SourcePackError("schema_drift", "NHTSA answered another campaign")
            return [("", statement)], 0
        notification = (
            payload.get("notification") if isinstance(payload, Mapping) else None
        )
        if not isinstance(notification, Mapping):
            raise SourcePackError(
                "schema_drift", "RASFF response lacks a notification object"
            )
        statement = parse_rasff_notification(notification)
        if statement["notice_number"] != selector["notification_reference"]:
            raise SourcePackError("schema_drift", "RASFF answered another notification")
        return [("/notification", statement)], 0

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        if (
            dict(request.get("parameters") or {})
            or request.get("from_ms") is not None
            or request.get("to_ms") is not None
        ):
            raise SourcePackError(
                "parameter_forbidden",
                "notice runs use the pinned selection, not ad-hoc parameters",
            )
        selection = self.declared["selection"]
        scope_hash = _digest(
            {"endpoint": self.source["endpoint"], "selection": selection}
        )
        try:
            state = (
                {"index": 0, "scope": scope_hash}
                if cursor is None
                else json.loads(cursor)
            )
            index = int(state["index"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SourcePackError(
                "cursor_drift", "notice cursor is not a valid checkpoint"
            ) from exc
        if state.get("scope") != scope_hash:
            raise SourcePackError(
                "cursor_drift", "notice cursor belongs to a different selection"
            )
        if index >= len(selection):
            return RuntimePage(
                (), None, 0, receipt={"status": 200, "selection_index": index}
            )
        selector = dict(selection[index])
        url, params = self.request(selector)
        status, raw = self._get(url, params)
        pairs, filtered = [], 0
        if status == 404:
            outcome = "not_found"
        else:
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise SourcePackError(
                    "schema_drift", "notice source returned a non-JSON body"
                ) from exc
            pairs, filtered = self._statements(payload, selector)
            outcome = "returned" if pairs else "not_found"
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(pairs) > limit:
            # Never a truncated page: raise the budget or narrow the selection.
            raise SourcePackError(
                "response_too_large",
                "page has more notices than the run's result budget",
            )
        records = [self._record(pointer, statement) for pointer, statement in pairs]
        next_cursor = (
            json.dumps({"index": index + 1, "scope": scope_hash}, sort_keys=True)
            if index + 1 < len(selection)
            else None
        )
        return RuntimePage(
            tuple(records),
            next_cursor,
            len(raw),
            receipt={
                "status": status,
                "selection_index": index,
                "selector": selector,
                "selection_size": len(selection),
                "scope_hash": scope_hash,
                "response_sha256": hashlib.sha256(raw).hexdigest(),
                "selector_outcome": outcome,
                "notices": len(records),
                "filtered_out_by_category": filtered,
                "final_page": next_cursor is None,
            },
        )

    def _record(self, pointer: str, statement: Mapping[str, Any]) -> dict[str, Any]:
        statement = {**statement, "payload_pointer": pointer}
        content = json.dumps(
            {k: v for k, v in statement.items() if k != "payload_pointer"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return {
            "id": f"{self.provider}:{statement['notice_number']}",
            "title": f"{SAFETY_AUTHORITIES[self.provider][0]} {statement['notice_number']}: "
            f"{statement.get('title') or 'notice'}",
            "url": statement.get("url") or self.source["endpoint"],
            "language": "en",
            **(
                {"published_at": statement["published"]}
                if statement.get("published")
                else {}
            ),
            **(
                {"updated_at": statement["revision_date"]}
                if statement.get("revision_date")
                else {}
            ),
            "content": content,
            "product_safety_notice": statement,
        }


ADAPTERS.update({connector: SafetyNoticeAdapter for connector in SAFETY_CONNECTORS})


def safety_fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored notice responses keyed by URL path plus the encoded query."""
    from urllib.parse import urlencode, urlsplit

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        key = urlsplit(url).path + ("?" + urlencode(params) if params else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = (
            b""
            if body is None
            else body.encode()
            if isinstance(body, str)
            else json.dumps(body, ensure_ascii=False).encode()
        )
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content,
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport
