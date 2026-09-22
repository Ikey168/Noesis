"""Opt-in Phoenix OTLP export, without replacing authoritative Noesis run records."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import time
from urllib.parse import urlsplit

from src.evaluation.runtime_errors import BackendError

OPERATIONS = frozenset({"ingest", "retrieve", "rerank", "answer", "report", "evaluate"})
STATUSES = frozenset(
    {
        "success",
        "timeout",
        "failed",
        "unsupported",
        "cancelled",
        "unavailable",
        "partial",
    }
)


def _id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 1000:
        raise ValueError("bounded existing correlation ID required")
    # Private project names and document identities never leave as plaintext.
    return hashlib.sha256(value.encode()).hexdigest()


class PhoenixTraceSink:
    """Own an independent tracer provider, finite span budget, and explicit lifetime.

    Endpoint must be a local collector. Export results mean submission, not a
    claim that Phoenix retained data or that its UI provides debugging value.
    Caller owns retention configuration; no trace backend becomes source truth.
    """

    def __init__(
        self,
        *,
        enabled=False,
        endpoint="http://127.0.0.1:6006/v1/traces",
        retention_notice=None,
        max_spans=1000,
        exporter=None,
    ):
        if type(max_spans) is not int or not 1 <= max_spans <= 10000:
            raise ValueError("bounded trace count required")
        self.enabled, self.max_spans, self.sent, self.closed = (
            enabled,
            max_spans,
            0,
            False,
        )
        self.provider = None
        if not enabled:
            return
        if not retention_notice or len(retention_notice) > 4000:
            raise ValueError("explicit local collector retention policy required")
        parsed = urlsplit(endpoint)
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            local = False
        if (
            not local
            or parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != "/v1/traces"
        ):
            raise ValueError(
                "only an explicit loopback OTLP trace collector is permitted"
            )
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                SimpleSpanProcessor,
                SpanExporter,
                SpanExportResult,
            )

            if exporter is None:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(endpoint=endpoint, timeout=2, headers={})
        except ImportError as exc:
            raise BackendError(
                "optional_dependency_unavailable",
                "install the optional OTLP tracing dependencies",
            ) from exc
        self.provider = TracerProvider(
            resource=Resource.create(
                {"service.name": "noesis", "openinference.project.name": "noesis"}
            )
        )

        class ObservedExporter(SpanExporter):
            """The SDK processor otherwise swallows export failures."""

            result = None

            def export(self, spans):
                try:
                    self.result = exporter.export(spans)
                except Exception:  # noqa: BLE001 - exporter errors can contain credentials
                    self.result = SpanExportResult.FAILURE
                return self.result

            def shutdown(self):
                exporter.shutdown()

            def force_flush(self, timeout_millis=30000):
                return exporter.force_flush(timeout_millis)

        self._exporter = ObservedExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        self.tracer = self.provider.get_tracer("noesis.optional.phoenix", "1.0.0")
        self.retention_policy_sha256 = _id(retention_notice)

    def emit(self, event):
        if not self.enabled:
            return {"status": "disabled", "exported": False}
        if self.closed:
            raise BackendError("trace_closed", "the trace exporter was closed")
        if self.sent >= self.max_spans:
            raise BackendError("trace_budget", "trace span budget exhausted")
        if (
            not isinstance(event, dict)
            or event.get("operation") not in OPERATIONS
            or event.get("status") not in STATUSES
        ):
            raise ValueError("allowlisted operation/status required")
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            Status,
            StatusCode,
            TraceFlags,
            set_span_in_context,
        )

        project, run = _id(event.get("project_id")), _id(event.get("run_id"))
        evidence = event.get("evidence_ids", [])
        if not isinstance(evidence, list) or len(evidence) > 100:
            raise ValueError("bounded evidence references required")
        latency = event.get("latency_ms", 0)
        if (
            type(latency) not in {float, int}
            or not math.isfinite(latency)
            or not 0 <= latency <= 3600000
        ):
            raise ValueError("invalid trace duration")
        attributes = {
            "noesis.project.sha256": project,
            "noesis.run.sha256": run,
            "noesis.evidence.sha256": [_id(value) for value in evidence],
            "noesis.status": event["status"],
            "noesis.latency_ms": latency,
            "noesis.retention_policy.sha256": self.retention_policy_sha256,
            "openinference.span.kind": "CHAIN",
        }
        # Correlate with a deterministic pseudonymous run parent without changing
        # the process-global OpenTelemetry provider or existing MLflow traces.
        parent = NonRecordingSpan(
            SpanContext(
                int(run[:32], 16) or 1,
                int(run[32:48], 16) or 1,
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        )
        end = time.time_ns()
        span = self.tracer.start_span(
            event["operation"],
            context=set_span_in_context(parent),
            start_time=end - int(latency * 1_000_000),
            attributes=attributes,
        )
        span.set_status(
            Status(StatusCode.OK if event["status"] == "success" else StatusCode.ERROR)
        )
        self._exporter.result = None
        span.end(end_time=end)
        self.sent += 1
        from opentelemetry.sdk.trace.export import SpanExportResult

        if self._exporter.result != SpanExportResult.SUCCESS:
            return {
                "status": "failed",
                "exported": False,
                "failure_code": "trace_export_failed",
                "span_count": self.sent,
                "retention_verified": False,
                "authoritative_run_store_changed": False,
            }
        return {
            "status": "submitted",
            "exported": True,
            "span_count": self.sent,
            "trace_id": f"{span.get_span_context().trace_id:032x}",
            "private_payload_exported": False,
            "retention_verified": False,
            "authoritative_run_store_changed": False,
        }

    def close(self):
        if not self.closed and self.provider is not None:
            self.provider.shutdown()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
