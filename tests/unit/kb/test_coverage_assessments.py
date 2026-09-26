"""Offline public-provider fixtures; no live credential or exhaustive recall claim."""

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.ingestion.source_pack_runtime import FixturePageAdapter, HTTPSPageAdapter, SourcePackRuntime
from src.ingestion.source_packs import SourcePackError, SourcePackStore, validate_source_pack
from src.kb.coverage_assessments import CoverageAssessmentStore, CoverageError
from src.kb.investigation_templates import _validate as validate_template
from src.kb.source_planner import SourcePlannerStore
from tools.knowledge_engine_mcp.coverage_assessments import register as register_coverage

ROOT = Path(__file__).resolve().parents[3]
SCOPES = {"knowledge:coverage:read", "knowledge:coverage:write",
          "namespace:research:read", "namespace:research:write"}


def _scope(*, topics=("methods",)):
    return {
        "dates": [{"from_ms": 0, "to_ms": 10_000_000_000_000}],
        "geographies": ["global"], "languages": ["en", "de"], "topics": list(topics),
    }


def _fixture():
    raw = json.loads((ROOT / "config/source_packs/openreview.json").read_text())
    raw["version"] = "1.0.1"
    raw["sources"][0]["auth"] = {"kind": "required-secret", "secret_ref": "NOESIS_TEST_OPENREVIEW"}
    return validate_source_pack(raw)


def _note():
    return {"id": "S", "forum": "S", "replyto": None, "readers": ["everyone"],
            "invitations": ["Venue/-/Submission"], "signatures": ["Venue/Authors"],
            "pdate": 1000, "tmdate": 1000,
            "content": {"abstract": {"value": "Public test manuscript."}}}


def _run(conn, pack, run_key, notes):
    source = pack["sources"][0]
    runtime = SourcePackRuntime(conn)
    adapter = HTTPSPageAdapter(source, transport=lambda **_: {
        "content": json.dumps({"notes": notes}),
    }, secret="fixture-key")
    return runtime.run({
        "pack_id": pack["pack_id"], "run_key": run_key, "operation": "search",
        "source_ids": [source["source_id"]], "required_sources": [source["source_id"]],
        "parameters": {"forum": "S"}, "max_pages": 2, "max_results": 10,
    }, principal_id="operator", adapters={source["source_id"]: adapter},
        secret_resolver=lambda _: "fixture-key", dns_resolver=lambda _: ["8.8.8.8"])


def _binding(pack, cell_ids, *, run_id=None, evidence=None, readiness=None):
    return {"pack_id": pack["pack_id"], "pack_version": pack["version"],
            "source_id": pack["sources"][0]["source_id"], "cell_ids": cell_ids,
            **({"run_id": run_id} if run_id else {}),
            **({"evidence": evidence} if evidence else {}),
            **({"readiness": readiness} if readiness else {})}


def _validate(name, value):
    schema = json.loads((ROOT / "contracts/schemas/jsonschema" / name).read_text())
    Draft202012Validator(schema).validate(value)


