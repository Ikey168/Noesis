"""Exact, condition-aware unit normalisation without pint (MT03, #2081)."""

from __future__ import annotations

import builtins
import sys
from fractions import Fraction

import pytest

from src.kb import materials_units as mu


def test_canonical_unit_per_property_and_native_value_kept():
    assert mu.PROPERTIES["bulk_modulus"]["canonical_unit"] == "GPa"
    assert mu.PROPERTIES["formation_energy_per_atom"]["canonical_unit"] == "eV/atom"
    assert mu.PROPERTIES["thermal_conductivity"]["canonical_unit"] == "W/(m*K)"
    result = mu.normalise("250000", "MPa", "bulk_modulus")
    assert result["native_value"] == "250000" and result["native_unit"] == "MPa"
    assert result["normalized_value"] == "250" and result["normalized_unit"] == "GPa"
    assert result["exact_decimal"] is True and result["receipt"]["sha256"]
    assert (
        mu.normalise("0.25", "W/(cm·K)", "thermal_conductivity")["normalized_value"]
        == "25"
    )
    assert mu.normalise("4250", "kg/m³", "density")["normalized_value"] == "4.25"


def test_offset_units_convert_with_offset_and_differences_without():
    celsius = mu.normalise_condition("temperature", "25", "°C")
    fahrenheit = mu.normalise_condition("temperature", "77", "°F")
    assert celsius["normalized_value"] == fahrenheit["normalized_value"] == "298.15"
    assert Fraction(celsius["normalized_exact"]) == Fraction(
        fahrenheit["normalized_exact"]
    )  # exact, not float-close
    assert (
        mu.normalise("1500", "°C", "fusion_temperature")["normalized_value"]
        == "1773.15"
    )
    # An uncertainty of 1 degC is 1 K: the scale applies, the offset does not.
    assert (
        mu.normalise("1", "°C", "fusion_temperature", difference=True)[
            "normalized_value"
        ]
        == "1"
    )
    assert (
        mu.normalise("9", "°F", "fusion_temperature", difference=True)[
            "normalized_value"
        ]
        == "5"
    )
    assert (
        mu.normalise_condition("pressure", "1", "atm")["normalized_value"] == "101325"
    )
    assert (
        mu.normalise_condition("pressure", "1", "bar")["normalized_value"] == "100000"
    )


def test_per_formula_unit_and_per_atom_are_explicit():
    per_fu = mu.normalise(
        "-2.5", "eV/f.u.", "formation_energy_per_atom", atoms_per_formula_unit=3
    )
    assert per_fu["normalized_exact"] == "-5/6" and per_fu["exact_decimal"] is False
    assert per_fu["normalized_value"] == "-0.833333333333"
    assert any(
        step["step"] == "per formula unit -> per atom"
        for step in per_fu["receipt"]["steps"]
    )
    missing = mu.normalise("-2.5", "eV/f.u.", "formation_energy_per_atom")
    assert (
        missing["unit_status"] == "unit_unknown" and "composition" in missing["reason"]
    )
    assert "normalized_value" not in missing  # absent, never written as None
    bare = mu.normalise("-2.5", "eV", "formation_energy_per_atom")
    assert bare["unit_status"] == "unit_unknown" and "basis" in bare["reason"]
    # kJ/mol (per mole of formula units) -> eV per formula unit exactly (2019 SI e and N_A), then per atom.
    molar = mu.normalise(
        "-964.853321233100184",
        "kJ/mol",
        "standard_enthalpy_of_formation",
        atoms_per_formula_unit=5,
    )
    assert (
        Fraction(molar["normalized_exact"])
        == Fraction("-964.853321233100184") * mu.EV_PER_KJ_MOL / 5
    )
    assert (
        molar["normalized_value"] == "-2" and molar["exact_decimal"] is True
    )  # -10 eV per formula unit, 5 atoms


@pytest.mark.parametrize(
    ("unit", "reason"),
    [
        ("kcal/mol", "ambiguous"),
        ("A", "ambiguous"),
        ("furlong", "not in the materials unit table"),
        (None, "not stated"),
        ("K", "measures temperature"),
    ],
)
def test_unknown_and_ambiguous_units_fail_closed(unit, reason):
    result = mu.normalise(
        "1",
        unit,
        "bulk_modulus" if unit == "K" else "standard_enthalpy_of_formation",
        atoms_per_formula_unit=2,
    )
    assert result["unit_status"] == "unit_unknown" and reason in result["reason"]
    assert result["comparable"] is False and result["native_value"] == "1"


def test_floats_are_rejected_and_exact_fractions_are_kept():
    with pytest.raises(ValueError):
        mu.normalise(1.5, "GPa", "bulk_modulus")
    third = mu.normalise("1", "psi", "bulk_modulus")
    assert Fraction(third["normalized_exact"]) == mu.PSI / 10**9


def test_normalisation_is_identical_with_and_without_pint(monkeypatch):
    with_pint = [
        mu.normalise("25", "°C", "fusion_temperature"),
        mu.normalise(
            "-3", "kJ/mol", "standard_enthalpy_of_formation", atoms_per_formula_unit=3
        ),
    ]
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "pint" or name.startswith("pint."):
            raise ImportError("pint is not installed in this lane")
        return real_import(name, *args, **kwargs)

    monkeypatch.setitem(sys.modules, "pint", None)
    monkeypatch.setattr(builtins, "__import__", blocked)
    without = [
        mu.normalise("25", "°C", "fusion_temperature"),
        mu.normalise(
            "-3", "kJ/mol", "standard_enthalpy_of_formation", atoms_per_formula_unit=3
        ),
    ]
    assert with_pint == without
    assert "pint" not in mu.__dict__
