"""Category-scoped attribute registry for the Products pack (PX02 #2094, PX05 #2097).

Each product category the pack covers is one versioned JSON file under
``config/product_categories/`` (contract ``noesis-product-category-v1``):

* **attributes** with their kind, canonical unit(s), the native units a
  category accepts, precision, measurement modes and, for component nominal
  values, the attribute holding the published tolerance;
* **label schemes** by regulation (EU energy-label classes per delegated act);
* **comparison** rows shown by default and the notice every comparison carries;
* **matching** rules: which providers are paired, how identifiers compare and
  which attributes corroborate or contradict a candidate;
* **provider mappings**: EPREL fields, Open Icecat feature labels and BMEcat
  feature names to attribute keys, with unit signs. A native field or feature a
  mapping does not name is kept on the record as a source field; nothing is
  mapped by guess.

Units are exact decimal factors registered in the shared quantitative store
(no pint). They are *category-aware*: a category lists the native units each
attribute accepts, so ``F`` is a farad only where capacitance is defined, and
per-cycle or per-annum energy never converts into a plain energy.

Electronic components (family ``component``) are identified by the normalised
manufacturer and the manufacturer part number as published
(:func:`component_key`); distributor SKUs are provider-scoped aliases, never
identity. ``lifecycle_status`` is stored only as a named source published it
(:func:`lifecycle_status`), never inferred.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

REGISTRY_CONTRACT = "noesis-product-category-v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_ROOT = REPO_ROOT / "config/product_categories"
DISPLAY_CATEGORY = "electronic-displays"
FAMILIES = frozenset({"display", "appliance", "component"})
KINDS = frozenset(
    {
        "quantity",
        "tolerance",
        "resolution",
        "label_class",
        "enum",
        "code",
        "code_set",
        "part",
    }
)
IDENTITIES = frozenset({"brand-designation", "manufacturer-mpn"})
CORROBORATION_RULES = frozenset({"within", "overlap", "same_scheme_overlap"})
# Energy-label classes accepted when a record names no registered scheme (the display pack's rule).
GENERIC_LABEL_CLASS = re.compile(r"A\+{0,3}|[A-G]")

_CAPACITANCE = {"mass": -1, "length": -2, "time": 4, "current": 2}
_VOLTAGE = {"mass": 1, "length": 2, "time": -3, "current": -1}
_RESISTANCE = {"mass": 1, "length": 2, "time": -3, "current": -2}
_ENERGY = {"length": 2, "mass": 1, "time": -2}
# Units the categories add to the display units (src.kb.products.PRODUCT_UNITS) and the quantitative
# built-ins (kg, h, C, percent, ...). symbol -> (dimension, exact factor to the dimension's base, aliases).
# Per-cycle and per-annum quantities carry their own dimension, so they never convert into plain energy.
CATEGORY_UNITS: dict[str, tuple[dict[str, int], str, list[str]]] = {
    "L": ({"length": 3}, "0.001", ["litre", "liter"]),
    "L/cycle": ({"length": 3, "cycle": -1}, "0.001", ["litre per cycle"]),
    "kWh/100cycles": ({**_ENERGY, "cycle": -1}, "36000", ["kWh per 100 cycles"]),
    "kWh/cycle": ({**_ENERGY, "cycle": -1}, "3600000", ["kWh per cycle"]),
    "kWh/annum": ({**_ENERGY, "annum": -1}, "3600000", ["kWh/a", "kWh per annum"]),
    "rpm": ({"rotation": 1, "time": -1}, "1", ["1/min", "min-1"]),
    "dB(A)": ({"a_weighted_sound_level": 1}, "1", ["dBA"]),
    "pF": (_CAPACITANCE, "0.000000000001", ["picofarad"]),
    "nF": (_CAPACITANCE, "0.000000001", ["nanofarad"]),
    "uF": (_CAPACITANCE, "0.000001", ["µF", "μF", "microfarad"]),
    "F": (_CAPACITANCE, "1", ["farad"]),
    "V": (_VOLTAGE, "1", ["volt"]),
    "kV": (_VOLTAGE, "1000", ["kilovolt"]),
    "mV": (_VOLTAGE, "0.001", ["millivolt"]),
    "ohm": (_RESISTANCE, "1", ["Ω", "Ohm"]),
    "kohm": (_RESISTANCE, "1000", ["kΩ", "kOhm"]),
    "Mohm": (_RESISTANCE, "1000000", ["MΩ", "MOhm"]),
    "mohm": (_RESISTANCE, "0.001", ["mΩ", "mOhm"]),
    "ppm/K": ({"temperature": -1}, "0.000001", ["ppm/°C"]),
}
# Published lifecycle texts that name one of the shared statuses; any other text stays declared as "unknown".
LIFECYCLE_STATUSES = ("active", "nrnd", "last-time-buy", "obsolete", "unknown")
_LIFECYCLE_TEXT = {
    "active": "active",
    "nrnd": "nrnd",
    "not recommended for new designs": "nrnd",
    "last time buy": "last-time-buy",
    "last-time-buy": "last-time-buy",
    "ltb": "last-time-buy",
    "obsolete": "obsolete",
}
# Legal-form tokens dropped from a manufacturer name for comparison (both sides of every match).
_LEGAL_FORMS = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "llc",
        "plc",
        "gmbh",
        "ag",
        "kg",
        "se",
        "sa",
        "sas",
        "srl",
        "spa",
        "bv",
        "nv",
        "ab",
        "oy",
        "as",
        "kk",
        "pte",
        "pty",
        "group",
    }
)
MISSING_MARKERS = frozenset(
    {"-", "--", "–", "n/a", "na", "n.a.", "not available", "unknown", "none", "null"}
)


class ProductCategoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@lru_cache(maxsize=4)
def _load(root: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(root).glob("*.json")):
        data = json.loads(path.read_text())
        result[str(data.get("category") or path.stem)] = data
    return result


def registry(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Every registered category keyed by id (a fresh copy; callers may not mutate the cache)."""
    return json.loads(json.dumps(_load(str(root or REGISTRY_ROOT))))


