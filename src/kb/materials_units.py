"""Exact, condition-aware unit normalisation for material properties (MT03, #2081).

Every property definition has one canonical unit (``PROPERTIES``); a value is
normalised to it without losing the native value and unit, and the
conditions a value holds under (temperature, pressure) are normalised the same
way (kelvin, pascal). The arithmetic is exact: every factor and offset is a
:class:`fractions.Fraction` taken from an SI definition (the 2019 exact
elementary charge and Avogadro constant, the international foot and pound,
the thermochemical calorie, the standard atmosphere), so a conversion never
depends on a floating-point registry or on the optional ``pint`` dependency,
which this module never imports. A normalised value is written as a decimal
when the fraction terminates, otherwise rounded to twelve places with the
exact fraction kept beside it (``normalized_exact``) and ``exact_decimal``
false; comparisons use the fraction.

What is handled explicitly rather than guessed:

* **offset units** - degrees Celsius and Fahrenheit convert to kelvin with
  their offsets; an *uncertainty* or tolerance in those units converts with the
  scale factor only (a difference has no offset);
* **per atom vs per formula unit** - ``eV/atom``, ``eV/f.u.`` and ``kJ/mol``
  (per mole of formula units as written) are different bases. Converting
  between them needs the number of atoms in the formula unit, taken from the
  record's own composition; without it the value is ``unit_unknown``;
* **unknown or ambiguous units** - a unit the table does not contain, one the
  source leaves ambiguous (``kcal`` without thermochemical/IT qualification,
  ``A`` for angstrom or ampere, bare ``eV`` for a per-atom property) or of the
  wrong dimension is ``unit_unknown`` with its reason, keeps its native value
  and is excluded from normalised comparison. Nothing is defaulted.
"""

from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

TABLE_VERSION = "materials-units:1.0.0"
METHOD = "exact rational conversion (materials unit table; pint is not used)"
ELEMENTARY_CHARGE = Fraction("1.602176634e-19")  # C, exact since 2019
AVOGADRO = Fraction("6.02214076e23")  # 1/mol, exact since 2019
EV_PER_KJ_MOL = Fraction(1000) / (
    ELEMENTARY_CHARGE * AVOGADRO
)  # kJ/mol -> eV per formula unit
CAL_TH = Fraction("4.184")  # thermochemical calorie in J (exact by definition)
ATM = Fraction(101325)
POUND_FORCE = Fraction("0.45359237") * Fraction("9.80665")  # N
PSI = POUND_FORCE / (Fraction("0.0254") ** 2)  # Pa
DECIMAL_PLACES = 12

