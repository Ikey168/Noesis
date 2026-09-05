"""Pint conversion restricted to explicit physical-unit definitions."""

from decimal import Decimal, ROUND_HALF_EVEN
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
