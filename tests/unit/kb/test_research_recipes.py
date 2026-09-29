from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.research_recipes import (
    EXECUTE_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    RecipeError,
    ResearchRecipeStore,
    execution_input_digest,
    validate_recipe,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def val(n, v):
    Draft202012Validator(json.loads((SCHEMAS / n).read_text())).validate(v)


def recipe(namespace="research"):
    return {
        "recipe_id": "source-check",
        "version": "1",
        "namespace": namespace,
        "inputs": {
            "query": {"type": "string", "required": True},
            "api_key": {"type": "string", "secret": True, "required": True},
        },
        "steps": [
            {
                "id": "fetch-step",
                "tool": "search",
                "depends_on": [],
                "input_schema": "query-v1",
                "output_schema": "results-v1",
                "required_scopes": ["source:read"],
                "source_terms": "public-web",
                "network": False,
            },
            {
                "id": "summary-step",
                "tool": "summarize",
                "depends_on": ["fetch-step"],
                "input_schema": "results-v1",
                "output_schema": "answer-v1",
                "optional": True,
            },
        ],
        "outputs": {"answer": "summary-step"},
        "compatibility": {"engine": ">=1"},
        "limits": {"retries": 1},
        "generation": 1,
        "valid_time": {},
        "producer": {"id": "fixture"},
        "policy": {"classification": "internal"},
        "provenance": {"source": "test"},
    }


def registered(s, r=None):
    return s.register(
        r or recipe(),
        principal_id="curator",
        scopes={WRITE_SCOPE},
        known_tools={"search", "summarize"},
    )


def preview(s, r, params=None, **kw):
    return s.preview(
        r["namespace"],
        r["recipe_revision_id"],
        params or {"query": "x", "api_key": {"secret_ref": "vault:key"}},
        scopes={READ_SCOPE},
        granted_scopes={"source:read"},
        allowed_sources={"public-web"},
        available_tool_versions={"search": "1", "summarize": "1"},
        **kw,
    )


def test_invalid_graph_cycles_unknown_tools_upgrades_and_hashing():
    r = recipe()
    assert (
        validate_recipe(r, known_tools={"search", "summarize"})["recipe_hash"]
        == validate_recipe(copy.deepcopy(r), known_tools={"search", "summarize"})[
            "recipe_hash"
        ]
    )
    bad = recipe()
    bad["steps"][0]["depends_on"] = ["summary-step"]
    with pytest.raises(RecipeError, match="cycle"):
        validate_recipe(bad, known_tools={"search", "summarize"})
    with pytest.raises(RecipeError, match="unknown tool"):
        validate_recipe(r, known_tools={"other"})
    secret = recipe()
    secret["inputs"]["api_key"]["default"] = "leak"
    with pytest.raises(RecipeError, match="secret inputs"):
        validate_recipe(secret, known_tools={"search", "summarize"})
    c = duckdb.connect(":memory:")
    s = ResearchRecipeStore(c)
    v = registered(s)
    changed = recipe()
    changed["steps"][0]["output_schema"] = "results-v2"
    with pytest.raises(RecipeError, match="different content"):
        s.register(
            changed,
            principal_id="c",
            scopes={WRITE_SCOPE},
            known_tools={"search", "summarize"},
        )
        val("noesis-research-recipe-v1.json", v)
        c.close()


def test_checkpoint_crash_resume_idempotency_partial_failure_timeout_budget():
    c = duckdb.connect(":memory:")
    s = ResearchRecipeStore(c, now=lambda: 100)
    r = registered(s)
    calls = []
    r_steps = s.recipe("research", r["recipe_revision_id"], scopes={READ_SCOPE})["steps"]
    # Adapter dispatch is bound to immutable recipe step IDs, even if tool names
    # are repeated or change between recipe revisions.
    adapters = {
        r_steps[0]["id"]: lambda step, state: calls.append("search") or {"items": [1]},
        r_steps[1]["id"]: lambda step, state: calls.append("summarize") or {"answer": "ok"},
    }
    with pytest.raises(RecipeError, match="crash injected"):
        s.run(
            "research",
            r["recipe_revision_id"],
            {"query": "x", "api_key": {"secret_ref": "vault:key"}},
            run_key="k",
            adapters=adapters,
            principal_id="runner",
            scopes={EXECUTE_SCOPE},
            secret_resolver=lambda _: "SECRET",
            granted_scopes={"source:read"},
            allowed_sources={"public-web"},
            tool_versions={"search": "1", "summarize": "1"},
            fail_after=1,
        )
    out = s.run(
        "research",
        r["recipe_revision_id"],
        {"query": "x", "api_key": {"secret_ref": "vault:key"}},
        run_key="k",
        adapters=adapters,
        principal_id="runner",
        scopes={EXECUTE_SCOPE},
        secret_resolver=lambda _: "SECRET",
        granted_scopes={"source:read"},
        allowed_sources={"public-web"},
        tool_versions={"search": "1", "summarize": "1"},
    )
    assert calls == ["search", "summarize"] and out["status"] == "completed"
    again = s.run(
        "research",
        r["recipe_revision_id"],
        {"query": "x", "api_key": {"secret_ref": "vault:key"}},
        run_key="k",
        adapters=adapters,
        principal_id="runner",
        scopes={EXECUTE_SCOPE},
        secret_resolver=lambda _: "SECRET",
        granted_scopes={"source:read"},
        allowed_sources={"public-web"},
        tool_versions={"search": "1", "summarize": "1"},
    )
    assert again["idempotent"]
    val("noesis-research-recipe-receipt-v1.json", out)
    c.close()


def test_adapters_bind_to_step_ids_and_optional_missing_outputs_are_durable():
    c = duckdb.connect(":memory:")
    s = ResearchRecipeStore(c, now=lambda: 100)
    r = registered(s)
    with pytest.raises(RecipeError, match="adapter keys must name recipe step IDs"):
        s.run(
            "research", r["recipe_revision_id"],
            {"query": "x", "api_key": {"secret_ref": "vault:key"}},
            run_key="unknown-adapter", adapters={"search": lambda *_: {"items": []}},
            principal_id="runner", scopes={EXECUTE_SCOPE}, granted_scopes={"source:read"},
            allowed_sources={"public-web"},
        )

    calls = []
    adapters = {"fetch-step": lambda *_: calls.append("fetch") or {"items": [1]}}
    with pytest.raises(RecipeError, match="crash injected"):
        s.run(
            "research", r["recipe_revision_id"],
            {"query": "x", "api_key": {"secret_ref": "vault:key"}},
            run_key="durable-omission", adapters=adapters,
            principal_id="runner", scopes={EXECUTE_SCOPE},
            secret_resolver=lambda _: "SECRET", granted_scopes={"source:read"},
            allowed_sources={"public-web"}, fail_after=2,
        )
    run_id = c.execute(
        "SELECT run_id FROM research_recipe_runs WHERE run_key='durable-omission'"
    ).fetchone()[0]
    checkpoint_rows = c.execute(
        "SELECT step_id,status,error_json FROM research_recipe_checkpoints WHERE run_id=? ORDER BY ordinal",
        [run_id],
    ).fetchall()
    assert [(row[0], row[1]) for row in checkpoint_rows] == [
        ("fetch-step", "completed"), ("summary-step", "omitted")
    ]
    # A later adapter appearing cannot silently replace the committed omission
    # for the same immutable run input.
    resumed = s.run(
        "research", r["recipe_revision_id"],
        {"query": "x", "api_key": {"secret_ref": "vault:key"}},
        run_key="durable-omission",
        adapters={**adapters, "summary-step": lambda *_: calls.append("late-summary") or {"answer": "new"}},
        principal_id="runner", scopes={EXECUTE_SCOPE},
        secret_resolver=lambda _: "SECRET", granted_scopes={"source:read"},
        allowed_sources={"public-web"},
    )
    assert calls == ["fetch"]
    assert resumed["omissions"] == [{"step_id": "summary-step", "code": "adapter_unavailable",
                                     "message": "no local adapter for step summary-step"}]
    c.close()


def test_execution_input_digest_is_redacted_and_part_of_idempotency_identity():
    assert execution_input_digest({"z": "SECRET", "a": 1}, secrets=["SECRET"]) == execution_input_digest(
        {"a": 1, "z": "[REDACTED]"}
    )
    c = duckdb.connect(":memory:")
    s = ResearchRecipeStore(c)
    r = registered(s)
    params = {"query": "x", "api_key": {"secret_ref": "vault:key"}}
    common = {
        "namespace": "research", "recipe_revision_id": r["recipe_revision_id"],
        "parameters": params, "run_key": "same-key",
        "adapters": {"fetch-step": lambda *_: {"items": [1]},
                     "summary-step": lambda *_: {"answer": "ok"}},
        "principal_id": "runner", "scopes": {EXECUTE_SCOPE},
        "secret_resolver": lambda _: "SECRET", "granted_scopes": {"source:read"},
        "allowed_sources": {"public-web"}, "execution_mode": "caller-supplied-fixture",
        "actions_executed": False,
    }
    first_hash = execution_input_digest({"fetch-step": {"items": [1], "key": "SECRET"}}, secrets=["SECRET"])
    second_hash = execution_input_digest({"fetch-step": {"items": [2], "key": "SECRET"}}, secrets=["SECRET"])
    first = s.run(**common, execution_input_hash=first_hash)
    replay = s.run(**common, execution_input_hash=first_hash)
    second = s.run(**common, execution_input_hash=second_hash)
    assert replay["idempotent"] and first["run_id"] == replay["run_id"]
    assert first["run_id"] != second["run_id"]
    assert first["execution_input_hash"] == first_hash
    assert first["execution_mode"] == "caller-supplied-fixture" and not first["actions_executed"]
    assert "SECRET" not in json.dumps(first)
    c.close()


def test_secret_scope_source_network_gates_and_redaction():
    c = duckdb.connect(":memory:")
    s = ResearchRecipeStore(c)
    r = registered(s)
    unsafe = s.preview(
        "research",
        r["recipe_revision_id"],
        {"query": "x", "api_key": "RAW"},
        scopes={READ_SCOPE},
    )
    assert not unsafe["valid"] and "RAW" not in json.dumps(unsafe)
    denied = s.preview(
        "research",
        r["recipe_revision_id"],
        {"query": "x", "api_key": {"secret_ref": "vault:key"}},
        scopes={READ_SCOPE},
        granted_scopes=set(),
        allowed_sources=set(),
    )
    assert not denied["valid"]
    good = preview(s, r)
    assert good["valid"] and good["secret_refs"] == {"api_key": "vault:key"}
    c.close()


def test_tool_upgrade_expired_snapshot_optional_step_and_replay_mismatch():
    c = duckdb.connect(":memory:")
    s = ResearchRecipeStore(c, now=lambda: 100)
    r = registered(s)
    with pytest.raises(RecipeError, match="expired"):
        s.run(
            "research",
            r["recipe_revision_id"],
            {"query": "x", "api_key": {"secret_ref": "v"}},
            run_key="expired",
            adapters={},
            principal_id="p",
            scopes={EXECUTE_SCOPE},
            granted_scopes={"source:read"},
            allowed_sources={"public-web"},
            tool_versions={"search": "1", "summarize": "1"},
            snapshot_tokens=[{"id": "s", "expires_at_ms": 99}],
        )
    out = s.run(
        "research",
        r["recipe_revision_id"],
        {"query": "x", "api_key": {"secret_ref": "v"}},
        run_key="optional",
        adapters={
            "fetch-step": lambda *_: {"items": []},
            "summary-step": lambda *_: (_ for _ in ()).throw(RuntimeError("no model")),
        },
        principal_id="p",
        scopes={EXECUTE_SCOPE},
        granted_scopes={"source:read"},
        allowed_sources={"public-web"},
        tool_versions={"search": "1", "summarize": "1"},
        snapshot_tokens=[{"id": "s", "expires_at_ms": 200}],
    )
    assert out["omissions"][0]["step_id"] == "summary-step"
    assert not s.replay(
        "research",
        out["run_id"],
        scopes={READ_SCOPE},
        current_tool_versions={"search": "2"},
    )["deterministic"]
    c.close()


def test_cancel_status_pagination_export_and_six_domains():
    c = duckdb.connect(":memory:")
    s = ResearchRecipeStore(c, now=lambda: 100)
    for ns in ("research", "political", "economic", "osint", "technical", "scientific"):
        r = registered(s, recipe(ns))
        out = s.run(
            ns,
            r["recipe_revision_id"],
            {"query": "x", "api_key": {"secret_ref": "v"}},
            run_key="fixture",
            adapters={
                "fetch-step": lambda *_: {"items": []},
                "summary-step": lambda *_: {"answer": "ok"},
            },
            principal_id="p",
            scopes={EXECUTE_SCOPE},
            granted_scopes={"source:read"},
            allowed_sources={"public-web"},
            tool_versions={"search": "1", "summarize": "1"},
        )
        assert s.status(ns, out["run_id"], scopes={READ_SCOPE})["status"] == "completed"
        assert s.export(ns, out["run_id"], scopes={READ_SCOPE})["dependency_complete"]
    assert s.list("research", scopes={READ_SCOPE}, limit=1)["items"]
    with pytest.raises(RecipeError, match="missing required scope"):
        s.list("research", scopes={"knowledge:read"})
        c.close()
