"""
Batch analytics framework (MCP rearchitecture plan, R5 / Track DS).

The precompute-heavy / serve-light pattern the analytics plane is built on:
a fit runs as a batch job (from a scheduler or a ``trigger_*`` MCP tool),
writes a **result table** in the warehouse, and logs the run to MLflow for
reproducibility. The panel-facing MCP tools then *read* those result tables
(computing on-demand only when it is cheap). This mirrors how
``outlet_scores`` already works, generalized so every analytic follows the
same shape.

An :class:`AnalyticJob` declares its result table, computes rows from the
source tables, and stores them idempotently. :func:`run_job` wires the
lock-serialized compute+store together and logs to MLflow when available
(a missing MLflow degrades to a warning — never a failure).

Stdlib-only at import; MLflow and the warehouse connector are imported
lazily so a tool server importing an ``AnalyticJob`` never pays for them.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


class AnalyticJob(ABC):
    """A batch analytic: compute rows from source tables into a result table.

    Subclasses set ``name`` and ``result_table`` and implement
    :meth:`result_ddl`, :meth:`compute` and :meth:`store`. Keep ``compute``
    read-only against the source tables; ``store`` is the only writer.
    """

    name: str = "analytic"
    result_table: str = "analytics_results"

    @abstractmethod
    def result_ddl(self) -> str:
        """``CREATE TABLE IF NOT EXISTS`` for the result table."""

    @abstractmethod
    def compute(self, conn) -> List[Dict[str, Any]]:
        """Compute result rows from the warehouse (read-only)."""

    @abstractmethod
    def store(self, conn, rows: List[Dict[str, Any]]) -> None:
        """Persist result rows idempotently (upsert keyed by the PK)."""

    def summary(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Metrics logged to MLflow and returned to the caller. Override to
        add job-specific figures; the row count is always included."""
        return {}

    def params(self) -> Dict[str, Any]:
        """Params logged to MLflow (config that shaped the fit)."""
        return {}


def read_results(
    conn,
    table: str,
    where: Optional[str] = None,
    params: Optional[Sequence[Any]] = None,
    order_by: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Read a result table as a list of dict rows (column-name keyed).

    The ``table`` and ``order_by`` are trusted (they come from job code, never
    user input); values are always bound as parameters.
    """
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += " LIMIT ?"
    bound = list(params or [])
    if limit is not None:
        bound.append(int(limit))
    cur = conn.execute(sql, bound)
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _log_to_mlflow(job_name: str, params: Dict[str, Any], metrics: Dict[str, Any]) -> bool:
    """Log a fit to MLflow; return whether it was logged. A missing MLflow or
    any logging error degrades to False (with a debug log), never raising."""
    try:
        import mlflow  # lazy: optional dependency
    except Exception:
        logger.debug("MLflow not available; skipping run logging for %s", job_name)
        return False
    try:
        mlflow.set_experiment("noesis-analytics")
        with mlflow.start_run(run_name=job_name):
            for key, value in params.items():
                mlflow.log_param(key, value)
            for key, value in metrics.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    mlflow.log_metric(key, value)
        return True
    except Exception:
        logger.warning("MLflow logging failed for %s", job_name, exc_info=True)
        return False


def run_job(
    job: AnalyticJob,
    conn=None,
    lock=None,
    log_mlflow: bool = True,
) -> Dict[str, Any]:
    """Run one analytic fit end-to-end: ensure the result table, compute under
    the warehouse lock, store, log to MLflow, and return a summary.

    Returns ``{"error": ...}`` on a compute/store failure (mirroring the
    existing batch runners), so a ``trigger_*`` tool can surface it cleanly.
    """
    if conn is None:
        from src.database.local_analytics_connector import get_shared_connection

        conn = get_shared_connection()
    if lock is None:
        import threading

        from src.database import local_analytics_connector as _conn_mod

        lock = getattr(_conn_mod, "_LOCK", None) or threading.Lock()

    try:
        with lock:
            conn.execute(job.result_ddl())
            rows = job.compute(conn)
            job.store(conn, rows)
    except Exception as exc:
        logger.warning("analytic job %s failed", job.name, exc_info=True)
        return {"error": f"{job.name} failed: {exc}"}

    metrics = {"rows": len(rows), **job.summary(rows)}
    mlflow_logged = _log_to_mlflow(job.name, job.params(), metrics) if log_mlflow else False
    return {
        "job": job.name,
        "result_table": job.result_table,
        "rows": len(rows),
        "mlflow_logged": mlflow_logged,
        **job.summary(rows),
    }
