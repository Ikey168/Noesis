"""Education-statistics MCP entry points: catalog registration, declared scopes, exclusions and answers (#2438)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.education_identity import EducationIdentity
from src.kb.education_statistics import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import education_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.education_statistics import EDUCATION_SCOPES, EDUCATION_TOOLS, EDUCATION_WRITES
from src.mcp_host.introspection import tool_map


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "education-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    identity = EducationIdentity(conn)
    identity.record_ror(h.NS, h.ror_records(), principal_id="svc", scopes=h.SCOPES)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert EDUCATION_TOOLS <= set(tools) and set(EDUCATION_SCOPES) == EDUCATION_TOOLS
    for name in EDUCATION_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in EDUCATION_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == EDUCATION_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in EDUCATION_TOOLS:
        assert by_name[name]["required_scopes"] == EDUCATION_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/science/providers/science.education-statistics.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in EDUCATION_TOOLS and name not in EDUCATION_WRITES
        assert op["required_scopes"] == EDUCATION_SCOPES[name]
    for name in ("institution_statistics_as_of", "country_education_statistics_as_of", "education_source_contracts",
                 "education_series_values"):
        description = tools[name].description.lower()
        assert "no rankings" in description and "no merging or averaging" in description, name


def test_reads_work_with_exactly_the_declared_scopes_and_writes_are_scoped(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    country = tools["country_education_statistics_as_of"].fn(namespace="global", country="DE",
                                                              concept="education_expenditure")
    assert country["status"] == "answered" and forbidden_keys(country) == []
    assert "university rankings or league tables" in country["exclusions"]
    institution = tools["institution_statistics_as_of"].fn(namespace="global", scheme="eter-id", code="DE0002")
    assert institution["status"] == "answered"
    refused = tools["propose_education_ror_matches"].fn(namespace="global")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:education:write", "namespace:global:write"}
    proposed = tools["propose_education_ror_matches"].fn(namespace="global")
    assert any(m["state"] == "exact" for m in proposed["matches"])
    state["scopes"] = set(h.READ_ONLY)
    by_ror = tools["institution_statistics_as_of"].fn(namespace="global", ror="https://ror.org/0zmc02b34")
    assert by_ror["subjects"] == [{"scheme": "eter-id", "code": "DE0001"}]
    matches = tools["list_education_identity_matches"].fn(namespace="global", state="candidate")
    assert {m["ror_id"] for m in matches["matches"]} == {"https://ror.org/0zfs01a23", "https://ror.org/0zfs04d56"}
    state["scopes"] = set()
    contracts = tools["education_source_contracts"].fn()
    assert contracts["live_verification"]["ipeds"]["status"] == "unverified-live"
