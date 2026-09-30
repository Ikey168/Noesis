"""Food composition and labelling sources for the Products ``food`` feature (#2216, FC01 and FC03-FC05).

Companion of :mod:`src.ingestion.product_sources` (it reuses its GTIN check and
the runtime's same-host HTTPS transport). Three providers run as sources of the
``products-displays`` source pack (``config/source_packs/products.json``,
connector ``food-composition``, operation ``food``) through
:mod:`src.ingestion.source_pack_runtime`: licence acceptance, budgets,
receipts and checkpoints apply unchanged. Access decisions are recorded in
:data:`PROVIDER_CONTRACTS` / :data:`TABLE_DECISIONS` and
``docs/development/food-composition-evidence/source-audit.md``.

* **Open Food Facts** (``open-food-facts``, FC03) - one product per page by
  GTIN (optionally a pinned OFF revision number) from the product API. Every
  OFF revision is a label revision: ingredients text, allergen and trace tags,
  nutriments and label tags as published, with OFF's data-quality,
  completeness and state flags verbatim. OFF-computed scores (Nutri-Score,
  NOVA, Eco-Score / environmental score, nutrient levels) and images are
  **excluded** at acquisition and reported as dropped in the page receipt.
  Records are ``crowd-sourced`` and carry the ODbL attribution.
* **USDA FoodData Central** (``fooddata-central``, FC04) - one food per page by
  FDC ID with the ``NOESIS_FDC_API_KEY`` secret; nutrient numbers, units,
  derivation codes and the data type as published; Branded label nutrients
  (per serving) stay distinct from food nutrients. Records are ``reference``.
* **Composition tables** (``composition-table``, FC05) - only tables whose
  access decision is ``acquire`` (the ANSES Ciqual table); one selected edition
  per page, bounded to explicit native food codes, values and flags verbatim.
  Reference-only and excluded tables are documented decisions, never fetched.

Every provider is ``unverified-live`` until a dated live run (#2302); request
paths and field names marked *verify* are authored from public documentation
and fixtures are synthetic.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.food_composition import (
    CONTRACT,
    PROVENANCE,
    FoodCompositionError,
    attribution_for,
    decimal_text,
    gtin_key,
    normalize_unit,
    validate_statement,
)

CONNECTOR = "food-composition"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PROVIDERS = ("open-food-facts", "fooddata-central", "composition-table")
MAX_SELECTION = 50
PROVIDER_HOSTS = {
    "open-food-facts": ("world.openfoodfacts.org",),
    "fooddata-central": ("api.nal.usda.gov",),
    "composition-table": ("ciqual.anses.fr",),
}
USER_AGENT = "Noesis-food-composition/1.0 (+https://github.com/Ikey168/Noesis)"

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "open-food-facts": {
        "publisher": "Open Food Facts (association Open Food Facts, France)",
        "documentation": "https://openfoodfacts.github.io/openfoodfacts-server/api/",
        "access": "product API v2, GET /api/v2/product/{barcode}.json?fields=...; a pinned revision adds rev=N "
        "(verify that rev is honoured by the v2 endpoint). The daily JSONL/Parquet dumps are the bulk alternative "
        "and are not used: a bounded GTIN list is read one product per request",
        "authentication": "none for reads; a descriptive User-Agent (app name, version, contact) is required",
        "rate_limits": "100 product reads per minute per IP, 10 searches per minute (search is never used)",
        "licence": "database: Open Database License (ODbL) 1.0; individual contents: Database Contents License "
        "(DbCL) 1.0; product images: CC BY-SA (never mirrored)",
        "terms_url": "https://world.openfoodfacts.org/terms-of-use",
        "attribution": "Contains information from Open Food Facts, made available under the ODbL v1.0",
        "obligations": [
            "attribution on every record, answer and export (carried as the record attribution)",
            "share-alike: a publicly used database derived from OFF data is offered under the ODbL",
            "keep OFF-derived records separable: own provider rows, crowd-sourced class, never merged with "
            "reference values",
            "no images mirrored (CC BY-SA)",
        ],
        "revision_semantics": "product rev (integer, increments on every edit) plus last_modified_t; each OFF "
        "revision observed is a label revision; earlier revisions are only as retrievable as the rev parameter "
        "allows (verify)",
        "excluded": ["nutriscore_* and nutrition_grade*", "nova_group*", "ecoscore_* / environmental_score_*",
                     "nutrient_levels*", "nutrition-score-* nutriments", "*-estimate-from-ingredients nutriments",
                     "image_* and selected_images"],
        "provenance_class": "crowd-sourced",
        "status": "unverified-live",
    },
    "fooddata-central": {
        "publisher": "U.S. Department of Agriculture, Agricultural Research Service (FoodData Central)",
        "documentation": "https://fdc.nal.usda.gov/api-guide",
        "access": "GET https://api.nal.usda.gov/fdc/v1/food/{fdcId}?format=full with the API key in the X-Api-Key "
        "header (api.data.gov)",
        "authentication": "data.gov API key held as the NOESIS_FDC_API_KEY secret reference; DEMO_KEY is never used",
        "rate_limits": "1,000 requests per hour per key by default (api.data.gov); exceeding it returns HTTP 429 "
        "and blocks the key for an hour",
        "data_types": {
            "Branded": "Global Branded Food Products Database: label data submitted by brand owners, with gtinUpc; "
            "foodNutrients are per 100 g calculated from the label, labelNutrients per serving",
            "Foundation": "analytical values with derivation codes and data points, per 100 g",
            "SR Legacy": "Standard Reference Legacy (final release 2018), per 100 g",
            "Survey": "FNDDS survey foods (WWEIA), per 100 g",
        },
        "licence": "public domain; published under CC0 1.0; citation requested",
        "terms_url": "https://fdc.nal.usda.gov/api-guide",
        "attribution": "U.S. Department of Agriculture, Agricultural Research Service. FoodData Central.",
        "revision_semantics": "publicationDate (M/D/YYYY) per food; a newer publication of the same FDC ID is a new "
        "revision; FDC may issue a new FDC ID for an updated Branded food, which is related by GTIN through "
        "reviewable identity, never merged",
        "provenance_class": "reference",
        "status": "unverified-live",
    },
    "composition-table": {
        "publisher": "national food composition compilers (see TABLE_DECISIONS)",
        "access": "only tables with decision acquire; one pinned edition archive per selection",
        "provenance_class": "reference",
        "status": "unverified-live",
    },
}
# FC01/FC05 access decisions per composition table: acquire, reference-only or excluded, with the reason.
TABLE_DECISIONS: dict[str, dict[str, Any]] = {
    "ciqual": {
        "name": "Ciqual French food composition table",
        "publisher": "Anses (French Agency for Food, Environmental and Occupational Health & Safety)",
        "decision": "acquire",
        "documentation": "https://ciqual.anses.fr/#/cms/download/node/20",
        "access": "edition archive (zip of XML files alim_*, const_*, compo_*) on ciqual.anses.fr",
        "licence": "Licence Ouverte / Etalab 2.0 (verify the edition's own notice)",
        "reason": "open licence permitting reuse with attribution, machine-readable XML, stable food and "
        "constituent codes and explicit editions",
        "verify": ["archive URL of the current edition", "XML element names", "INFOODS code element name"],
    },
    "efsa": {
        "name": "EFSA food composition data (nutrient composition database for dietary exposure)",
        "publisher": "European Food Safety Authority",
        "decision": "reference-only",
        "documentation": "https://www.efsa.europa.eu/en/data-report/food-composition-data",
        "reason": "a compilation of national tables with borrowed and harmonised values published as workbooks; "
        "acquiring it would present harmonised values beside (and duplicating) the national sources, which FC05 "
        "forbids. Cited as a documented decision, not stored as data",
        "citation": "EFSA (European Food Safety Authority). Food composition data. "
        "https://www.efsa.europa.eu/en/data-report/food-composition-data",
    },
    "bls": {
        "name": "Bundeslebensmittelschlüssel (BLS)",
        "publisher": "Max Rubner-Institut",
        "decision": "reference-only",
        "documentation": "https://www.blsdb.de/",
        "reason": "historical editions (BLS 3.x) are licensed per user and may not be redistributed; the reuse terms "
        "of the current edition must be verified before any acquisition",
        "citation": "Max Rubner-Institut. Bundeslebensmittelschlüssel (BLS). https://www.blsdb.de/",
    },
    "cofid": {
        "name": "McCance and Widdowson's Composition of Foods Integrated Dataset (CoFID)",
        "publisher": "UK Health Security Agency / Office for Health Improvement and Disparities",
        "decision": "reference-only",
        "documentation": "https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid",
        "reason": "Open Government Licence permits reuse, but the dataset is a single Excel workbook without stable "
        "per-food access; deferred to an operator import rather than bounded acquisition in this coverage",
        "citation": "Public Health England. McCance and Widdowson's The Composition of Foods Integrated Dataset "
        "2021. https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid",
    },
    "frida": {
        "name": "Frida Food Data (Denmark)",
        "publisher": "DTU National Food Institute",
        "decision": "excluded",
        "reason": "outside the bounded coverage (one EU national table is selected); no market in scope",
    },
}
LIVE_VERIFICATION = {
    "open-food-facts": {"status": "unverified-live", "intended": "live-verified",
                        "note": "fixture-verified parser; no dated live run from this runtime (#2302)"},
    "fooddata-central": {"status": "unverified-live", "intended": "live-verified",
                         "note": "fixture-verified parser; needs NOESIS_FDC_API_KEY for a dated live run (#2302)"},
    "composition-table:ciqual": {"status": "unverified-live", "intended": "live-verified",
                                 "note": "fixture-verified XML parser; archive URL to verify (#2302)"},
    "composition-table:efsa": {"status": "not-implemented", "intended": "not-implemented",
                               "note": "reference-only access decision"},
    "composition-table:bls": {"status": "not-implemented", "intended": "not-implemented",
                              "note": "reference-only access decision"},
    "composition-table:cofid": {"status": "not-implemented", "intended": "not-implemented",
                                "note": "reference-only access decision"},
    "composition-table:frida": {"status": "not-implemented", "intended": "not-implemented",
                                "note": "excluded"},
}
# FC01 bounded coverage: the GTIN list and the generic-food list (synthetic fixtures until #2302).
BOUNDED_COVERAGE = {
    "markets": ["EU (Open Food Facts world database, products sold in DE/FR/NL)", "US (FDC Branded)"],
    "gtins": ["4000000000105", "4000000000150", "0071000000208 (UPC-A 071000000208 in FDC Branded)",
              "4000000000204 (not in Open Food Facts: not_found)"],
    "generic_foods": {
        "fooddata-central": ["Foundation 9990201 (apple, raw, with skin)", "SR Legacy 9990301 (oats)"],
        "ciqual": ["13039 (apple, raw, with skin)", "9310 (oat flakes)"],
    },
    "food_groups": ["meat products (frozen)", "dairy (yoghurt)", "cereal bars", "fruit", "cereals"],
    "provenance_rule": "crowd-sourced (OFF) and reference (FDC, composition tables) are distinct provenance "
    "classes and are never merged into one value; differences are shown, never reconciled",
}

OFF_FIELDS = (
    "code,rev,last_modified_t,product_name,product_name_en,generic_name,brands,quantity,lc,categories_tags,"
    "ingredients_text,ingredients_text_en,ingredients_text_fr,ingredients_text_de,allergens_tags,traces_tags,"
    "nutriments,nutrition_data_per,serving_size,labels,labels_tags,data_quality_tags,data_quality_warnings_tags,"
    "data_quality_errors_tags,completeness,states_tags"
)
_OFF_EXCLUDED = re.compile(
    r"^(nutriscore|nutrition_grade|nutrition_score|nova|ecoscore|environmental_score|nutrient_levels|image|"
    r"selected_images)"
)
_OFF_EXCLUDED_NUTRIMENTS = re.compile(r"^(nutrition-score|nova|.*-estimate-from-ingredients|carbon-footprint)")
_OFF_KCAL = re.compile(r"^energy-kcal$")
_OFF_KJ = re.compile(r"^(energy|energy-kj)$")


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (Mapping, list)):
        return None
    text = str(value).strip()
    return text or None


def _pointer(*parts: Any) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                          ).hexdigest()


# ------------------------------------------------------------------ selections


def selection_entries(source: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    declared = dict(source.get("food_composition") or {})
    provider = str(declared.get("provider") or "")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_mapping", f"food-composition sources declare a provider in {PROVIDERS}")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_mapping", f"{provider} is fetched from {PROVIDER_HOSTS[provider][0]} only")
    entries = [dict(e) for e in declared.get("selection") or []]
    if not 1 <= len(entries) <= MAX_SELECTION:
        raise SourcePackError("unbounded_source",
                              f"food-composition sources need an explicit selection of 1-{MAX_SELECTION} entries")
    for entry in entries:
        keys = set(entry) - {"label"}
        if provider == "open-food-facts":
            if keys not in ({"gtin"}, {"gtin", "rev"}) or gtin_key(entry["gtin"]) is None:
                raise SourcePackError("invalid_mapping", "Open Food Facts selectors name a valid GTIN (and a rev)")
            if "rev" in entry and not str(entry["rev"]).isdigit():
                raise SourcePackError("invalid_mapping", "an Open Food Facts rev is a positive integer")
        elif provider == "fooddata-central":
            if keys != {"fdc_id"} or not str(entry["fdc_id"]).isdigit():
                raise SourcePackError("invalid_mapping", "FoodData Central selectors name one numeric fdc_id")
        else:
            if keys != {"table", "edition", "edition_date", "archive", "food_codes"}:
                raise SourcePackError("invalid_mapping", "table selectors name table, edition, edition_date, "
                                                         "archive and food_codes")
            decision = TABLE_DECISIONS.get(str(entry["table"]))
            if decision is None or decision["decision"] != "acquire":
                raise SourcePackError(
                    "access_decision",
                    f"table {entry['table']!r} has no acquire decision (FC01); it is documented, never fetched")
            codes = entry["food_codes"]
            if not isinstance(codes, list) or not 1 <= len(codes) <= MAX_SELECTION:
                raise SourcePackError("unbounded_source", "a table selection names 1-50 native food codes")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry["edition_date"])):
                raise SourcePackError("invalid_mapping", "edition_date is an ISO date")
    return provider, entries


def request_for(provider: str, entry: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """(path, query) of one selected page, relative to the source endpoint."""
    if provider == "open-food-facts":
        query = {"fields": OFF_FIELDS}
        if "rev" in entry:
            query["rev"] = str(entry["rev"])
        return f"/api/v2/product/{quote(str(entry['gtin']))}.json", query
    if provider == "fooddata-central":
        return f"/fdc/v1/food/{int(entry['fdc_id'])}", {"format": "full"}
    return str(entry["archive"]), {}


# ------------------------------------------------------------------ parsers


def _statement(provider: str, provider_key: str, food_kind: str, *, revision: Mapping[str, Any],
               identifiers: Mapping[str, Any], names: Mapping[str, Any], ingredients: list, allergens: list,
               nutrients: list, claims: list, source_fields: Mapping[str, Any], url: str | None,
               table: str | None = None) -> dict[str, Any]:
    value = {
        "contract": CONTRACT, "provider": provider, "provider_key": provider_key, "food_kind": food_kind,
        "provenance_class": PROVENANCE[provider], "revision": dict(revision), "identifiers": dict(identifiers),
        "names": dict(names), "ingredient_statements": ingredients, "allergen_declarations": allergens,
        "nutrient_values": nutrients, "labelling_claims": claims, "source_fields": dict(source_fields),
        "attribution": attribution_for(provider, table), "url": url,
    }
    try:
        return validate_statement(value)
    except FoodCompositionError as exc:
        raise SourcePackError("schema_drift", exc.message) from exc


def _gtin(value: Any) -> dict[str, Any] | None:
    from src.ingestion.product_sources import gtin_state

    text = _text(value)
    if text is None:
        return None
    state = gtin_state(text)
    return {"value": text, "state": state["state"], "key": gtin_key(text)}


def off_dropped_fields(product: Mapping[str, Any]) -> list[str]:
    """Excluded OFF fields a response carries: never parsed, never stored, reported in the receipt."""
    dropped = [key for key in product if _OFF_EXCLUDED.match(str(key))]
    nutriments = product.get("nutriments") if isinstance(product.get("nutriments"), Mapping) else {}
    dropped += [f"nutriments/{key}" for key in nutriments if _OFF_EXCLUDED_NUTRIMENTS.match(str(key))]
    return sorted(dropped)


def _off_standard_unit(name: str) -> str:
    if _OFF_KCAL.match(name):
        return "kcal"
    if _OFF_KJ.match(name):
        return "kJ"
    if name == "alcohol":
        return "% vol"
    return "g"


def parse_off_product(body: Mapping[str, Any], url: str) -> dict[str, Any]:
    product = body.get("product")
    if not isinstance(product, Mapping):
        raise SourcePackError("schema_drift", "Open Food Facts response lacks a product object")
    code = _text(product.get("code") or body.get("code"))
    rev = product.get("rev")
    if code is None or rev is None or not str(rev).isdigit():
        raise SourcePackError("schema_drift", "Open Food Facts product lacks its code or revision number")
    modified = product.get("last_modified_t")
    date = (datetime.fromtimestamp(int(modified), tz=timezone.utc).date().isoformat()
            if str(modified or "").isdigit() else None)
    language = _text(product.get("lc"))
    ingredients = []
    for key in sorted(k for k in product if str(k) == "ingredients_text" or str(k).startswith("ingredients_text_")):
        text = _text(product.get(key))
        if text:
            lang = language if key == "ingredients_text" else key.rsplit("_", 1)[1]
            if key != "ingredients_text" and any(i["text"] == text and i["language"] == lang for i in ingredients):
                continue
            ingredients.append({"text": text, "language": lang, "locator": {"json_pointer": _pointer("product", key)}})
    allergens = [
        {"relation": relation, "value": str(tag), "declared_as": "tag",
         "locator": {"json_pointer": _pointer("product", key, i)}}
        for key, relation in (("allergens_tags", "contains"), ("traces_tags", "may_contain"))
        for i, tag in enumerate(product.get(key) or []) if _text(tag)
    ]
    nutriments = product.get("nutriments") if isinstance(product.get("nutriments"), Mapping) else {}
    names = sorted({re.sub(r"_(100g|serving)$", "", k) for k in nutriments if re.search(r"_(100g|serving)$", k)})
    nutrients = []
    for name in names:
        if _OFF_EXCLUDED_NUTRIMENTS.match(name):
            continue
        for suffix, basis in (("100g", "per 100 g"), ("serving", "per serving")):
            key = f"{name}_{suffix}"
            if key not in nutriments:
                continue
            raw = nutriments[key]
            flags = {k: nutriments[f"{name}_{k}"] for k in ("value", "unit") if f"{name}_{k}" in nutriments}
            if suffix == "serving" and product.get("serving_size"):
                flags["serving_size"] = product.get("serving_size")
            if product.get("nutrition_data_per"):
                flags["nutrition_data_per"] = product.get("nutrition_data_per")
            nutrients.append({
                "nutrient": {"scheme": "off-nutriment", "id": name, "name": name, "tagname": None},
                "amount": None if raw is None else str(raw), "amount_decimal": decimal_text(raw),
                "unit": normalize_unit(_off_standard_unit(name)), "basis": basis,
                "derivation": {"code": "off-standard-unit",
                               "description": "OFF publishes *_100g and *_serving in its standard unit; the "
                                              "contributor's entered value and unit are the value flags"},
                "value_kind": "off-nutriment", "value_flags": flags,
                "locator": {"json_pointer": _pointer("product", "nutriments", key)},
            })
    claims = [{"text": str(tag), "tag": str(tag), "locator": {"json_pointer": _pointer("product", "labels_tags", i)}}
              for i, tag in enumerate(product.get("labels_tags") or []) if _text(tag)]
    brands = [b.strip() for b in str(product.get("brands") or "").split(",") if b.strip()]
    return _statement(
        "open-food-facts", f"off:{code}", "food-product",
        revision={"value": str(rev), "basis": "off-revision", "date": date,
                  "declared": None if modified is None else str(modified)},
        identifiers={"gtin": _gtin(code), "off_code": code},
        names={"product_name": _text(product.get("product_name")) or _text(product.get("product_name_en")),
               "generic_name": _text(product.get("generic_name")), "brands": brands,
               "quantity": _text(product.get("quantity")), "language": language,
               "categories": list(product.get("categories_tags") or [])},
        ingredients=ingredients, allergens=allergens, nutrients=nutrients, claims=claims,
        source_fields={k: product.get(k) for k in ("data_quality_tags", "data_quality_warnings_tags",
                                                   "data_quality_errors_tags", "completeness", "states_tags",
                                                   "labels", "serving_size", "nutrition_data_per")
                       if k in product},
        url=f"https://world.openfoodfacts.org/product/{code}",
    )


def _fdc_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if match := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text):
        month, day, year = (int(p) for p in match.groups())
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]):
        return text[:10]
    return None


def parse_fdc_food(body: Mapping[str, Any], url: str) -> dict[str, Any]:
    fdc_id = body.get("fdcId")
    data_type = _text(body.get("dataType"))
    if fdc_id is None or data_type is None:
        raise SourcePackError("schema_drift", "FoodData Central food lacks fdcId or dataType")
    published = _text(body.get("publicationDate"))
    nutrients = []
    for i, item in enumerate(body.get("foodNutrients") or []):
        if not isinstance(item, Mapping):
            continue
        nutrient = item.get("nutrient") if isinstance(item.get("nutrient"), Mapping) else {}
        number = _text(nutrient.get("number"))
        if number is None:
            continue
        derivation = item.get("foodNutrientDerivation") if isinstance(item.get("foodNutrientDerivation"),
                                                                      Mapping) else None
        amount = item.get("amount")
        nutrients.append({
            "nutrient": {"scheme": "fdc-nutrient-number", "id": number, "name": _text(nutrient.get("name")),
                         "tagname": None},
            "amount": None if amount is None else str(amount), "amount_decimal": decimal_text(amount),
            "unit": normalize_unit(nutrient.get("unitName")), "basis": "per 100 g",
            "derivation": None if derivation is None else {"code": _text(derivation.get("code")),
                                                            "description": _text(derivation.get("description"))},
            "value_kind": "food-nutrient",
            "value_flags": {k: item[k] for k in ("dataPoints", "min", "max", "median") if k in item},
            "locator": {"json_pointer": _pointer("foodNutrients", i)},
        })
    serving = " ".join(str(body[k]) for k in ("servingSize", "servingSizeUnit") if body.get(k) is not None)
    for name, item in sorted((body.get("labelNutrients") or {}).items()):
        if not isinstance(item, Mapping):
            continue
        amount = item.get("value")
        nutrients.append({
            "nutrient": {"scheme": "fdc-label-nutrient", "id": str(name), "name": str(name), "tagname": None},
            "amount": None if amount is None else str(amount), "amount_decimal": decimal_text(amount),
            # labelNutrients publish no unit: it stays absent, never guessed.
            "unit": normalize_unit(item.get("unit")), "basis": f"per serving ({serving})" if serving else "per serving",
            "derivation": None, "value_kind": "label-nutrient", "value_flags": {},
            "locator": {"json_pointer": _pointer("labelNutrients", name)},
        })
    ingredients = ([{"text": _text(body["ingredients"]), "language": "en",
                     "locator": {"json_pointer": "/ingredients"}}] if _text(body.get("ingredients")) else [])
    branded = data_type == "Branded"
    category = body.get("brandedFoodCategory")
    if not category and isinstance(body.get("foodCategory"), Mapping):
        category = body["foodCategory"].get("description")
    return _statement(
        "fooddata-central", f"fdc:{fdc_id}", "food-product" if branded else "generic-food",
        revision={"value": published or "unknown", "basis": "fdc-publication-date", "date": _fdc_date(published),
                  "declared": published},
        identifiers={"fdc_id": str(fdc_id), "data_type": data_type, "gtin": _gtin(body.get("gtinUpc")),
                     "ndb_number": _text(body.get("ndbNumber")), "food_code": _text(body.get("foodCode"))},
        names={"description": _text(body.get("description")), "brand_owner": _text(body.get("brandOwner")),
               "brands": [b for b in [_text(body.get("brandName")), _text(body.get("brandOwner"))] if b],
               "product_name": _text(body.get("description")), "category": _text(category)},
        ingredients=ingredients, allergens=[], nutrients=nutrients, claims=[],
        source_fields={k: body.get(k) for k in ("dataType", "marketCountry", "availableDate", "modifiedDate",
                                                 "servingSize", "servingSizeUnit", "householdServingFullText")
                       if k in body},
        url=f"https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients",
    )


def _xml_rows(raw: str, tag: str) -> list[dict[str, str | None]]:
    try:
        root = ET.fromstring(raw.lstrip("﻿").strip())
    except ET.ParseError as exc:
        raise SourcePackError("schema_drift", f"table XML is not well formed: {exc}") from exc
    return [{child.tag: (child.text.strip() if child.text and child.text.strip() else None) for child in row}
            for row in root.iter(tag)]


_CIQUAL_UNIT = re.compile(r"\(\s*([^()/]+?)\s*/\s*100\s*g\s*\)\s*$")


def _ciqual_value_type(text: str | None) -> str:
    """The table's own value marker, classified verbatim: trace, missing, less-than, else as published."""
    if text is None or text.strip() in {"-", ""}:
        return "missing"
    folded = text.strip().casefold()
    if folded.startswith("trace"):
        return "trace"
    if folded.startswith("<"):
        return "less-than"
    return "as-published"


