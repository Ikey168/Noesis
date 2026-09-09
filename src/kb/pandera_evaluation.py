"""Optional release-pinned validation; originals and production ingestion survive."""

import base64
import csv
import io
import json
import math
import re
import time
from datetime import date
from importlib.metadata import version

from src.kb.dataset_intelligence import (
    READ_SCOPE,
    DatasetIntelligenceError,
    _digest,
    _require,
)

PIN = "0.33.1"


def _valid_type(value, kind):
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return type(value) is int
    if kind == "number":
        return type(value) in {int, float} and math.isfinite(value)
    if kind == "boolean":
        return type(value) is bool
    if kind == "json":
        return isinstance(value, (list, dict))
    if kind == "date":
        try:
            date.fromisoformat(str(value))
            return True
        except ValueError:
            return False
    return False


def _parse(content, format, delimiter, max_rows):
    if len(content.encode()) > 2_000_000:
        raise DatasetIntelligenceError("input_limit", "validation input exceeds 2 MB")
    if format == "csv":
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        names = reader.fieldnames or []
        if not names or len(names) > 100 or len(set(names)) != len(names):
            raise DatasetIntelligenceError(
                "invalid_columns", "one to 100 unique CSV headers required"
            )
        rows = []
        for row in reader:
            if None in row or len(rows) >= max_rows:
                raise DatasetIntelligenceError(
                    "row_limit", "CSV row count or width exceeds bounds"
                )
            rows.append(row)
        return rows, content.encode()
    if format != "parquet":
        raise DatasetIntelligenceError(
            "format_unsupported", "validation supports CSV or base64 Parquet"
        )
    import pyarrow as pa
    import pyarrow.parquet as pq

    raw = base64.b64decode(content, validate=True)
    parquet = pq.ParquetFile(pa.BufferReader(raw))
    metadata = parquet.metadata
    expanded = sum(
        metadata.row_group(i).total_byte_size for i in range(metadata.num_row_groups)
    )
    if (
        metadata.num_rows > max_rows
        or metadata.num_columns > 100
        or expanded > 50_000_000
    ):
        raise DatasetIntelligenceError(
            "parquet_limit",
            "Parquet declared rows, columns or expanded bytes exceed bounds",
        )
    return parquet.read().to_pylist(), raw


