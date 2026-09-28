"""Material record model: identity, structure, property values, conditions and method provenance (MT02, #2080).

``noesis-material-record-v1`` is the provider-neutral shape every Materials
adapter emits: one **entry** per source record (a Materials Project material,
a JARVIS or OQMD entry, a WebBook species, a COD structure) carrying

* ``material`` - formula as published, reduced formula, composition and, when
  published, name, CAS number and InChI;
* ``structure`` - space group, lattice, source structure ID and whether the
  structure is computed (relaxed) or measured (with its measurement
  temperature and pressure);
* ``values`` - ``property_value`` items: the property (from the controlled
  vocabulary :data:`src.kb.materials_units.PROPERTIES`), the value as a decimal
  string with its native unit, uncertainty as published, the ``condition_set``
  and the ``method_provenance``;
* ``release`` (the ``dataset_release``), ``retrieved_at``, ``locator`` and the
  dataset-level references.

Every value is self-describing: which material, which property, under which
conditions, by which method, from which release, where it was read.
Conditions a source does not state are ``{"status": "unstated"}``, never a
default. The method class is ``measured``, ``computed`` (with method,
functional, code and version - never a bare "DFT") or ``evaluated``, and a
provider can only emit the classes it publishes (:data:`PROVIDER_CLASSES`).
No record type can hold a Noesis-predicted value: the provider must be a
source, the class one of the three, and prediction/estimate/consensus fields
are rejected.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from fractions import Fraction
from math import gcd
from typing import Any

from src.kb import materials_units as mu

CONTRACT = "noesis-material-record-v1"
READ_SCOPE = "knowledge:materials:read"
WRITE_SCOPE = "knowledge:materials:write"
METHOD_CLASSES = ("measured", "computed", "evaluated")
PROVIDER_CLASSES = {
    "materials-project": {"computed"},
    "jarvis-dft": {"computed"},
    "oqmd": {"computed"},
    "nist-webbook": {"measured", "evaluated"},
    "cod": {"measured"},
}
PROVIDERS = tuple(PROVIDER_CLASSES)
RELEASE_BASES = ("provider-stated", "operator-declared", "entry-revision")
VALUE_STATUS = ("active", "deprecated", "withdrawn")
REFERENCE_LEVELS = ("value", "dataset")
GENERIC_METHODS = {
    "dft",
    "density functional theory",
    "ab initio",
    "first principles",
    "calculation",
    "computed",
}
FORBIDDEN_KEYS = {
    "predicted",
    "prediction",
    "estimated",
    "estimate",
    "ml_estimate",
    "noesis_estimate",
    "consensus",
    "best_estimate",
    "average",
    "averaged",
    "mean_of_sources",
}
_ELEMENT = re.compile(r"([A-Z][a-z]?)(\d+(?:\.\d+)?)?")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INSTANT = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$"
)


class MaterialRecordError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message, code="invalid_material_record"):
    raise MaterialRecordError(code, message)


def _text(value, field, *, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty text")
    return value.strip()


def _optional_text(value, field, *, limit=2000):
    return None if value is None else _text(value, field, limit=limit)


def _decimal(value, field):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int)):
        _fail(f"{field} must be a decimal string, not {type(value).__name__}")
    try:
        mu.exact(value)
    except ValueError:
        _fail(f"{field} is not a finite decimal number")
    return str(value)


def _compact(mapping: dict[str, Any]) -> dict[str, Any]:
    """Absent values stay absent: keys with ``None`` are dropped, never written as 'None'."""

    return {k: v for k, v in mapping.items() if v is not None}


def _forbid(value, path="record"):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                _fail(
                    f"{path}.{key}: a material record never holds a predicted, estimated or consensus value",
                    "prediction_forbidden",
                )
            _forbid(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _forbid(item, f"{path}[{index}]")


# ------------------------------------------------------------------ formulas


def parse_formula(formula: str) -> dict[str, Fraction]:
    """Element counts of a formula as written (``TiO2``, ``Al2 O3``, ``- O2 Ti -``, ``Ca(OH)2``)."""

    text = _text(formula, "formula", limit=200).strip("- ").replace(" ", "")

    def parse(chunk: str) -> dict[str, Fraction]:
        counts: dict[str, Fraction] = {}
        index = 0
        while index < len(chunk):
            if chunk[index] == "(":
                depth, end = 1, index + 1
                while end < len(chunk) and depth:
                    depth += {"(": 1, ")": -1}.get(chunk[end], 0)
                    end += 1
                if depth:
                    _fail(f"unbalanced parentheses in formula {formula!r}")
                inner = parse(chunk[index + 1 : end - 1])
                match = re.match(r"\d+(?:\.\d+)?", chunk[end:])
                factor = Fraction(match.group(0)) if match else Fraction(1)
                for element, count in inner.items():
                    counts[element] = counts.get(element, Fraction(0)) + count * factor
                index = end + (len(match.group(0)) if match else 0)
                continue
            match = _ELEMENT.match(chunk, index)
            if not match:
                _fail(f"formula {formula!r} is not a chemical formula")
            element, count = match.group(1), Fraction(match.group(2) or 1)
            counts[element] = counts.get(element, Fraction(0)) + count
            index = match.end()
        return counts

    counts = parse(text)
    if not counts or any(c <= 0 for c in counts.values()):
        _fail(f"formula {formula!r} has no positive element counts")
    return counts


def reduced_formula(counts: dict[str, Fraction]) -> str:
    """Canonical reduced formula: elements alphabetically, counts divided by their common factor."""

    denominators = 1
    for count in counts.values():
        denominators = (
            denominators * count.denominator // gcd(denominators, count.denominator)
        )
    integers = {el: int(count * denominators) for el, count in counts.items()}
    common = 0
    for value in integers.values():
        common = gcd(common, value)
    return "".join(
        f"{el}{'' if n // common == 1 else n // common}"
        for el, n in sorted(integers.items())
    )


def atoms_per_formula_unit(material: dict[str, Any]) -> Fraction:
    return sum((Fraction(v) for v in material["composition"].values()), Fraction(0))


def material(formula, *, name=None, cas=None, inchi=None):
    counts = parse_formula(formula)
    return _compact(
        {
            "formula": formula.strip(),
            "reduced_formula": reduced_formula(counts),
            "composition": {
                el: mu.fraction_text(c) if c.denominator != 1 else str(c.numerator)
                for el, c in sorted(counts.items())
            },
            "name": _optional_text(name, "name", limit=300),
            "cas": _optional_text(cas, "cas", limit=40),
            "inchi": _optional_text(inchi, "inchi", limit=2000),
        }
    )


# ---------------------------------------------------------- conditions & method


def _condition(kind, stated):
    if stated is None:
        return {"status": "unstated"}
    if not isinstance(stated, dict) or "value" not in stated:
        _fail(f"{kind} is {{value, unit}} as published or omitted (unstated)")
    return mu.normalise_condition(
        kind, _decimal(stated["value"], kind), stated.get("unit")
    )


def condition_set(
    *, temperature=None, pressure=None, phase=None, orientation=None, other=None
):
    """Conditions as the source states them; anything not stated is explicitly ``unstated``."""

    result = {
        "temperature": _condition("temperature", temperature),
        "pressure": _condition("pressure", pressure),
    }
    for key, value in (("phase", phase), ("orientation", orientation)):
        if value is None:
            result[key] = {"status": "unstated"}
        elif isinstance(value, dict):
            result[key] = _compact(
                {
                    "status": "stated",
                    "value": _text(value.get("value"), key, limit=200),
                    "basis": _optional_text(value.get("basis"), f"{key}.basis"),
                }
            )
        else:
            result[key] = {"status": "stated", "value": _text(value, key, limit=200)}
    if other:
        if not isinstance(other, dict) or any(
            not isinstance(v, str) for v in other.values()
        ):
            _fail("other conditions are text as published")
        result["other"] = dict(sorted(other.items()))
    return result


def _condition_part(value):
    if value.get("status") == "unstated":
        return "unstated"
    if value.get("unit_status") == "unit_unknown":
        return {"unit_unknown": [value["value"], value.get("unit")]}
    if "normalized_exact" in value:
        return value["normalized_exact"]
    return value["value"].casefold()


def condition_key(conditions: dict[str, Any]) -> str:
    """Source-independent identity of a condition set (exact normalised temperature/pressure, phase, orientation)."""

    parts = {
        k: _condition_part(conditions[k])
        for k in ("temperature", "pressure", "phase", "orientation")
    }
    parts["other"] = {
        k: v.casefold() for k, v in sorted((conditions.get("other") or {}).items())
    }
    return digest(parts)


def method_provenance(
    method_class,
    *,
    method=None,
    functional=None,
    mixing_scheme=None,
    code=None,
    code_version=None,
    technique=None,
    evaluation=None,
    note=None,
):
    if method_class not in METHOD_CLASSES:
        _fail(
            f"method class must be one of {', '.join(METHOD_CLASSES)}",
            "invalid_method_class",
        )
    if method_class == "computed":
        method = _text(method, "method.method", limit=200)
        functional = _text(functional, "method.functional", limit=200)
        if functional.casefold() in GENERIC_METHODS:
            _fail(
                "a computed value names its functional, never a generic 'DFT'",
                "generic_method",
            )
        return _compact(
            {
                "class": "computed",
                "method": method,
                "functional": functional,
                "mixing_scheme": _optional_text(
                    mixing_scheme, "method.mixing_scheme", limit=200
                ),
                "code": _optional_text(code, "method.code", limit=200) or "unstated",
                "code_version": _optional_text(
                    code_version, "method.code_version", limit=200
                )
                or "unstated",
                "note": _optional_text(note, "method.note"),
            }
        )
    if technique is not None and functional is not None:
        _fail("a measured or evaluated value carries no functional")
    if method_class == "measured":
        return _compact(
            {
                "class": "measured",
                "technique": _optional_text(technique, "method.technique", limit=300)
                or "unstated",
                "note": _optional_text(note, "method.note"),
            }
        )
    return _compact(
        {
            "class": "evaluated",
            "evaluation": _text(evaluation, "method.evaluation", limit=300),
            "technique": _optional_text(technique, "method.technique", limit=300),
            "note": _optional_text(note, "method.note"),
        }
    )


def method_key(method: dict[str, Any]) -> str:
    return digest(
        {k: str(v).casefold() for k, v in sorted(method.items()) if k != "note"}
    )


# ----------------------------------------------------------------- references


def _doi(value):
    if value is None:
        return None
    text = (
        re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(value).strip(), flags=re.I)
        .strip()
        .lower()
    )
    if not re.fullmatch(r"10\.\d{4,9}/\S+", text):
        _fail(f"{value!r} is not a DOI")
    return text


def reference(level, text, *, doi=None, standard=None, locator=None):
    """A literature, dataset or test-method citation as the source prints it."""

    if level not in REFERENCE_LEVELS:
        _fail("reference level is value or dataset")
    return _compact(
        {
            "level": level,
            "text": _text(text, "reference.text"),
            "doi": _doi(doi),
            "standard": _optional_text(standard, "reference.standard", limit=100),
            "locator": _optional_text(locator, "reference.locator"),
        }
    )


def release(label, *, basis, released_on=None, sequence=None):
    """A dataset release: label, basis, the source's release date and/or its own ordinal (e.g. a COD revision)."""

    if sequence is not None and (type(sequence) is not int or sequence < 0):
        _fail("release sequence is the source's non-negative ordinal")
    if basis not in RELEASE_BASES:
        _fail(f"release basis must be one of {', '.join(RELEASE_BASES)}")
    if released_on is not None:
        if not isinstance(released_on, str) or not _ISO_DATE.fullmatch(released_on):
            _fail("released_on is an ISO date (YYYY-MM-DD)")
        date.fromisoformat(released_on)
    return _compact(
        {
            "label": _text(label, "release.label", limit=100),
            "basis": basis,
            "released_on": released_on,
            "sequence": sequence,
        }
    )


