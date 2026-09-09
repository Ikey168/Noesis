"""Regression coverage for real dispatch, publication and CLI outcome boundaries."""

import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace

import duckdb
import numpy as np
import pytest

from src.evaluation import media_backends as media
from src.evaluation import runtime_jobs
from src.evaluation.runtime_errors import BackendError
from src.ingestion.revisions import DocumentRevisionStore
from src.kb.optional_runtime import OptionalAnalysisStore


@pytest.fixture
def worker(tmp_path, monkeypatch):
    import resource

    # Production workers are disposable processes; never constrain pytest itself.
    monkeypatch.setattr(resource, "setrlimit", lambda *a: None)

    def execute(operation, payload, **kwargs):
        incoming, outgoing = tmp_path / "input.json", tmp_path / "output.json"
        incoming.write_text(json.dumps({"operation": operation, "payload": payload}))
        runtime_jobs._worker(
            SimpleNamespace(worker=incoming, output=outgoing, max_output_bytes=1000000)
        )
        return json.loads(outgoing.read_text())

    return execute


def ragas_payload(ref=None):
    contexts = (
        []
        if ref is None
        else [
            {
                "id": ref["document_id"],
                "revision": ref["revision_id"],
                "text": "Captured evidence",
            }
        ]
    )
    return {
        "cases": [
            {
                "id": "fixture",
                "question": "Q",
                "answer": "A",
                "contexts": contexts,
                "citations": [v["id"] for v in contexts],
                "reference_context_ids": [v["id"] for v in contexts],
                "reference_contexts": [v["text"] for v in contexts],
                "label_origin": "fixture",
            }
        ]
    }


@pytest.fixture
def source_store():
    conn = duckdb.connect()
    row = DocumentRevisionStore(conn).observe(
        {"document_id": "doc", "content": "Captured evidence"}
    )
    try:
        yield conn, [{"document_id": "doc", "revision_id": row["revision_id"]}]
    finally:
        conn.close()


@pytest.mark.parametrize("max_seconds", [None, 12.5])
def test_whisperx_dispatch_decodes_verified_bytes_not_reopened_path(
    tmp_path, monkeypatch, max_seconds
):
    path = tmp_path / "captured.wav"
    raw = b"captured audio fixture"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    transcript = {"media_id": "original", "segments": []}
    samples = np.zeros(16000, dtype=np.float32)
    calls = []
    legacy = ModuleType("whisperx.audio")
    legacy.load_audio = lambda *a, **k: pytest.fail("legacy path decoder must not run")
    monkeypatch.setitem(sys.modules, "whisperx.audio", legacy)

    def decode(captured, **limits):
        assert captured == raw
        assert limits == {"max_seconds": 600 if max_seconds is None else max_seconds}
        calls.append("decode")
        path.write_bytes(b"changed after verified read")
        return samples

    class Aligner:
        def __init__(self, language):
            assert language == "de" and calls == ["decode"]

        def align(self, audio, original, *, media_sha256):
            assert audio is samples and original is transcript
            assert media_sha256 == digest
            return {"media_sha256": digest}

    monkeypatch.setattr(media, "decode_audio_bytes", decode)
    monkeypatch.setattr(media, "WhisperXAligner", Aligner)
    payload = {
        "path": str(path),
        "sha256": digest,
        "language": "de",
        "transcript": transcript,
    }
    if max_seconds is not None:
        payload["max_seconds"] = max_seconds
    assert media.media_job("whisperx", payload) == {"media_sha256": digest}
    assert calls == ["decode"]
    with pytest.raises(BackendError, match="differs"):
        media.media_job("whisperx", payload)


def test_whisperx_duration_rejected_before_model_load(tmp_path, monkeypatch):
    path = tmp_path / "captured.wav"
    path.write_bytes(b"audio fixture")

    def decode(*a, **k):
        raise BackendError("input_limit", "audio duration exceeds the allowed maximum")

    monkeypatch.setattr(media, "decode_audio_bytes", decode)
    monkeypatch.setattr(
        media, "WhisperXAligner", lambda *a, **k: pytest.fail("no model load")
    )
    with pytest.raises(BackendError, match="duration"):
        media.media_job(
            "whisperx",
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "language": "de",
                "transcript": {},
            },
        )


def test_ocr_page_truncation_is_partial_task(monkeypatch):
    class Backend:
        def recognize(self, image, *, receipt):
            return {"status": "partial", "truncated": True, "text": "unfinished"}

    monkeypatch.setattr(media, "render_pages", lambda *a, **k: iter([(None, {})]))
    result = media.ocr_pdf("unused", "a" * 64, Backend())
    assert result["status"] == "partial" and result["complete"] is False


