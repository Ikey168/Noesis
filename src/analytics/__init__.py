"""
The analytics plane (MCP rearchitecture plan, R5 / Track DS).

Data-science techniques exposed as canvas capabilities under the
statistical-honesty contract: fits run as batch jobs writing result tables
(:mod:`src.analytics.framework`), the MCP tools read those tables, and every
output carries its sample size, method and assumptions
(:mod:`src.analytics.honesty`). Wave 1a ships anomaly detection
(:mod:`src.analytics.anomalies`) plus score-confidence and
stance-significance (:mod:`src.analytics.confidence`).

Stdlib-only maths (:mod:`src.analytics.stats`) so the tool servers stay
import-safe and the analytics run without numpy/scipy.
"""

from src.analytics.framework import AnalyticJob, read_results, run_job
from src.analytics.honesty import (
    analytic_envelope,
    honesty_output_schema,
    interval,
    validate_analytic_output,
)

__all__ = [
    "AnalyticJob",
    "run_job",
    "read_results",
    "analytic_envelope",
    "honesty_output_schema",
    "interval",
    "validate_analytic_output",
]
