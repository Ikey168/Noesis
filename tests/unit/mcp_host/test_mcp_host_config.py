"""Unit tests for src/mcp_host/config.py (server spec loading)."""

import json
from pathlib import Path

from src.mcp_host.config import DEFAULT_MCP_JSON, ServerSpec, load_server_specs

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_real_mcp_json_yields_only_project_servers():
    specs = load_server_specs()
    assert DEFAULT_MCP_JSON.exists()
    names = {s.name for s in specs}
    # All 12 tools/*_mcp servers, and nothing npx-launched.
    assert len(specs) == 12
    assert "neuronews-kg" in names
    assert "neuronews-pipeline" in names
    assert "memory" not in names
    assert "playwright" not in names
    assert "postgres" not in names
    for spec in specs:
        assert spec.command in ("python", "python3")
        assert spec.args[0].startswith("tools/")
        assert spec.args[0].endswith("server.py")
        assert Path(spec.cwd) == REPO_ROOT
        # The server entry point actually exists.
        assert (REPO_ROOT / spec.args[0]).exists()


def _write(tmp_path, payload) -> Path:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return path


def test_filters_non_project_entries(tmp_path):
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "npx-thing": {"type": "stdio", "command": "npx", "args": ["-y", "x"]},
                "http-thing": {"type": "http", "command": "python3", "args": ["tools/x.py"]},
                "no-args": {"type": "stdio", "command": "python3"},
                "elsewhere": {"type": "stdio", "command": "python3", "args": ["scripts/x.py"]},
                "good": {
                    "type": "stdio",
                    "command": "python3",
                    "args": ["tools/kg_mcp/server.py"],
                    "env": {"FOO": "bar"},
                },
                "not-a-dict": "nope",
            }
        },
    )
    specs = load_server_specs(path)
    assert [s.name for s in specs] == ["good"]
    assert specs[0] == ServerSpec(
        name="good",
        command="python3",
        args=("tools/kg_mcp/server.py",),
        env={"FOO": "bar"},
        cwd=str(tmp_path),
    )


def test_type_defaults_to_stdio(tmp_path):
    path = _write(
        tmp_path,
        {"mcpServers": {"g": {"command": "python3", "args": ["tools/a/server.py"]}}},
    )
    assert len(load_server_specs(path)) == 1


def test_missing_file_yields_empty_list(tmp_path):
    assert load_server_specs(tmp_path / "nope.json") == []


def test_malformed_json_yields_empty_list(tmp_path):
    assert load_server_specs(_write(tmp_path, "{not json")) == []


def test_non_dict_servers_yields_empty_list(tmp_path):
    assert load_server_specs(_write(tmp_path, {"mcpServers": ["x"]})) == []
