import pytest

from src.evaluation.runtime_jobs import execute_job


def test_isolated_job_completes_and_records_bounds():
    result = execute_job("health", {}, timeout_s=30)
    assert result["status"] == "completed", result
    assert "e5" in result["result"]["backends"]
    assert result["peak_process_tree_rss_bytes"] > 0
    assert result["hosted_inference_used"] is False


def test_deadline_and_precancellation_are_explicit():
    result = execute_job("health", {}, timeout_s=0.01)
    assert result["failure_code"] == "deadline_exceeded"
    assert result["elapsed_seconds"] < 5
    assert execute_job("e5", {}, cancelled=lambda: True)["status"] == "cancelled"


def test_job_rejects_arbitrary_operations_and_invalid_limits():
    with pytest.raises(ValueError):
        execute_job("os.system", {"code": "not allowed"})
    with pytest.raises(ValueError):
        execute_job("health", {}, threads=99)
    with pytest.raises(ValueError):
        execute_job("health", {}, timeout_s=float("nan"))


def test_separate_optional_python_is_explicit_and_does_not_accept_payload_commands(
    monkeypatch, tmp_path
):
    from src.evaluation.runtime_errors import BackendError
    from src.evaluation.runtime_jobs import _interpreter

    monkeypatch.setenv(
        "NOESIS_OPTIONAL_PYTHON_OUTLINES", str(tmp_path / "missing-python")
    )
    with pytest.raises(BackendError, match="executable"):
        _interpreter("outlines")
    monkeypatch.delenv("NOESIS_OPTIONAL_PYTHON_OUTLINES")
    assert _interpreter("outlines")
