"""Condition-aware property comparison and bounded property-range search (MT09, #2087).

``compare_material_property`` aligns two values only when all of these hold:

* the same property definition (one property per request);
* the same method class - measured, computed and evaluated values are never
  aligned with each other;
* the same phase identity - the same source record or records joined by an
  accepted phase-level match (:func:`src.kb.materials_identity.phase_groups`);
  a composition-level request never aligns two unmatched records;
* comparable conditions - temperature within :data:`TEMPERATURE_TOLERANCE_K`,
  pressure within :data:`PRESSURE_RELATIVE_TOLERANCE` (relative), equal
  phase and orientation; a condition stated on one side only, or in an
  unknown unit, is not comparable, and conditions unstated on both sides are
  compared as unstated (and say so);
* both values normalised (no ``unit_unknown``).

Everything else is listed side by side as not comparable, with the reasons
against every other value. Aligned computed values name their functionals and
are flagged when those differ. No average, consensus or best estimate is
produced: every value keeps its source, release, method and locator.

``search_materials_by_property`` is bounded to acquired records: the current
value per source is selected first, then filtered by method class,
conditions and ranges; an entry is a hit when each range is met by one of its
own values (ranges are never satisfied by combining sources).
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from src.kb import materials_records as mr
from src.kb import materials_units as mu
from src.kb.materials_identity import group_of, phase_groups
from src.kb.materials_records import READ_SCOPE
from src.kb.materials_store import MaterialsError, MaterialsStore, authorize

COMPARISON_CONTRACT = "noesis-material-comparison-v1"
SEARCH_CONTRACT = "noesis-material-search-v1"
TEMPERATURE_TOLERANCE_K = Fraction(1)
PRESSURE_RELATIVE_TOLERANCE = Fraction(2, 100)
MAX_SEARCH_RESULTS = 200
TOLERANCES = {
    "temperature": f"{mu.decimal_text(TEMPERATURE_TOLERANCE_K)[0]} K absolute",
    "pressure": f"{mu.decimal_text(PRESSURE_RELATIVE_TOLERANCE)[0]} relative",
}


def _quantity_reason(kind, left, right):
    a, b = left.get("status"), right.get("status")
    if a == b == "unstated":
        return None
    if "unstated" in (a, b):
        return f"{kind} stated on one side only"
    if "unit_unknown" in (left.get("unit_status"), right.get("unit_status")):
        return f"{kind} in an unknown unit"
    x, y = Fraction(left["normalized_exact"]), Fraction(right["normalized_exact"])
    if kind == "temperature":
        if abs(x - y) > TEMPERATURE_TOLERANCE_K:
            return f"temperature differs ({left['normalized_value']} K vs {right['normalized_value']} K)"
        return None
    if max(x, y) and abs(x - y) / max(abs(x), abs(y)) > PRESSURE_RELATIVE_TOLERANCE:
        return f"pressure differs ({left['normalized_value']} Pa vs {right['normalized_value']} Pa)"
    return None


def _text_reason(kind, left, right):
    a, b = left.get("status"), right.get("status")
    if a == b == "unstated":
        return None
    if "unstated" in (a, b):
        return f"{kind} stated on one side only"
    if left["value"].casefold() != right["value"].casefold():
        return f"{kind} differs ({left['value']} vs {right['value']})"
    return None


def condition_reasons(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    reasons = [
        _quantity_reason(k, left[k], right[k]) for k in ("temperature", "pressure")
    ]
    reasons += [_text_reason(k, left[k], right[k]) for k in ("phase", "orientation")]
    if {k: v.casefold() for k, v in (left.get("other") or {}).items()} != {
        k: v.casefold() for k, v in (right.get("other") or {}).items()
    }:
        reasons.append("other stated conditions differ")
    return [r for r in reasons if r]


def not_comparable_reasons(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    reasons = []
    if left["property"] != right["property"]:
        reasons.append("different property definitions")
    if left["method_class"] != right["method_class"]:
        reasons.append(
            f"method class differs ({left['method_class']} vs {right['method_class']})"
        )
    if left["phase_group"] != right["phase_group"]:
        reasons.append("no reviewed phase-level match between the records")
    for side in (left, right):
        if side["normalized"].get("unit_status") != "normalized":
            reasons.append(
                f"{side['provider']} {side['native_id']}: unit unknown ({side['normalized'].get('reason')})"
            )
    return reasons + condition_reasons(left["conditions"], right["conditions"])


def _conditions_summary(conditions):
    summary = {}
    for key in ("temperature", "pressure"):
        part = conditions[key]
        summary[key] = (
            "unstated"
            if part["status"] == "unstated"
            else f"{part['normalized_value']} {part['normalized_unit']}"
            if "normalized_value" in part
            else f"{part['value']} {part.get('unit') or '(unit not stated)'} (unit unknown)"
        )
    for key in ("phase", "orientation"):
        summary[key] = conditions[key].get("value", "unstated")
    return summary


def provenance(method):
    """Method provenance for output: the calculation or technique under its own name, never an honesty 'method'."""

    return {("calculation" if k == "method" else k): v for k, v in method.items()}


def _functional(value):
    method = value["method"]
    return (
        method.get("functional") or method.get("technique") or method.get("evaluation")
    )


class MaterialsComparison:
    def __init__(self, conn, *, now=None):
        self.conn = conn
        self.store = MaterialsStore(conn, initialize=False, now=now)

    def resolve(self, namespace, material, *, as_of_ms=None):
        """(entry ids, scope label) for a record key, an entry id or a formula."""

        groups = phase_groups(self.conn, namespace)
        entries = self.store.entries(namespace, as_of_ms=as_of_ms)
        text = str(material or "").strip()
        by_key = {
            f"materials:entry:{e['provider']}:{e['native_id']}": e for e in entries
        }
        chosen = by_key.get(text) or next(
            (e for e in entries if e["entry_id"] == text), None
        )
        if chosen is not None:
            group = group_of(groups, chosen["provider"], chosen["native_id"])
            members = [
                e["entry_id"]
                for e in entries
                if group_of(groups, e["provider"], e["native_id"]) == group
            ]
            return members, {
                "level": "phase",
                "phase_group": group,
                "note": "the record plus records joined to it by accepted phase-level matches",
            }
        try:
            formula = mr.material(text)["reduced_formula"]
        except mr.MaterialRecordError as exc:
            raise MaterialsError(
                "invalid_request", "material is a record key, an entry id or a formula"
            ) from exc
        members = [e["entry_id"] for e in entries if e["reduced_formula"] == formula]
        if not members:
            raise MaterialsError(
                "not_found", f"no acquired record has the composition {formula}"
            )
        return members, {
            "level": "composition",
            "reduced_formula": formula,
            "note": "every record with this reduced formula; records align only through accepted "
            "phase-level matches (polymorphs share a composition)",
        }

    def _values(self, namespace, entry_ids, prop, as_of_ms):
        groups = phase_groups(self.conn, namespace)
        values = []
        for value in self.store.current_values(
            namespace, as_of_ms=as_of_ms, entry_ids=entry_ids, prop=prop
        ):
            entry = self.store.entry(namespace, value["entry_id"], as_of_ms=as_of_ms)
            values.append(
                {
                    **value,
                    "phase_group": group_of(
                        groups, value["provider"], value["native_id"]
                    ),
                    "locator": entry["content"]["locator"],
                    "reduced_formula": entry["reduced_formula"],
                }
            )
        return values

    @staticmethod
    def view(value):
        result = {
            k: value[k]
            for k in (
                "series_key",
                "provider",
                "native_id",
                "record_key",
                "property",
                "value",
                "method_class",
                "release",
                "version",
                "change",
                "locator",
                "phase_group",
                "references",
                "status",
            )
            if k in value
        }
        result["method_provenance"] = provenance(value["method"])
        result.update(
            unit=value.get("unit"),
            conditions=_conditions_summary(value["conditions"]),
            normalized={
                k: value["normalized"][k]
                for k in (
                    "normalized_value",
                    "normalized_unit",
                    "exact_decimal",
                    "unit_status",
                    "reason",
                )
                if k in value["normalized"]
            },
        )
        for key in ("uncertainty", "document_id", "absent_from_newer_releases"):
            if key in value:
                result[key] = value[key]
        return {k: v for k, v in result.items() if v is not None}

    def compare(self, namespace, material, prop, *, scopes, as_of_ms=None):
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        definition = mr.property_definition(prop)
        entry_ids, scope = self.resolve(namespace, material, as_of_ms=as_of_ms)
        values = sorted(
            self._values(namespace, entry_ids, prop, as_of_ms),
            key=lambda v: (v["provider"], v["native_id"], v["series_key"]),
        )
        groups: list[list[dict[str, Any]]] = []
        for value in values:
            for group in groups:
                if all(not not_comparable_reasons(value, member) for member in group):
                    group.append(value)
                    break
            else:
                groups.append([value])
        aligned, alone = [], []
        for group in groups:
            if len(group) < 2:
                alone.extend(group)
                continue
            functionals = sorted({_functional(v) for v in group})
            item = {
                "group_id": "align:"
                + mr.digest(sorted(v["series_key"] for v in group))[:16],
                "method_class": group[0]["method_class"],
                "phase_group": group[0]["phase_group"],
                "functionals": functionals,
                "values": [self.view(v) for v in group],
                "conditions": _conditions_summary(group[0]["conditions"]),
            }
            if group[0]["method_class"] == "computed":
                item["mixed_functionals"] = len(functionals) > 1
                if len(functionals) > 1:
                    item["functional_note"] = (
                        "computed with different functionals ("
                        + ", ".join(functionals)
                        + "); aligned by definition, conditions and phase only"
                    )
            if all(
                v["conditions"][k]["status"] == "unstated"
                for v in group
                for k in ("temperature", "pressure")
            ):
                item["conditions_note"] = (
                    "temperature and pressure are unstated for every value (compared as unstated)"
                )
            aligned.append(item)
        not_comparable = []
        for value in alone:
            reasons = []
            for other in values:
                if other is not value:
                    reasons.append(
                        {
                            "other": other["series_key"],
                            "other_source": f"{other['provider']} {other['native_id']}",
                            "reasons": not_comparable_reasons(value, other),
                        }
                    )
            not_comparable.append(
                {
                    "value": self.view(value),
                    "reasons": reasons
                    or [
                        {
                            "reasons": [
                                "no other value of this property for the requested material"
                            ]
                        }
                    ],
                }
            )
        result = {
            "contract": COMPARISON_CONTRACT,
            "material": material,
            "scope": scope,
            "property": {
                "property": prop,
                "canonical_unit": definition["canonical_unit"],
                "label": definition["label"],
            },
            "aligned": aligned,
            "not_comparable": not_comparable,
            "method": "rule-based alignment: same property, method class, reviewed phase identity and "
            "conditions within stated tolerances; exact unit normalisation",
            "assumptions": [
                f"temperature tolerance {TOLERANCES['temperature']}",
                f"pressure tolerance {TOLERANCES['pressure']}",
                "conditions unstated on both sides are compared as unstated",
                "no averaging, consensus or best estimate is produced",
            ],
            "n": len(values),
            "tolerances": TOLERANCES,
            "semantics": "values are aligned only when comparable; everything else is shown side by side with "
            "the reasons it is not comparable",
            "snapshot_id": self.store.snapshot_id(namespace, as_of_ms=as_of_ms),
        }
        if as_of_ms is not None:
            result["as_of_ms"] = as_of_ms
        result["evidence_bundle"] = self.bundle(result, values)
        return result

    @staticmethod
    def bundle(result, values):
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        created = max((v["observed_at_ms"] for v in values), default=0)
        builder = EvidenceBundleBuilder(
            "receipt",
            {
                "operation": "compare-material-property",
                "material": result["material"],
                "property": result["property"]["property"],
                "snapshot_id": result["snapshot_id"],
            },
            created_at_ms=created,
        )
        refs = []
        for value in values:
            object_id = f"evidence:{value['series_key']}@{value['version']}"
            builder.add_object(
                "evidence",
                {
                    "kind": "material-property-value",
                    "value": MaterialsComparison.view(value),
                    "locator": {
                        "cited": bool(value.get("document_id")),
                        "document_id": value.get("document_id"),
                        "source_url": value["locator"],
                    },
                },
                object_id=object_id,
            )
            refs.append(object_id)
        builder.add_object(
            "receipt",
            {k: v for k, v in result.items() if k != "evidence_bundle"},
            object_id=f"comparison:{result['snapshot_id']}:{result['property']['property']}",
            references=refs,
            root=True,
        )
        return builder.build()

    # ------------------------------------------------------------------ search

    def search(
        self,
        namespace,
        ranges,
        *,
        scopes,
        conditions=None,
        method_class=None,
        as_of_ms=None,
        limit=50,
    ):
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        if not isinstance(ranges, list) or not 1 <= len(ranges) <= 5:
            raise MaterialsError(
                "invalid_request", "one to five property ranges are required"
            )
        if method_class is not None and method_class not in mr.METHOD_CLASSES:
            raise MaterialsError(
                "invalid_request",
                f"method_class is one of {', '.join(mr.METHOD_CLASSES)}",
            )
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        wanted = self._query_conditions(conditions or {})
        parsed = [
            {**self._range(item), "key": str(index)}
            for index, item in enumerate(ranges)
        ]
        per_entry: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for item in parsed:
            for value in self.store.current_values(
                namespace, as_of_ms=as_of_ms, prop=item["property"]
            ):
                if method_class and value["method_class"] != method_class:
                    continue
                if (
                    value["normalized"].get("unit_status") != "normalized"
                    or value["status"] == "withdrawn"
                ):
                    continue
                if not self._conditions_match(value["conditions"], wanted):
                    continue
                number = Fraction(value["normalized"]["normalized_exact"])
                if (item["min"] is not None and number < item["min"]) or (
                    item["max"] is not None and number > item["max"]
                ):
                    continue
                per_entry.setdefault(value["entry_id"], {}).setdefault(
                    item["key"], []
                ).append(value)
        hits = []
        for eid, matched in per_entry.items():
            if len(matched) != len(parsed):
                continue
            entry = self.store.entry(namespace, eid, as_of_ms=as_of_ms)
            cited = [
                dict(
                    self.view(
                        {
                            **v,
                            "phase_group": None,
                            "locator": entry["content"]["locator"],
                        }
                    )
                )
                for key in sorted(matched)
                for v in matched[key]
            ]
            hits.append(
                {
                    "record_key": entry["record_key"],
                    "provider": entry["provider"],
                    "native_id": entry["native_id"],
                    "reduced_formula": entry["reduced_formula"],
                    "formula": entry["content"]["material"]["formula"],
                    "values": cited,
                }
            )
        hits.sort(key=lambda h: (h["reduced_formula"], h["provider"], h["native_id"]))
        return {
            "contract": SEARCH_CONTRACT,
            "hits": hits[:limit],
            "truncated": len(hits) > limit,
            "ranges": [
                {
                    k: (mu.decimal_text(v)[0] if isinstance(v, Fraction) else v)
                    for k, v in r.items()
                    if v is not None and k != "key"
                }
                for r in parsed
            ],
            "method": "bounded filter over acquired records: current value per source first, then method class, "
            "conditions and ranges in canonical units (exact)",
            "assumptions": [
                "only acquired records are searched; absence is not evidence a material lacks the "
                "property",
                "an entry is a hit when each range is met by one of its own values",
                f"condition tolerances: {TOLERANCES['temperature']}, {TOLERANCES['pressure']}",
                "values with unknown units or withdrawn by the source are excluded",
            ],
            "n": len(hits),
            "semantics": "each hit cites the value(s) that met every range; no ranking, "
            "suitability or recommendation",
            "snapshot_id": self.store.snapshot_id(namespace, as_of_ms=as_of_ms),
        }

    @staticmethod
    def _range(item):
        if not isinstance(item, dict):
            raise MaterialsError(
                "invalid_request", "a range is {property, min?, max?, unit}"
            )
        prop = item.get("property")
        definition = mr.property_definition(prop)
        unit = item.get("unit") or definition["canonical_unit"]
        bounds = {}
        for key in ("min", "max"):
            if item.get(key) is None:
                bounds[key] = None
                continue
            bound = mu.canonical_fraction(str(item[key]), unit, prop)
            if bound is None:
                raise MaterialsError(
                    "invalid_request",
                    f"range unit {unit!r} does not convert to "
                    f"{definition['canonical_unit']}",
                )
            bounds[key] = bound
        if bounds["min"] is None and bounds["max"] is None:
            raise MaterialsError("invalid_request", "a range needs min and/or max")
        return {"property": prop, "unit": definition["canonical_unit"], **bounds}

    @staticmethod
    def _query_conditions(conditions):
        wanted = {}
        for kind in ("temperature", "pressure"):
            if conditions.get(kind) is not None:
                spec = conditions[kind]
                value = mu.condition_fraction(
                    kind, str(spec.get("value")), spec.get("unit")
                )
                if value is None:
                    raise MaterialsError(
                        "invalid_request", f"{kind} needs a value and a known unit"
                    )
                wanted[kind] = value
        if conditions.get("phase"):
            wanted["phase"] = str(conditions["phase"]).casefold()
        return wanted

    @staticmethod
    def _conditions_match(stated, wanted):
        for kind in ("temperature", "pressure"):
            if kind not in wanted:
                continue
            part = stated[kind]
            if part.get("unit_status") != "normalized":
                return (
                    False  # unstated or unknown never matches a stated query condition
                )
            value = Fraction(part["normalized_exact"])
            if (
                kind == "temperature"
                and abs(value - wanted[kind]) > TEMPERATURE_TOLERANCE_K
            ):
                return False
            if kind == "pressure" and abs(
                value - wanted[kind]
            ) > PRESSURE_RELATIVE_TOLERANCE * abs(wanted[kind]):
                return False
        if (
            "phase" in wanted
            and stated["phase"].get("value", "").casefold() != wanted["phase"]
        ):
            return False
        return True
