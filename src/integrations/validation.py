"""Declarative Pandera validation without implicit coercion."""

from .common import IntegrationError, receipt


def validate_rows(rows, columns, *, unique=None, comparisons=(), max_errors=100):
    import pandas as pd
    import pandera.pandas as pa

    if len(rows) > 10000 or len(columns) > 100 or not 1 <= max_errors <= 1000:
        raise IntegrationError(
            "input_limit", "Validation batch/schema/report exceeds limits"
        )
    types = {"string": str, "integer": int, "number": float, "boolean": bool}
    schema = {}
    for column in columns:
        checks = []
        for key, factory in [
            ("minimum", pa.Check.ge),
            ("maximum", pa.Check.le),
            ("allowed", pa.Check.isin),
        ]:
            if key in column:
                checks.append(factory(column[key]))
        kind = column["type"]
        if kind not in types:
            raise IntegrationError(
                "unsupported_type", f"Unsupported validation type: {kind}"
            )
        schema[column["name"]] = pa.Column(
            types[kind],
            checks=checks,
            nullable=column.get("nullable", False),
            coerce=False,
        )
    frame_checks = []
    for comparison in comparisons:
        if set(comparison) != {"left", "operator", "right"} or comparison[
            "operator"
        ] not in {"le", "lt", "eq", "ge", "gt"}:
            raise IntegrationError("invalid_comparison", "Use named-column comparisons")
        left, right = comparison["left"], comparison["right"]
        op = comparison["operator"]
        if left not in schema or right not in schema:
            raise IntegrationError("invalid_comparison", "Unknown column")
        frame_checks.append(
            pa.Check(
                lambda df, l=left, r=right, o=op: getattr(df[l], o)(df[r]),
                name=f"{left}_{op}_{right}",
            )
        )
    validator = pa.DataFrameSchema(
        schema, checks=frame_checks, strict=True, unique=unique or None, coerce=False
    )
    frame = pd.DataFrame(rows, columns=list(schema) if not rows else None)
    failures = []
    total = 0
    try:
        validator.validate(frame, lazy=True)
    except pa.errors.SchemaErrors as exc:
        total = len(exc.failure_cases)
        failures = __import__("json").loads(
            exc.failure_cases.head(max_errors).to_json(orient="records")
        )
    return receipt(
        "pandera",
        "pandera",
        {
            "rows_sha256": __import__(
                "src.integrations.common", fromlist=["digest"]
            ).digest(rows),
            "columns": columns,
            "unique": unique,
            "comparisons": list(comparisons),
        },
        {
            "valid": total == 0,
            "failure_count": total,
            "failures": failures,
            "truncated": total > max_errors,
            "coercion": False,
            "row_count": len(rows),
        },
    )