def _registry() -> dict[str, dict[str, Any]]:
    return _load(str(REGISTRY_ROOT))


def categories(family: str | None = None) -> list[str]:
    return sorted(
        k for k, v in _registry().items() if family is None or v.get("family") == family
    )


def category(category_id: str) -> dict[str, Any]:
    spec = _registry().get(category_id)
    if spec is None:
        raise ProductCategoryError(
            "unknown_category", f"product category {category_id!r} is not registered"
        )
    return spec


def family(category_id: str | None) -> str | None:
    spec = _registry().get(category_id or "")
    return None if spec is None else str(spec["family"])


def resolve_label(label: Any) -> str | None:
    """The registered category a pinned label (or category id) names, or None; never a guess from similarity."""
    text = str(label or "").strip().casefold()
    if not text:
        return None
    for category_id, spec in _registry().items():
        if text == category_id or text in {
            str(item).casefold() for item in spec.get("labels") or []
        }:
            return category_id
    return None


def resolve(record_category: Mapping[str, Any] | str | None) -> str | None:
    """The category of a product record (its pinned ``category.label``)."""
    if isinstance(record_category, Mapping):
        return resolve_label(record_category.get("label"))
    return resolve_label(record_category)


def labels(category_id: str) -> list[str]:
    return [str(item) for item in category(category_id).get("labels") or []]


def attribute_spec(category_id: str | None, attribute: str) -> dict[str, Any] | None:
    spec = _registry().get(category_id or "")
    if spec is None:
        return None
    item = (spec.get("attributes") or {}).get(attribute)
    return None if item is None else dict(item)


def label_schemes(category_id: str | None) -> dict[str, Any]:
    spec = _registry().get(category_id or "")
    return {} if spec is None else dict(spec.get("label_schemes") or {})


def provider_mapping(category_id: str | None, provider: str) -> dict[str, Any]:
    spec = _registry().get(category_id or "")
    return (
        {} if spec is None else dict((spec.get("providers") or {}).get(provider) or {})
    )


def eprel_fields(
    category_id: str | None,
) -> dict[str, tuple[str, str | None, str | None]]:
    """EPREL field -> (attribute, mode, native unit), in the registry's order."""
    return {
        row[0]: (row[1], row[2], row[3])
        for row in provider_mapping(category_id, "eprel").get("fields") or []
    }


def eprel_category_for_group(product_group: str) -> str | None:
    for category_id, spec in _registry().items():
        if product_group in (dict(spec.get("providers") or {}).get("eprel") or {}).get(
            "product_groups", []
        ):
            return category_id
    return None