@pytest.mark.parametrize(
    "status", ["completed", "unavailable", "partial", "failed", "cancelled"]
)
def test_worker_retains_backend_task_status(worker, monkeypatch, status):
    output = {"status": status, "cases": []}
    if status != "completed":
        output["failure_code"] = "fixture_" + status
    monkeypatch.setattr(runtime_jobs, "dispatch", lambda *a: output)
    outcome = worker("ragas", {})
    assert outcome["status"] == status
    assert outcome["result"] == output
    if status != "completed":
        assert outcome["failure_code"] == "fixture_" + status


@pytest.mark.parametrize("status", ["unavailable", "partial", "failed", "cancelled"])
@pytest.mark.parametrize("legacy_wrapper", [False, True])
def test_noncomplete_result_never_published_and_diagnostics_replayed(
    source_store, status, legacy_wrapper
):
    conn, refs = source_store
    calls, events = [], []

    def executor(*a, **k):
        calls.append(True)
        return {
            "status": "completed" if legacy_wrapper else status,
            "result": {"status": status, "failure_code": "fixture_" + status},
        }

    class Trace:
        def emit(self, event):
            events.append(event)

    store = OptionalAnalysisStore(conn, executor=executor)
    kwargs = {
        "source_refs": refs,
        "principal_id": "alice",
        "scopes": {"operator"},
        "trace_sink": Trace(),
    }
    result = store.run("r", "incomplete", "ragas", ragas_payload(), **kwargs)
    assert result["outcome"]["status"] == status
    assert result["artifact"] is None
    assert events[0]["status"] == status
    assert conn.execute("SELECT count(*) FROM knowledge_artifacts").fetchone()[0] == 0
    assert store.run("r", "incomplete", "ragas", ragas_payload(), **kwargs)["replayed"]
    assert len(calls) == len(events) == 1
    assert (
        store.inspect("r", "incomplete", principal_id="alice", scopes={"operator"})
        == result
    )


