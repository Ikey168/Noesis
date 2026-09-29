"""The Chemicals and Substances bundle composes its provider with Legal, Products and platform providers (#2313)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.domains.pack_format import validate_manifest
from src.ingestion.source_packs import validate_source_pack
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.substances import SUBSTANCE_READS, SUBSTANCE_TOOLS, required_scopes

ROOT = Path(__file__).resolve().parents[3]
BOUND = {"chemicals.substances", "legal.core", "products.safety", "platform.entity-identity",
         "platform.subscriptions", "platform.source-runtime"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def test_pack_files_validate_and_reference_the_source_pack():
    pack = json.loads((ROOT / "packs/chemicals/pack.json").read_text())
    assert validate_manifest(pack) == []
    source_pack = validate_source_pack(json.loads((ROOT / pack["source_pack"]).read_text()))
    assert source_pack["pack_id"] == "chemicals-substances"
    assert {s["substances"]["provider"] for s in source_pack["sources"]} == {"pubchem", "echa-clp", "echa-reach",
                                                                             "comptox"}
    assert validate_composition_manifest(adapt_all()["chemicals"]) == []


def test_descriptor_declares_operations_with_the_catalog_scopes_stores_and_probe():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "chemicals.substances")
    assert validate_provider_descriptor(descriptor) == []
    tools = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert set(tools) <= SUBSTANCE_TOOLS
    for name, operation in tools.items():
        mutability = "read" if name in SUBSTANCE_READS else "write"
        assert operation["required_scopes"] == required_scopes(name, mutability), name
        assert (operation["side_effect"] == "read-only") == (name in SUBSTANCE_READS), name
    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    listed = {t["name"]: t for t in catalog["tools"] if t["name"] in tools}
    assert all(listed[n]["required_scopes"] == tools[n]["required_scopes"] for n in tools)
    owned = {s["store"] for s in descriptor["stores"]}
    others = {s["store"] for d in provider_descriptors() if d["id"] != "chemicals.substances" for s in d["stores"]}
    assert owned == {"src.kb.substances_store", "src.kb.substances_identity", "src.kb.substances_links"}
    assert not owned & others  # no parallel entity, legal, notice or subscription store
    assert descriptor["readiness_probes"] == [{"id": "revisions", "kind": "table-exists",
                                               "target": "substance_revisions"}]


def test_the_bundle_resolves_to_its_provider_plus_shared_providers():
    bundles = adapt_all()
    result = resolve([{"pack": "chemicals", "version": bundles["chemicals"]["version"]}], list(bundles.values()),
                     provider_descriptors())
    assert result.ok, result.failure
    bound = {b["provider"] for b in result.plan["bindings"] if "chemicals" in b["consumers"]}
    assert bound == BOUND
    assert {"pack_id": "chemicals-substances", "version": "1.0.0", "range": "^1.0.0"} in result.plan["source_packs"]


def test_disabling_chemicals_is_a_selection_change_that_keeps_legal_and_products():
    from src.kb import substances_bundle

    conn, coordinator, _, _ = _migrated()
    assert substances_bundle.is_enabled(conn, "global")
    result = substances_bundle.set_enabled(conn, "global", False, principal_id="operator", scopes={"operator"})
    assert result["authority"] == "composition-coordinator" and result["enabled"] is False
    assert result["receipt"]["status"] == "published"
    plan = coordinator.active()["plan"]
    assert "chemicals" not in {p["id"] for p in plan["packs"]}
    assert {"legal", "products"} <= {p["id"] for p in plan["packs"]}
    assert not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='chemicals_bundle_state'"
                            ).fetchone()
    again = substances_bundle.set_enabled(conn, "global", True, principal_id="operator", scopes={"operator"})
    assert again["enabled"] is True and again["receipt"]["status"] == "published"
