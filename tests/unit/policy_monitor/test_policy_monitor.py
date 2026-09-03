from __future__ import annotations

import asyncio
import json
import socket
import urllib.request
from copy import deepcopy
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.evidence_bundle import verify_bundle
from src.kb import contract
from src.kb.contract import KBContractError
from src.policy_monitor import (
    PolicyMonitorError,
    authorized_view,
    export_policy_bundle,
    grant_private_access,
    provision,
    public_view,
    run_demo,
)
from src.policy_monitor.workflow import (
    DEFAULT_DOMAINS,
    DEFAULT_FIXTURE,
    PRINCIPAL,
    _watch_state,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED = json.loads(
    (REPO_ROOT / "examples/policy-monitor/expected.json").read_text(encoding="utf-8")
)


@pytest.fixture()
def scenario(tmp_path, monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("policy monitor attempted network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    result = run_demo(
        tmp_path / "artifacts",
        db_path=tmp_path / "policy-monitor.duckdb",
    )
    conn = duckdb.connect(str(tmp_path / "policy-monitor.duckdb"))
    try:
        yield result, conn, tmp_path
    finally:
        conn.close()


def test_offline_demo_matches_reviewable_expected_receipt(scenario):
    result, conn, tmp_path = scenario
    expected = EXPECTED
    metrics = result["public"]["metrics"]

    assert metrics["public_document_count"] == expected["public_document_count"]
    assert metrics["supporting_publications"] == expected["publication_count"]
    assert metrics["probable_reporting_origins"] == expected["probable_origin_count"]
    assert metrics["contradictions"] == expected["contradiction_count"]
    assert metrics["votes"] == expected["vote_count"]
    assert result["provision"]["revision"]["change_class"] == expected["revision_class"]
    assert result["authorized"]["private_guidance"]["status"] == "stale"
    assert result["watch"]["replay"]["matches"] is True
    assert result["bundle"]["verification"]["status"] == expected["bundle_status"]
    statements = {item["id"]: item for item in result["public"]["statements"]}
    for statement_id, paths in expected["statement_evidence"].items():
        assert [item["path"] for item in statements[statement_id]["evidence"]] == paths
    assert all(
        statements[statement_id]["verdict"] == "inferred"
        for statement_id in expected["prediction_statements"]
    )
    assert result["provision"]["lineage_rows"] == 40
    assert result["provision"]["entity_extraction"]["actors_written"] > 0
    assert result["provision"]["positions_written"] > 0
    assert any(
        item["value"] == expected["quantitative_observation"]["value"]
        for item in result["provision"]["quantitative_assertions"]
    )
    assert conn.execute("SELECT COUNT(*) FROM dataset_observations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vote_records").fetchone()[0] == 1
    kb_documents = contract.kb_documents(
        "clean-heat-public",
        conn=conn,
        config_path=DEFAULT_DOMAINS,
    )
    assert len(kb_documents["data"]) == expected["public_document_count"]
    assert contract.kb_entities(
        "clean-heat-public",
        conn=conn,
        config_path=DEFAULT_DOMAINS,
    )["data"]
    for name in (
        "public-answer.json",
        "authorized-answer.json",
        "brief.md",
        "watch.json",
        "evidence-bundle.json",
        "receipt.json",
    ):
        assert (tmp_path / "artifacts" / name).is_file()


def test_receipt_schema_and_every_brief_line_is_cited(scenario):
    result, _conn, _tmp_path = scenario
    schema = json.loads(
        (
            REPO_ROOT
            / "contracts/schemas/jsonschema/noesis-policy-monitor-v1.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(result["public"])
    Draft202012Validator(schema).validate(result["authorized"])
    assert all("[" in line and line.endswith("]") for line in result["brief"].splitlines()[2:])


def test_public_surface_is_silent_about_private_corpus(scenario):
    result, conn, _tmp_path = scenario
    public = result["public"]
    private_id = result["provision"]["private_document_id"]
    serialized = json.dumps(public, sort_keys=True)

    assert public["visibility"] == "public"
    assert [key for key in public if key.startswith("private")] == EXPECTED[
        "public_private_fields"
    ]
    assert private_id not in serialized
    assert "Clean Heat compliance guidance" not in serialized
    assert "Internal guidance" not in serialized

    conn.execute("BEGIN TRANSACTION")
    before = public_view(conn)
    conn.execute("DELETE FROM document_domains WHERE document_id = ?", [private_id])
    conn.execute("DELETE FROM versioned_assertions WHERE visibility = 'private'")
    conn.execute("DELETE FROM documents WHERE document_id = ?", [private_id])
    after = public_view(conn)
    conn.execute("ROLLBACK")
    assert before == after


def test_private_comparison_requires_grant_and_audit_omits_memo_text(tmp_path):
    conn = duckdb.connect(":memory:")
    try:
        provisioned = provision(conn)
        with pytest.raises(PolicyMonitorError, match="not authorized"):
            authorized_view(conn, "ungranted-user")
        grant_private_access(conn, "alice", granted_at_ms=0)
        receipt = authorized_view(conn, "alice")
        assert receipt["private_guidance"]["status"] == "stale"
        assert provisioned["private_document_id"] in json.dumps(receipt)
        audit = "\n".join(
            row[0]
            for row in conn.execute(
                "SELECT details_json FROM policy_monitor_audit ORDER BY sequence"
            ).fetchall()
        )
        fixture = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
        assert fixture["private_memo"]["content"] not in audit
        assert fixture["private_memo"]["title"] not in audit
    finally:
        conn.close()


def test_watch_event_is_idempotent_and_replayable(scenario):
    first, conn, tmp_path = scenario
    from src.kb.watches import WatchError, record_external_snapshot

    events = first["watch"]["poll"]["events"]
    assert [event["event_type"] for event in events] == EXPECTED["watch_events"]
    assert events[0]["reason_code"] == (
        "private_guidance_conflicts_with_newer_public_record"
    )
    fixture = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    duplicate = record_external_snapshot(
        conn,
        PRINCIPAL,
        first["watch"]["watch"]["watch_id"],
        fixture["timeline"]["revision_watermark"],
        _watch_state(
            fixture,
            first["provision"]["private_document_id"],
            stale=True,
        ),
        observed_at_ms=fixture["timeline"]["as_of_ms"],
    )
    assert duplicate["emitted_events"] == 0
    with pytest.raises(WatchError) as conflict:
        record_external_snapshot(
            conn,
            PRINCIPAL,
            first["watch"]["watch"]["watch_id"],
            fixture["timeline"]["revision_watermark"],
            {"guidance_status": {"stale": False}},
            observed_at_ms=fixture["timeline"]["as_of_ms"],
        )
    assert conflict.value.code == "watermark_conflict"
    before_counts = conn.execute(
        "SELECT"
        " (SELECT COUNT(*) FROM documents),"
        " (SELECT COUNT(*) FROM argument_claims),"
        " (SELECT COUNT(*) FROM document_actors),"
        " (SELECT COUNT(*) FROM policy_positions),"
        " (SELECT COUNT(*) FROM vote_records),"
        " (SELECT COUNT(*) FROM reporting_origins WHERE active),"
        " (SELECT COUNT(*) FROM versioned_assertions)"
    ).fetchone()
    conn.close()
    second = run_demo(
        tmp_path / "artifacts-rerun",
        db_path=tmp_path / "policy-monitor.duckdb",
    )
    assert second["watch"]["replay"]["matches"] is True
    assert second["watch"]["replay"]["stored"] == first["watch"]["replay"]["stored"]
    assert second["bundle"]["bundle_id"] == first["bundle"]["bundle_id"]
    rerun_conn = duckdb.connect(str(tmp_path / "policy-monitor.duckdb"))
    try:
        after_counts = rerun_conn.execute(
            "SELECT"
            " (SELECT COUNT(*) FROM documents),"
            " (SELECT COUNT(*) FROM argument_claims),"
            " (SELECT COUNT(*) FROM document_actors),"
            " (SELECT COUNT(*) FROM policy_positions),"
            " (SELECT COUNT(*) FROM vote_records),"
            " (SELECT COUNT(*) FROM reporting_origins WHERE active),"
            " (SELECT COUNT(*) FROM versioned_assertions)"
        ).fetchone()
    finally:
        rerun_conn.close()
    assert after_counts == before_counts


def test_bundle_defaults_public_requires_grant_and_detects_mutation(scenario):
    result, conn, _tmp_path = scenario
    public_bundle = export_policy_bundle(conn)
    public_json = json.dumps(public_bundle, sort_keys=True)
    assert result["provision"]["private_document_id"] not in public_json
    assert verify_bundle(public_bundle).status == "valid"

    with pytest.raises(PolicyMonitorError, match="authenticated principal"):
        export_policy_bundle(conn, include_private=True)
    with pytest.raises(PolicyMonitorError, match="not authorized"):
        export_policy_bundle(
            conn,
            principal_id="ungranted-user",
            include_private=True,
        )
    private_bundle = export_policy_bundle(
        conn,
        principal_id=PRINCIPAL,
        include_private=True,
    )
    assert result["provision"]["private_document_id"] in json.dumps(private_bundle)
    assert verify_bundle(private_bundle).status == "valid"

    tampered = deepcopy(public_bundle)
    payload = next(item for item in tampered["objects"] if item["type"] == "receipt")
    payload["payload"]["metrics"]["votes"] = 99
    verification = verify_bundle(tampered)
    assert verification.status == "invalid"
    assert any("digest" in error for error in verification.errors)


def test_python_contract_enforces_same_privacy_boundary(scenario):
    result, conn, _tmp_path = scenario
    public = contract.policy_monitor_status(conn=conn)
    assert public["data"] == result["public"]
    with pytest.raises(KBContractError) as error:
        contract.policy_monitor_status("ungranted-user", True, conn=conn)
    assert error.value.code == "unauthorized"
    authorized = contract.policy_monitor_status(PRINCIPAL, True, conn=conn)
    assert authorized["data"]["private_guidance"]["status"] == "stale"
    bundle = contract.policy_monitor_bundle(conn=conn)
    assert verify_bundle(bundle["data"]).status == "valid"


def test_mcp_and_rest_adapters_route_to_canonical_contract(monkeypatch):
    from src.api.routes import kb_routes
    from tools.kb_mcp import server

    calls = []

    def fake_status(principal_id=None, include_private=False):
        calls.append((principal_id, include_private))
        return {"contract": "noesis-kb-v1", "data": {"visibility": "public"}}

    monkeypatch.setattr(contract, "policy_monitor_status", fake_status)
    tools = asyncio.run(server.mcp.get_tools())
    assert tools["policy_monitor_status"].fn() == fake_status()
    assert kb_routes.policy_monitor_public() == fake_status()
    private = kb_routes.policy_monitor_private({"sub": "alice"})
    assert private["contract"] == "noesis-kb-v1"
    assert calls[-1] == ("alice", True)


def test_fixture_rejects_non_cc0_or_non_synthetic_input(tmp_path):
    payload = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    payload["license"] = "unknown"
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    conn = duckdb.connect(":memory:")
    try:
        with pytest.raises(PolicyMonitorError, match="synthetic and CC0"):
            provision(conn, fixture_path=fixture)
    finally:
        conn.close()
