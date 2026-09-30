"""The Clinical Evidence bundle's optional ``surveillance`` feature and the ``clinical.surveillance`` provider (#2027)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    satisfies,
    seal_manifest,
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb import clinical_bundle
from src.kb.surveillance import NEVER_SENTENCE, feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.clinical import (
    SURVEILLANCE_SCOPES,
    SURVEILLANCE_TOOLS,
    SURVEILLANCE_WRITES,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "clinical-evidence", "version": "0.1.1", "range": "^0.1.1"}
CAPABILITIES = {
    "clinical.surveillance-series",
    "clinical.surveillance-vintages",
    "clinical.surveillance-monitoring",
}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def plan_for(features=None, bundles=None, descriptors=None):
    bundles = bundles or adapt_all()
    roots = [
        {
            "pack": "clinical-evidence",
            "version": bundles["clinical-evidence"]["version"],
        },
        {"pack": "science", "version": bundles["science"]["version"]},
    ]
    if features is not None:
        roots[0]["features"] = features
    result = resolve(
        roots,
        list(bundles.values()),
        descriptors if descriptors is not None else provider_descriptors(),
    )
    assert result.ok, result.failure
    return result.plan


def without_feature(bundles):
    """Today's Clinical Evidence manifest: the same bundle without the surveillance contributions."""
    manifest = copy.deepcopy(bundles["clinical-evidence"])
    body = {
        k: v
        for k, v in manifest.items()
        if k not in {"content_hash", "optional_features"}
    }
    contributes = body["contributes"]
    contributes["capabilities"] = [
        c for c in contributes["capabilities"] if c["id"] not in CAPABILITIES
    ]
    contributes["providers"] = [
        p for p in contributes["providers"] if p["id"] != "clinical.surveillance"
    ]
    contributes["profiles"] = [
        p for p in contributes["profiles"] if p["id"] != "clinical.surveillance-dossier"
    ]
    return {**bundles, "clinical-evidence": seal_manifest(body)}


def test_descriptor_declares_capabilities_operations_scopes_stores_probes_and_source_pack():
    descriptor = next(
        d for d in provider_descriptors() if d["id"] == "clinical.surveillance"
    )
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {
        "name": "src.kb.surveillance",
        "version": "1.0.0",
        "server": "noesis-knowledge-engine",
    }
    assert {c["id"] for c in descriptor["capabilities"]} == CAPABILITIES
    tools = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert set(tools) == SURVEILLANCE_TOOLS
    for name, operation in tools.items():
        assert operation["required_scopes"] == SURVEILLANCE_SCOPES[name]
        assert operation["side_effect"] == (
            "local-mutation" if name in SURVEILLANCE_WRITES else "read-only"
        )
    scopes = {s for o in descriptor["operations"] for s in o["required_scopes"]}
    assert {
        "knowledge:clinical:read",
        "knowledge:clinical:write",
        "knowledge:clinical:review",
        "knowledge:subscriptions:read",
        "knowledge:subscriptions:write",
        "knowledge:geospatial:read",
    } <= scopes
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "clinical.surveillance"
        for s in d["stores"]
    }
    assert not owned & others
    assert all(
        t.startswith("surveillance_") for s in descriptor["stores"] for t in s["tables"]
    )
    assert descriptor["source_packs"] == [PACK]
    manifest = json.loads(
        (ROOT / "config/source_packs/clinical-evidence.json").read_text()
    )
    assert manifest["version"] == "0.1.4"  # 0.1.2 medicines, 0.1.3 capacity, 0.1.4 devices (#2654); ^0.1.1 holds


