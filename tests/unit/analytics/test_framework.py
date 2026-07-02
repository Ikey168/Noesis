"""Unit tests for the batch analytics framework (src/analytics/framework.py)."""

from typing import Any, Dict, List

from src.analytics.framework import AnalyticJob, read_results, run_job


class ToyJob(AnalyticJob):
    name = "toy"
    result_table = "toy_results"

    def __init__(self, fail=False):
        self.fail = fail

    def result_ddl(self):
        return "CREATE TABLE IF NOT EXISTS toy_results (k VARCHAR PRIMARY KEY, v INTEGER)"

    def compute(self, conn) -> List[Dict[str, Any]]:
        if self.fail:
            raise RuntimeError("compute exploded")
        return [{"k": "a", "v": 1}, {"k": "b", "v": 2}]

    def store(self, conn, rows):
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO toy_results VALUES (?, ?)", [r["k"], r["v"]]
            )

    def params(self):
        return {"threshold": 3}

    def summary(self, rows):
        return {"total": sum(r["v"] for r in rows)}


def test_run_job_end_to_end(conn, lock):
    result = run_job(ToyJob(), conn=conn, lock=lock, log_mlflow=False)
    assert result["job"] == "toy"
    assert result["rows"] == 2
    assert result["total"] == 3
    assert result["mlflow_logged"] is False
    # The result table was created and populated.
    rows = read_results(conn, "toy_results", order_by="k")
    assert rows == [{"k": "a", "v": 1}, {"k": "b", "v": 2}]


def test_run_job_is_idempotent(conn, lock):
    run_job(ToyJob(), conn=conn, lock=lock, log_mlflow=False)
    run_job(ToyJob(), conn=conn, lock=lock, log_mlflow=False)
    assert len(read_results(conn, "toy_results")) == 2  # upsert, not duplicated


def test_run_job_reports_compute_error(conn, lock):
    result = run_job(ToyJob(fail=True), conn=conn, lock=lock, log_mlflow=False)
    assert "error" in result and "toy failed" in result["error"]


def test_mlflow_logging_degrades_when_unavailable(conn, lock, monkeypatch):
    # With MLflow requested but not installed, logging is skipped, not fatal.
    import builtins

    real_import = builtins.__import__

    def no_mlflow(name, *args, **kwargs):
        if name == "mlflow":
            raise ImportError("no mlflow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_mlflow)
    result = run_job(ToyJob(), conn=conn, lock=lock, log_mlflow=True)
    assert result["mlflow_logged"] is False
    assert result["rows"] == 2


def test_read_results_where_and_limit(conn, lock):
    run_job(ToyJob(), conn=conn, lock=lock, log_mlflow=False)
    rows = read_results(conn, "toy_results", where="v > ?", params=[1])
    assert rows == [{"k": "b", "v": 2}]
    assert len(read_results(conn, "toy_results", limit=1)) == 1


def test_run_job_falls_back_to_shared_connection(conn, monkeypatch):
    # With conn/lock omitted, run_job resolves the process-wide warehouse.
    import src.database.local_analytics_connector as connector

    monkeypatch.setattr(connector, "get_shared_connection", lambda: conn)
    result = run_job(ToyJob(), log_mlflow=False)
    assert result["rows"] == 2
    assert read_results(conn, "toy_results", order_by="k")[0] == {"k": "a", "v": 1}


def test_mlflow_success_path_is_logged(conn, lock, monkeypatch):
    # Inject a fake mlflow module so the logging path runs without the real SDK.
    import sys
    import types
    from contextlib import contextmanager

    calls = {"params": {}, "metrics": {}, "experiment": None}
    fake = types.ModuleType("mlflow")

    @contextmanager
    def start_run(run_name=None):
        calls["run_name"] = run_name
        yield None

    fake.set_experiment = lambda name: calls.__setitem__("experiment", name)
    fake.start_run = start_run
    fake.log_param = lambda k, v: calls["params"].__setitem__(k, v)
    fake.log_metric = lambda k, v: calls["metrics"].__setitem__(k, v)
    monkeypatch.setitem(sys.modules, "mlflow", fake)

    result = run_job(ToyJob(), conn=conn, lock=lock, log_mlflow=True)
    assert result["mlflow_logged"] is True
    assert calls["experiment"] == "noesis-analytics"
    assert calls["params"] == {"threshold": 3}
    assert calls["metrics"]["total"] == 3  # metrics logged


def test_mlflow_failure_is_swallowed(conn, lock, monkeypatch):
    import sys
    import types

    fake = types.ModuleType("mlflow")

    def boom(name):
        raise RuntimeError("mlflow server down")

    fake.set_experiment = boom
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    result = run_job(ToyJob(), conn=conn, lock=lock, log_mlflow=True)
    assert result["mlflow_logged"] is False  # never fatal
    assert result["rows"] == 2
