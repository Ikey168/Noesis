"""Clinical Evidence composition (H12): own provider plus shared Science/platform providers, coordinator enablement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.contracts import validate_composition_manifest, validate_provider_set
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb import clinical_bundle
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
SHARED = {"science.literature", "science.methodology", "science.systematic-reviews", "science.paper-families",
          "platform.source-runtime", "platform.subscriptions"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def test_clinical_manifest_resolves_to_its_own_provider_plus_shared_providers():
    manifest = json.loads((ROOT / "packs/clinical-evidence/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    assert validate_provider_set(provider_descriptors()) == []  # one owner per store
    _, coordinator, _, receipt = _migrated()
    assert receipt["status"] == "published"
    plan = coordinator.active()["plan"]
    clinical = [b for b in plan["bindings"] if "clinical-evidence" in b["consumers"]]
    assert {b["provider"] for b in clinical} == {"clinical.core"} | SHARED
    owned = {s["record_type"] for d in provider_descriptors() if d["id"] == "clinical.core" for s in d["stores"]}
    shared = {s["record_type"] for d in provider_descriptors() if d["id"] in SHARED for s in d["stores"]}
    assert owned == {"clinical-record", "clinical-evidence-map", "clinical-monitor"} and not owned & shared
    assert {"methodology-study", "review-protocol", "paper-family"} <= shared  # Science owners extended, not copied
    assert {"pack_id": "clinical-evidence", "version": "0.1.0", "range": "^0.1.0"} in plan["source_packs"]


def test_disabling_clinical_is_a_selection_change_that_keeps_shared_providers():
    conn, coordinator, _, _ = _migrated()
    assert clinical_bundle.is_enabled(conn, "clinical")
    status = clinical_bundle.readiness(conn, "clinical", scopes={"operator", "knowledge:clinical:read"})
    assert {o["provider"] for o in status["composition"]["operations"]} >= {"clinical.core", "science.methodology"}
    result = clinical_bundle.set_enabled(conn, "clinical", False, principal_id="operator", scopes={"operator"})
    assert result["authority"] == "composition-coordinator" and result["enabled"] is False
    assert result["receipt"]["status"] == "published"
    plan = coordinator.active()["plan"]
    assert "clinical-evidence" not in {p["id"] for p in plan["packs"]}
    installed = {d["id"] for d in coordinator.installed("provider")}
    assert SHARED | {"clinical.core"} <= installed  # nothing uninstalled
    with pytest.raises(clinical_bundle.BundleError) as caught:
        clinical_bundle.require_enabled(conn, "clinical")
    assert caught.value.code == "bundle_disabled"
    again = clinical_bundle.set_enabled(conn, "clinical", True, principal_id="operator", scopes={"operator"})
    assert again["enabled"] is True and again["receipt"]["status"] == "published"
    with pytest.raises(clinical_bundle.BundleError):
        clinical_bundle.set_enabled(conn, "clinical", False, principal_id="analyst", scopes={"knowledge:read"})


def test_science_keeps_working_when_clinical_is_disabled():
    conn, coordinator, _, _ = _migrated()
    clinical_bundle.set_enabled(conn, "clinical", False, principal_id="operator", scopes={"operator"})
    plan = coordinator.active()["plan"]
    assert "science" in {p["id"] for p in plan["packs"]}
    literature = next(b for b in plan["bindings"] if b["capability"] == "science.literature-claims")
    assert "science" in literature["consumers"] and literature["provider"] == "science.literature"
    from src.domains.research.paper_families import PaperFamilyStore
    from src.kb.methodology_provenance import MethodologyStore
    from src.kb.systematic_reviews import SystematicReviewStore

    scopes = {"knowledge:methodology:write", "knowledge:methodology:read"}
    study = MethodologyStore(conn).register_study("research", "doi:10.5555/s", "1", "Study", {"type": "cohort"}, {},
                                                  [], [], [], principal_id="p", scopes=scopes)
    assert MethodologyStore(conn).study("research", study["study_id"], scopes=scopes)["title"] == "Study"
    SystematicReviewStore(conn)
    PaperFamilyStore(conn)
    readiness = coordinator.readiness(scopes={"operator"})
    science_ops = [o for o in readiness["operations"] if o["provider"] == "science.literature"]
    assert science_ops


def test_the_source_pack_schedule_is_owned_through_composition():
    import duckdb

    from src.ingestion.source_pack_runtime import SourcePackRuntime, schedule_owners
    from src.ingestion.source_packs import SourcePackStore, validate_source_pack

    conn = duckdb.connect()
    manifest = validate_source_pack(json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text()))
    SourcePackStore(conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
    SourcePackRuntime(conn).set_schedule("clinical-evidence", {"kind": "interval", "interval_s": 86400},
                                         principal_id="operator")
    conn, coordinator, _, _ = _migrated(conn)
    claimed = coordinator.claim_source_schedules("clinical-evidence")
    assert "composition:clinical-evidence" in claimed["clinical-evidence"]
    released = coordinator.release_source_schedules("clinical-evidence")
    assert released[0]["schedule_removed"] is False  # the legacy owner keeps it
    assert schedule_owners(conn, "clinical-evidence") == ["legacy:operator"]
