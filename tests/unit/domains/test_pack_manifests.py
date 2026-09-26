"""Every distributable pack under ``packs/`` is contract-valid and bound to real code.

A pack manifest is advisory metadata that is easy to let drift: a renamed tool,
a dropped schema or a moved source pack leaves it pointing at nothing. These
checks tie each manifest's references to the generated MCP catalog, the schema
directory and the source-pack validator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.domains import pack_install
from src.domains.pack_format import PackManifest, validate_manifest
from src.ingestion.source_packs import validate_source_pack

ROOT = Path(__file__).resolve().parents[3]
PACKS = sorted((ROOT / "packs").glob("*/pack.json"))
SCHEMAS = ROOT / "contracts/schemas/jsonschema"
CATALOG = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
TOOLS = {tool["name"]: tool for tool in CATALOG["tools"]}


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_consolidated_domains_ship_as_packs():
    names = {path.parent.name for path in PACKS}
    assert {"science", "market", "osint"} <= names
    for path in PACKS:
        assert _load(path)["name"] == path.parent.name


@pytest.mark.parametrize("path", PACKS, ids=lambda path: path.parent.name)
def test_manifest_is_valid_and_references_resolve(path: Path):
    data = _load(path)
    assert validate_manifest(data) == []
    for schema in data.get("ontology_extensions", {}).get("reuses", []):
        # A reused contract is a JSON schema or a documented contract family.
        assert (SCHEMAS / f"{schema}.json").exists() or (
            ROOT / "contracts" / f"{schema}.md").exists(), schema
    if "source_pack" in data:
        source_pack = ROOT / data["source_pack"]
        assert source_pack.exists(), data["source_pack"]
        validate_source_pack(_load(source_pack))
    for example in data.get("query_examples", []):
        assert {"intent", "tool", "arguments", "semantics"} <= set(example)
        tool = TOOLS.get(example["tool"])
        assert tool is not None, f"{example['tool']} is not a registered MCP tool"
        properties = tool["input_schema"].get("properties", {})
        assert set(example["arguments"]) <= set(properties), example["tool"]
        assert set(tool["input_schema"].get("required") or []) <= set(example["arguments"]), (
            example["tool"])


@pytest.mark.parametrize("name", ["science", "market", "osint"])
def test_consolidated_pack_installs_and_uninstalls(name: str):
    data = _load(ROOT / "packs" / name / "pack.json")
    try:
        first = pack_install.install_manifest(PackManifest.from_dict(data))
        again = pack_install.install_manifest(PackManifest.from_dict(data))
        assert first == again
        assert pack_install.installed_packs()[name] == data["version"]
        assert first["capabilities"] == data["capabilities"]
    finally:
        assert pack_install.uninstall(name) is True