def test_credential_gap_recovery_empty_successful_and_access():
    conn = duckdb.connect(":memory:")
    pack = _fixture()
    SourcePackStore(conn).install(pack, principal_id="operator", enable=True)
    runtime = SourcePackRuntime(conn)
    runtime.accept_license(pack["pack_id"], pack["sources"][0]["source_id"], principal_id="operator")
    store = CoverageAssessmentStore(conn)
    scope = _scope(topics=("methods", "datasets"))
    cells = store._cells(scope)
    methods = [c["cell_id"] for c in cells if c["topic"] == "methods"]
    english = next(c["cell_id"] for c in cells if c["topic"] == "methods" and c["language"] == "en")
    request = {"pack_id": pack["pack_id"], "run_key": "preflight", "operation": "search",
               "source_ids": [pack["sources"][0]["source_id"]],
               "parameters": {"forum": "S"}}
    preflight = runtime.preflight(request, dns_resolver=lambda _: ["8.8.8.8"])
    assert preflight["sources"][0]["failures"] == ["credential_missing"]
    readiness = {"observed_at_ms": store.now(), "manifest_hash": pack["manifest_hash"],
                 "failures": preflight["sources"][0]["failures"]}
    blocked = store.save("research", "blocked", scope,
                         [_binding(pack, methods, readiness=readiness)],
                         principal_id="alice", scopes=SCOPES)
    assert blocked["denominator"] == 4
    assert {c["status"] for c in blocked["cells"]} == {"unavailable", "unattempted"}
    assert "credential_missing" in next(c for c in blocked["cells"] if c["cell_id"] == english)["reasons"]
    assert all("fixture-key" not in json.dumps(c) for c in blocked["cells"])
    _validate("noesis-investigation-coverage-v1.json", blocked)

    empty_run = _run(conn, pack, "empty", [])
    empty = store.save("research", "empty", scope, [_binding(pack, methods, run_id=empty_run["run_id"])],
                       principal_id="alice", scopes=SCOPES)
    empty_cell = next(c for c in empty["cells"] if c["cell_id"] == english)
    assert empty_cell["status"] == "unavailable"
    assert "query_scope_unverified" in empty_cell["reasons"]

    populated_run = _run(conn, pack, "populated", [_note()])
    row = conn.execute(
        "SELECT document_id,revision_id FROM document_revision_records "
        "WHERE run_id=? AND committed_watermark IS NOT NULL", [populated_run["run_id"]],
    ).fetchone()
    assert row
    ref = {"document_id": row[0], "revision_id": row[1]}
    access = SCOPES | {f"document:{row[0]}:read"}
    recovered = store.save("research", "recovered", scope,
                           [_binding(pack, methods, run_id=populated_run["run_id"],
                                     evidence={english: [ref]})],
                           principal_id="alice", scopes=access)
    assert next(c for c in recovered["cells"] if c["cell_id"] == english)["status"] == "observed"
    assert next(c for c in recovered["cells"] if c["language"] == "de" and c["topic"] == "methods")["status"] == "unavailable"
    comparison = store.compare("research", blocked["assessment_id"], recovered["assessment_id"],
                               principal_id="alice", scopes=access)
    assert comparison["credential_availability_changed"]
    assert any(c["classification"] == "credential_availability_changed" for c in comparison["changes"])
    exported = store.export("research", blocked["assessment_id"], recovered["assessment_id"],
                            principal_id="alice", scopes=access)
    assert exported["receipts"]["after"][0]["receipt"]["run_id"] == populated_run["run_id"]
    assert exported["report_ready"]
    _validate("noesis-investigation-coverage-comparison-v1.json", exported)
    with pytest.raises(CoverageError) as denied:
        store.inspect("research", recovered["assessment_id"], principal_id="alice", scopes=SCOPES)
    assert denied.value.code == "unauthorized"
    assert store.save("research", "recovered", scope,
                      [_binding(pack, methods, run_id=populated_run["run_id"],
                                evidence={english: [ref]})],
                      principal_id="alice", scopes=access)["idempotent"]
    restricted = store.save("research", "restricted", scope,
                            [_binding(pack, methods, run_id=populated_run["run_id"],
                                      evidence={english: [ref]})],
                            principal_id="alice", scopes=SCOPES)
    denied_cell = next(c for c in restricted["cells"] if c["cell_id"] == english)
    assert denied_cell["status"] == "unavailable"
    assert "access_restriction" in denied_cell["reasons"]
    assert denied_cell["sources"][0]["citations"] == []
    assert row[0] not in json.dumps(restricted)

    narrower = _scope()
    narrow_cells = store._cells(narrower)
    narrow_english = next(c["cell_id"] for c in narrow_cells if c["language"] == "en")
    assert narrow_english == english
    narrow = store.save("research", "narrow", narrower,
                        [_binding(pack, [narrow_english], run_id=populated_run["run_id"],
                                  evidence={narrow_english: [ref]})],
                        principal_id="alice", scopes=access)
    scope_comparison = store.compare("research", blocked["assessment_id"], narrow["assessment_id"],
                                     principal_id="alice", scopes=access)
    assert not scope_comparison["scope_equal"]
    assert scope_comparison["denominators"] == {"before": 4, "after": 2}
    assert all(change["classification"] == "scope_changed" for change in scope_comparison["changes"])
    replay = store.save("research", "narrow", narrower,
                        [_binding(pack, [narrow_english], run_id=populated_run["run_id"],
                                  evidence={narrow_english: [ref]})],
                        principal_id="alice", scopes=access)
    assert replay["idempotent"]
    assert store.compare("research", narrow["assessment_id"], replay["assessment_id"],
                         principal_id="alice", scopes=access)["changes"] == []


def test_stale_readiness_and_bad_revision_are_explicit():
    conn = duckdb.connect(":memory:")
    pack = _fixture()
    SourcePackStore(conn).install(pack, principal_id="operator", enable=True)
    store = CoverageAssessmentStore(conn, now=lambda: 500_000_000)
    scope = _scope()
    english = store._cells(scope)[0]["cell_id"]
    stale = store.save("research", "stale", scope, [_binding(
        pack, [english], readiness={"observed_at_ms": 1, "manifest_hash": pack["manifest_hash"],
                                     "failures": ["credential_missing"]})],
        principal_id="alice", scopes=SCOPES)
    assert "stale_readiness" in stale["cells"][0]["reasons"]
    assert "credential_missing" not in stale["cells"][0]["reasons"]
    multiple = store.save("research", "multiple", scope, [_binding(
        pack, [english], readiness={"observed_at_ms": 500_000_000,
                                    "manifest_hash": pack["manifest_hash"],
                                    "failures": ["credential_missing", "network_policy"]})],
        principal_id="alice", scopes=SCOPES)
    assert {"credential_missing", "network_policy"} <= set(multiple["cells"][0]["reasons"])
    with pytest.raises(CoverageError) as wrong:
        store.save("research", "wrong", scope, [_binding(
            pack, [english], evidence={english: [{"document_id": "x", "revision_id": "y"}]})],
            principal_id="alice", scopes=SCOPES)
    assert wrong.value.code == "invalid_evidence"


