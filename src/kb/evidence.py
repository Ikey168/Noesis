"""
Evidence-quality summary: "what fraction of this analysis is model-grade?"

The platform's honesty contract requires that any analytic answer can state
its evidence quality. Prediction rows carry ``prediction_mode`` (#958) and a
confidence; this module aggregates them per table and overall, and the KB
contract attaches the summary to every ``coverage`` answer, so it rides both
the MCP and REST surfaces for free.
"""

from __future__ import annotations

from typing import Any, Dict

#: prediction tables summarized (claim_links carries its own mode natively)
_TABLES = (
    "argument_claims",
    "source_stances",
    "document_frames",
    "policy_positions",
    "claim_conflicts",
    "stance_drift_events",
    "claim_links",
)


def _table_exists(conn, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchone()
        is not None
    )


def _has_column(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_name = ? AND column_name = ?",
            [table, column],
        ).fetchone()
        is not None
    )


def evidence_quality_summary(conn) -> Dict[str, Any]:
    """Per-table and overall mode distribution + mean confidence.

    ``model_grade_fraction`` counts fine-tuned and pretrained model modes
    against the total; rows with unrecognized or NULL modes are not silently
    counted as model output.
    """
    tables: Dict[str, Any] = {}
    total_rows = 0
    total_model_grade = 0

    for table in _TABLES:
        if not _table_exists(conn, table) or not _has_column(
            conn, table, "prediction_mode"
        ):
            continue
        modes = dict(
            conn.execute(
                f"SELECT COALESCE(prediction_mode, 'unknown'), COUNT(*)"
                f" FROM {table} GROUP BY 1"
            ).fetchall()
        )
        if not modes:
            continue
        mean_confidence = None
        if _has_column(conn, table, "confidence"):
            row = conn.execute(
                f"SELECT AVG(confidence) FROM {table}"
                " WHERE confidence IS NOT NULL"
            ).fetchone()
            if row and row[0] is not None:
                mean_confidence = round(float(row[0]), 4)
        rows = sum(int(count) for count in modes.values())
        model_grade = sum(
            int(count)
            for mode, count in modes.items()
            if mode.startswith(("model:", "pretrained:", "zero-shot:"))
        )
        tables[table] = {
            "rows": rows,
            "modes": {mode: int(count) for mode, count in modes.items()},
            "model_grade_fraction": round(model_grade / rows, 4) if rows else 0.0,
            "mean_confidence": mean_confidence,
        }
        total_rows += rows
        total_model_grade += model_grade

    return {
        "tables": tables,
        "total_rows": total_rows,
        "model_grade_fraction": (
            round(total_model_grade / total_rows, 4) if total_rows else 0.0
        ),
    }
