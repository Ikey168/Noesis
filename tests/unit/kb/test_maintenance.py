from __future__ import annotations

import json
from pathlib import Path

import duckdb
import jsonschema

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, load_source_packs
from src.kb.maintenance import MaintenanceOrchestrator, fixture_adapter_provider
from src.kb.unified_query import UnifiedQueryEngine, build_local_catalog

ROOT = Path(__file__).resolve().parents[3]


class Clock:
    def __init__(self, value: int = 1_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def setup_pack(clock: Clock):
    conn = duckdb.connect(":memory:")
    manifest = load_source_packs(ROOT / "config/source_packs")[0]
    SourcePackStore(conn).install(
        manifest, principal_id="operator", enable=True, now_ms=1
    )
    runtime = SourcePackRuntime(conn, now=clock, sleep=lambda _: None)
    for source in manifest["sources"]:
        runtime.accept_license(
            manifest["pack_id"], source["source_id"], principal_id="operator"
        )
    runtime.set_schedule(
        manifest["pack_id"],
        {"kind": "interval", "interval_s": 60, "next_run_at_ms": 100},
        principal_id="operator",
    )
    return conn, manifest, runtime, MaintenanceOrchestrator(conn, root=ROOT, now=clock)


def execution_kwargs(orchestrator):
    return {
        "principal_id": "operator",
        "adapter_provider": fixture_adapter_provider(orchestrator),
        "secret_resolver": lambda _: "fixture",
        "dns_resolver": lambda _: ["8.8.8.8"],
    }


def test_bounded_dispatch_idempotency_same_pack_exclusion_and_takeover():
    clock = Clock(180_100)
    conn, manifest, _, orchestrator = setup_pack(clock)
    first = orchestrator.enqueue_due(max_catchup=3)
    assert len(first["created"]) == 3
    assert len(orchestrator.enqueue_due(max_catchup=3)["created"]) == 1
    assert orchestrator.enqueue_due(max_catchup=3)["created"] == []

    lease_a = orchestrator.claim("worker-a", lease_ms=1_000)
    assert lease_a and orchestrator.claim("worker-b", lease_ms=1_000) is None
    assert orchestrator.release(
        lease_a["job_id"], "worker-a", lease_a["fencing_token"]
    )["released"]
    lease_b = orchestrator.claim("worker-b", lease_ms=1_000)
    assert lease_b and lease_b["fencing_token"] > lease_a["fencing_token"]
    clock.value += 1_001
    lease_c = orchestrator.claim("worker-c", lease_ms=1_000)
    assert lease_c and lease_c["job_id"] == lease_b["job_id"]
    assert lease_c["fencing_token"] > lease_b["fencing_token"]
    assert orchestrator.recover_stale()["recovered"] == 0

    orchestrator.set_schedule_paused(manifest["pack_id"], True, principal_id="admin")
    assert orchestrator.enqueue_due()["created"] == []
    conn.close()


def test_crash_resume_atomic_query_visibility_receipts_and_replay():
    clock = Clock()
    conn, manifest, _, orchestrator = setup_pack(clock)
    orchestrator.enqueue_due()
    failed = orchestrator.run_once(
        "worker", fail_after_phase="workflow", **execution_kwargs(orchestrator)
    )
    assert failed["status"] == "retry"

    engine = UnifiedQueryEngine(
        build_local_catalog(conn, domains=manifest["domains"], include_memory=False)
    )
    request = {
        "query": "indicator",
        "scope": {"domains": manifest["domains"]},
        "surfaces": ["lexical"],
        "budgets": {"max_results": 20, "per_source_results": 20, "token_budget": 500},
    }
    assert engine.execute(request, scopes={"knowledge:read"})["items"] == []

    clock.value = int(failed["retry_at_ms"])
    receipt = orchestrator.run_once("worker-2", **execution_kwargs(orchestrator))
    assert receipt["status"] == "complete"
    assert set(receipt["artifacts"]["published"]) == {
        "index",
        "embedding",
        "entity",
        "claim",
        "relation",
        "summary",
    }
    assert all(
        item["status"] == "rebuilt" for item in receipt["artifacts"]["rebuild_receipts"]
    )
    generation = receipt["generation"]
    assert orchestrator.replay_generation(generation["generation_id"])["matched"]
    lineage = orchestrator.generation_lineage(generation["generation_id"])
    assert lineage["complete"] and lineage["artifact_edges"]
    assert any(
        str(edge.get("dependency_id", "")).startswith("document-revision:")
        for edge in lineage["artifact_edges"]
    )
    assert engine.execute(request, scopes={"knowledge:read"})["items"]
    assert (
        conn.execute("SELECT COUNT(*) FROM knowledge_maintenance_events").fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_subscription_watermarks WHERE namespace=?",
            [f"knowledge-generation:{manifest['pack_id']}"],
        ).fetchone()[0]
        == 1
    )

    clock.value = 60_100
    assert len(orchestrator.enqueue_due()["created"]) == 1
    empty_delta = orchestrator.run_once("worker-3", **execution_kwargs(orchestrator))
    assert empty_delta["status"] == "complete"
    assert empty_delta["documents"]["selected"] == 0
    assert empty_delta["artifacts"]["watermark"] == receipt["artifacts"]["watermark"]
    assert empty_delta["artifacts"]["published"] == receipt["artifacts"]["published"]
    assert all(
        item["omissions"] == ["empty-delta"]
        for item in empty_delta["artifacts"]["rebuild_receipts"]
    )

    for name, value in (
        ("noesis-maintenance-job-receipt-v1.json", receipt),
        ("noesis-knowledge-generation-v1.json", generation),
    ):
        schema = json.loads((ROOT / "contracts/schemas/jsonschema" / name).read_text())
        jsonschema.validate(value, schema)
    conn.close()


def test_cancel_retry_attempt_history_and_health_are_credential_safe():
    clock = Clock()
    conn, _, _, orchestrator = setup_pack(clock)
    job_id = orchestrator.enqueue_due()["created"][0]
    cancelled = orchestrator.cancel(job_id, principal_id="admin")
    assert cancelled["status"] == "cancelled"
    retried = orchestrator.retry(job_id, principal_id="admin")
    assert retried["status"] == "retry"
    first = orchestrator.run_once(
        "worker", fail_after_phase="source", **execution_kwargs(orchestrator)
    )
    assert first["status"] == "retry"
    clock.value = int(first["retry_at_ms"])
    second = orchestrator.run_once("worker", **execution_kwargs(orchestrator))
    assert second["status"] == "complete"
    job = orchestrator.inspect_job(job_id)
    assert [item["attempt"] for item in job["attempt_history"]] == [1, 2]
    encoded = json.dumps(job)
    assert "fixture" not in encoded and "secret" not in encoded
    health = orchestrator.health()
    assert {
        "freshness_ms",
        "schedule_lag_ms",
        "processing_lag_ms",
        "mean_recovery_time_ms",
    } <= set(health)
    conn.close()
