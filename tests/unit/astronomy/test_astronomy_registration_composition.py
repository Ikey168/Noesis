"""The space-object registration provider in the Astronomy pack: optional features, tools and scopes (#2224, SO12)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.readiness import CompositionView
from src.composition.shadow import provider_descriptors
from src.kb.astronomy_registration import feature_enabled
from tests.unit.astronomy.test_astronomy_composition import CORE, ROOT, bound, isolated_registry, plan_for  # noqa: F401
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp import astronomy_registration as tools_module
from tools.knowledge_engine_mcp.astronomy_registration import REGISTRATION_SCOPES, REGISTRATION_TOOLS

REGISTRATION = "astronomy-space-object-registration"
DISCOS = "astronomy-discos"


def exposed(plan):
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    return {t.split(".", 1)[1] for t in view.tools if t.startswith("noesis-knowledge-engine.")} & REGISTRATION_TOOLS


def test_descriptor_owns_the_registration_store_and_pins_the_astronomy_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "astronomy.space-object-registration")
    assert [s["store"] for s in descriptor["stores"]] == ["src.kb.astronomy_registration"]
    assert descriptor["source_packs"][0]["pack_id"] == "astronomy-and-space"
    pack = json.loads((ROOT / "config/source_packs/astronomy.json").read_text())
    assert {s["connector"] for s in pack["sources"] if s.get("astronomy_registration")} == {"astronomy-registration"}
    for operation in descriptor["operations"]:
        tool = operation["tool"].split(".", 1)[1]
        assert tool in REGISTRATION_TOOLS
        assert operation["required_scopes"] == REGISTRATION_SCOPES.get(tool, ["knowledge:astronomy:read"])


def test_registration_features_are_off_by_default_and_hide_their_tools():
    plan = plan_for()
    assert REGISTRATION not in plan["features"]["astronomy"] and exposed(plan) == set()
    assert "astronomy.space-object-registration" not in bound(plan)


def test_registration_feature_binds_its_provider_satcat_objects_and_ownership_identity():
    plan = plan_for([REGISTRATION])
    providers = bound(plan)
    assert {"astronomy.space-object-registration", "astronomy.launches", "ownership.core"} <= providers
    tools = exposed(plan)
    assert {"object_registration_as_of", "reentry_record", "create_space_registration_monitor"} <= tools
    assert "discos_access_status" not in tools
    with_discos = plan_for([REGISTRATION, DISCOS])
    assert "discos_access_status" in exposed(with_discos)


def test_catalog_scopes_and_mutability():
    generated = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    catalog = {t["name"]: t for t in generated["tools"] if t["name"] in REGISTRATION_TOOLS}
    assert set(catalog) == REGISTRATION_TOOLS
    assert catalog["match_space_registration_parties"]["mutability"] == "write"
    assert catalog["object_registration_as_of"]["mutability"] == "read"
    for name, tool in catalog.items():
        assert tool["required_scopes"] == REGISTRATION_SCOPES.get(
            name, ["knowledge:astronomy:write" if tool["mutability"] == "write" else "knowledge:astronomy:read"])


def test_discos_is_disabled_without_an_account_and_status_reports_live_verification(monkeypatch):
    monkeypatch.delenv(tools_module.DISCOS_SECRET, raising=False)
    status = tools_module.bundle_status(duckdb.connect(":memory:"))
    discos = next(s for s in status["sources"] if s["provider"] == "esa-discos")
    assert discos["state"] == "disabled: no account configured"
    assert all(s["live_verification"]["status"] in {"unverified-live", "not-implemented", "link-only"}
               for s in status["sources"])
    assert status["features"] == {REGISTRATION: False, DISCOS: False}
    monkeypatch.setenv(tools_module.DISCOS_SECRET, "configured")
    discos = next(s for s in tools_module.bundle_status(duckdb.connect(":memory:"))["sources"]
                  if s["provider"] == "esa-discos")
    assert discos["state"] == "disabled: the astronomy-discos feature is off"


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn, REGISTRATION) is False
    coordinator.select("astronomy", bundles["astronomy"]["version"], features=[REGISTRATION])
    assert coordinator.activate("registration-on")["status"] == "published"
    assert feature_enabled(conn, REGISTRATION) is True and feature_enabled(conn, DISCOS) is False
    coordinator.select("astronomy", bundles["astronomy"]["version"], features=[])
    coordinator.activate("registration-off")
    assert feature_enabled(conn, REGISTRATION) is False


def test_unknown_feature_is_refused():
    with pytest.raises(Exception):
        feature_enabled(duckdb.connect(":memory:"), "astronomy-launches")