def test_the_feature_and_the_dossier_profile_are_declared_off_by_default():
    manifest = json.loads((ROOT / "packs/clinical-evidence/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    feature = next(f for f in manifest["optional_features"] if f["id"] == "surveillance")
    assert feature["id"] == "surveillance" and feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == CAPABILITIES | {
        "geospatial.feature-query",
        "geospatial.place-resolution",
        "science.literature-claims",
    }
    profile = next(
        p
        for p in manifest["contributes"]["profiles"]
        if p["id"] == "clinical.surveillance-dossier"
    )
    assert {
        "reporting date",
        "reference date",
        "case definition",
        "kind",
        "vintage",
    } <= set(profile["vocabulary"])
    assert profile["workflow_defaults"]["never"] == NEVER_SENTENCE


def test_with_the_feature_off_the_bindings_equal_todays():
    bundles = adapt_all()
    today = plan_for(
        bundles=without_feature(bundles),
        descriptors=[
            d for d in provider_descriptors() if d["id"] != "clinical.surveillance"
        ],
    )
    off = plan_for(bundles=bundles)
    assert json.dumps(off["bindings"], sort_keys=True) == json.dumps(
        today["bindings"], sort_keys=True
    )
    assert off["source_packs"] == today["source_packs"]
    assert "clinical.surveillance" not in {b["provider"] for b in off["bindings"]}


def test_with_the_feature_on_the_provider_binds_with_one_authority_per_store():
    plan = plan_for(["surveillance"])
    bound = {
        b["provider"] for b in plan["bindings"] if "clinical-evidence" in b["consumers"]
    }
    assert {
        "clinical.surveillance",
        "clinical.core",
        "geospatial.core",
        "science.literature",
    } <= bound
    assert {
        b["capability"]
        for b in plan["bindings"]
        if b["provider"] == "clinical.surveillance"
    } == CAPABILITIES
    # The bundle keeps its ^0.1.0 pin, which the additive 0.1.1 source pack satisfies, as does the provider's ^0.1.1.
    installed = json.loads(
        (ROOT / "config/source_packs/clinical-evidence.json").read_text()
    )["version"]
    (pin,) = [
        ref for ref in plan["source_packs"] if ref["pack_id"] == "clinical-evidence"
    ]
    assert satisfies(installed, pin["range"]) and satisfies(installed, PACK["range"])
    stores = {}
    for descriptor in provider_descriptors():
        for store in descriptor["stores"]:
            stores.setdefault(store["record_type"], set()).add(descriptor["id"])
    assert all(len(owners) == 1 for owners in stores.values())
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert {f"noesis-knowledge-engine.{t}" for t in SURVEILLANCE_TOOLS} <= set(
        view.tools
    )
    off = CompositionView(plan_for([]), provider_descriptors(), adapt_all().values())
    assert not {f"noesis-knowledge-engine.{t}" for t in SURVEILLANCE_TOOLS} & set(
        off.tools
    )


def test_the_feature_consumes_the_shared_geospatial_and_science_providers_without_copying_them():
    plan = plan_for(["surveillance"])
    for capability, provider in (
        ("geospatial.place-resolution", "geospatial.core"),
        ("geospatial.feature-query", "geospatial.core"),
        ("science.literature-claims", "science.literature"),
    ):
        binding = next(b for b in plan["bindings"] if b["capability"] == capability)
        assert (
            binding["provider"] == provider
            and "clinical-evidence" in binding["consumers"]
        )
    descriptor = next(
        d for d in provider_descriptors() if d["id"] == "clinical.surveillance"
    )
    tables = {t for s in descriptor["stores"] for t in s["tables"]}
    assert not {
        t
        for t in tables
        if "geometr" in t or "feature" in t or "ontolog" in t or "subscription" in t
    }


def test_enabling_is_a_selection_change_with_a_receipt_and_disabling_clinical_keeps_science():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select(
        "clinical-evidence",
        bundles["clinical-evidence"]["version"],
        features=["surveillance"],
    )
    receipt = coordinator.activate("clinical-surveillance-on")
    assert receipt["status"] == "published" and feature_enabled(conn) is True
    plan = coordinator.active()["plan"]
    assert "clinical.surveillance" in {b["provider"] for b in plan["bindings"]}
    result = clinical_bundle.set_enabled(
        conn, "clinical", False, principal_id="operator", scopes={"operator"}
    )
    assert result["enabled"] is False and feature_enabled(conn) is False
    plan = coordinator.active()["plan"]
    assert "science" in {p["id"] for p in plan["packs"]}
    literature = next(
        b for b in plan["bindings"] if b["capability"] == "science.literature-claims"
    )
    assert literature["provider"] == "science.literature"
    assert {"clinical.surveillance", "geospatial.core"} <= {
        d["id"] for d in coordinator.installed("provider")
    }


def test_readiness_reports_each_decision_and_no_new_pack_directory_or_flag():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    decisions = {p: r["access_decision"] for p, r in report["providers"].items()}
    assert decisions["ecdc-atlas"] == decisions["rki-survstat"] == "not-implemented"
    assert not list(ROOT.glob("packs/*surveil*")) and not list(
        ROOT.glob("config/source_packs/*surveil*")
    )
    assert not [
        t for t in SURVEILLANCE_TOOLS if t.startswith("set_") and t.endswith("_enabled")
    ]
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert (
        "optional `surveillance` feature" in doc
        and "`noesis-surveillance-record-v1`" in doc
    )
