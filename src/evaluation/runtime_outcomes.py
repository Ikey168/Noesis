"""Task-completion semantics shared by workers, scoring and publication.

Only explicitly registered schemas carry task status. Never recurse through
arbitrary dictionaries: source metadata, NLI abstention and pending report
review are not execution failures. Incomplete output is diagnostic, not a
publishable enrichment.
"""

from __future__ import annotations

import re

from src.evaluation.runtime_errors import BackendError

STATUSES = frozenset({"completed", "partial", "unavailable", "failed", "cancelled"})
STATUS_OPERATIONS = frozenset(
    {"ragas", "scrape-fixture", "paddleocr", "lightonocr", "pdf-docling"}
)
UNAVAILABLE_CODES = frozenset(
    {
        "model_unavailable",
        "optional_dependency_unavailable",
        "runtime_unavailable",
        "resource_monitor_unavailable",
    }
)


def _invalid():
    raise BackendError("invalid_model_output", "invalid optional backend outcome")


def _status(value):
    if not isinstance(value, str) or value not in STATUSES:
        _invalid()
    return value


def _code(value, fallback):
    # Native exception text and credential-bearing messages are not error codes.
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value)
        else fallback
    )


def _coverage_incomplete(value):
    if not isinstance(value, dict):
        _invalid()
    if "coverage_complete" in value and type(value["coverage_complete"]) is not bool:
        _invalid()
    return value.get("coverage_complete") is False


def backend_outcome(operation, output):
    """Wrap a result using its registered task-completeness contract."""
    if output is None:
        _invalid()
    status = "completed"
    if operation in STATUS_OPERATIONS:
        if not isinstance(output, dict):
            _invalid()
        # Historic OCR/Docling results may expose only completeness. Ragas and
        # the scraper have always declared top-level task status.
        if "status" in output:
            status = _status(output["status"])
        elif operation in {"ragas", "scrape-fixture"}:
            _invalid()
    if status == "completed" and operation in {"paddleocr", "lightonocr"}:
        if "complete" in output and type(output["complete"]) is not bool:
            _invalid()
        pages = output.get("pages")
        if not isinstance(pages, list) or not pages:
            _invalid()
        for page in pages:
            if not isinstance(page, dict):
                _invalid()
            if "status" in page and _status(page["status"]) != "completed":
                status = "partial"
            if "truncated" in page and type(page["truncated"]) is not bool:
                _invalid()
            if page.get("truncated") is True:
                status = "partial"
        if output.get("complete") is False:
            status = "partial"
    if operation == "mdeberta" and isinstance(output, dict):
        assessments = output.get("assessments", [output])
        if not isinstance(assessments, list) or not assessments:
            _invalid()
        if any(_coverage_incomplete(row) for row in assessments):
            status = "partial"
    if operation in {"stance", "frames"} and isinstance(output, dict):
        evidence = output.get("evidence", [])
        if not isinstance(evidence, list):
            _invalid()
        for row in evidence:
            if not isinstance(row, dict):
                _invalid()
            if _coverage_incomplete(row.get("assessment")):
                status = "partial"
    outcome = {"status": status, "result": output}
    if status != "completed":
        outcome["failure_code"] = _code(
            output.get("failure_code") if isinstance(output, dict) else None,
            "backend_" + status,
        )
    return outcome


def normalize_outcome(operation, outcome):
    """Validate envelopes and downgrade legacy completed wrappers defensively.

    Supervisory failures/cancellations win. Never upgrade an incomplete result.
    """
    if not isinstance(outcome, dict):
        _invalid()
    status = _status(outcome.get("status"))
    if status != "completed":
        return dict(outcome)
    nested = backend_outcome(operation, outcome.get("result"))
    return {**outcome, **nested}


def exception_outcome(exc):
    """Serialize stable, credential-free failure diagnostics."""
    if isinstance(exc, ImportError):
        status, code = "unavailable", "optional_dependency_unavailable"
    else:
        code = _code(getattr(exc, "code", None), "backend_failed")
        status = (
            "unavailable"
            if code in UNAVAILABLE_CODES
            else "cancelled"
            if code == "cancelled"
            else "failed"
        )
    return {"status": status, "failure_code": code, "failure_type": type(exc).__name__}
