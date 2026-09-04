"""Contract tests for the complete MCP capability plane (issue #1008).

These tests intentionally exercise FastMCP's registered ``FunctionTool``
objects, rather than only the underlying ``src`` functions.  That catches the
adapter-layer failures agents actually see: modules that no longer import,
missing descriptions/schemas, accidental write access, and malformed empty-
warehouse responses.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
from pathlib import Path

import duckdb
import pytest

from src.database.local_warehouse_seed import ensure_schema_and_seed


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_PATHS = sorted((REPO_ROOT / "tools").glob("*_mcp/server.py"))

# One side-effect-free smoke call per independently deployed server.  Required
# arguments deliberately point at a missing record where appropriate: a read
# adapter must still return its documented empty/error value rather than raise.
READ_SMOKES = {
    "argument_mcp": ("am_stats", {}),
    "blog_mcp": ("list_subscriptions", {}),
    "catalog_mcp": ("capability_catalog", {}),
    "contract_mcp": ("list_contracts", {}),
    "dataset_mcp": ("get_stats", {}),
    "domain_packs_mcp": ("list_packs", {}),
    "kb_mcp": ("kb_domains", {}),
    "kg_mcp": ("kg_stats", {}),
    "lineage_mcp": ("list_namespaces", {}),
    "monitoring_mcp": ("current_metrics", {}),
    "osint_mcp": ("contradiction_scan", {}),
    "pipeline_mcp": ("list_connector_types", {}),
    "provisioning_mcp": ("kg_list", {}),
    "research_mcp": ("venues", {}),
    "schema_mcp": ("list_routes", {}),
    "security_mcp": ("security_posture", {}),
    "sources_mcp": ("list_sources", {}),
    "statistics_mcp": ("stats", {}),
}

# Tools permitted to write by the capability-plane contract. They are still
# invoked against an isolated warehouse below, but are excluded from the
# read-only checksum assertion.
DOCUMENTED_WRITES = {
    ("argument_mcp", "trigger_actor_batch"),
    ("argument_mcp", "trigger_attribution_batch"),
    ("argument_mcp", "trigger_outlet_clustering"),
    ("argument_mcp", "trigger_outlet_scoring"),
    ("argument_mcp", "trigger_relation_extraction"),
    ("blog_mcp", "subscribe_feed"),
    ("blog_mcp", "unsubscribe_feed"),
    ("blog_mcp", "harvest_feed"),
    ("domain_packs_mcp", "enable_pack"),
    ("domain_packs_mcp", "disable_pack"),
    ("domain_packs_mcp", "run_enrichers"),
    ("pipeline_mcp", "run_connector"),
    ("pipeline_mcp", "run_stage"),
    ("pipeline_mcp", "trigger_followthrough_check"),
    ("pipeline_mcp", "trigger_detect_anomalies"),
    ("pipeline_mcp", "trigger_lead_lag"),
    ("pipeline_mcp", "trigger_cluster_narratives"),
    ("pipeline_mcp", "trigger_embed_documents"),
    ("pipeline_mcp", "trigger_summarize_documents"),
    ("provisioning_mcp", "kg_deploy"),
    ("provisioning_mcp", "kg_attach_pipeline"),
    ("provisioning_mcp", "kg_attach_sources"),
    ("provisioning_mcp", "kg_ingest"),
    ("provisioning_mcp", "kg_teardown"),
}

# Analytics whose advertised output contract must be machine-checkable before
# an agent calls them.  A result may contain more fields, but never fewer than
# the honesty envelope.
HONEST_ANALYTICS = {
    "argument_mcp": {"score_confidence", "stance_significance"},
    "kg_mcp": {"kg_communities", "kg_centrality"},
    "pipeline_mcp": {
        "detect_anomalies", "lead_lag", "cluster_narratives",
        "semantic_drift", "forecast_topic", "speaker_balance",
    },
    "research_mcp": {"venues"},
    "osint_mcp": {
        "corroborate", "source_reliability", "image_reuse_findings", "image_reuse",
    },
}


def _load_server(path: Path):
    name = f"mcp_surface_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tools(module):
    """Registered tools through FastMCP 2.x's public async discovery API."""
    return asyncio.run(module.mcp.get_tools())