def feature_map(
    category_id: str | None, provider: str
) -> dict[str, tuple[str, str | None, dict[str, str] | None]]:
    """Casefolded native feature name -> (attribute, mode, feature-specific unit signs), in the registry's order."""
    return {
        str(row[0]).casefold(): (
            row[1],
            row[2],
            dict(row[3]) if len(row) > 3 and row[3] else None,
        )
        for row in provider_mapping(category_id, provider).get("features") or []
    }


def _sign_lookup(signs: Mapping[str, str], sign: str) -> str | None:
    if sign in signs:
        return signs[sign]
    folded = [
        value for key, value in signs.items() if key.casefold() == sign.casefold()
    ]
    # Case can be the only difference (mΩ / MΩ): a sign matching several entries only by case is unknown.
    return folded[0] if len(set(folded)) == 1 else None


def unit_for_sign(
    category_id: str | None,
    provider: str,
    sign: Any,
    feature_units: Mapping[str, str] | None = None,
) -> str | None:
    """The registered unit a provider's unit sign means for this category (feature-specific signs first)."""
    text = "" if sign is None else str(sign).strip()
    if not text:
        return None
    if feature_units:
        found = _sign_lookup(feature_units, text)
        if found:
            return found
    return _sign_lookup(
        dict(provider_mapping(category_id, provider).get("unit_signs") or {}), text
    )


def lifecycle_status(text: Any) -> dict[str, Any] | None:
    """A published lifecycle text as {declared, value}; unrecognised text is kept with value "unknown"."""
    declared = None if text is None else re.sub(r"\s+", " ", str(text)).strip()
    if not declared or declared.casefold() in MISSING_MARKERS:
        return None
    return {
        "declared": declared,
        "value": _LIFECYCLE_TEXT.get(declared.casefold(), "unknown"),
    }


def mpn_key(value: Any) -> str | None:
    """A manufacturer part number for comparison: whitespace removed, upper case; every other character kept.

    Separators and suffixes (``-TR``, ``/NOPB``, packaging and tolerance codes) name different parts, so they stay.
    """
    text = "" if value is None else re.sub(r"\s+", "", str(value)).upper()
    return None if not text or text.casefold() in MISSING_MARKERS else text


def manufacturer_key(value: Any) -> str | None:
    """A manufacturer name for comparison: the shared surface normalisation without trailing legal forms."""
    from src.kb.entities import normalize_surface

    text = "" if value is None else str(value).strip()
    if not text or text.casefold() in MISSING_MARKERS:
        return None
    tokens = normalize_surface(text).split()
    while len(tokens) > 1 and tokens[-1] in _LEGAL_FORMS:
        tokens = tokens[:-1]
    return " ".join(tokens) or text.casefold()


def component_key(manufacturer: Any, mpn: Any) -> str | None:
    maker, part = manufacturer_key(manufacturer), mpn_key(mpn)
    return None if maker is None or part is None else f"{maker}|{part}"


def known_units() -> set[str]:
    from src.kb.products import PRODUCT_UNITS
    from src.kb.quantitative import _BUILTIN_UNITS

    return set(PRODUCT_UNITS) | set(CATEGORY_UNITS) | set(_BUILTIN_UNITS) | {"px"}