def test_actual_ragas_missing_import_worker_to_publication(
    worker, monkeypatch, source_store
):
    monkeypatch.setitem(sys.modules, "ragas", None)
    conn, refs = source_store
    store = OptionalAnalysisStore(conn, executor=worker)
    result = store.run(
        "r",
        "missing",
        "ragas",
        ragas_payload(refs[0]),
        source_refs=refs,
        principal_id="alice",
        scopes={"operator"},
    )
    assert result["outcome"]["status"] == "unavailable"
    assert result["outcome"]["result"]["failure_code"] == "ragas_dependency_unavailable"
    assert result["artifact"] is None
    assert conn.execute("SELECT count(*) FROM knowledge_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["unavailable", "partial", "failed", "cancelled"])
def test_cli_exit_and_replay_follow_backend_status(
    worker, monkeypatch, tmp_path, capsys, status
):
    from src.kb import optional_runtime
    from src.noesis_cli.config import initialize, open_warehouse
    from src.noesis_cli.optional import main

    config, _ = initialize(root=tmp_path / "workspace")
    with open_warehouse(config) as conn:
        row = DocumentRevisionStore(conn).observe(
            {"document_id": "doc", "content": "Captured evidence"}
        )
    ref = {"document_id": "doc", "revision_id": row["revision_id"]}
    request = {
        "kind": "analysis",
        "namespace": "r",
        "run_id": "cli",
        "backend": "ragas",
        "sources": [ref],
        "parameters": ragas_payload(ref),
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    monkeypatch.setenv("NOESIS_OPTIONAL_SCOPES", "operator")
    monkeypatch.setattr(optional_runtime, "execute_job", worker)
    monkeypatch.setattr(
        runtime_jobs,
        "dispatch",
        lambda *a: {"status": status, "failure_code": "fixture_" + status},
    )
    args = ["--config", str(config.path), "--request", str(path)]
    assert main(args) == 3
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"]["status"] == status and output["artifact"] is None
    assert main(args) == 3
    assert json.loads(capsys.readouterr().out)["replayed"] is True


@pytest.mark.parametrize(
    "operation,output,expected",
    [
        (
            "mdeberta",
            {"coverage_complete": False, "status": "insufficient_evidence"},
            "partial",
        ),
        ("mdeberta", {"assessments": [{"coverage_complete": False}]}, "partial"),
        (
            "mdeberta",
            {"coverage_complete": True, "status": "conflicting_evidence"},
            "completed",
        ),
        ("stance", {"status": "unsupported", "labels": []}, "completed"),
        (
            "outlines",
            {"status": "pending_review", "support_verified": False},
            "completed",
        ),
        ("lightonocr", {"complete": False, "pages": [{"truncated": True}]}, "partial"),
        ("ragas", {"status": "invented"}, "failed"),
    ],
)
def test_operation_specific_completeness_not_arbitrary_status_scanning(
    worker, monkeypatch, operation, output, expected
):
    monkeypatch.setattr(runtime_jobs, "dispatch", lambda *a: output)
    assert worker(operation, {})["status"] == expected


def test_real_subprocess_unavailable_dependency_does_not_publish(
    tmp_path, monkeypatch, source_store
):
    # Force a missing optional import in the disposable child without changing
    # installed packages or replacing the real dispatcher/supervisor.
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "sitecustomize.py").write_text(
        "import sys\nsys.modules['ragas'] = None\n"
    )
    monkeypatch.syspath_prepend(str(startup))
    monkeypatch.delenv("NOESIS_OPTIONAL_PYTHON_RAGAS", raising=False)
    conn, refs = source_store
    store = OptionalAnalysisStore(conn)
    result = store.run(
        "r",
        "native-missing",
        "ragas",
        ragas_payload(refs[0]),
        source_refs=refs,
        principal_id="alice",
        scopes={"operator"},
    )
    assert result["outcome"]["execution"] == "isolated-local-process"
    assert result["outcome"]["status"] == "unavailable"
    assert result["outcome"]["failure_code"] == "ragas_dependency_unavailable"
    assert result["artifact"] is None
    assert conn.execute("SELECT count(*) FROM knowledge_artifacts").fetchone()[0] == 0


def test_legacy_saved_unavailable_outcome_cannot_reappear_as_success(source_store):
    conn, refs = source_store
    calls = []

    def executor(*a, **k):
        calls.append(True)
        return {
            "status": "unavailable",
            "result": {
                "status": "unavailable",
                "failure_code": "ragas_dependency_unavailable",
            },
        }

    store = OptionalAnalysisStore(conn, executor=executor)
    kwargs = {"source_refs": refs, "principal_id": "alice", "scopes": {"operator"}}
    original = store.run("r", "legacy", "ragas", ragas_payload(), **kwargs)
    legacy = json.loads(json.dumps(original))
    legacy["outcome"]["status"] = "completed"
    legacy["artifact"] = {"artifact_id": "historical-invalid-artifact"}
    conn.execute(
        "UPDATE optional_analysis_runs SET result_json=?", [json.dumps(legacy)]
    )
    result = store.inspect("r", "legacy", principal_id="alice", scopes={"operator"})
    assert result["outcome"]["status"] == "unavailable" and result["artifact"] is None
    assert result["suppressed_artifact_id"] == "historical-invalid-artifact"
    replay = store.run("r", "legacy", "ragas", ragas_payload(), **kwargs)
    assert replay["replayed"] and replay["outcome"]["status"] == "unavailable"
    assert len(calls) == 1
    # Preserve historical diagnostic bytes rather than silently rewrite history.
    assert (
        json.loads(
            conn.execute("SELECT result_json FROM optional_analysis_runs").fetchone()[0]
        )
        == legacy
    )


@pytest.mark.parametrize(
    "error,expected",
    [
        (ImportError("secret native payload"), "unavailable"),
        (
            BackendError("optional_dependency_unavailable", "secret native payload"),
            "unavailable",
        ),
        (BackendError("model_unavailable", "secret native payload"), "unavailable"),
        (BackendError("runtime_unavailable", "secret native payload"), "unavailable"),
        (BackendError("cancelled", "secret native payload"), "cancelled"),
        (BackendError("invalid_model_output", "secret native payload"), "failed"),
    ],
)
def test_worker_exception_codes_preserve_availability_without_messages(
    worker, monkeypatch, error, expected
):
    def dispatch(*a):
        raise error

    monkeypatch.setattr(runtime_jobs, "dispatch", dispatch)
    result = worker("health", {})
    assert result["status"] == expected and "secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "output", [None, {"vectors": [float("nan")]}, {"value": object()}]
)
def test_worker_invalid_serialization_is_explicit_failure(worker, monkeypatch, output):
    monkeypatch.setattr(runtime_jobs, "dispatch", lambda *a: output)
    result = worker("health", {})
    assert (
        result["status"] == "failed"
        and result["failure_code"] == "invalid_model_output"
    )