def test_public_wrapper_and_cross_pack_template():
    template = json.loads((ROOT / "config/investigation_templates/cross-pack-coverage.json").read_text())
    assert validate_template(template)["name"] == "Cross-Pack Coverage Assessment"
    conn = duckdb.connect(":memory:")
    pack = _fixture()
    SourcePackStore(conn).install(pack, principal_id="operator", enable=True)

    class MCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorate(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorate

    calls = []

    def safe(operation, *, write=False, required_scope=None):
        calls.append((write, required_scope))
        return operation(conn)

    mcp = MCP()
    register_coverage(mcp, safe, lambda: ("alice", SCOPES))
    scope = _scope()
    cell = CoverageAssessmentStore._cells(scope)[0]["cell_id"]
    saved = mcp.tools["save_coverage_assessment"](
        "research", "wrapper", scope, [_binding(pack, [cell])])
    inspected = mcp.tools["inspect_coverage_assessment"]("research", saved["assessment_id"])
    assert inspected["denominator"] == 2
    assert calls[:2] == [(True, "knowledge:coverage:write"),
                         (False, "knowledge:coverage:read")]


def test_empty_successful_requires_pinned_query_capability_and_exact_run_scope():
    conn = duckdb.connect(":memory:")
    raw = json.loads((ROOT / "config/source_packs/openreview.json").read_text())
    raw["pack_id"] = "fixture-coverage"
    raw["sources"][0]["source_id"] = "fixture-index"
    raw["sources"][0]["endpoint"] = "https://example.org/records"
    raw["sources"][0]["auth"] = {"kind": "none"}
    pack = validate_source_pack(raw)
    source = pack["sources"][0]
    SourcePackStore(conn).install(pack, principal_id="operator", enable=True)
    runtime = SourcePackRuntime(conn)
    runtime.accept_license(pack["pack_id"], source["source_id"], principal_id="operator")
    planner_scopes = SCOPES | {"knowledge:source-planner:read", "knowledge:source-planner:write"}
    capability = SourcePlannerStore(conn).register_capability(
        "research", source["source_id"], "1.0.0",
        coverage={"languages": ["en"], "geographies": ["global"], "topics": ["methods"],
                  "date_windows": [{"from_ms": 0, "to_ms": 10_000_000_000_000}]},
        authority={}, access={}, latency={}, cost={}, rate_limits={},
        query_forms=["topic", "language", "geography", "date-range"],
        connector={}, dependency_group="fixture",
        principal_id="alice", scopes=planner_scopes,
    )
    request = {
        "pack_id": pack["pack_id"], "run_key": "strict-empty", "operation": "search",
        "mode": "backfill", "backfill": {"from_ms": 0, "to_ms": 10_000_000_000_000},
        "source_ids": [source["source_id"]], "required_sources": [source["source_id"]],
        "parameters": {"topic": "methods", "language": "en", "geography": "global"},
        "max_pages": 2, "max_results": 10,
    }
    run = runtime.run(request, principal_id="operator",
                      adapters={source["source_id"]: FixturePageAdapter(source, [[]])},
                      dns_resolver=lambda _: ["8.8.8.8"])
    assert run["status"] == "complete" and run["watermark"] is not None
    store = CoverageAssessmentStore(conn)
    scope = _scope()
    cells = store._cells(scope)
    english = next(c["cell_id"] for c in cells if c["language"] == "en")
    german = next(c["cell_id"] for c in cells if c["language"] == "de")
    binding = _binding(pack, [english, german], run_id=run["run_id"])
    binding["capability_id"] = capability["capability_id"]
    saved = store.save("research", "strict-empty", scope, [binding],
                       principal_id="alice", scopes=planner_scopes | {"knowledge:gaps:write"},
                       record_gap_observations=True)
    assert next(c for c in saved["cells"] if c["cell_id"] == english)["status"] == "empty-successful"
    assert next(c for c in saved["cells"] if c["cell_id"] == german)["status"] == "out-of-scope"
    assert saved["sources"][0]["commit_state"] == "committed"
    assert conn.execute(
        "SELECT count(*) FROM research_coverage_observations WHERE namespace='research'"
    ).fetchone() == (2,)

    failed = runtime.run(
        {**request, "run_key": "failed", "retries": 0},
        principal_id="operator",
        adapters={source["source_id"]: FixturePageAdapter(
            source, [], failures={0: SourcePackError("source_unavailable", "fixture failure")})},
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    assert failed["status"] == "failed" and failed["watermark"] is None
    failed_binding = _binding(pack, [english], run_id=failed["run_id"])
    failed_binding["capability_id"] = capability["capability_id"]
    gap = store.save("research", "failed", scope, [failed_binding],
                     principal_id="alice", scopes=planner_scopes)
    failed_cell = next(c for c in gap["cells"] if c["cell_id"] == english)
    assert failed_cell["status"] == "unavailable"
    assert "failed_acquisition" in failed_cell["reasons"]
    assert gap["sources"][0]["commit_state"] == "failed_uncommitted"
