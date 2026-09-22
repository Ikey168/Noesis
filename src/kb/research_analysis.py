"""Pinned research-analysis manifests, isolated execution, and cell provenance."""

import hashlib
import json
import math
from pathlib import Path
import re
import time

from src.kb.research_projects import _hash, _json

READ_SCOPE = "knowledge:analysis:read"
WRITE_SCOPE = "knowledge:analysis:write"
EXECUTE_SCOPE = "knowledge:analysis:execute"
_DDL = """
CREATE TABLE IF NOT EXISTS research_analyses(
 analysis_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,request_hash TEXT NOT NULL,
 manifest_json TEXT NOT NULL,inputs_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_analysis_runs(
 run_id TEXT PRIMARY KEY,analysis_id TEXT NOT NULL,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 status TEXT NOT NULL,cancel_requested BOOLEAN NOT NULL,result_json TEXT NOT NULL);
"""


class AnalysisError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _text(value):
    if not isinstance(value, str) or not value or len(value) > 10000:
        raise AnalysisError("invalid_analysis", "bounded nonempty text is required")
    return value


def _notebook(value):
    if not isinstance(value, dict) or value.get("nbformat") != 4 or not isinstance(value.get("cells"), list) or not 1 <= len(value["cells"]) <= 1000:
        raise AnalysisError("invalid_notebook", "a v4 notebook with one to 1000 cells is required")
    cells, ids = [], set()
    for index, cell in enumerate(value["cells"]):
        if not isinstance(cell, dict) or cell.get("cell_type") not in {"code", "markdown"}:
            raise AnalysisError("invalid_notebook", "only code and Markdown cells are supported")
        source = cell.get("source", "")
        if isinstance(source, list) and all(isinstance(line, str) for line in source):
            source = "".join(source)
        if not isinstance(source, str):
            raise AnalysisError("invalid_notebook", "cell source must be text")
        identity = cell.get("id") or _hash([index, source])[:16]
        if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identity) or identity in ids:
            raise AnalysisError("invalid_notebook", "cell ids must be unique and valid")
        ids.add(identity)
        cells.append({"id": identity, "cell_type": cell["cell_type"], "source": source, "metadata": {},
                      **({"outputs": [], "execution_count": None} if cell["cell_type"] == "code" else {})})
    result = {"nbformat": 4, "nbformat_minor": 5, "metadata": {"kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"}}, "cells": cells}
    if len(_json(result).encode()) > 1024*1024:
        raise AnalysisError("invalid_notebook", "notebook code exceeds 1 MiB")
    return result


def _budgets(value):
    bounds = {"cell_timeout_seconds": (1, 300), "run_timeout_seconds": (1, 1800), "memory_mb": (128, 4096), "cpus": (1, 4), "max_output_bytes": (1024, 32*1024*1024)}
    if not isinstance(value, dict) or set(value) != set(bounds):
        raise AnalysisError("invalid_budget", "explicit cell/run time, memory, CPU, and output limits are required")
    for key, (minimum, maximum) in bounds.items():
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise AnalysisError("invalid_budget", f"{key} exceeds supported bounds")
    if value["cell_timeout_seconds"] > value["run_timeout_seconds"]:
        raise AnalysisError("invalid_budget", "cell timeout cannot exceed the whole run timeout")
    return dict(value)


class ResearchAnalysisStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time()*1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(state, principal_id, scopes, required=READ_SCOPE):
        if principal_id and "operator" in scopes:
            return
        if not principal_id or state["owner"] != principal_id or required not in scopes:
            raise AnalysisError("unauthorized", "current analysis scope and ownership are required")
        ns = state["namespace"]
        if f"namespace:{ns}:write" not in scopes and (required != READ_SCOPE or f"namespace:{ns}:read" not in scopes):
            raise AnalysisError("unauthorized", "current analysis namespace access is required")
        for item in [*state["manifest"]["inputs"], *state["manifest"]["metrics"]]:
            ns = item["namespace"]
            if f"namespace:{ns}:read" not in scopes and f"namespace:{ns}:write" not in scopes:
                raise AnalysisError("unauthorized", "current input namespace access is required")
        if state["manifest"]["inputs"] and "knowledge:dataset:read" not in scopes:
            raise AnalysisError("unauthorized", "dataset read scope is required")
        if state["manifest"]["metrics"] and "knowledge:quantitative:read" not in scopes:
            raise AnalysisError("unauthorized", "metric definition read scope is required")

    def _freeze(self, manifest, scopes):
        from src.kb.dataset_intelligence import DatasetIntelligenceStore
        store = DatasetIntelligenceStore(self.conn, initialize=False)
        frozen = {}
        for spec in manifest["inputs"]:
            release = store.release(spec["namespace"], spec["release_id"], scopes=scopes)
            if not release:
                raise AnalysisError("input_unavailable", "a declared dataset release is unavailable")
            partitions = self.conn.execute("SELECT status FROM dataset_partitions WHERE namespace=? AND release_id=? AND table_id=?",
                [spec["namespace"], spec["release_id"], spec["table_id"]]).fetchall()
            if not partitions or any(row[0] != "completed" for row in partitions):
                raise AnalysisError("input_incomplete", "dataset table requires completed ingestion partitions")
            receipts = self.conn.execute("SELECT receipt_json FROM dataset_ingestion_receipts WHERE namespace=? AND release_id=? AND table_id=?",
                [spec["namespace"], spec["release_id"], spec["table_id"]]).fetchall()
            if any(json.loads(row[0]).get("truncated") for row in receipts):
                raise AnalysisError("input_incomplete", "dataset ingestion was truncated")
            selected = store.slice(spec["namespace"], spec["release_id"], spec["table_id"], scopes=scopes,
                offset=spec["offset"], limit=spec["limit"])
            frozen[spec["name"]] = {"release": release, "slice": selected}
        metrics = []
        for spec in manifest["metrics"]:
            row = self.conn.execute("SELECT to_json(r) FROM quantitative_metric_revisions r WHERE namespace=? AND revision_id=?", [spec["namespace"], spec["revision_id"]]).fetchone()
            if not row:
                raise AnalysisError("input_unavailable", "a pinned metric definition is unavailable")
            metrics.append(json.loads(row[0]))
        result = {"datasets": frozen, "metrics": metrics}
        if len(_json(result).encode()) > 16*1024*1024:
            raise AnalysisError("input_budget_exceeded", "frozen inputs exceed 16 MiB")
        return result

    def register(self, namespace, request_key, manifest, *, principal_id, scopes):
        fields = {"notebook", "inputs", "metrics", "parameters", "environment", "budgets", "network"}
        if not isinstance(manifest, dict) or set(manifest) != fields or manifest["network"] != "none":
            raise AnalysisError("invalid_manifest", "analysis requires pinned inputs, notebook, parameters, environment, budgets, and network=none")
        if not isinstance(manifest["inputs"], list) or not 1 <= len(manifest["inputs"]) <= 20 or not isinstance(manifest["metrics"], list) or len(manifest["metrics"]) > 100:
            raise AnalysisError("invalid_inputs", "declare one to 20 dataset slices and at most 100 metric revisions")
        names = set()
        for spec in manifest["inputs"]:
            if not isinstance(spec, dict) or set(spec) != {"name", "namespace", "release_id", "table_id", "offset", "limit"}:
                raise AnalysisError("invalid_inputs", "dataset slices require explicit namespace, release, table, offset, and limit")
            for key in ("name", "namespace", "release_id", "table_id"):
                _text(spec[key])
            if spec["name"] in names or type(spec["offset"]) is not int or spec["offset"] < 0 or type(spec["limit"]) is not int or not 1 <= spec["limit"] <= 1000:
                raise AnalysisError("invalid_inputs", "slice names must be unique and row bounds explicit")
            names.add(spec["name"])
        for spec in manifest["metrics"]:
            if not isinstance(spec, dict) or set(spec) != {"namespace", "revision_id"}:
                raise AnalysisError("invalid_inputs", "metrics require namespace and exact revision id")
            _text(spec["namespace"])
            _text(spec["revision_id"])
        if not isinstance(manifest["parameters"], dict) or len(_json(manifest["parameters"]).encode()) > 1024*1024:
            raise AnalysisError("invalid_parameters", "parameters must be a JSON object within 1 MiB")
        env = manifest["environment"]
        if not isinstance(env, dict) or set(env) != {"image_id"} or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(env["image_id"])):
            raise AnalysisError("invalid_environment", "pin a local container image SHA256 identity")
        from src.kb.analysis_runtime import PodmanNotebookRuntime
        runtime = PodmanNotebookRuntime()
        normalized = {**manifest, "notebook": _notebook(manifest["notebook"]), "budgets": _budgets(manifest["budgets"]),
                      "environment": {**env, "runner_sha256": hashlib.sha256(runtime.runner.read_bytes()).hexdigest()}}
        state = {"contract": "noesis-research-analysis-v1", "analysis_id": "analysis:" + _hash([_text(namespace), principal_id, _text(request_key)])[:32],
                 "namespace": namespace, "owner": principal_id, "manifest": normalized}
        self._authorize(state, principal_id, scopes, WRITE_SCOPE)
        digest = _hash(state)
        prior = self.conn.execute("SELECT request_hash,manifest_json FROM research_analyses WHERE analysis_id=?", [state["analysis_id"]]).fetchone()
        if prior:
            if prior[0] != digest:
                raise AnalysisError("idempotency_conflict", "analysis key already identifies a different manifest")
            return {**json.loads(prior[1]), "idempotent": True}
        frozen = self._freeze(normalized, scopes)
        state.update(input_hash=_hash(frozen), notebook_hash=_hash(normalized["notebook"]), registered_at_ms=self.now())
        self.conn.execute("INSERT INTO research_analyses VALUES (?,?,?,?,?,?)", [state["analysis_id"], namespace, principal_id, digest, _json(state), _json(frozen)])
        return state

    def inspect(self, namespace, analysis_id, *, principal_id, scopes):
        row = self.conn.execute("SELECT manifest_json FROM research_analyses WHERE namespace=? AND analysis_id=?", [namespace, analysis_id]).fetchone()
        if not row:
            raise AnalysisError("analysis_unavailable", "analysis manifest is unavailable")
        state = json.loads(row[0])
        self._authorize(state, principal_id, scopes)
        return state

    def execute(self, namespace, analysis_id, request_key, *, principal_id, scopes, runtime=None):
        from src.kb.analysis_runtime import PodmanNotebookRuntime
        state = self.inspect(namespace, analysis_id, principal_id=principal_id, scopes=scopes)
        self._authorize(state, principal_id, scopes, EXECUTE_SCOPE)
        run_id = "analysis-run:" + _hash([analysis_id, _text(request_key)])[:32]
        prior = self.conn.execute("SELECT status,result_json FROM research_analysis_runs WHERE run_id=?", [run_id]).fetchone()
        if prior:
            if prior[0] == "result_ready":
                frozen = json.loads(self.conn.execute("SELECT inputs_json FROM research_analyses WHERE analysis_id=?", [analysis_id]).fetchone()[0])
                return self._publish(state, json.loads(prior[1]), frozen, idempotent=True)
            return {"run_id": run_id, "status": prior[0], "result": json.loads(prior[1]), "idempotent": True}
        self.conn.execute("INSERT INTO research_analysis_runs VALUES (?,?,?,?,?,false,?)", [run_id, analysis_id, namespace, principal_id, "running", _json({"started_at_ms": self.now()})])
        frozen = {}
        try:
            frozen = self._freeze(state["manifest"], scopes)
            if _hash(frozen) != state["input_hash"]:
                raise AnalysisError("input_changed", "declared input snapshot no longer matches its registered hash")
            actual = runtime or PodmanNotebookRuntime()
            if hasattr(actual, "runner") and hashlib.sha256(actual.runner.read_bytes()).hexdigest() != state["manifest"]["environment"]["runner_sha256"]:
                raise AnalysisError("environment_changed", "registered notebook runner changed; register a new manifest")
            def cancelled():
                return bool(self.conn.execute("SELECT cancel_requested FROM research_analysis_runs WHERE run_id=?", [run_id]).fetchone()[0])
            result = actual.execute(state["manifest"], frozen, run_id=run_id, cancelled=cancelled)
            if cancelled():
                result["status"] = "cancelled"
            if result.get("status") not in {"complete", "failed", "timeout", "cancelled"}:
                raise AnalysisError("invalid_execution_receipt", "runtime returned an unsupported state")
            if result["status"] == "complete":
                if _hash(_notebook(result.get("notebook"))) != state["notebook_hash"]:
                    raise AnalysisError("invalid_execution_receipt", "executed notebook differs from registered code")
                for cell in result["notebook"]["cells"]:
                    source = cell["source"] if isinstance(cell["source"], str) else "".join(cell["source"])
                    if cell["cell_type"] == "code" and source.strip() and type(cell.get("execution_count")) is not int:
                        raise AnalysisError("invalid_execution_receipt", "a code cell has no execution count")
        except Exception as exc:
            result = {"status": "failed", "error_type": getattr(exc, "code", type(exc).__name__), "error": str(exc)[:2000]}
        result["environment_packages"] = result.pop("environment", None)
        result.update(analysis_id=analysis_id, run_id=run_id, input_hash=state["input_hash"], notebook_hash=state["notebook_hash"],
                      environment=state["manifest"]["environment"], substantive_claim_verified=False)
        self.conn.execute("UPDATE research_analysis_runs SET status='result_ready',result_json=? WHERE run_id=?", [_json(result), run_id])
        return self._publish(state, result, frozen, idempotent=False)

    def _publish(self, state, result, frozen, *, idempotent):
        namespace, analysis_id, run_id = state["namespace"], state["analysis_id"], result["run_id"]
        self.conn.execute("BEGIN")
        try:
            if self.conn.execute("SELECT cancel_requested FROM research_analysis_runs WHERE run_id=?", [run_id]).fetchone()[0]:
                result["status"] = "cancelled"
            if result["status"] == "complete":
                from src.kb.artifacts import ArtifactGraph
                graph = ArtifactGraph(self.conn)
                dependencies = []
                for name, data in frozen["datasets"].items():
                    item = graph.register(namespace, "source", f"{analysis_id}:input:{name}",
                        {"release_id": data["release"]["release_id"], "namespace": data["release"]["namespace"], "slice_hash": _hash(data),
                         "row_ids": [row["row_id"] for row in data["slice"]["items"]]},
                        configuration={"input_hash": state["input_hash"]}, producer={"name": "research-analysis", "version": "1"}, dependencies=[], generation=1)
                    dependencies.append({"dependency_id": item["artifact_id"], "kind": "source", "content_hash": item["content_hash"]})
                for metric in frozen["metrics"]:
                    item = graph.register(namespace, "source", f"{analysis_id}:metric:{metric['revision_id']}",
                        {"revision_id": metric["revision_id"], "namespace": metric["namespace"], "content_hash": _hash(metric)},
                        configuration={}, producer={"name": "research-analysis", "version": "1"}, dependencies=[], generation=1)
                    dependencies.append({"dependency_id": item["artifact_id"], "kind": "source", "content_hash": item["content_hash"]})
                artifacts = []
                for cell in result.get("notebook", {}).get("cells", []):
                    for index, output in enumerate(cell.get("outputs", [])):
                        artifact = graph.register(namespace, "summary", f"{run_id}:{cell['id']}:{index}",
                            {"cell_id": cell["id"], "output": output, "run_id": run_id, "substantive_claim_verified": False},
                            configuration={"notebook_hash": state["notebook_hash"], "environment": state["manifest"]["environment"]},
                            producer={"name": "nbclient", "version": "0.10.4"}, dependencies=dependencies, generation=1)
                        artifacts.append(artifact)
                result["artifacts"] = artifacts
            self.conn.execute("UPDATE research_analysis_runs SET status=?,result_json=? WHERE run_id=?", [result["status"], _json(result), run_id])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"run_id": run_id, "status": result["status"], "result": result, "idempotent": idempotent}

    def cancel(self, namespace, run_id, *, principal_id, scopes):
        row = self.conn.execute("SELECT analysis_id,status FROM research_analysis_runs WHERE namespace=? AND run_id=?", [namespace, run_id]).fetchone()
        if not row:
            raise AnalysisError("run_unavailable", "analysis run is unavailable")
        state = self.inspect(namespace, row[0], principal_id=principal_id, scopes=scopes)
        self._authorize(state, principal_id, scopes, EXECUTE_SCOPE)
        if row[1] in {"running", "result_ready"}:
            self.conn.execute("UPDATE research_analysis_runs SET cancel_requested=true WHERE run_id=?", [run_id])
        return {"run_id": run_id, "cancel_requested": row[1] in {"running", "result_ready"}, "status": row[1]}

    def recover(self, namespace, run_id, *, principal_id, scopes):
        run = self.inspect_run(namespace, run_id, principal_id=principal_id, scopes=scopes)
        state = self.inspect(namespace, run["analysis_id"], principal_id=principal_id, scopes=scopes)
        self._authorize(state, principal_id, scopes, EXECUTE_SCOPE)
        if run["status"] == "result_ready":
            frozen = json.loads(self.conn.execute("SELECT inputs_json FROM research_analyses WHERE analysis_id=?", [state["analysis_id"]]).fetchone()[0])
            return self._publish(state, run["result"], frozen, idempotent=True)
        if run["status"] != "running":
            return run
        started = run["result"].get("started_at_ms")
        if started is None or self.now() < started + (state["manifest"]["budgets"]["run_timeout_seconds"] + 30)*1000:
            raise AnalysisError("run_may_be_active", "recovery must wait for the hard container deadline and cleanup grace")
        result = {**run["result"], "status": "interrupted", "error_type": "worker_interrupted", "substantive_claim_verified": False}
        self.conn.execute("UPDATE research_analysis_runs SET status='interrupted',result_json=? WHERE run_id=? AND status='running'", [_json(result), run_id])
        return self.inspect_run(namespace, run_id, principal_id=principal_id, scopes=scopes)

    def list_runs(self, namespace, analysis_id, *, principal_id, scopes, offset=0, limit=100):
        self.inspect(namespace, analysis_id, principal_id=principal_id, scopes=scopes)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise AnalysisError("invalid_page", "nonnegative offset and limit from one to 100 required")
        rows = self.conn.execute("SELECT run_id,status,cancel_requested FROM research_analysis_runs WHERE analysis_id=? ORDER BY run_id LIMIT ? OFFSET ?", [analysis_id, limit+1, offset]).fetchall()
        return {"runs": [{"run_id": row[0], "status": row[1], "cancel_requested": row[2]} for row in rows[:limit]],
                "next_offset": offset+limit if len(rows) > limit else None}

    def inspect_run(self, namespace, run_id, *, principal_id, scopes):
        row = self.conn.execute("SELECT analysis_id,status,result_json,cancel_requested FROM research_analysis_runs WHERE namespace=? AND run_id=?", [namespace, run_id]).fetchone()
        if not row:
            raise AnalysisError("run_unavailable", "analysis run is unavailable")
        self.inspect(namespace, row[0], principal_id=principal_id, scopes=scopes)
        return {"run_id": run_id, "analysis_id": row[0], "status": row[1], "result": json.loads(row[2]), "cancel_requested": row[3]}

    def export(self, namespace, run_id, *, principal_id, scopes):
        run = self.inspect_run(namespace, run_id, principal_id=principal_id, scopes=scopes)
        state = self.inspect(namespace, run["analysis_id"], principal_id=principal_id, scopes=scopes)
        omissions, inputs = [], None
        try:
            fresh = self._freeze(state["manifest"], scopes)
            if _hash(fresh) != state["input_hash"]:
                raise AnalysisError("input_changed", "input contents changed")
            inputs = fresh
        except Exception as exc:
            omissions.append({"kind": "inputs", "reason": getattr(exc, "code", type(exc).__name__)})
        result = {"contract": "noesis-analysis-export-v1", "analysis": state, "run": run,
                  "permitted_inputs": inputs, "omissions": omissions, "substantive_claim_verified": False}
        return {**result, "sha256": _hash(result)}

    def export_package(self, namespace, run_id, *, principal_id, scopes):
        if "knowledge:packages:read" not in scopes and "operator" not in scopes:
            raise AnalysisError("unauthorized", "package read scope is required")
        bundle = self.export(namespace, run_id, principal_id=principal_id, scopes=scopes)
        # Build offline in memory so private dataset bytes are not copied into
        # the shared package component registry or retained after this request.
        import duckdb
        from src.kb.research_packages import ResearchPackageStore, WRITE_SCOPE as PACKAGE_WRITE
        with duckdb.connect() as conn:
            packages = ResearchPackageStore(conn)
            auth = {"principal_id": principal_id, "scopes": {PACKAGE_WRITE}}
            inputs_id, code_id, output_id = (run_id + suffix for suffix in (":inputs", ":code", ":outputs"))
            packages.register_component(namespace, "dataset", inputs_id, bundle["permitted_inputs"],
                access_status="inaccessible" if bundle["omissions"] else "accessible", metadata={"omissions": bundle["omissions"]}, **auth)
            packages.register_component(namespace, "method", code_id, bundle["analysis"], dependencies=[inputs_id], **auth)
            packages.register_component(namespace, "asset", output_id, bundle["run"], dependencies=[code_id, inputs_id], **auth)
            manifest = packages.create_manifest(namespace, {"format_version": "1.0", "question": "Replay registered research analysis " + run_id,
                "plan": {"analysis_id": bundle["analysis"]["analysis_id"]}, "snapshot": {"input_hash": bundle["analysis"]["input_hash"]},
                "evidence": [inputs_id], "transformations": [code_id], "findings": [output_id],
                "limitations": ["Successful computation does not verify a substantive claim", *bundle["omissions"]],
                "policies": {"network": "none", "executable_by_default": False}, "compatibility": {"notebook": "4", "runtime": "rootless-podman"}}, **auth)
            return packages.build(namespace, manifest["package_id"], [output_id], allow_partial=True, **auth)


