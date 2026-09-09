"""Optional Pint evaluation over immutable Noesis unit definitions."""

import hashlib
import json
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from functools import lru_cache
from importlib.metadata import version

from src.kb.quantitative import CALCULATE_SCOPE, QuantitativeError, _decimal, _require

PIN = "0.25.2"


@lru_cache(maxsize=32)
def _registry(serial):
    from pint import UnitRegistry

    if version("pint") != PIN:
        raise QuantitativeError(
            "unsupported_backend_version", "Pint evaluation requires version " + PIN
        )
    units = json.loads(serial)
    registry = UnitRegistry(
        None, non_int_type=Decimal, autoconvert_offset_to_baseunit=False
    )
    dimensions = sorted({d for u in units for d in u["dimension"]})
    bases = {d: "b" + hashlib.sha256(d.encode()).hexdigest()[:16] for d in dimensions}
    for name in bases.values():
        registry.define(f"{name} = [d{name}]")
    names = {}
    for unit in units:
        name = "u" + hashlib.sha256(unit["unit_id"].encode()).hexdigest()[:16]
        names[unit["unit_id"]] = name
        base = (
            " * ".join(
                f"{bases[d]} ** {power}"
                for d, power in sorted(unit["dimension"].items())
            )
            or "1"
        )
        factor, offset = _decimal(unit["factor"]), _decimal(unit["offset"])
        definition = f"{name} = {factor} * {base}"
        if offset:
            definition += f"; offset: {factor * offset}"
        registry.define(definition)
    return registry, names, bases


class PintUnitEvaluation:
    def __init__(self, store):
        self.store = store

    def _units(self, namespace, symbols):
        units = [self.store._unit(symbol, namespace) for symbol in symbols]
        if any(u["currency_code"] or "currency" in u["dimension"] for u in units):
            raise QuantitativeError(
                "unsupported_currency",
                "currency semantics require the existing Noesis rate-evidence path",
            )
        serial = json.dumps(
            sorted(
                {u["unit_id"]: u for u in units}.values(), key=lambda u: u["unit_id"]
            ),
            sort_keys=True,
        )
        return units, serial

    def convert(
        self, namespace, value, from_unit, to_unit, *, scopes, principal_id, precision=6
    ):
        _require(scopes, CALCULATE_SCOPE)
        if not principal_id:
            raise QuantitativeError("unauthorized", "authenticated principal required")
        units, serial = self._units(namespace, [from_unit, to_unit])
        source, target = units
        registry, names, _ = _registry(serial)
        if source["dimension"] != target["dimension"]:
            raise QuantitativeError(
                "dimensional_error", "units have incompatible dimensions"
            )
        number = _decimal(value)
        # Pin decimal arithmetic independently of the calling process context.
        with localcontext() as context:
            context.prec = 28
            converted = (
                registry.Quantity(number, names[source["unit_id"]])
                .to(names[target["unit_id"]])
                .magnitude
            )
            quant = Decimal(1).scaleb(-min(max(int(precision), 0), 12))
            output = str(Decimal(converted).quantize(quant, rounding=ROUND_HALF_EVEN))
        request = {
            "value": str(value),
            "from_unit": source["unit_id"],
            "to_unit": target["unit_id"],
            "precision": precision,
            "backend": "pint",
            "backend_version": PIN,
            "registry_hash": hashlib.sha256(serial.encode()).hexdigest(),
            "decimal_precision": 28,
        }
        return self.store._calculation(
            namespace,
            "conversion",
            request,
            {"value": output, "unit_id": target["unit_id"]},
            input_ids=[u["unit_id"] for u in units],
            principal_id=principal_id,
            formula_revision_id=None,
        )

    def product_dimensions(self, namespace, terms, *, scopes):
        _require(scopes, CALCULATE_SCOPE)
        if not 1 <= len(terms) <= 20 or any(
            type(p) is not int or not -8 <= p <= 8 for p in terms.values()
        ):
            raise QuantitativeError(
                "invalid_formula", "bounded integer unit powers required"
            )
        units, serial = self._units(namespace, list(terms))
        registry, names, bases = _registry(serial)
        result = registry.Quantity(Decimal(1))
        for unit, power in zip(units, terms.values(), strict=True):
            if _decimal(unit["offset"]):
                raise QuantitativeError(
                    "offset_formula",
                    "absolute offset units require explicit difference semantics",
                )
            result *= registry.Quantity(Decimal(1), names[unit["unit_id"]]) ** power
        dimensions = {
            d: int(result.dimensionality.get("[d" + b + "]", 0))
            for d, b in bases.items()
        }
        return {d: p for d, p in dimensions.items() if p}