# ------------------------------------------------------------------ values


def property_definition(property_id):
    definition = mu.PROPERTIES.get(property_id)
    if definition is None:
        _fail(
            f"property {property_id!r} is not in the controlled vocabulary",
            "unknown_property",
        )
    return {"property": property_id, **definition}


def property_value(
    prop,
    value,
    unit,
    *,
    conditions,
    method,
    uncertainty=None,
    references=None,
    status="active",
    field=None,
):
    """One published value; ``uncertainty`` is ``{value, unit?, kind}`` as published or omitted."""

    property_definition(prop)
    if status not in VALUE_STATUS:
        _fail(f"value status must be one of {', '.join(VALUE_STATUS)}")
    item = {
        "property": prop,
        "value": _decimal(value, f"{prop}.value"),
        "unit": _optional_text(unit, "unit", limit=40),
        "conditions": conditions,
        "method": method,
        "status": status,
        "references": [dict(r) for r in references or []],
        "field": _optional_text(field, "field", limit=200),
    }
    if uncertainty is not None:
        if not isinstance(uncertainty, dict) or "value" not in uncertainty:
            _fail("uncertainty is {value, unit?, kind} as published")
        item["uncertainty"] = _compact(
            {
                "value": _decimal(uncertainty["value"], f"{prop}.uncertainty"),
                "unit": _optional_text(
                    uncertainty.get("unit"), "uncertainty.unit", limit=40
                ),
                "kind": _optional_text(
                    uncertainty.get("kind"), "uncertainty.kind", limit=100
                )
                or "as published",
            }
        )
    return _compact(item)


