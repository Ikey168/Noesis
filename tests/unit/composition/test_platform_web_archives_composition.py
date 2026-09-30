"""The platform.web-archives provider and the Osint bundle's optional web-archive features (#2330, WA13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.ingestion.wayback import save_page_now_enabled
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def osint_plan(features=None):
    bundles = adapt_all()
    root = {"pack": "osint", "version": bundles["osint"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "osint" in b["consumers"]}


def test_descriptor_validates_and_declares_read_operations_and_the_scoped_write():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "platform.web-archives")
    assert validate_provider_descriptor(descriptor) == []
    path = ROOT / "packs/platform/providers/platform.web-archives.json"
    assert path.exists() and not (ROOT / "packs/platform/manifest.json").exists()
    capabilities = {c["id"]: c for c in descriptor["capabilities"]}
    assert set(capabilities) == {"platform.web-archives", "platform.web-archive-capture"}
    operations = {op["id"]: op for op in descriptor["operations"]}
    for read in ("resolve", "list-captures", "as-of"):
        assert read in capabilities["platform.web-archives"]["operations"]
    assert operations["as-of"]["side_effect"] == "read-only"
    assert operations["list-captures"]["side_effect"] == "read-only"
    spn = operations["save-page-now"]
    assert spn["side_effect"] == "external-publication"
    assert spn["required_scopes"] == ["knowledge:citation:archive-request"]
    constraints = capabilities["platform.web-archive-capture"]["semantic_constraints"]
    assert constraints["optional_feature"] == "save-page-now" and constraints["default_enabled"] is False
    # Readiness probes target the existing citation preservation store; no new store is declared.
    (store,) = descriptor["stores"]
    assert store["store"] == "src.kb.citation_preservation" and "citation_snapshots" in store["tables"]
    assert {p["target"] for p in descriptor["readiness_probes"]} == {"citation_snapshots", "web_archive_captures"}
    assert capabilities["platform.web-archives"]["semantic_constraints"]["no_new_store"] is True


def test_osint_features_are_off_by_default_and_bind_the_platform_provider_when_selected():
    composition = json.loads((ROOT / "packs/osint/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert features["web-archives"]["default"] is False and features["save-page-now"]["default"] is False
    assert validate_composition_manifest(adapt_all()["osint"]) == []
    default = osint_plan()
    assert "platform.web-archives" not in bound(default)
    assert {"web-archives", "save-page-now"} <= {o["feature"] for o in default["omissions"] if o["pack"] == "osint"}
    selected = osint_plan(["web-archives"])
    assert "platform.web-archives" in bound(selected)
    assert "platform.web-archive-capture" not in {b["capability"] for b in selected["bindings"]}
    spn = osint_plan(["save-page-now"])
    assert {"platform.web-archives", "platform.web-archive-capture"} <= {b["capability"] for b in spn["bindings"]}


def test_save_page_now_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert save_page_now_enabled(conn) is False
    coordinator.select("osint", bundles["osint"]["version"], features=["web-archives"])
    assert coordinator.activate("osint-web-archives")["status"] == "published"
    assert save_page_now_enabled(conn) is False
    coordinator.select("osint", bundles["osint"]["version"], features=["web-archives", "save-page-now"])
    assert coordinator.activate("osint-save-page-now")["status"] == "published"
    assert save_page_now_enabled(conn) is True