def _call(tool, kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _sample_value(name, schema):
    overrides = {
        "domain": "news",
        "since": "1970-01-01T00:00:00Z",
        "task": "claims",
        "table": "documents",
        "path": "/api/v1/kb/brief",
        "stage": "ingest",
        "url": "invalid://offline-smoke",
        "document": {
            "document_id": "mcp-smoke", "source_type": "note",
            "language": "en", "ingested_at": 0,
            "content": "The offline MCP smoke test reported one result.",
        },
    }
    if name in overrides:
        return overrides[name]
    kind = schema.get("type")
    if kind is None:
        for option in schema.get("anyOf", []):
            if option.get("type") != "null":
                kind = option.get("type")
                schema = option
                break
    return {
        "string": "missing",
        "integer": 1,
        "number": 0.5,
        "boolean": False,
        "array": [0.1, 0.2] if name in {"a", "b"} else [],
        "object": {},
    }.get(kind, "missing")


def _tool_cases():
    cases = []
    for path in SERVER_PATHS:
        module = _load_server(path)
        for name, tool in _tools(module).items():
            cases.append((path.parent.name, name))
    return cases


ALL_TOOL_CASES = _tool_cases()


@pytest.fixture()
def seeded_warehouse(tmp_path, monkeypatch):
    path = tmp_path / "mcp-surface.duckdb"
    conn = duckdb.connect(str(path))
    ensure_schema_and_seed(conn)
    conn.close()
    monkeypatch.setenv("NOESIS_DB_PATH", str(path))
    monkeypatch.setenv("NOESIS_DOMAINS_CONFIG", str(REPO_ROOT / "config" / "domains.yml"))
    monkeypatch.setenv("NOESIS_SUBSCRIPTIONS_PATH", str(tmp_path / "subscriptions.json"))
    monkeypatch.delenv("NOESIS_OSINT_GATED_TOOLS", raising=False)
    return path


@pytest.mark.parametrize("server_path", SERVER_PATHS, ids=lambda p: p.parent.name)
def test_every_server_and_tool_is_discoverable(server_path):
    module = _load_server(server_path)
    tools = _tools(module)
    assert tools, f"{server_path.parent.name} registered no tools"
    for name, tool in tools.items():
        assert tool.name == name
        assert tool.description and tool.description.strip(), f"{name} lacks a description"
        assert tool.parameters.get("type") == "object", f"{name} lacks an input schema"
        assert tool.output_schema is not None, f"{name} lacks an output schema"


@pytest.mark.parametrize("server_name", sorted(READ_SMOKES))
def test_each_server_has_a_well_formed_read_result(server_name, seeded_warehouse):
    path = REPO_ROOT / "tools" / server_name / "server.py"
    module = _load_server(path)
    tools = _tools(module)
    tool_name, kwargs = READ_SMOKES[server_name]
    result = _call(tools[tool_name], kwargs)
    assert isinstance(result, (dict, list)), (
        f"{server_name}.{tool_name} returned an unstructured {type(result).__name__}"
    )


def test_read_smokes_do_not_mutate_the_warehouse(seeded_warehouse):
    before = hashlib.sha256(seeded_warehouse.read_bytes()).digest()
    for server_name, (tool_name, kwargs) in READ_SMOKES.items():
        module = _load_server(REPO_ROOT / "tools" / server_name / "server.py")
        _call(_tools(module)[tool_name], kwargs)
    after = hashlib.sha256(seeded_warehouse.read_bytes()).digest()
    assert after == before


@pytest.mark.parametrize(
    ("server_name", "tool_name"), ALL_TOOL_CASES,
    ids=lambda value: str(value),
)
def test_every_tool_returns_json_and_reads_do_not_mutate(
    server_name, tool_name, seeded_warehouse
):
    """Invoke the complete adapter surface, not merely one tool per server."""
    module = _load_server(REPO_ROOT / "tools" / server_name / "server.py")
    tool = _tools(module)[tool_name]
    properties = tool.parameters.get("properties", {})
    kwargs = {
        name: _sample_value(name, properties.get(name, {}))
        for name in tool.parameters.get("required", [])
    }
    before = hashlib.sha256(seeded_warehouse.read_bytes()).digest()
    result = _call(tool, kwargs)
    assert isinstance(result, (dict, list, str, int, float, bool, type(None)))
    if (server_name, tool_name) not in DOCUMENTED_WRITES:
        after = hashlib.sha256(seeded_warehouse.read_bytes()).digest()
        assert after == before, f"{server_name}.{tool_name} mutated the warehouse"


@pytest.mark.parametrize(
    ("server_name", "tool_name"),
    [
        (server, tool)
        for server, names in HONEST_ANALYTICS.items()
        for tool in sorted(names)
    ],
)
def test_analytic_output_schemas_advertise_the_honesty_envelope(server_name, tool_name):
    module = _load_server(REPO_ROOT / "tools" / server_name / "server.py")
    tool = _tools(module)[tool_name]
    schema = tool.output_schema or {}
    required = set(schema.get("required", []))
    properties = set(schema.get("properties", {}))
    assert {"n", "method", "assumptions"} <= required
    assert {"n", "method", "assumptions"} <= properties
