from __future__ import annotations

import json
import asyncio
from pathlib import Path

import duckdb
from jsonschema import Draft7Validator

from src.domains.technical.impact import assess_inventory
from src.domains.technical.inventory import InventoryStore
from src.domains.technical.model import (
    package_object_id,
    record_advisory_range,
    record_object,
)


def _package(conn, coordinate):
    object_id = package_object_id(coordinate)
    record_object(
        conn,
        object_type="package",
        object_id=object_id,
        coordinate=coordinate,
        canonical_name=coordinate,
        source_document_id="registry:" + coordinate,
        observed_at=1,
    )
    return object_id


def _advisory(
    conn,
    package_id,
    advisory_id,
    events,
    *,
    status="active",
    ecosystem="npm",
    range_type="ECOSYSTEM",
):
    record_object(
        conn,
        object_type="advisory",
        object_id=advisory_id,
        canonical_name=advisory_id,
        status=status,
        source_url="https://osv.dev/vulnerability/" + advisory_id,
        source_document_id="osv:" + advisory_id,
        observed_at=2,
    )
    record_advisory_range(
        conn,
        advisory_id,
        package_id,
        ecosystem=ecosystem,
        range_type=range_type,
        events=events,
        source_document_id="osv:" + advisory_id,
        observed_at=2,
    )


def test_npm_fixed_boundary_prerelease_withdrawal_and_transitive_origin():
    conn = duckdb.connect(":memory:")
    store = InventoryStore(conn)
    package_id = _package(conn, "pkg:npm:@scope/lib")
    _advisory(
        conn,
        package_id,
        "advisory:OSV-1",
        [{"introduced": "1.0.0"}, {"fixed": "2.0.0"}],
    )
    _advisory(
        conn,
        package_id,
        "advisory:OSV-OLD",
        [{"introduced": "0"}],
        status="withdrawn",
    )
    record_object(
        conn,
        object_type="version",
        object_id="version:3",
        coordinate="pkg:npm:@scope/lib",
        canonical_name="@scope/lib",
        version="3.0.0",
        source_document_id="npm:release:3",
        observed_at=3,
    )
    lockfile = json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"dependencies": {"@scope/lib": "^2.0.0"}},
                "node_modules/@scope/lib": {"version": "2.0.0-rc.1"},
                "node_modules/parent/node_modules/@scope/lib": {"version": "2.0.0"},
            },
        }
    )
    inventory = store.import_inventory(lockfile, "package-lock.json", owner_id="alice")
    result = assess_inventory(conn, inventory["inventory_id"], owner_id="alice")
    schema = json.loads((Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema/noesis-technical-impact-v1.json").read_text())
    assert not list(Draft7Validator(schema).iter_errors(result))
    direct, transitive = result["findings"]
    assert direct["entry"]["direct"] and direct["overall"] == "affected"
    assert direct["advisories"][0]["finding"] == "affected"
    assert direct["advisories"][1]["reason"] == "withdrawn_advisory"
    assert (
        direct["upstream_review_candidates"][0]["relevance"]
        == "heuristic_newer_release"
    )
    assert not transitive["entry"]["direct"]
    assert transitive["advisories"][0]["finding"] == "unaffected_under_assessed_range"
    assert transitive["overall"] == "unknown"
    conn.close()


def test_impact_mcp_read_is_owner_scoped(tmp_path, monkeypatch):
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "impact.duckdb")
    with duckdb.connect(path) as conn:
        inventory = InventoryStore(conn).import_inventory(
            "Missing==1.0\n", "requirements.txt", owner_id="alice"
        )
    actor = ["alice"]
    scopes = {"knowledge:technical:read"}
    monkeypatch.setattr(server, "_context", lambda: (actor[0], scopes))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    tool = tools["assess_technical_inventory_impact"].fn
    result = tool(inventory_id=inventory["inventory_id"])
    assert result["findings"][0]["reason"] == "package_not_acquired"
    actor[0] = "bob"
    assert tool(inventory_id=inventory["inventory_id"])["error"]["code"] == "not_found"
    scopes.clear()
    assert tool(inventory_id=inventory["inventory_id"])["error"]["code"] == "unauthorized"


def test_pypi_prerelease_and_unknown_without_acquired_coverage():
    conn = duckdb.connect(":memory:")
    store = InventoryStore(conn)
    package_id = _package(conn, "pkg:pypi:foo-bar")
    _advisory(
        conn,
        package_id,
        "advisory:PY-1",
        [{"introduced": "1.0"}, {"fixed": "1.2.0"}],
        ecosystem="pypi",
    )
    inventory = store.import_inventory(
        "Foo_Bar==1.2.0rc1\nMissing==1.0\nUnpinned>=2\n",
        "requirements.txt",
        owner_id="alice",
    )
    result = assess_inventory(conn, inventory["inventory_id"], owner_id="alice")
    assert result["findings"][0]["overall"] == "affected"
    assert result["findings"][1]["reason"] == "package_not_acquired"
    assert result["findings"][2]["reason"] == "unresolved_inventory_entry"
    conn.close()
