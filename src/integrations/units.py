"""Pint conversion restricted to explicit physical-unit definitions."""

from decimal import ROUND_HALF_EVEN, Decimal

from .common import IntegrationError, receipt


def convert_physical(value, source, target, *, precision=6):
    import pint

    if type(precision) is not int or not 0 <= precision <= 12:
        raise IntegrationError(
            "invalid_precision", "precision must be between zero and twelve"
        )
    number = Decimal(str(value))
    if not number.is_finite():
        raise IntegrationError("invalid_input", "value must be finite")
    # Default Pint physical definitions are versioned by the package receipt.
    # Currency and arbitrary namespaces must use the canonical quantitative ledger.
    units = pint.UnitRegistry(non_int_type=Decimal)
    try:
        result = units.Quantity(number, source).to(target)
    except (pint.errors.PintError, TypeError) as exc:
        raise IntegrationError("invalid_unit_conversion", str(exc)) from exc
    magnitude = Decimal(str(result.magnitude)).quantize(
        Decimal(1).scaleb(-precision), rounding=ROUND_HALF_EVEN
    )
    return receipt(
        "pint",
        "pint",
        {
            "value": str(value),
            "source": source,
            "target": target,
            "precision": precision,
        },
        {
            "value": str(magnitude),
            "unit": str(result.units),
            "dimension": str(result.dimensionality),
            "registry": "Pint default physical definitions; no FX or economic comparability semantics",
        },
    )


def convert_registered(value, source, target, *, precision=6):
    """Evaluate two resolved, immutable Noesis definitions in an empty registry.

    Symbols and aliases are resolved by Noesis before entry; generated Pint names
    prevent user unit text from becoming registry definition syntax.
    """
    import pint

    from .common import digest

    if type(precision) is not int or not 0 <= precision <= 12:
        raise IntegrationError(
            "invalid_precision", "precision must be between zero and twelve"
        )
    for definition in (source, target):
        if definition.get("currency_code") or "currency" in definition["dimension"]:
            raise IntegrationError(
                "economic_unit",
                "Currency conversions require the Noesis exchange-rate ledger",
            )
    units = pint.UnitRegistry(None, non_int_type=Decimal, on_redefinition="raise")
    dimensions = sorted(set(source["dimension"]) | set(target["dimension"]))
    names = {dimension: "base_" + digest(dimension)[:20] for dimension in dimensions}
    for name in names.values():
        units.define(name + " = [" + name + "]")
    for name, definition in (("source_unit", source), ("target_unit", target)):
        factor, offset = Decimal(definition["factor"]), Decimal(definition["offset"])
        if not factor.is_finite() or factor <= 0 or not offset.is_finite():
            raise IntegrationError(
                "invalid_registry", "Unit factor must be positive and offset finite"
            )
        terms = []
        for dimension, exponent in sorted(definition["dimension"].items()):
            if type(exponent) is not int or abs(exponent) > 32:
                raise IntegrationError(
                    "invalid_registry", "Unit dimension exponent exceeds limit"
                )
            terms.append(names[dimension] + " ** " + str(exponent))
        reference = " * ".join(terms)
        # Noesis stores base=(value+offset)*factor; Pint offset is in base units.
        specification = (
            name + " = " + str(factor) + (" * " + reference if reference else "")
        )
        if offset:
            specification += "; offset: " + str(offset * factor)
        units.define(specification)
    number = Decimal(str(value))
    if not number.is_finite():
        raise IntegrationError("invalid_input", "value must be finite")
    try:
        result = units.Quantity(number, "source_unit").to("target_unit")
    except pint.errors.PintError as exc:
        raise IntegrationError("invalid_unit_conversion", str(exc)) from exc
    magnitude = result.magnitude.quantize(
        Decimal(1).scaleb(-precision), rounding=ROUND_HALF_EVEN
    )
    definitions = [source, target]
    return receipt(
        "pint",
        "pint",
        {
            "value": str(value),
            "from_unit": source["unit_id"],
            "to_unit": target["unit_id"],
            "precision": precision,
            "unit_definitions": definitions,
            "registry_hash": digest(definitions),
        },
        {
            "value": str(magnitude),
            "unit_id": target["unit_id"],
            "dimension": target["dimension"],
        },
    )
