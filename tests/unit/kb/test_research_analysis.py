import copy

import duckdb
import pytest

from src.kb.research_analysis import ResearchAnalysisStore, AnalysisError, compare_analysis_outputs
from src.kb.dataset_intelligence import DatasetIntelligenceStore, INGEST_SCOPE
from tests.unit.kb.test_dataset_intelligence import _dataset, _release

AUTH = {"principal_id": "alice", "scopes": {"knowledge:analysis:read", "knowledge:analysis:write", "knowledge:analysis:execute",
    "knowledge:dataset:read", "namespace:r:write", "namespace:economic:read"}}


def setup():
    conn = duckdb.connect()
    datasets = DatasetIntelligenceStore(conn)
    dataset = _dataset(datasets)
    release = _release(datasets, dataset)
    table_id = dataset["tables"][0]["table_id"]
    result = datasets.ingest("economic", release["release_id"], table_id, "csv", "geo,period,value\nDE,2026,2\nFR,2026,3\n",
                            {"year": 2026}, principal_id="curator", scopes={INGEST_SCOPE})
    assert result["status"] == "completed"
    manifest = {"notebook": {"nbformat": 4, "cells": [{"id": "sum", "cell_type": "code", "source": "print(2+3)"}]},
        "inputs": [{"name": "observations", "namespace": "economic", "release_id": release["release_id"], "table_id": table_id, "offset": 0, "limit": 100}],
        "metrics": [], "parameters": {}, "environment": {"image_id": "sha256:" + "0"*64}, "network": "none",
        "budgets": {"cell_timeout_seconds": 5, "run_timeout_seconds": 15, "memory_mb": 256, "cpus": 1, "max_output_bytes": 1024*1024}}
    return ResearchAnalysisStore(conn), manifest


class Runtime:
    calls = 0
    def execute(self, manifest, frozen_inputs, **kwargs):
        self.calls += 1
        notebook = copy.deepcopy(manifest["notebook"])
        notebook["cells"][0].update(execution_count=1, outputs=[{"output_type": "display_data", "metadata": {}, "data": {"application/json": {"sum": 5}}}])
        return {"status": "complete", "notebook": notebook, "environment": "fixture-runtime"}


def test_pinned_manifest_execution_replay_cell_artifacts_and_export():
    store, manifest = setup()
    state = store.register("r", "analysis", manifest, **AUTH)
    assert store.register("r", "analysis", manifest, **AUTH)["idempotent"]
    runtime = Runtime()
    run = store.execute("r", state["analysis_id"], "run", runtime=runtime, **AUTH)
    assert run["status"] == "complete", run
    assert run["result"]["artifacts"][0]["dependencies"]
    assert run["result"]["environment_packages"] == "fixture-runtime"
    assert not run["result"]["substantive_claim_verified"]
    assert store.execute("r", state["analysis_id"], "run", runtime=runtime, **AUTH)["idempotent"]
    assert runtime.calls == 1
    exported = store.export("r", run["run_id"], **AUTH)
    assert len(exported["permitted_inputs"]["datasets"]["observations"]["slice"]["items"]) == 2
    assert exported["omissions"] == []
    assert store.list_runs("r", state["analysis_id"], **AUTH)["runs"][0]["status"] == "complete"


def test_unavailable_or_changed_inputs_never_select_latest():
    store, manifest = setup()
    state = store.register("r", "analysis", manifest, **AUTH)
    store.conn.execute("UPDATE dataset_rows_v2 SET values_json='{}'")
    runtime = Runtime()
    run = store.execute("r", state["analysis_id"], "changed", runtime=runtime, **AUTH)
    assert run["status"] == "failed" and run["result"]["error_type"] == "input_changed"
    assert runtime.calls == 0
    assert store.export("r", run["run_id"], **AUTH)["omissions"][0]["reason"] == "input_changed"
    store.conn.execute("DELETE FROM dataset_releases")
    run = store.execute("r", state["analysis_id"], "missing", runtime=runtime, **AUTH)
    assert run["result"]["error_type"] == "input_unavailable" and runtime.calls == 0


