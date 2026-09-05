"""Pint conversion restricted to explicit physical-unit definitions."""

import ast
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException

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


def evaluate_registered_formula(expression, inputs, target, *, precision=6):
    """Evaluate bounded arithmetic with dimensional quantities, without eval()."""
    import pint

    from .common import digest

    if type(precision) is not int or not 0 <= precision <= 12:
        raise IntegrationError("invalid_precision", "precision must be 0..12")
    if (
        not isinstance(expression, str)
        or not 1 <= len(expression) <= 4096
        or not 1 <= len(inputs) <= 32
    ):
        raise IntegrationError("input_limit", "Formula and inputs exceed bounds")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise IntegrationError("unsafe_formula", "Invalid formula expression") from exc
    if len(list(ast.walk(tree))) > 128:
        raise IntegrationError("input_limit", "Formula exceeds operation budget")
    definitions = {name: item["unit_definition"] for name, item in inputs.items()}
    definitions["$result"] = target
    units = pint.UnitRegistry(None, non_int_type=Decimal, on_redefinition="raise")
    dimensions = sorted(
        {
            dimension
            for definition in definitions.values()
            for dimension in definition["dimension"]
        }
    )
    if len(dimensions) > 32:
        raise IntegrationError("input_limit", "Too many formula dimensions")
    base_names = {
        dimension: "base_" + digest(dimension)[:20] for dimension in dimensions
    }
    for name in base_names.values():
        units.define(name + " = [" + name + "]")
    names = {}
    for index, (name, definition) in enumerate(sorted(definitions.items())):
        if definition.get("currency_code") or "currency" in definition["dimension"]:
            raise IntegrationError(
                "economic_unit", "Economic semantics remain in Noesis"
            )
        factor, offset = Decimal(definition["factor"]), Decimal(definition["offset"])
        if not factor.is_finite() or factor <= 0 or not offset.is_finite():
            raise IntegrationError("invalid_registry", "Invalid unit definition")
        if offset:
            raise IntegrationError(
                "offset_formula",
                "Convert offset units explicitly before formula evaluation",
            )
        terms = []
        for dimension, exponent in sorted(definition["dimension"].items()):
            if type(exponent) is not int or abs(exponent) > 32:
                raise IntegrationError("invalid_registry", "Invalid dimension exponent")
            terms.append(base_names[dimension] + " ** " + str(exponent))
        names[name] = "quantity_" + str(index)
        units.define(
            names[name]
            + " = "
            + str(factor)
            + (" * " + " * ".join(terms) if terms else "")
        )
    values = {}
    for name, item in inputs.items():
        try:
            number = Decimal(str(item["value"]))
        except DecimalException as exc:
            raise IntegrationError(
                "invalid_input", "Formula values must be decimals"
            ) from exc
        if not number.is_finite():
            raise IntegrationError("invalid_input", "Formula values must be finite")
        values[name] = units.Quantity(number, names[name])

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return units.Quantity(Decimal(ast.get_source_segment(expression, node)), "")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -evaluate(node.operand)
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right
        raise IntegrationError("unsafe_formula", "Formula expression is unsupported")

    try:
        quantity = evaluate(tree).to(names["$result"])
        value = quantity.magnitude.quantize(
            Decimal(1).scaleb(-precision), rounding=ROUND_HALF_EVEN
        )
    except pint.errors.PintError as exc:
        raise IntegrationError(
            "dimensional_error", "Formula has incompatible dimensions"
        ) from exc
    except (DecimalException, ZeroDivisionError) as exc:
        raise IntegrationError("arithmetic_error", "Formula arithmetic failed") from exc
    return receipt(
        "pint",
        "pint",
        {
            "expression": expression,
            "inputs": inputs,
            "target_unit": target,
            "precision": precision,
            "registry_hash": digest(definitions),
        },
        {
            "value": str(value),
            "unit_id": target["unit_id"],
            "dimension": target["dimension"],
        },
    )
