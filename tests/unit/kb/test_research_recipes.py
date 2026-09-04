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
                "id": "search",
                "tool": "search",
                "depends_on": [],
                "input_schema": "query-v1",
                "output_schema": "results-v1",
                "required_scopes": ["source:read"],
                "source_terms": "public-web",
                "network": False,
            },
            {
                "id": "summarize",
                "tool": "summarize",
                "depends_on": ["search"],
                "input_schema": "results-v1",
                "output_schema": "answer-v1",
                "optional": True,
            },
        ],
        "outputs": {"answer": "summarize"},
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
    bad["steps"][0]["depends_on"] = ["summarize"]
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
    adapters = {
        "search": lambda step, state: calls.append("search") or {"items": [1]},
        "summarize": lambda step, state: calls.append("summarize") or {"answer": "ok"},
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
            "search": lambda *_: {"items": []},
            "summarize": lambda *_: (_ for _ in ()).throw(RuntimeError("no model")),
        },
        principal_id="p",
        scopes={EXECUTE_SCOPE},
        granted_scopes={"source:read"},
        allowed_sources={"public-web"},
        tool_versions={"search": "1", "summarize": "1"},
        snapshot_tokens=[{"id": "s", "expires_at_ms": 200}],
    )
    assert out["omissions"][0]["step_id"] == "summarize"
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
                "search": lambda *_: {"items": []},
                "summarize": lambda *_: {"answer": "ok"},
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