def validate_release(
    store,
    namespace,
    release_id,
    table_id,
    *,
    content,
    format="csv",
    delimiter=",",
    coercion="native",
    constraints=None,
    max_rows=10000,
    max_errors=100,
    principal_id,
    scopes,
):
    _require(scopes, READ_SCOPE)
    if (
        not principal_id
        or "operator" not in scopes
        and f"namespace:{namespace}:write" not in scopes
    ):
        raise DatasetIntelligenceError(
            "unauthorized",
            "authenticated namespace writer required to retain validation",
        )
    if (
        coercion not in {"none", "native", "de-DE"}
        or delimiter not in {",", ";", "\t"}
        or not 1 <= max_rows <= 10000
        or not 1 <= max_errors <= 1000
    ):
        raise DatasetIntelligenceError(
            "invalid_controls", "unsupported coercion, delimiter or limits"
        )
    release, table = store._table(namespace, release_id, table_id)
    constraints = constraints or {
        "version": "1",
        "ranges": {},
        "unique": [],
        "cross_fields": [],
    }
    names = {c["name"] for c in table["columns"]}
    if (
        not isinstance(constraints, dict)
        or set(constraints) != {"version", "ranges", "unique", "cross_fields"}
        or not isinstance(constraints["version"], str)
        or not constraints["version"]
        or len(json.dumps(constraints)) > 20000
    ):
        raise DatasetIntelligenceError(
            "invalid_checks", "versioned bounded declarative constraints required"
        )
    ranges, unique, cross = (
        constraints[k] for k in ("ranges", "unique", "cross_fields")
    )
    if (
        not isinstance(ranges, dict)
        or not set(ranges) <= names
        or not isinstance(unique, list)
        or any(not isinstance(n, str) or n not in names for n in unique)
        or not isinstance(cross, list)
        or len(cross) > 20
    ):
        raise DatasetIntelligenceError(
            "invalid_checks", "checks must use pinned schema columns"
        )
    for limits in ranges.values():
        if (
            not isinstance(limits, dict)
            or not set(limits) <= {"min", "max"}
            or any(
                type(v) not in {int, float} or not math.isfinite(v)
                for v in limits.values()
            )
        ):
            raise DatasetIntelligenceError(
                "invalid_checks", "ranges need finite numeric bounds"
            )
    for check in cross:
        if (
            not isinstance(check, dict)
            or set(check) != {"left", "op", "right"}
            or check["left"] not in names
            or check["right"] not in names
            or check["op"] not in {"le", "lt", "eq"}
        ):
            raise DatasetIntelligenceError(
                "invalid_checks", "cross-field checks support le/lt/eq"
            )
    config = {
        "release_id": release_id,
        "dataset_revision_id": release["dataset_revision_id"],
        "release_hash": release["release_hash"],
        "table_id": table_id,
        "pandera": PIN,
        "constraints": constraints,
        "coercion": coercion,
        "format": format,
        "delimiter": delimiter,
        "max_rows": max_rows,
        "max_errors": max_errors,
    }
    identity = "pandera-validation:" + _digest([namespace, config, content])
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS pandera_validations(validation_id TEXT PRIMARY KEY, namespace TEXT, receipt TEXT, original_payload BLOB)"
    )
    prior = store.conn.execute(
        "SELECT receipt,original_payload FROM pandera_validations WHERE validation_id=? AND namespace=?",
        [identity, namespace],
    ).fetchone()
    if prior:
        receipt = json.loads(prior[0])
        if _digest(bytes(prior[1]).hex()) != receipt["original_payload_hash"]:
            raise DatasetIntelligenceError(
                "replay_mismatch", "retained original validation bytes changed"
            )
        return receipt
    if version("pandera") != PIN:
        raise DatasetIntelligenceError(
            "unsupported_version", "Pandera " + PIN + " required"
        )
    import pandas as pd
    import pandera.pandas as pa

    start = time.perf_counter()
    rows, raw = _parse(content, format, delimiter, max_rows)
    if len(table["columns"]) > 100:
        raise DatasetIntelligenceError(
            "column_limit", "pinned schema exceeds 100 columns"
        )
    errors, coercions, normalized = [], [], []

    def error(index, column, phase, reason, value):
        errors.append(
            {
                "row_index": index,
                "csv_line": index + 2
                if format == "csv" and index is not None
                else None,
                "column": column,
                "phase": phase,
                "check": reason,
                "original_value": str(value)[:200],
            }
        )

    for i, row in enumerate(rows):
        mapped = {}
        for unknown in set(row) - names:
            error(i, unknown, "validation", "unknown_column", row[unknown])
        for column in table["columns"]:
            name, kind = column["name"], column["type"]
            if kind not in {"string", "integer", "number", "boolean", "date", "json"}:
                raise DatasetIntelligenceError(
                    "unsupported_type", "unsupported Noesis unit type: " + kind
                )
            value = row.get(name)
            original = value
            if coercion != "none":
                try:
                    if (
                        coercion == "de-DE"
                        and kind == "number"
                        and isinstance(value, str)
                        and value
                    ):
                        if not re.fullmatch(
                            r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?", value
                        ):
                            raise ValueError("invalid declared German decimal")
                        value = value.replace(".", "").replace(",", ".")
                    value = store._value(value, kind)
                    if type(value) is not type(original) or value != original:
                        coercions.append(
                            {
                                "row_index": i,
                                "column": name,
                                "from": str(original)[:200],
                                "to": str(value)[:200],
                                "policy": coercion,
                            }
                        )
                except (ValueError, TypeError) as exc:
                    error(i, name, "coercion", str(exc)[:100], original)
                    value = None
            mapped[name] = value
        normalized.append(mapped)
    frame = pd.DataFrame(
        normalized, columns=[c["name"] for c in table["columns"]], dtype=object
    )
    columns = {}
    for column in table["columns"]:
        name, kind = column["name"], column["type"]
        checks = [
            pa.Check(
                lambda v, kind=kind: _valid_type(v, kind),
                element_wise=True,
                name="type:" + kind,
            )
        ]
        for bound, value in ranges.get(name, {}).items():
            checks.append(
                pa.Check(
                    lambda v, bound=bound, value=value: (
                        _valid_type(v, "number")
                        and (v >= value if bound == "min" else v <= value)
                    ),
                    element_wise=True,
                    name="range:" + bound + ":" + str(value),
                )
            )
        columns[name] = pa.Column(
            None,
            checks=checks,
            nullable=column["nullable"],
            unique=name in unique,
            coerce=False,
        )
    dataframe_checks = []
    for check in cross:

        def compare(frame, check=check):
            def valid(row):
                left, right = row[check["left"]], row[check["right"]]
                if left is None or right is None:
                    return True
                try:
                    return (
                        left <= right
                        if check["op"] == "le"
                        else left < right
                        if check["op"] == "lt"
                        else left == right
                    )
                except TypeError:
                    return False

            return frame.apply(valid, axis=1)

        dataframe_checks.append(
            pa.Check(compare, name="cross:" + json.dumps(check, sort_keys=True))
        )
    primary_key = list(table.get("primary_key") or [])
    if primary_key:
        dataframe_checks.append(
            pa.Check(
                lambda frame, keys=primary_key: ~frame.duplicated(
                    subset=keys, keep=False
                ),
                name="primary_key:" + ",".join(primary_key),
            )
        )
    schema = pa.DataFrameSchema(
        columns, checks=dataframe_checks, coerce=False, strict=True
    )
    try:
        schema.validate(frame, lazy=True)
    except pa.errors.SchemaErrors as failure:
        for item in failure.failure_cases.to_dict("records"):
            index = item.get("index")
            index = int(index) if index is not None and not pd.isna(index) else None
            column = item.get("column")
            original = (
                rows[index].get(column)
                if index is not None and column in rows[index]
                else item.get("failure_case")
            )
            error(index, column, "validation", str(item.get("check")), original)
    receipt = {
        "contract": "noesis-pandera-validation-v1",
        "validation_id": identity,
        "namespace": namespace,
        "config": config,
        "status": "rejected" if errors else "validated",
        "rows": len(rows),
        "error_count": len(errors),
        "errors": errors[:max_errors],
        "errors_truncated": len(errors) > max_errors,
        "coercion_count": len(coercions),
        "coercions": coercions[:max_errors],
        "coercions_truncated": len(coercions) > max_errors,
        "original_payload_hash": _digest(raw.hex()),
        "original_bytes": len(raw),
        "release_provenance": release["provenance"],
        "elapsed_ms": (time.perf_counter() - start) * 1000,
        "production_ingestion_changed": False,
    }
    store.conn.execute(
        "INSERT INTO pandera_validations VALUES(?,?,?,?)",
        [identity, namespace, json.dumps(receipt), raw],
    )
    return receipt