def parse_ciqual(files: Mapping[str, str], entry: Mapping[str, Any], url: str) -> list[dict[str, Any]]:
    def member(prefix: str) -> str:
        found = [name for name in files if name.rsplit("/", 1)[-1].startswith(prefix)]
        if len(found) != 1:
            raise SourcePackError("schema_drift", f"Ciqual archive lacks exactly one {prefix}* file")
        return files[found[0]]

    foods = {r.get("alim_code"): r for r in _xml_rows(member("alim_"), "ALIM")}
    constituents = {r.get("const_code"): r for r in _xml_rows(member("const_"), "CONST")}
    compo = _xml_rows(member("compo_"), "COMPO")
    wanted = [str(c) for c in entry["food_codes"]]
    statements = []
    for code in wanted:
        food = foods.get(code)
        if food is None:
            continue
        nutrients = []
        for i, row in enumerate(compo):
            if row.get("alim_code") != code:
                continue
            const = constituents.get(row.get("const_code")) or {}
            name = const.get("const_nom_eng") or const.get("const_nom_fr")
            unit_match = _CIQUAL_UNIT.search(name or "")
            teneur = row.get("teneur")
            nutrients.append({
                "nutrient": {"scheme": "ciqual-const-code", "id": str(row.get("const_code")), "name": name,
                             "tagname": const.get("code_INFOODS")},
                "amount": teneur,
                "amount_decimal": decimal_text(teneur) if _ciqual_value_type(teneur) == "as-published" else None,
                "unit": normalize_unit(unit_match.group(1) if unit_match else None),
                "basis": "per 100 g" if unit_match else None, "derivation": None, "value_kind": "table-value",
                "value_flags": {"teneur": teneur, "value_type": _ciqual_value_type(teneur),
                                **{k: row.get(k) for k in ("min", "max", "code_confiance", "source_code")
                                   if row.get(k) is not None}},
                "locator": {"json_pointer": _pointer("compo", i), "file": "compo"},
            })
        statements.append(_statement(
            "composition-table", f"ciqual:{code}", "generic-food",
            revision={"value": str(entry["edition"]), "basis": "table-edition", "date": entry["edition_date"],
                      "declared": str(entry["edition"])},
            identifiers={"table": "ciqual", "edition": str(entry["edition"]), "food_code": code,
                         "group_code": food.get("alim_grp_code"), "gtin": None},
            names={"description": food.get("alim_nom_eng") or food.get("alim_nom_fr"),
                   "description_fr": food.get("alim_nom_fr"), "product_name": food.get("alim_nom_eng"),
                   "brands": []},
            ingredients=[], allergens=[], nutrients=nutrients, claims=[],
            source_fields={"table": TABLE_DECISIONS["ciqual"]["name"], "edition": str(entry["edition"])},
            url=url, table="ciqual",
        ))
    return statements


