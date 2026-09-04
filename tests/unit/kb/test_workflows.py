from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.workflows import (
    STAGE_ORDER,
    WorkflowError,
    WorkflowStore,
    reference_handlers,
    reference_manifest,
    validate_manifest,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/knowledge_engine_reference/corpus.json"
SCHEMAS = ROOT / "contracts/schemas/jsonschema"


@pytest.fixture()
def conn():
    value = duckdb.connect(":memory:")
    yield value
    value.close()


def test_manifest_and_receipt_schemas_and_canonical_hash() -> None:
    manifest = reference_manifest()
    normalized = validate_manifest(manifest)
    assert normalized["manifest_hash"] == validate_manifest(copy.deepcopy(manifest))["manifest_hash"]
    workflow_schema = json.loads((SCHEMAS / "noesis-knowledge-workflow-v1.json").read_text())
    receipt_schema = json.loads((SCHEMAS / "noesis-workflow-stage-receipt-v1.json").read_text())
    watermark_schema = json.loads((SCHEMAS / "noesis-workflow-watermark-v1.json").read_text())
    for schema in (workflow_schema, receipt_schema, watermark_schema):
        Draft7Validator.check_schema(schema)
    assert not list(Draft7Validator(workflow_schema).iter_errors(normalized))
    fixture = json.loads(
        (ROOT / "contracts/examples/workflows/reference-workflow.json").read_text()
    )
    assert validate_manifest(fixture)["manifest_hash"] == normalized["manifest_hash"]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value.update(namespace=""), "invalid_manifest"),
        (lambda value: value.update(domains=[]), "ambiguous_scope"),
        (lambda value: value["stages"].reverse(), "invalid_stage_order"),
        (
            lambda value: value["stages"][0].update(capability="arbitrary:execute"),
            "undeclared_capability",
        ),
        (
            lambda value: value["stages"][0]["resources"].update(max_bytes=10**12),
            "invalid_budget",
        ),
    ],
)
def test_manifest_rejects_unsafe_or_ambiguous_definitions(mutate, code: str) -> None:
    manifest = reference_manifest()
    mutate(manifest)
    with pytest.raises(WorkflowError) as caught:
        validate_manifest(manifest)
    assert caught.value.code == code


def test_checkpoints_resume_watermark_and_idempotent_replay(conn) -> None:
    store = WorkflowStore(conn)
    manifest = reference_manifest("checkpoint")
    calls: list[str] = []

    def handler(context, state):
        calls.append(context.stage)
        return {**state, context.stage: True, "coverage": {"complete": True}}

    handlers = {stage: handler for stage in STAGE_ORDER}
    with pytest.raises(WorkflowError, match="interruption"):
        store.execute(
            manifest,
            handlers,
            {"seed": 1},
            run_key="run-1",
            fail_after=2,
            now_ms=100,
        )
    assert calls == ["ingest", "extract"]
    assert store.watermark("checkpoint", "knowledge-engine-reference") is None
    assert store.recoverable()["runs"][0]["safe_to_resume"]

    completed = store.execute(
        manifest, handlers, {"seed": 1}, run_key="run-1", now_ms=200
    )
    assert completed["status"] == "completed"
    assert calls == list(STAGE_ORDER)
    assert [item["stage"] for item in completed["receipts"]] == list(STAGE_ORDER)
    assert completed["watermark"]["watermark"] == 1
    assert conn.execute("SELECT COUNT(*) FROM knowledge_subscription_watermarks").fetchone() == (1,)
    replay = store.execute(manifest, handlers, {"seed": 1}, run_key="run-1", now_ms=300)
    assert replay["idempotent"] and calls == list(STAGE_ORDER)


def test_commit_barrier_and_exact_generation_reads(conn) -> None:
    store = WorkflowStore(conn)
    manifest = reference_manifest("generations")

    def handler(context, state):
        return {**state, "last": context.stage, "value": state["value"]}

    handlers = {stage: handler for stage in STAGE_ORDER}
    first = store.execute(manifest, handlers, {"value": "one"}, run_key="one", now_ms=100)
    second = store.execute(manifest, handlers, {"value": "two"}, run_key="two", now_ms=200)
    assert first["watermark"]["watermark"] == 1
    assert second["watermark"]["watermark"] == 2
    old = store.read_stage("generations", "knowledge-engine-reference", 1, "index")
    new = store.read_stage("generations", "knowledge-engine-reference", 2, "index")
    assert old["output"]["value"] == "one"
    assert new["output"]["value"] == "two"
    with pytest.raises(WorkflowError) as caught:
        store.read_stage("generations", "knowledge-engine-reference", 3, "query")
    assert caught.value.code == "uncommitted_state"


def test_cancellation_and_output_budgets_do_not_publish(conn) -> None:
    store = WorkflowStore(conn)
    manifest = reference_manifest("bounded")
    with pytest.raises(WorkflowError) as cancelled:
        store.execute(
            manifest,
            {},
            {"seed": 1},
            run_key="cancelled",
            cancelled=lambda: True,
        )
    assert cancelled.value.code == "cancelled"
    assert store.watermark("bounded", "knowledge-engine-reference") is None

    tiny = reference_manifest("tiny")
    tiny["stages"] = tiny["stages"][:1]
    tiny["stages"][0]["resources"]["max_bytes"] = 5
    with pytest.raises(WorkflowError) as too_large:
        store.execute(
            tiny,
            {"ingest": lambda context, state: {"large": "payload"}},
            {},
            run_key="large",
        )
    assert too_large.value.code == "stage_too_large"


def test_full_reference_workflow_uses_real_subsystems_and_redacts_private(conn) -> None:
    fixture = json.loads(FIXTURE.read_text())
    store = WorkflowStore(conn)
    result = store.execute(
        reference_manifest("reference-test"),
        reference_handlers(conn),
        {"documents": fixture["documents"], "fixture_clock_ms": fixture["clock_ms"]},
        run_key="fixture-v1",
        now_ms=fixture["clock_ms"],
    )
    state = result["state"]
    assert result["status"] == "completed"
    assert len(result["receipts"]) == 7
    assert state["verification"]["valid"]
    assert state["report"]["verified"]
    assert state["events"]["events"]
    assert all(item["citations"] for item in state["query"]["items"])
    exported_ids = {item["component_id"] for item in state["package"]["payload"]}
    assert "ref-private-memo" not in exported_ids
    assert any(
        item["component_id"] == "ref-private-memo" and item["reason"] == "redaction-policy"
        for item in state["package"]["manifest"]["omissions"]
    )
    assert {item["domain"] for item in state["query"]["items"]} == {
        "economic",
        "osint",
        "political",
        "research",
        "scientific",
        "technical",
    }
    for table in (
        "documents",
        "knowledge_extractor_outputs",
        "canonical_events",
        "knowledge_artifacts",
        "knowledge_subscriptions",
        "portable_namespace_components",
    ):
        assert conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone() == (1,)


def test_reference_fixture_malformed_input_is_quarantined(conn) -> None:
    fixture = json.loads(FIXTURE.read_text())
    initial = {
        "documents": fixture["documents"] + [fixture["failure_inputs"][0]],
    }
    result = WorkflowStore(conn).execute(
        reference_manifest("quarantine"),
        reference_handlers(conn),
        initial,
        run_key="malformed",
        now_ms=fixture["clock_ms"],
    )
    assert result["watermark"]["coverage"] == {"complete": False, "invalid": 1}
    assert "ref-malformed" not in result["state"]["document_ids"]