# unit text -> (kind, factor to the kind's base unit, offset in base units, basis)
# Bases: energy eV, temperature K, pressure Pa, molar heat capacity/entropy J/(mol*K),
# thermal conductivity W/(m*K), density g/cm^3, length angstrom, volume per atom angstrom^3/atom.
_UNITS: dict[str, tuple[str, Fraction, Fraction, str | None]] = {
    "eV": ("energy", Fraction(1), Fraction(0), None),
    "meV": ("energy", Fraction(1, 1000), Fraction(0), None),
    "eV/atom": ("energy", Fraction(1), Fraction(0), "atom"),
    "meV/atom": ("energy", Fraction(1, 1000), Fraction(0), "atom"),
    "eV/f.u.": ("energy", Fraction(1), Fraction(0), "formula-unit"),
    "eV/formula unit": ("energy", Fraction(1), Fraction(0), "formula-unit"),
    "kJ/mol": ("energy", EV_PER_KJ_MOL, Fraction(0), "formula-unit"),
    "J/mol": ("energy", EV_PER_KJ_MOL / 1000, Fraction(0), "formula-unit"),
    "kcal_th/mol": ("energy", EV_PER_KJ_MOL * CAL_TH, Fraction(0), "formula-unit"),
    "K": ("temperature", Fraction(1), Fraction(0), None),
    "°C": ("temperature", Fraction(1), Fraction("273.15"), None),
    "degC": ("temperature", Fraction(1), Fraction("273.15"), None),
    "°F": ("temperature", Fraction(5, 9), Fraction("459.67") * Fraction(5, 9), None),
    "degF": ("temperature", Fraction(5, 9), Fraction("459.67") * Fraction(5, 9), None),
    "Pa": ("pressure", Fraction(1), Fraction(0), None),
    "kPa": ("pressure", Fraction(1000), Fraction(0), None),
    "MPa": ("pressure", Fraction(10**6), Fraction(0), None),
    "GPa": ("pressure", Fraction(10**9), Fraction(0), None),
    "bar": ("pressure", Fraction(10**5), Fraction(0), None),
    "kbar": ("pressure", Fraction(10**8), Fraction(0), None),
    "atm": ("pressure", ATM, Fraction(0), None),
    "Torr": ("pressure", ATM / 760, Fraction(0), None),
    "psi": ("pressure", PSI, Fraction(0), None),
    "J/(mol*K)": ("molar-heat-capacity", Fraction(1), Fraction(0), None),
    "J/mol*K": (
        "molar-heat-capacity",
        Fraction(1),
        Fraction(0),
        None,
    ),  # NIST WebBook spelling
    "cal_th/(mol*K)": ("molar-heat-capacity", CAL_TH, Fraction(0), None),
    "W/(m*K)": ("thermal-conductivity", Fraction(1), Fraction(0), None),
    "W/(cm*K)": ("thermal-conductivity", Fraction(100), Fraction(0), None),
    "mW/(m*K)": ("thermal-conductivity", Fraction(1, 1000), Fraction(0), None),
    "g/cm^3": ("density", Fraction(1), Fraction(0), None),
    "kg/m^3": ("density", Fraction(1, 1000), Fraction(0), None),
    "Å": ("length", Fraction(1), Fraction(0), None),
    "angstrom": ("length", Fraction(1), Fraction(0), None),
    "nm": ("length", Fraction(10), Fraction(0), None),
    "pm": ("length", Fraction(1, 100), Fraction(0), None),
    "Å^3/atom": ("volume-per-atom", Fraction(1), Fraction(0), "atom"),
    "Å^3": ("volume", Fraction(1), Fraction(0), None),
}
# Spellings the sources leave ambiguous: never guessed.
AMBIGUOUS = {
    "kcal/mol": "calorie not qualified as thermochemical or international table",
    "cal/mol": "calorie not qualified as thermochemical or international table",
    "cal/(mol*K)": "calorie not qualified as thermochemical or international table",
    "A": "angstrom or ampere",
    "mmHg": "conventional or density-dependent millimetre of mercury not stated",
}
_ALIASES = {
    "·": "*",
    "³": "^3",
    "Å3": "Å^3",
    "cm3": "cm^3",
    "m3": "m^3",
    "Kelvin": "K",
    "kelvin": "K",
}