# ------------------------------------------------------------------ runtime adapter


class FoodCompositionAdapter:
    """One page per selected GTIN, FDC ID or table edition on the runtime's default transport."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.provider, self.entries = selection_entries(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self._secret = secret
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "food_composition": {"provider": self.provider, "selection_size": len(self.entries),
                                 "record_contract": CONTRACT},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[int, bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        base = self.source["endpoint"].rstrip("/") + path
        headers = {"Accept": "application/json, application/zip, application/xml", "User-Agent": USER_AGENT}
        if self.provider == "fooddata-central":
            if not self._secret:
                raise SourcePackError("authentication_failed", "the NOESIS_FDC_API_KEY secret is not configured")
            headers["X-Api-Key"] = self._secret
        response = self.transport(url=base, params=dict(query), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(str(response.get("final_url") or base)).hostname or "").casefold() != host:
            raise SourcePackError("network_policy", "food source was served from another host")
        status = int(response.get("status", 200))
        folded = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(folded.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"source refused the request (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"source returned HTTP {status}")
        if status >= 400 and status != 404:
            raise SourcePackError("schema_drift", f"source returned HTTP {status}")
        return status, raw, base + ("?" + urlencode(sorted(query.items())) if query else "")

    def _parse(self, entry: Mapping[str, Any], raw: bytes, url: str) -> tuple[list[dict[str, Any]], list[str]]:
        if self.provider == "composition-table":
            files: dict[str, str] = {}
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    for name in archive.namelist():
                        if name.lower().endswith(".xml"):
                            files[name] = archive.read(name).decode("utf-8")
            except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
                raise SourcePackError("schema_drift", "table archive is not a zip of UTF-8 XML files") from exc
            return parse_ciqual(files, entry, url), []
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourcePackError("schema_drift", "food source returned a non-JSON body") from exc
        if not isinstance(payload, Mapping):
            raise SourcePackError("schema_drift", "food source returned a non-object body")
        if self.provider == "open-food-facts":
            if payload.get("status") in (0, "0") or payload.get("product") is None:
                return [], []
            statement = parse_off_product(payload, url)
            if gtin_key(statement["identifiers"]["off_code"]) != gtin_key(entry["gtin"]):
                raise SourcePackError("schema_drift", "Open Food Facts answered another barcode")
            if "rev" in entry and statement["revision"]["value"] != str(entry["rev"]):
                raise SourcePackError("schema_drift", "Open Food Facts answered another revision")
            return [statement], off_dropped_fields(payload["product"])
        statement = parse_fdc_food(payload, url)
        if statement["identifiers"]["fdc_id"] != str(entry["fdc_id"]):
            raise SourcePackError("schema_drift", "FoodData Central answered another FDC ID")
        return [statement], []

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "food runs use the pinned selection, not ad-hoc parameters")
        scope_hash = _digest({"endpoint": self.source["endpoint"], "selection": self.entries})
        try:
            state = {"index": 0, "scope": scope_hash} if cursor is None else json.loads(cursor)
            index = int(state["index"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SourcePackError("cursor_drift", "food cursor is not a valid checkpoint") from exc
        if state.get("scope") != scope_hash:
            raise SourcePackError("cursor_drift", "food cursor belongs to a different selection")
        if index >= len(self.entries):
            return RuntimePage((), None, 0, receipt={"status": 200, "selection_index": index})
        entry = self.entries[index]
        path, query = request_for(self.provider, entry)
        status, raw, url = self._get(path, query)
        if status == 404:
            statements, dropped = [], []
        else:
            statements, dropped = self._parse(entry, raw, url)
        outcome = "returned" if statements else "not_found"
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(statements) > limit:
            raise SourcePackError("response_too_large", "page has more foods than the run's result budget")
        records = [self._record(s) for s in statements]
        next_cursor = (json.dumps({"index": index + 1, "scope": scope_hash}, sort_keys=True)
                       if index + 1 < len(self.entries) else None)
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt={
            "status": status, "provider": self.provider, "selection_index": index, "selector": entry,
            "selection_size": len(self.entries), "scope_hash": scope_hash,
            "response_sha256": hashlib.sha256(raw).hexdigest(), "selector_outcome": outcome,
            "statements": len(records), "excluded_fields_dropped": dropped,
            "live_verification": LIVE_VERIFICATION.get(self.provider if self.provider != "composition-table"
                                                       else f"composition-table:{entry.get('table')}",
                                                       {}).get("status"),
            "final_page": next_cursor is None,
        })

    def _record(self, statement: Mapping[str, Any]) -> dict[str, Any]:
        names = statement["names"]
        title = names.get("product_name") or names.get("description") or statement["provider_key"]
        return {
            "id": f"{statement['provider']}:{statement['provider_key']}",
            "title": f"{title} ({statement['provider']}, revision {statement['revision']['value']})",
            "url": statement.get("url") or self.source["endpoint"],
            "language": "en",
            **({"updated_at": statement["revision"]["date"]} if statement["revision"].get("date") else {}),
            "content": json.dumps(statement, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            "food_composition": dict(statement),
        }


FIXTURE_SECRET = "fixture-credential"
ADAPTERS = {CONNECTOR: FoodCompositionAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query; ``zip_files`` pages are zipped in memory."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del timeout
        key = urlsplit(url).path + ("?" + urlencode(sorted(dict(params or {}).items())) if params else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        if page.get("requires_secret") and not headers.get("X-Api-Key"):
            return {"status": 403, "headers": {}, "content": b""}
        if "zip_files" in page:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, text in sorted(page["zip_files"].items()):
                    info = zipfile.ZipInfo(name, date_time=(2020, 7, 7, 0, 0, 0))
                    archive.writestr(info, text)
            content = buffer.getvalue()
        else:
            body = page.get("body")
            content = (b"" if body is None else body.encode() if isinstance(body, str)
                       else json.dumps(body, ensure_ascii=False).encode())
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = FoodCompositionAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                     secret=FIXTURE_SECRET)
    records, cursor = [], None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            break
    return records


__all__ = [
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "FIXTURE_SECRET", "FoodCompositionAdapter", "LIVE_VERIFICATION",
    "PROVIDERS", "PROVIDER_CONTRACTS", "TABLE_DECISIONS", "fixture_transport", "off_dropped_fields",
    "parse_ciqual", "parse_fdc_food", "parse_off_product", "replay_native_fixture", "request_for",
    "selection_entries",
]