def test_cancellation_and_current_input_access():
    store, manifest = setup()
    state = store.register("r", "analysis", manifest, **AUTH)
    class Cancelled(Runtime):
        def execute(self, manifest, frozen_inputs, *, run_id, cancelled):
            store.cancel("r", run_id, **AUTH)
            assert cancelled()
            return {"status": "cancelled"}
    run = store.execute("r", state["analysis_id"], "cancel", runtime=Cancelled(), **AUTH)
    assert run["status"] == "cancelled"
    with pytest.raises(AnalysisError, match="input namespace"):
        store.inspect_run("r", run["run_id"], **{**AUTH, "scopes": AUTH["scopes"] - {"namespace:economic:read"}})


def test_output_numeric_tolerances_and_missing_values():
    notebook = {"cells": [{"id": "cell", "cell_type": "code", "outputs": [{"data": {"application/json": {"value": 1.0, "missing": None}}}]}]}
    left = {"status": "complete", "notebook": notebook, "input_hash": "input", "notebook_hash": "code", "environment": "env"}
    right = copy.deepcopy(left)
    right["notebook"]["cells"][0]["outputs"][0]["data"]["application/json"]["value"] = 1.0001
    assert not compare_analysis_outputs(left, right)["equal"]
    assert compare_analysis_outputs(left, right, absolute_tolerance=0.001)["equal"]
    right["notebook"]["cells"][0]["outputs"][0]["data"]["application/json"]["missing"] = 0
    assert not compare_analysis_outputs(left, right, absolute_tolerance=0.001)["equal"]


def test_artifact_publication_failure_reuses_staged_computation(monkeypatch):
    from src.kb.artifacts import ArtifactGraph
    store, manifest = setup()
    state = store.register("r", "analysis", manifest, **AUTH)
    original = ArtifactGraph.register
    def fail(*args, **kwargs):
        raise RuntimeError("publication interrupted")
    runtime = Runtime()
    monkeypatch.setattr(ArtifactGraph, "register", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        store.execute("r", state["analysis_id"], "run", runtime=runtime, **AUTH)
    assert store.conn.execute("SELECT status FROM research_analysis_runs").fetchone()[0] == "result_ready"
    monkeypatch.setattr(ArtifactGraph, "register", original)
    assert store.execute("r", state["analysis_id"], "run", runtime=runtime, **AUTH)["status"] == "complete"
    assert runtime.calls == 1


def test_expired_worker_recovery_preserves_unknown_outcome():
    store, manifest = setup()
    clock = [1000]
    store.now = lambda: clock[0]
    state = store.register('r', 'analysis', manifest, **AUTH)
    class Crashed(Runtime):
        def execute(self, *args, **kwargs):
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        store.execute('r', state['analysis_id'], 'interrupted', runtime=Crashed(), **AUTH)
    run_id = store.list_runs('r', state['analysis_id'], **AUTH)['runs'][0]['run_id']
    with pytest.raises(AnalysisError, match='hard container deadline'):
        store.recover('r', run_id, **AUTH)
    clock[0] += 46000
    assert store.recover('r', run_id, **AUTH)['status'] == 'interrupted'
    runtime = Runtime()
    assert store.execute('r', state['analysis_id'], 'interrupted', runtime=runtime, **AUTH)['status'] == 'interrupted'
    assert runtime.calls == 0
    with pytest.raises(AnalysisError, match='completed notebook'):
        compare_analysis_outputs({}, {})


def test_package_discloses_missing_inputs_and_retains_cell_artifact_dependencies():
    from src.kb.research_packages import ResearchPackageStore
    store, manifest = setup()
    state = store.register('r', 'analysis', manifest, **AUTH)
    run = store.execute('r', state['analysis_id'], 'run', runtime=Runtime(), **AUTH)
    artifact = run['result']['artifacts'][0]
    assert artifact['dependencies'][0]['kind'] == 'source'
    store.conn.execute('DELETE FROM dataset_releases')
    package = store.export_package('r', run['run_id'], **{**AUTH, 'scopes': {*AUTH['scopes'], 'knowledge:packages:read'}})
    assert package['status'] == 'partial'
    assert package['closure']['omissions'][0]['reason'] == 'inaccessible'
    assert ResearchPackageStore(store.conn).verify(package)['valid']
    assert not any(member['component_type'] == 'dataset' for member in package['members'])