def structure(
    source_structure_id,
    *,
    method_class,
    space_group=None,
    crystal_system=None,
    lattice=None,
    cell_setting=None,
    nsites=None,
    volume=None,
    measurement=None,
):
    if method_class not in {"computed", "measured"}:
        _fail("a structure is computed (relaxed) or measured")
    group = None
    if space_group is not None:
        number = space_group.get("number")
        if number is not None and (type(number) is not int or not 1 <= number <= 230):
            _fail("space group number is 1-230")
        group = _compact(
            {
                "symbol": _optional_text(
                    space_group.get("symbol"), "space_group.symbol", limit=40
                ),
                "number": number,
            }
        )
    cell = None
    if lattice is not None:
        cell = {
            k: _decimal(lattice[k], f"lattice.{k}")
            for k in ("a", "b", "c", "alpha", "beta", "gamma")
            if lattice.get(k) is not None
        }
        cell["unit"] = _text(lattice.get("unit") or "Å", "lattice.unit", limit=20)
    if measurement is not None and method_class != "measured":
        _fail("only a measured structure carries measurement conditions")
    record = _compact(
        {
            "source_structure_id": _text(
                str(source_structure_id), "source_structure_id", limit=200
            ),
            "method_class": method_class,
            "space_group": group,
            "crystal_system": _optional_text(
                crystal_system, "crystal_system", limit=40
            ),
            "lattice": cell,
            "cell_setting": cell_setting or "unstated",
            "nsites": nsites if nsites is None else int(nsites),
            "volume": None
            if volume is None
            else {
                "value": _decimal(volume["value"], "volume"),
                "unit": _text(volume.get("unit") or "Å^3", "volume.unit"),
            },
        }
    )
    if method_class == "measured":
        measurement = measurement or {}
        record["measurement"] = {
            "temperature": _condition("temperature", measurement.get("temperature")),
            "pressure": _condition("pressure", measurement.get("pressure")),
        }
    return record


