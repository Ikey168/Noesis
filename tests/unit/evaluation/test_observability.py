import pytest

from src.evaluation.observability import PhoenixTraceSink
from src.evaluation.runtime_errors import BackendError


def test_disabled_tracing_never_initializes_an_exporter():
    sink = PhoenixTraceSink(enabled=False, exporter=object())
    assert sink.emit({"private": "not sent"}) == {
        "status": "disabled",
        "exported": False,
    }
    assert sink.provider is None


def test_native_otel_span_contains_only_pseudonymous_correlation():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    event = {
        "project_id": "private-project-name",
        "run_id": "run",
        "evidence_ids": ["secret-doc"],
        "operation": "answer",
        "status": "timeout",
        "latency_ms": 20,
        "body": "secret evidence",
        "token": "private-token",
    }
    with PhoenixTraceSink(
        enabled=True,
        retention_notice="Local test exporter only",
        max_spans=1,
        exporter=exporter,
    ) as sink:
        result = sink.emit(event)
        assert result["status"] == "submitted"
        spans = exporter.get_finished_spans()
        assert len(spans) == 1 and spans[0].name == "answer"
        attrs = str(dict(spans[0].attributes))
        assert all(
            secret not in attrs
            for secret in (
                "private-project-name",
                "secret-doc",
                "secret evidence",
                "private-token",
            )
        )
        with pytest.raises(BackendError, match="budget"):
            sink.emit(event)
    assert sink.closed


@pytest.mark.parametrize("raises", [False, True])
def test_export_failure_is_not_reported_as_submission(raises):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class FailedExporter(SpanExporter):
        def export(self, spans):
            if raises:
                raise RuntimeError("private collector failure")
            return SpanExportResult.FAILURE

    with PhoenixTraceSink(
        enabled=True, retention_notice="test", exporter=FailedExporter(), max_spans=1
    ) as sink:
        result = sink.emit(
            {
                "project_id": "p",
                "run_id": "r",
                "operation": "answer",
                "status": "unsupported",
            }
        )
        assert result["status"] == "failed" and not result["exported"]
        assert result["failure_code"] == "trace_export_failed"
        assert "private" not in str(result)
        with pytest.raises(BackendError, match="budget"):
            sink.emit({})


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://external.example/v1/traces",
        "http://localhost/v1/traces",
        "http://127.0.0.1/v1/traces?token=secret",
    ],
)
def test_nonlocal_or_credentialed_telemetry_is_rejected(endpoint):
    with pytest.raises(ValueError, match="loopback"):
        PhoenixTraceSink(enabled=True, endpoint=endpoint, retention_notice="test")
