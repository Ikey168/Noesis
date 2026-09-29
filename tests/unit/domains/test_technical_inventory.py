from __future__ import annotations

import asyncio
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains.technical.inventory import (
    InventoryError,
    InventoryStore,
    parse_inventory,
)
from src.domains.technical.model import (
    package_object_id,
    record_advisory_range,
    record_object,
)

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "contracts/schemas/jsonschema/noesis-technical-inventory-v1.json"
    ).read_text()
)


def test_package_lock_preserves_scoped_names_duplicates_and_provenance():
    content = json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"dependencies": {"@Acme/UI": "^1.2.0", "parent": "1.0.0"}},
                "node_modules/@Acme/UI": {"version": "1.2.3"},
                "node_modules/parent": {"version": "1.0.0"},
                "node_modules/parent/node_modules/@Acme/UI": {"version": "1.1.0"},
                "node_modules/local": {"version": "file:../local", "link": True},
            },
        }
    )
    result = parse_inventory(content, "package-lock.json")
    scoped = [
        entry
        for entry in result["entries"]
        if entry["coordinate"] == "pkg:npm:@acme/ui"
    ]
    assert [(entry["version"], entry["direct"]) for entry in scoped] == [
        ("1.2.3", True),
        ("1.1.0", False),
    ]
    assert result["counts"] == {"pinned": 3, "unresolved": 0, "unsupported": 1}
    assert len(result["inventory_hash"]) == 64


def test_requirements_normalize_names_and_label_unresolved_lines():
    result = parse_inventory(
        "Foo_Bar==1.2.3\nfoo.bar==1.2.3 # duplicate\nrequests>=2\n-r more.txt\n",
        "requirements.txt",
    )
    assert [entry["coordinate"] for entry in result["entries"][:2]] == [
        "pkg:pypi:foo-bar",
        "pkg:pypi:foo-bar",
    ]
    assert [entry["status"] for entry in result["entries"]] == [
        "pinned",
        "pinned",
        "unresolved",
        "unsupported",
    ]
    assert [entry["origin"] for entry in result["entries"]] == [
        "line:1",
        "line:2",
        "line:3",
        "line:4",
    ]


@pytest.mark.parametrize(
    "content,format,code",
    [
        ("{", "package-lock.json", "invalid_inventory"),
        ('{"lockfileVersion": 4}', "package-lock.json", "invalid_inventory"),
        (
            '{"lockfileVersion": 3, "packages": {"x": []}}',
            "package-lock.json",
            "invalid_inventory",
        ),
        ("a==1.0", "unknown", "unsupported_format"),
        ("x" * 2_000_001, "requirements.txt", "too_large"),
    ],
)
def test_invalid_or_unbounded_inputs_are_rejected(content, format, code):
    with pytest.raises(InventoryError) as error:
        parse_inventory(content, format)
    assert error.value.code == code


def test_store_is_owner_scoped_and_links_only_acquired_exact_identities():
    conn = duckdb.connect(":memory:")
    store = InventoryStore(conn)
    coordinate = "pkg:pypi:foo-bar"
    package_id = package_object_id(coordinate)
    record_object(
        conn,
        object_type="package",
        object_id=package_id,
        coordinate=coordinate,
        canonical_name="Foo_Bar",
        source_document_id="pypi:foo",
        observed_at=1,
    )
    record_advisory_range(
        conn,
        "advisory:OSV-1",
        package_id,
        ecosystem="pypi",
        range_type="ECOSYSTEM",
        events=[{"introduced": "0"}],
        source_document_id="osv:1",
        observed_at=2,
    )
    imported = store.import_inventory(
        "Foo_Bar==1.2.3\n", "requirements.txt", owner_id="alice"
    )
    assert not list(Draft7Validator(SCHEMA).iter_errors(imported))
    assert (
        store.import_inventory(
            "Foo_Bar==1.2.3\n", "requirements.txt", owner_id="alice"
        )["inventory_id"]
        == imported["inventory_id"]
    )
    page = store.inspect(imported["inventory_id"], owner_id="alice")
    assert not list(Draft7Validator(SCHEMA).iter_errors(page))
    assert page["entries"][0]["acquired_package"]["source_document_id"] == "pypi:foo"
    assert page["entries"][0]["acquired_advisories"][0]["source_document_id"] == "osv:1"
    with pytest.raises(InventoryError) as denied:
        store.inspect(imported["inventory_id"], owner_id="bob")
    assert denied.value.code == "not_found"
    conn.close()


def test_inventory_public_mcp_tools_are_scoped_and_replay(tmp_path, monkeypatch):
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "technical.duckdb")
    actor = ["alice"]
    scopes = {"knowledge:technical:write", "knowledge:technical:read"}
    monkeypatch.setattr(server, "_context", lambda: (actor[0], scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    assert tools["import_technical_inventory"].parameters["required"] == [
        "content",
        "format",
    ]
    content = "Foo_Bar==1.2.3\n"
    imported = tools["import_technical_inventory"].fn(
        content=content, format="requirements.txt"
    )
    replayed = tools["import_technical_inventory"].fn(
        content=content, format="requirements.txt"
    )
    assert replayed["inventory_id"] == imported["inventory_id"]
    page = tools["inspect_technical_inventory"].fn(
        inventory_id=imported["inventory_id"]
    )
    assert page["entries"][0]["coordinate"] == "pkg:pypi:foo-bar"
    actor[0] = "bob"
    assert (
        tools["inspect_technical_inventory"].fn(inventory_id=imported["inventory_id"])[
            "error"
        ]["code"]
        == "not_found"
    )
    scopes.clear()
    assert (
        tools["import_technical_inventory"].fn(
            content=content, format="requirements.txt"
        )["error"]["code"]
        == "unauthorized"
    )