def entry(
    provider,
    native_id,
    *,
    title,
    material_record,
    values,
    release_record,
    retrieved_at,
    locator,
    references=None,
    structure_record=None,
    status="active",
    identifiers=None,
    source_updated_at=None,
):
    """One source record (the unit a source-pack page delivers).

    ``source_updated_at`` is the source's own last-update date or instant for
    the record when it publishes one; it orders captures of one release so a
    late-arriving older capture never reads as a correction.
    """

    if provider not in PROVIDER_CLASSES:
        _fail(
            f"{provider!r} is not a materials source; Noesis never supplies its own values",
            "unknown_provider",
        )
    if not isinstance(retrieved_at, str) or not _INSTANT.fullmatch(retrieved_at):
        _fail("retrieved_at is an ISO-8601 instant with offset")
    datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    if not isinstance(locator, str) or not locator.startswith("https://"):
        _fail("locator is the source's https URL for the record")
    if status not in VALUE_STATUS:
        _fail(f"entry status must be one of {', '.join(VALUE_STATUS)}")
    if source_updated_at is not None and (
        not isinstance(source_updated_at, str)
        or not (
            _ISO_DATE.fullmatch(source_updated_at)
            or _INSTANT.fullmatch(source_updated_at)
        )
    ):
        _fail("source_updated_at is an ISO date or an instant with offset")
    allowed = PROVIDER_CLASSES[provider]
    for item in values:
        if item["method"]["class"] not in allowed:
            _fail(
                f"{provider} publishes {sorted(allowed)} values, not {item['method']['class']}",
                "class_not_published",
            )
    if structure_record is not None and structure_record["method_class"] not in allowed:
        _fail(
            f"{provider} does not publish {structure_record['method_class']} structures",
            "class_not_published",
        )
    record = _compact(
        {
            "contract": CONTRACT,
            "provider": provider,
            "native_id": _text(str(native_id), "native_id", limit=200),
            "title": _text(title, "title", limit=500),
            "material": material_record,
            "structure": structure_record,
            "values": [dict(v) for v in values],
            "release": release_record,
            "retrieved_at": retrieved_at,
            "locator": locator,
            "status": status,
            "references": [dict(r) for r in references or []],
            "identifiers": dict(sorted((identifiers or {}).items())) or None,
            "source_updated_at": source_updated_at,
        }
    )
    _forbid(record)
    return record


