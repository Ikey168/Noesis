"""Explicit build adapters for non-report Creation artifacts (#1575)."""

import hashlib

import duckdb
import pytest

from src.kb.authored_reports import AuthoredReportStore
from src.kb.intake_creation import IntakeCreationStore
from src.kb.intake_modes import IntakeError
from tests.unit.kb.test_intake_creation import SCOPES, _report

KW = {"principal_id": "alice", "scopes": SCOPES}


def _finished(conn, creations, artifact_type="slide_deck"):
    project = creations.create("research", f"make-{artifact_type}", title="Recovery deck", audience="Operators",
                               artifact_type=artifact_type, purpose="Teach the recovery", criteria=["Accurate"],
                               inputs=None, workspace_links=None, **KW)
    report = AuthoredReportStore(conn, now=lambda: 1000).create("research", "deck-source", _report(), **KW)
    pid = project["project_id"]
    step = creations.command("research", pid, "attach", expected_revision=1, action="attach_report",
                             payload={"report_id": report["report_id"], "revision": 1}, **KW)
    step = creations.command("research", pid, "review", expected_revision=step["revision"], action="review",
                             payload={"checks": {"Accurate": True}, "notes": "Checked"}, **KW)
    return creations.command("research", pid, "finish", expected_revision=step["revision"], action="finish",
                             payload=None, **KW)


def test_build_adapter_runs_once_and_export_carries_receipt():
    conn = duckdb.connect(":memory:")
    calls = []

    def deck(source, *, idempotency_key):
        calls.append((source["sha256"], idempotency_key))
        body = source["authored_report_export"]["markdown"].encode()
        return {"artifact_locator": "file:///builds/deck.pptx",
                "artifact_sha256": hashlib.sha256(body).hexdigest(), "adapter_version": "deck-builder 1.2"}

    creations = IntakeCreationStore(conn, now=lambda: 2000, build_adapters={"slide_deck": deck})
    finished = _finished(conn, creations)
    call = dict(expected_revision=finished["revision"], idempotency_key="build-1", **KW)

    built = creations.build("research", finished["project_id"], **call)
    replay = creations.build("research", finished["project_id"], **call)

    assert built["status"] == "completed" and replay["idempotent"] and len(calls) == 1
    receipt = built["receipt"]
    assert receipt["publication_authorized"] is False
    assert receipt["result"]["adapter_version"] == "deck-builder 1.2"
    exported = creations.export("research", finished["project_id"], **KW)
    assert exported["builds"] == [receipt] and exported["publication_authorized"] is False


def test_missing_adapter_unsupported_type_and_unfinished_project_are_explicit():
    conn = duckdb.connect(":memory:")
    public = IntakeCreationStore(conn, now=lambda: 2000)
    finished = _finished(conn, public, "static_site")
    unavailable = public.build("research", finished["project_id"], expected_revision=finished["revision"],
                               idempotency_key="b", **KW)
    assert unavailable == {"contract": "noesis-intake-creation-build-v1", "status": "unavailable",
                           "reason": "build_adapter_unavailable", "artifact_type": "static_site", "built": False}
    with pytest.raises(IntakeError) as error:
        public.create("research", "x", title="App", audience="Users", artifact_type="mobile_app",
                      purpose="Ship", criteria=["Works"], inputs=None, workspace_links=None, **KW)
    assert error.value.code == "unsupported_artifact"
    report_type = _finished(conn, public, "documentation")
    with pytest.raises(IntakeError) as error:
        public.build("research", report_type["project_id"], expected_revision=report_type["revision"],
                     idempotency_key="d", **KW)
    assert error.value.code == "not_build_type"
    with pytest.raises(IntakeError) as error:
        IntakeCreationStore(conn, build_adapters={"mobile_app": lambda *a, **k: {}})
    assert error.value.code == "invalid_adapter"


def test_failing_adapter_is_indeterminate_and_not_exported():
    conn = duckdb.connect(":memory:")

    def broken(source, *, idempotency_key):
        return {"artifact_locator": "x"}

    creations = IntakeCreationStore(conn, now=lambda: 2000, build_adapters={"code_package": broken})
    finished = _finished(conn, creations, "code_package")
    result = creations.build("research", finished["project_id"], expected_revision=finished["revision"],
                             idempotency_key="b", **KW)
    assert result["status"] == "indeterminate" and result["receipt"]["error_code"] == "invalid_adapter_result"
    assert "builds" not in creations.export("research", finished["project_id"], **KW)