# Property vocabulary: canonical unit per property definition (MT02 record ``property_definition``).
PROPERTIES: dict[str, dict[str, Any]] = {
    "formation_energy_per_atom": {
        "label": "formation energy per atom",
        "kind": "energy",
        "basis": "atom",
        "canonical_unit": "eV/atom",
        "dimension": "energy per atom",
    },
    "energy_above_hull": {
        "label": "energy above the convex hull",
        "kind": "energy",
        "basis": "atom",
        "canonical_unit": "eV/atom",
        "dimension": "energy per atom",
    },
    "standard_enthalpy_of_formation": {
        "label": "standard enthalpy of formation",
        "kind": "energy",
        "basis": "atom",
        "canonical_unit": "eV/atom",
        "dimension": "energy per atom",
    },
    "band_gap": {
        "label": "electronic band gap",
        "kind": "energy",
        "basis": None,
        "canonical_unit": "eV",
        "dimension": "energy",
    },
    "bulk_modulus": {
        "label": "bulk modulus",
        "kind": "pressure",
        "basis": None,
        "canonical_unit": "GPa",
        "dimension": "pressure (elastic modulus)",
    },
    "shear_modulus": {
        "label": "shear modulus",
        "kind": "pressure",
        "basis": None,
        "canonical_unit": "GPa",
        "dimension": "pressure (elastic modulus)",
    },
    "density": {
        "label": "mass density",
        "kind": "density",
        "basis": None,
        "canonical_unit": "g/cm^3",
        "dimension": "mass per volume",
    },
    "heat_capacity_cp": {
        "label": "isobaric molar heat capacity",
        "kind": "molar-heat-capacity",
        "basis": None,
        "canonical_unit": "J/(mol*K)",
        "dimension": "energy per amount per temperature",
    },
    "standard_entropy": {
        "label": "standard molar entropy",
        "kind": "molar-heat-capacity",
        "basis": None,
        "canonical_unit": "J/(mol*K)",
        "dimension": "energy per amount per temperature",
    },
    "fusion_temperature": {
        "label": "fusion (melting) temperature",
        "kind": "temperature",
        "basis": None,
        "canonical_unit": "K",
        "dimension": "temperature",
    },
    "thermal_conductivity": {
        "label": "thermal conductivity",
        "kind": "thermal-conductivity",
        "basis": None,
        "canonical_unit": "W/(m*K)",
        "dimension": "power per length per temperature",
    },
    "volume_per_atom": {
        "label": "volume per atom",
        "kind": "volume-per-atom",
        "basis": "atom",
        "canonical_unit": "Å^3/atom",
        "dimension": "volume per atom",
    },
}
CONDITION_UNITS = {"temperature": "K", "pressure": "Pa"}


def unit_key(unit: Any) -> str | None:
    """The table spelling of a published unit (whitespace and dot operators unified), or ``None``."""

    if not isinstance(unit, str) or not unit.strip():
        return None
    text = unit.strip()
    for old, new in _ALIASES.items():
        text = text.replace(old, new)
    text = (
        text.replace(" ", "")
        if text not in _UNITS and text.replace(" ", "") in _UNITS
        else text
    )
    return text


def exact(value: Any) -> Fraction:
    """A published decimal as an exact fraction; floats and non-finite values are rejected."""

    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("values are decimal strings, never floats")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal number: {value!r}") from exc
    if not number.is_finite():
        raise ValueError("values must be finite")
    return Fraction(number)


def decimal_text(value: Fraction) -> tuple[str, bool]:
    """(decimal text, exact?) - exact when the fraction terminates, else rounded to twelve places."""

    denominator = value.denominator
    for prime in (2, 5):
        while denominator % prime == 0:
            denominator //= prime
    if denominator == 1:
        text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return (text if text not in {"-0", ""} else "0"), True
    rounded = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
        Decimal(1).scaleb(-DECIMAL_PLACES), rounding=ROUND_HALF_EVEN
    )
    return format(rounded, "f"), False


def fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def parse_fraction(text: str) -> Fraction:
    return Fraction(text)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _unknown(native_value, native_unit, reason):
    result = {
        "native_value": native_value,
        "native_unit": native_unit,
        "unit_status": "unit_unknown",
        "reason": reason,
        "comparable": False,
    }
    return {k: v for k, v in result.items() if v is not None}


def _lookup(unit):
    key = unit_key(unit)
    if key is None:
        return None, "unit not stated by the source"
    if key in AMBIGUOUS:
        return None, f"ambiguous unit {unit!r}: {AMBIGUOUS[key]}"
    if key not in _UNITS:
        return None, f"unit {unit!r} is not in the materials unit table"
    return (key, *_UNITS[key]), None