def validate_registry(
    value: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Structural checks over every category file; an empty list means the registry is consistent."""
    data = dict(value if value is not None else _registry())
    errors: list[str] = []
    units = known_units()
    seen_labels: dict[str, str] = {}
    for category_id, spec in data.items():
        where = f"{category_id}:"
        if spec.get("contract") != REGISTRY_CONTRACT:
            errors.append(f"{where} contract must be {REGISTRY_CONTRACT}")
        if spec.get("category") != category_id:
            errors.append(f"{where} category id differs from its key")
        if spec.get("family") not in FAMILIES:
            errors.append(f"{where} family must be one of {sorted(FAMILIES)}")
        if spec.get("identity") not in IDENTITIES:
            errors.append(f"{where} identity must be one of {sorted(IDENTITIES)}")
        if (spec.get("family") == "component") != (
            spec.get("identity") == "manufacturer-mpn"
        ):
            errors.append(
                f"{where} components (and only components) use manufacturer-mpn identity"
            )
        if not spec.get("labels"):
            errors.append(f"{where} at least one pinned label is required")
        for label in spec.get("labels") or []:
            key = str(label).casefold()
            if key in seen_labels:
                errors.append(
                    f"{where} label {label!r} is also used by {seen_labels[key]}"
                )
            seen_labels[key] = category_id
        attributes = dict(spec.get("attributes") or {})
        schemes = dict(spec.get("label_schemes") or {})
        for name, item in attributes.items():
            kind = item.get("kind")
            if kind not in KINDS:
                errors.append(f"{where} {name} has unknown kind {kind!r}")
            if kind in {"quantity", "tolerance"}:
                canonical = list(item.get("canonical_units") or [])
                if not canonical:
                    errors.append(f"{where} {name} needs canonical_units")
                for unit in canonical + list(item.get("units") or []):
                    if unit not in units:
                        errors.append(
                            f"{where} {name} names unregistered unit {unit!r}"
                        )
                if item.get("units") and not set(canonical) <= set(item["units"]):
                    errors.append(
                        f"{where} {name} canonical units must be accepted units"
                    )
                if not isinstance(item.get("precision"), int):
                    errors.append(f"{where} {name} needs an integer precision")
            if (
                item.get("tolerance")
                and attributes.get(item["tolerance"], {}).get("kind") != "tolerance"
            ):
                errors.append(
                    f"{where} {name} tolerance attribute must be of kind tolerance"
                )
            if kind == "part" and item.get("part_of") not in attributes:
                errors.append(f"{where} {name} is part of an unregistered attribute")
        for scheme, item in schemes.items():
            if not item.get("regulation") or not item.get("classes"):
                errors.append(
                    f"{where} label scheme {scheme} needs a regulation and classes"
                )
        for name in (spec.get("comparison") or {}).get("attributes") or []:
            if name not in attributes:
                errors.append(f"{where} comparison names unregistered attribute {name}")
        matching = dict(spec.get("matching") or {})
        pairs = matching.get("providers")
        if pairs != "distinct" and not (
            isinstance(pairs, list) and all(len(p) == 2 for p in pairs)
        ):
            errors.append(
                f"{where} matching.providers must be 'distinct' or provider pairs"
            )
        if matching.get("identifier") not in {"designation", "component"}:
            errors.append(
                f"{where} matching.identifier must be designation or component"
            )
        for rule in matching.get("corroboration") or []:
            if rule.get("attribute") not in attributes:
                errors.append(
                    f"{where} corroboration names unregistered attribute {rule.get('attribute')}"
                )
            if rule.get("rule") not in CORROBORATION_RULES:
                errors.append(
                    f"{where} corroboration rule {rule.get('rule')!r} is unknown"
                )
            if rule.get("required") and not rule.get("missing_reason"):
                errors.append(
                    f"{where} a required corroboration states its missing reason"
                )
        for provider, mapping in (spec.get("providers") or {}).items():
            rows = list(mapping.get("fields") or []) + list(
                mapping.get("features") or []
            )
            for row in rows:
                target = row[1]
                if target not in attributes:
                    errors.append(
                        f"{where} {provider} maps to unregistered attribute {target}"
                    )
                elif row[2] is not None and row[2] not in (
                    attributes[target].get("modes") or []
                ):
                    errors.append(
                        f"{where} {provider} maps {target} to undeclared mode {row[2]}"
                    )
            for sign, unit in dict(mapping.get("unit_signs") or {}).items():
                if unit not in units:
                    errors.append(
                        f"{where} {provider} unit sign {sign!r} names unregistered unit {unit}"
                    )
    return errors


__all__ = [
    "CATEGORY_UNITS",
    "DISPLAY_CATEGORY",
    "LIFECYCLE_STATUSES",
    "REGISTRY_CONTRACT",
    "ProductCategoryError",
    "attribute_spec",
    "categories",
    "category",
    "component_key",
    "eprel_category_for_group",
    "eprel_fields",
    "family",
    "feature_map",
    "label_schemes",
    "labels",
    "lifecycle_status",
    "manufacturer_key",
    "mpn_key",
    "provider_mapping",
    "registry",
    "resolve",
    "resolve_label",
    "unit_for_sign",
    "validate_registry",
]