def validate(record):
    """Re-validate a stored or received entry by rebuilding every part through its constructor."""

    if not isinstance(record, dict) or record.get("contract") != CONTRACT:
        _fail("not a noesis-material-record-v1 record")
    _forbid(record)
    try:
        source_material = record["material"]
        rebuilt_material = material(
            source_material["formula"],
            name=source_material.get("name"),
            cas=source_material.get("cas"),
            inchi=source_material.get("inchi"),
        )
        values = []
        for item in record.get("values") or []:
            conditions = {}
            for kind in ("temperature", "pressure"):
                part = item["conditions"][kind]
                conditions[kind] = (
                    None
                    if part.get("status") == "unstated"
                    else {"value": part["value"], "unit": part.get("unit")}
                )
            for kind in ("phase", "orientation"):
                part = item["conditions"][kind]
                conditions[kind] = (
                    None
                    if part.get("status") == "unstated"
                    else {"value": part["value"], "basis": part.get("basis")}
                )
            method = dict(item["method"])
            method_class = method.pop("class")
            if method_class == "computed":
                for key in ("code", "code_version"):
                    if method.get(key) == "unstated":
                        method.pop(key)
            elif method.get("technique") == "unstated":
                method.pop("technique")
            values.append(
                property_value(
                    item["property"],
                    item["value"],
                    item.get("unit"),
                    conditions=condition_set(
                        **conditions, other=item["conditions"].get("other")
                    ),
                    method=method_provenance(method_class, **method),
                    uncertainty=item.get("uncertainty"),
                    references=[
                        reference(
                            r["level"],
                            r["text"],
                            doi=r.get("doi"),
                            standard=r.get("standard"),
                            locator=r.get("locator"),
                        )
                        for r in item.get("references") or []
                    ],
                    status=item.get("status", "active"),
                    field=item.get("field"),
                )
            )
        structure_record = None
        if record.get("structure") is not None:
            s = record["structure"]
            measurement = None
            if s.get("measurement"):
                measurement = {
                    k: None
                    if v.get("status") == "unstated"
                    else {"value": v["value"], "unit": v.get("unit")}
                    for k, v in s["measurement"].items()
                }
            structure_record = structure(
                s["source_structure_id"],
                method_class=s["method_class"],
                space_group=s.get("space_group"),
                crystal_system=s.get("crystal_system"),
                lattice=s.get("lattice"),
                cell_setting=s.get("cell_setting"),
                nsites=s.get("nsites"),
                volume=s.get("volume"),
                measurement=measurement,
            )
        rel = record["release"]
        return entry(
            record["provider"],
            record["native_id"],
            title=record["title"],
            material_record=rebuilt_material,
            values=values,
            release_record=release(
                rel["label"],
                basis=rel["basis"],
                released_on=rel.get("released_on"),
                sequence=rel.get("sequence"),
            ),
            retrieved_at=record["retrieved_at"],
            locator=record["locator"],
            references=[
                reference(
                    r["level"],
                    r["text"],
                    doi=r.get("doi"),
                    standard=r.get("standard"),
                    locator=r.get("locator"),
                )
                for r in record.get("references") or []
            ],
            structure_record=structure_record,
            status=record.get("status", "active"),
            identifiers=record.get("identifiers"),
            source_updated_at=record.get("source_updated_at"),
        )
    except (KeyError, TypeError) as exc:
        raise MaterialRecordError(
            "invalid_material_record", f"incomplete material record: {exc}"
        ) from exc


# ------------------------------------------------------------ schema registry

SCHEMA_FILES = {
    "noesis-material-record": "contracts/schemas/jsonschema/noesis-material-record-v1.json",
    "noesis-material-comparison": "contracts/schemas/jsonschema/noesis-material-comparison-v1.json",
}


def schema_definitions(root=None):
    from pathlib import Path

    root = Path(root) if root else Path(__file__).resolve().parents[2]
    return {
        name: json.loads((root / path).read_text())
        for name, path in SCHEMA_FILES.items()
    }


def register_schemas(conn, *, principal_id, scopes, root=None):
    """Register the pack's contracts in the existing schema registry (idempotent per version)."""

    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1",
            "name": name,
            "kind": "schema",
            "semantic_version": "1.0.0",
            "content": content,
            "owner": "materials.core",
            "dependencies": [],
            "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/materials"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(
            registry.register(
                definition,
                f"materials-schema:{name}:1.0.0:{digest(content)[:16]}",
                principal_id=principal_id,
                scopes=scopes,
            )
        )
    return results
