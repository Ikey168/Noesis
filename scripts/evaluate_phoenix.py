"""Bounded local Phoenix transport/retention and receipt-diagnostic evaluation.

Requires an explicitly started local Phoenix deployment. Operational probes are
labelled authored; no human debugging study or model quality is inferred.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.evaluation.observability import PhoenixTraceSink


def evaluate_mlflow(events):
    """Compare a real local MLflow tracking store using the same safe fields."""
    os.environ["MLFLOW_DISABLE_TELEMETRY"] = "true"
    from mlflow import MlflowClient
    from mlflow.entities import Metric, Param

    latencies, reads, runs = [], [], []
    with tempfile.TemporaryDirectory(prefix="noesis-mlflow-comparison-") as directory:
        client = MlflowClient(
            tracking_uri="sqlite:///" + str(Path(directory) / "tracking.db")
        )
        experiment = client.create_experiment(
            "noesis", artifact_location=str(Path(directory) / "artifacts")
        )
        for event in events:
            start = time.perf_counter()
            run = client.create_run(
                experiment,
                tags={
                    "noesis.run_id": hashlib.sha256(
                        event["run_id"].encode()
                    ).hexdigest(),
                    "noesis.project_id": hashlib.sha256(
                        event["project_id"].encode()
                    ).hexdigest(),
                },
            )
            client.log_batch(
                run.info.run_id,
                metrics=[
                    Metric(
                        "latency_ms", event["latency_ms"], int(time.time() * 1000), 0
                    )
                ],
                params=[
                    Param("operation", event["operation"]),
                    Param("status", event["status"]),
                ],
            )
            client.set_terminated(run.info.run_id)
            latencies.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            saved = client.get_run(run.info.run_id)
            reads.append((time.perf_counter() - start) * 1000)
            runs.append(
                {
                    "run_id": saved.info.run_id,
                    "params": saved.data.params,
                    "tags": saved.data.tags,
                    "metrics": saved.data.metrics,
                }
            )
        for run in runs:
            client.delete_run(run["run_id"])
        deleted = all(
            client.get_run(run["run_id"]).info.lifecycle_stage == "deleted"
            for run in runs
        )
    return {
        "version": importlib.metadata.version("mlflow-skinny"),
        "deployment": "native local SQLite tracking store; no remote service",
        "runs": runs,
        "all_statuses_round_trip": {r["params"]["status"] for r in runs}
        == {e["status"] for e in events},
        "private_payload_excluded": all(
            value not in json.dumps(runs)
            for value in [
                "PRIVATE_PAYLOAD_MUST_NOT_LEAVE",
                "PRIVATE_TOKEN_MUST_NOT_LEAVE",
                "Berlin-public-evidence",
                "https://www.berlin.de/",
            ]
        ),
        "create_log_terminate_milliseconds": latencies,
        "read_milliseconds": reads,
        "median_write_milliseconds": median(latencies),
        "soft_deletion_verified": deleted,
        "retention": "MLflow deletion marks runs deleted; the disposable SQLite directory is removed after measurement. No automatic production TTL is inferred.",
        "diagnostic_value": "Status and operation are recoverable from both MLflow and Phoenix; neither safe projection adds root-cause evidence beyond the authoritative receipt.",
    }


def evaluate(endpoint):
    from phoenix.client import Client

    run_prefix = "noesis-evaluation-" + str(time.time_ns())
    statuses = ["success", "timeout", "unsupported"]
    events = [
        {
            "project_id": "Berlin-public-evidence",
            "run_id": run_prefix + "-" + status,
            "evidence_ids": ["https://www.berlin.de/"],
            "operation": "answer",
            "status": status,
            "latency_ms": i + 1,
            "body": "PRIVATE_PAYLOAD_MUST_NOT_LEAVE",
            "token": "PRIVATE_TOKEN_MUST_NOT_LEAVE",
        }
        for i, status in enumerate(statuses)
    ]
    disabled = PhoenixTraceSink(enabled=False)
    disabled_us, receipt_us, exported_ms, results = [], [], [], []
    for event in events:
        start = time.perf_counter_ns()
        assert disabled.emit(event)["status"] == "disabled"
        disabled_us.append((time.perf_counter_ns() - start) / 1000)
        start = time.perf_counter_ns()
        json.dumps(event)
        receipt_us.append((time.perf_counter_ns() - start) / 1000)
    with PhoenixTraceSink(
        enabled=True,
        endpoint=endpoint + "/v1/traces",
        retention_notice="Disposable local evaluation; configured one-day retention; delete evaluation storage after inspection.",
        max_spans=3,
    ) as sink:
        for event in events:
            start = time.perf_counter()
            results.append(sink.emit(event))
            exported_ms.append((time.perf_counter() - start) * 1000)
    client = Client(base_url=endpoint)
    trace_ids = [row["trace_id"] for row in results if row["exported"]]
    spans = []
    deadline = time.monotonic() + 15
    while trace_ids and time.monotonic() < deadline:
        spans = client.spans.get_spans(
            project_identifier="noesis", trace_ids=trace_ids, limit=10
        )
        if len(spans) == 3:
            break
        time.sleep(0.2)
    raw = json.dumps(spans, default=str)
    no_private_payload = all(
        value not in raw
        for value in [
            "PRIVATE_PAYLOAD_MUST_NOT_LEAVE",
            "PRIVATE_TOKEN_MUST_NOT_LEAVE",
            "Berlin-public-evidence",
            "https://www.berlin.de/",
        ]
    )
    observed = {row["attributes"].get("noesis.status") for row in spans}
    project = client.projects.get(project_name="noesis")
    return {
        "contract": "noesis-phoenix-local-evaluation-v1",
        "versions": {
            name: importlib.metadata.version(name)
            for name in [
                "arize-phoenix",
                "arize-phoenix-client",
                "opentelemetry-sdk",
                "opentelemetry-exporter-otlp-proto-http",
            ]
        },
        "input_origin": "authored success/timeout/unsupported operational probes; not a human debugging study",
        "endpoint": endpoint,
        "export_results": results,
        "stored_spans": spans,
        "project": project,
        "stored_statuses": sorted(observed),
        "all_statuses_round_trip": observed == set(statuses),
        "private_payload_excluded": no_private_payload,
        "trace_id_correlates_with_receipt_run": all(
            results[i].get("trace_id")
            == hashlib.sha256(events[i]["run_id"].encode()).hexdigest()[:32]
            for i in range(3)
        ),
        "measurements": {
            "disabled_emit_microseconds": disabled_us,
            "receipt_serialization_microseconds": receipt_us,
            "synchronous_otlp_emit_milliseconds": exported_ms,
            "median_otlp_emit_milliseconds": median(exported_ms),
        },
        "diagnostic_comparison": {
            "existing_receipts": "Retain authoritative status, evidence and run identity locally.",
            "phoenix": "Queryable status/correlation spans round-trip without source text; no additional root-cause content exported.",
            "mlflow_deployment_comparison": evaluate_mlflow(events),
        },
        "decision": "defer production adoption: successful local transport alone adds no demonstrated diagnostic value beyond existing receipts; synchronous export adds measured request latency",
        "limits": {
            "events": 3,
            "export_timeout_seconds": 2,
            "retention_expiry_observed": False,
            "external_service_cost": 0,
            "human_debugging_effort_measured": False,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:16006")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    # Validate the endpoint before initializing the Phoenix API client.
    with PhoenixTraceSink(
        enabled=True,
        endpoint=args.endpoint + "/v1/traces",
        retention_notice="endpoint validation",
    ):
        pass
    report = evaluate(args.endpoint)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                k: report[k]
                for k in [
                    "all_statuses_round_trip",
                    "private_payload_excluded",
                    "trace_id_correlates_with_receipt_run",
                    "decision",
                ]
            }
        )
    )


if __name__ == "__main__":
    main()