def compare_analysis_outputs(left, right, *, absolute_tolerance=0.0, relative_tolerance=0.0):
    if any(run.get("status") != "complete" or not run.get("notebook", {}).get("cells") for run in (left, right)):
        raise AnalysisError("run_incomplete", "output comparison requires two completed notebook runs")
    if any(type(v) not in {int, float} or not math.isfinite(v) or v < 0 for v in (absolute_tolerance, relative_tolerance)):
        raise AnalysisError("invalid_tolerance", "numeric tolerances must be finite and nonnegative")
    def equal(a, b):
        if type(a) in {int, float} and type(b) in {int, float}:
            return math.isclose(a, b, abs_tol=absolute_tolerance, rel_tol=relative_tolerance)
        if type(a) != type(b):
            return False
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(equal(a[key], b[key]) for key in a)
        if isinstance(a, list):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        return a == b
    def outputs(run):
        return {cell["id"]: [{key: value for key, value in output.items() if key != "execution_count"} for output in cell.get("outputs", [])]
                for cell in run.get("notebook", {}).get("cells", []) if cell.get("cell_type") == "code"}
    a, b = outputs(left), outputs(right)
    return {"equal": equal(a, b), "absolute_tolerance": absolute_tolerance, "relative_tolerance": relative_tolerance,
            "changed_cells": sorted(key for key in a.keys() | b.keys() if key not in a or key not in b or not equal(a[key], b[key])),
            "same_inputs": left.get("input_hash") == right.get("input_hash"), "same_code": left.get("notebook_hash") == right.get("notebook_hash"),
            "same_environment": left.get("environment") == right.get("environment"), "substantive_claim_verified": False}
