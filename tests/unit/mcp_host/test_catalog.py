"""Contract and policy tests for the generated MCP capability catalog."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import duckdb
from jsonschema import Draft7Validator

from src.mcp_host.catalog import STATES, build_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_CONFIG = REPO_ROOT / ".mcp.json"
CATALOG_SCHEMA = REPO_ROOT / "contracts/schemas/jsonschema/noesis-mcp-catalog-v1.json"
CATALOG_ARTIFACT = REPO_ROOT / "contracts/generated/noesis-mcp-catalog-v1.json"


def _build(**kwargs):
    defaults = {
        "enabled_pack_names": {"news"},
        "configured_backends": set(),
    }
    defaults.update(kwargs)
    return asyncio.run(build_catalog(**defaults))


def test_catalog_is_generated_from_every_registered_fastmcp_server():
    catalog = _build(granted_scopes={"public", "knowledge:read", "operator"})
    project_servers = [
        server for server in catalog["servers"] if server["kind"] == "noesis"
    ]

    assert len(project_servers) == 23
    assert catalog["conformance"] == {
        "passed": True,
        "errors": [],
        "missing_registrations": [],
        "stale_registrations": [],
    }
    assert all(server["name"].startswith("noesis-") for server in project_servers)
    assert all(server["tool_count"] > 0 for server in project_servers)
    assert sum(server["tool_count"] for server in project_servers) == len(
        catalog["tools"]
    )
    tool_ids = {tool["id"] for tool in catalog["tools"]}
    assert all(set(server["tools"]) <= tool_ids for server in project_servers)
    assert {tool["state"] for tool in catalog["tools"]} <= STATES
    assert all(
        tool["input_schema"].get("type") == "object" for tool in catalog["tools"]
    )
    assert all(tool["output_schema"] for tool in catalog["tools"])


def test_generated_artifact_conforms_to_versioned_schema():
    artifact = json.loads(CATALOG_ARTIFACT.read_text(encoding="utf-8"))
    schema = json.loads(CATALOG_SCHEMA.read_text(encoding="utf-8"))
    errors = list(Draft7Validator(schema).iter_errors(artifact))
    assert not errors, [error.message for error in errors]
    assert artifact["conformance"]["passed"] is True
    assert artifact == _build(
        granted_scopes={"public", "knowledge:read", "operator"},
        include_unusable=True,
    )


def test_public_catalog_only_returns_usable_least_privilege_reads():
    catalog = _build(include_unusable=False)

    assert catalog["tools"]
    assert all(tool["state"] == "available" for tool in catalog["tools"])
    assert all(tool["mutability"] == "read" for tool in catalog["tools"])
    assert all(
        set(tool["required_scopes"]) <= {"public", "knowledge:read"}
        for tool in catalog["tools"]
    )
    assert all(domain["visibility"] == "public" for domain in catalog["domains"])
    assert catalog["summary"]["hidden_private_domains"] == 1


def test_catalog_distinguishes_policy_and_readiness_states():
    diagnostic = _build(include_unusable=True)
    states = {tool["state"] for tool in diagnostic["tools"]}
    server_states = {server["state"] for server in diagnostic["servers"]}
    assert {"available", "degraded", "disabled", "unauthorized"} <= states
    assert "unavailable" in server_states

    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE documents(id VARCHAR)")
    conn.execute("CREATE TABLE document_domains(document_id VARCHAR, domain VARCHAR)")
    empty = _build(conn=conn, include_unusable=True)
    assert "empty" in {tool["state"] for tool in empty["tools"]}
    conn.close()


def test_private_domain_metadata_is_omitted_until_principal_is_granted(tmp_path):
    domain_config = tmp_path / "domains.yml"
    domain_config.write_text(
        """version: 1
domains:
  - name: public-research
    backing: corpus-view
    embedding_model: model
    tags: [research]
  - name: clandestine-project
    backing: namespace
    namespace: secret_archive
    namespace_backend: table-prefix
    embedding_model: model
    tags: [private]
""",
        encoding="utf-8",
    )
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE documents(id VARCHAR)")
    conn.execute("INSERT INTO documents VALUES ('d1')")
    conn.execute("CREATE TABLE document_domains(document_id VARCHAR, domain VARCHAR)")
    conn.execute("INSERT INTO document_domains VALUES ('d1', 'public-research')")
    conn.execute("CREATE TABLE kg_secret_archive_documents(id VARCHAR)")
    conn.execute("INSERT INTO kg_secret_archive_documents VALUES ('private-d1')")
    conn.execute(
        "CREATE TABLE claim_watch_domain_grants(principal_id VARCHAR, domain VARCHAR)"
    )

    hidden = _build(
        domain_config=domain_config,
        conn=conn,
        principal_id="analyst",
        include_private=True,
        include_unusable=False,
    )
    rendered = json.dumps(hidden)
    assert "clandestine-project" not in rendered
    assert "secret_archive" not in rendered
    assert hidden["summary"]["hidden_private_domains"] == 1

    conn.execute(
        "INSERT INTO claim_watch_domain_grants VALUES ('analyst', 'clandestine-project')"
    )
    visible = _build(
        domain_config=domain_config,
        conn=conn,
        principal_id="analyst",
        include_private=True,
        include_unusable=False,
    )
    assert [item["name"] for item in visible["namespaces"]] == ["secret_archive"]
    assert {item["name"] for item in visible["domains"]} == {
        "public-research",
        "clandestine-project",
    }
    conn.close()


def test_missing_and_stale_registrations_fail_conformance(tmp_path):
    registration = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    registration["mcpServers"].pop("noesis-catalog")
    registration["mcpServers"]["noesis-stale"] = {
        "type": "stdio",
        "command": "python",
        "args": ["tools/stale_mcp/server.py"],
    }
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps(registration), encoding="utf-8")

    catalog = _build(mcp_path=path)
    assert catalog["conformance"]["passed"] is False
    assert (
        "tools/catalog_mcp/server.py" in catalog["conformance"]["missing_registrations"]
    )
    assert "tools/stale_mcp/server.py" in catalog["conformance"]["stale_registrations"]