def normalise(
    value: Any,
    unit: Any,
    property_id: str,
    *,
    atoms_per_formula_unit: Any = None,
    difference: bool = False,
) -> dict[str, Any]:
    """Normalise one published value to its property's canonical unit, exactly.

    ``difference`` converts a difference or uncertainty: the scale factor
    applies, a unit offset does not. ``atoms_per_formula_unit`` is required
    only to move between per-atom and per-formula-unit bases.
    """

    definition = PROPERTIES.get(property_id)
    if definition is None:
        raise ValueError(f"unknown property definition {property_id!r}")
    native = exact(value)
    native_text = str(value)
    found, reason = _lookup(unit)
    if found is None:
        return _unknown(native_text, unit, reason)
    key, kind, factor, offset, basis = found
    if kind != definition["kind"]:
        return _unknown(
            native_text,
            unit,
            f"unit {unit!r} measures {kind}, not {definition['dimension']}",
        )
    steps = [
        {
            "step": "to base",
            "factor": fraction_text(factor),
            "offset": "0" if difference else fraction_text(offset),
        }
    ]
    base = native * factor + (0 if difference else offset)
    target_basis = definition["basis"]
    if basis != target_basis:
        if target_basis == "atom" and basis == "formula-unit":
            if atoms_per_formula_unit is None:
                return _unknown(
                    native_text,
                    unit,
                    "per-formula-unit value without a composition to count atoms",
                )
            atoms = Fraction(atoms_per_formula_unit)
            if atoms <= 0:
                return _unknown(
                    native_text, unit, "atoms per formula unit must be positive"
                )
            base = base / atoms
            steps.append(
                {
                    "step": "per formula unit -> per atom",
                    "divide_by": fraction_text(atoms),
                    "basis": "atoms in the formula unit as the source writes it",
                }
            )
        else:
            stated = basis or "unstated"
            return _unknown(
                native_text,
                unit,
                f"basis {stated} does not match the property's basis "
                f"({target_basis or 'none'})",
            )
    _, c_factor, c_offset, _ = _UNITS[definition["canonical_unit"]]
    result = (base - (0 if difference else c_offset)) / c_factor
    steps.append(
        {
            "step": "base to canonical",
            "divide_by": fraction_text(c_factor),
            "subtract": "0" if difference else fraction_text(c_offset),
        }
    )
    text, is_exact = decimal_text(result)
    receipt = {
        "method": METHOD,
        "table": TABLE_VERSION,
        "native": {"value": native_text, "unit": unit},
        "table_unit": key,
        "property": property_id,
        "difference": difference,
        "steps": steps,
        "result": {
            "value": text,
            "unit": definition["canonical_unit"],
            "exact": fraction_text(result),
        },
    }
    return {
        "native_value": native_text,
        "native_unit": unit,
        "normalized_value": text,
        "normalized_unit": definition["canonical_unit"],
        "normalized_exact": fraction_text(result),
        "exact_decimal": is_exact,
        "unit_status": "normalized",
        "comparable": True,
        "receipt": {**receipt, "sha256": _digest(receipt)},
    }


def normalise_condition(kind: str, value: Any, unit: Any) -> dict[str, Any]:
    """A stated temperature or pressure in kelvin / pascal, exactly; unknown units are reported, not guessed."""

    target = CONDITION_UNITS[kind]
    native = exact(value)
    found, reason = _lookup(unit)
    if found is None or found[1] != kind:
        unknown = {
            "value": str(value),
            "unit": unit,
            "status": "stated",
            "unit_status": "unit_unknown",
            "reason": reason or f"unit {unit!r} is not a {kind} unit",
        }
        return {k: v for k, v in unknown.items() if v is not None}
    _, _, factor, offset, _ = found
    result = native * factor + offset
    text, is_exact = decimal_text(result)
    return {
        "value": str(value),
        "unit": unit,
        "status": "stated",
        "unit_status": "normalized",
        "normalized_value": text,
        "normalized_unit": target,
        "normalized_exact": fraction_text(result),
        "exact_decimal": is_exact,
    }


def canonical_fraction(
    value: Any, unit: Any, property_id: str, *, atoms_per_formula_unit: Any = None
) -> Fraction | None:
    """A query bound in the property's canonical unit (exact), or ``None`` when its unit is unknown."""

    result = normalise(
        value, unit, property_id, atoms_per_formula_unit=atoms_per_formula_unit
    )
    return (
        None
        if result["unit_status"] != "normalized"
        else Fraction(result["normalized_exact"])
    )


def condition_fraction(kind: str, value: Any, unit: Any) -> Fraction | None:
    result = normalise_condition(kind, value, unit)
    return (
        Fraction(result["normalized_exact"])
        if result["unit_status"] == "normalized"
        else None
    )
