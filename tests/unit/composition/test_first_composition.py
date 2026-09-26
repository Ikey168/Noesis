"""First composition proof: OSINT + Research + Geospatial (C08.1-C08.6).

Everything runs offline against the repository's real bundles, provider
descriptors, workflow templates and the captured Berlin WFS layers. Page
fixtures stand in for the *provider input* only: acquisition, projection,
spatial queries, corroboration, literature lookup and session artifacts run
through the real local code paths via the dispatcher.

What this proves: composition, binding, lifecycle and local execution
behavior. What it does not prove: live provider availability, and the quality
of any investigative or scholarly conclusion.

Tests run in file order against one module-scoped world, because the Berlin
replay is the slow part (tens of seconds) and later disturbances build on the
journeys.
"""

from __future__ import annotations

import copy
import json

import duckdb
import pytest

from src.composition import local_adapters
from src.composition.adapter import adapt_all, v1_view, workflow_templates
from src.composition.contracts import validate_provider_descriptor, validate_readiness, validate_workflow_template
from src.composition.lifecycle import CompositionCoordinator, SimulatedCrash as ActivationCrash
from src.composition.resolver import resolve
from src.composition.shadow import SHADOW_REPORT, provider_descriptors
from src.composition.workflows import (
    DispatchError,
    WorkflowBindings,
    WorkflowDispatcher,
    run_workflow,
)
from src.domains import pack_install
from src.domains import registry as domain_registry
from src.domains.pack_format import PackManifest
from src.ingestion.source_pack_upgrades import SourcePackUpgradeStore
from src.ingestion.source_packs import SourcePackError, SourcePackStore
from src.kb.geospatial_features import GeospatialFeatureStore
from src.kb.intake_modes import IntakeStore
from src.kb.research_recipes import ResearchRecipeStore
from tests.unit import geospatial_pack_helpers as berlin

DISTRICTS = "alkis_bezirke:bezirksgrenzen"
SCHOOLS = "schulen:schulen"
INTAKE_SCOPES = {"knowledge:intake:read", "knowledge:intake:write", "namespace:research:read",
                 "namespace:research:write"}
RECIPE_SCOPES = {"knowledge:recipes:write", "knowledge:recipes:execute", "knowledge:recipes:read"}
SCOPES = berlin.SCOPES | INTAKE_SCOPES | {"operator", "knowledge:read"}
TEMPLATES = {t["id"]: t for t in workflow_templates()}
OSINT_WF = TEMPLATES["osint.location-investigation"]
RESEARCH_WF = TEMPLATES["science.place-evidence"]


class Clock:
    def __init__(self):
        self.value = 1_790_000_000_000

    def __call__(self):
        self.value += 1
        return self.value


class ProcessDeath(BaseException):
    """The process dies mid-step: nothing in the dispatcher can catch or retry it."""


class World:
    """Shared state for the proof; attributes are filled in as the journeys run."""


def _handlers(world, *, fixture=True):
    return {
        local_adapters.SOURCE_TOOL: local_adapters.acquire_source(
            world.runtime, principal_id="operator",
            fixture_adapters=world.runtime.fixture_adapters(world.source["pack_id"], berlin.ROOT) if fixture else {},
            dns_resolver=berlin.PUBLIC_DNS),
        local_adapters.FEATURES_TOOL: local_adapters.features_within(world.features, principal_id="analyst",
                                                                     scopes=SCOPES),
        local_adapters.CORROBORATE_TOOL: local_adapters.corroboration(world.conn),
        local_adapters.LITERATURE_TOOL: local_adapters.literature(world.conn),
        local_adapters.SESSION_TOOL: local_adapters.session_artifact(world.intake, principal_id="alice",
                                                                     scopes=INTAKE_SCOPES),
    }


def _lookups(world):
    return {local_adapters.SOURCE_TOOL: local_adapters.source_run_receipt(world.conn, world.source["pack_id"]),
            local_adapters.SESSION_TOOL: local_adapters.session_command_receipt(world.conn)}


def _dispatcher(world, plan=None, **options):
    plan = plan or world.coordinator.active()["plan"]
    return WorkflowDispatcher(world.conn, plan, world.coordinator.installed("provider"),
                              options.pop("handlers", None) or _handlers(world), receipt_lookup=_lookups(world),
                              now=world.clock, **options)


def _record(session_id, artifact_id):
    def build(results, statuses):
        acquired = statuses.get("acquire")
        locate = results.get("locate") or {}
        coverage = {"acquisition": acquired or "not-in-template",
                    "missing_sources": [] if acquired in (None, "completed")
                    else ["geospatial-berlin:berlin-bezirksgrenzen", "geospatial-berlin:berlin-schulen"]}
        return {"namespace": "research", "session_id": session_id, "artifact_id": artifact_id,
                "artifact": {"references": locate.get("references", []),
                             "total_members": locate.get("total_members"),
                             "boundary": locate.get("boundary"),
                             "corroboration": (results.get("corroborate") or {}).get("corroboration"),
                             "literature": (results.get("literature") or {}).get("literature"),
                             "coverage": coverage}}
    return build


def _locate():
    return {"namespace": "global", "collection": SCHOOLS, "boundary_name": "Mitte",
            "boundary_collection": DISTRICTS, "limit": 5000}


def _osint_arguments(world, session_id, *, run_key="osint-journey"):
    return {"acquire": {"request": {"pack_id": world.source["pack_id"], "run_key": run_key, "operation": "features",
                                    "source_ids": ["berlin-bezirksgrenzen", "berlin-schulen"],
                                    "max_results": 5000, "max_bytes": 5_000_000, "timeout_ms": 120_000}},
            "locate": _locate(), "corroborate": {"claim_id": "claim:berlin-mitte-1"},
            "record": _record(session_id, "location-finding")}


def _research_arguments(session_id):
    return {"locate": _locate(), "literature": {"topic": "Berlin schools"},
            "record": _record(session_id, "place-evidence")}


def _session(world, key, mode="Deep Research"):
    return world.intake.create("research", mode, key, intent="Place evidence for Berlin Mitte",
                               principal_id="alice", scopes=INTAKE_SCOPES)["session_id"]


def _journey(world, template, arguments, run_key, parameters, dispatcher=None):
    return run_workflow(dispatcher or _dispatcher(world), world.recipes, world.bindings, template,
                        namespace="research", run_key=run_key, parameters=parameters, arguments=arguments,
                        principal="alice", scopes=SCOPES, recipe_scopes=RECIPE_SCOPES)


def _snapshot(conn):
    tables = ("source_pack_versions", "source_pack_current", "source_pack_checkpoints", "source_pack_watermarks",
              "geospatial_features", "geospatial_feature_revisions", "geospatial_feature_current",
              "intake_session_revisions")
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in tables}


@pytest.fixture(scope="module")
def world():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    w = World()
    w.conn = duckdb.connect(":memory:")
    w.clock = Clock()
    w.source, w.runtime = berlin.install(w.conn)
    w.features = GeospatialFeatureStore(w.conn)
    w.intake = IntakeStore(w.conn)
    w.recipes = ResearchRecipeStore(w.conn)
    w.bindings = WorkflowBindings(w.conn, now=w.clock)
    w.coordinator = CompositionCoordinator(w.conn, now=w.clock, legacy_config=lambda: ["news", "research"])
    w.bundles = adapt_all()
    for manifest in w.bundles.values():
        w.coordinator.install(manifest)
    for descriptor in provider_descriptors():
        w.coordinator.install(descriptor)
    for bundle in ("osint", "science", "geospatial"):
        w.coordinator.cutover(bundle)

    # OSINT first: its optional acquisition projects the Berlin layers once.
    w.coordinator.select("osint", "^1.0.0")
    w.osint_receipt = w.coordinator.activate("activate-osint")
    w.osint_plan = w.coordinator.active()["plan"]
    w.osint_session = _session(w, "osint-location")
    w.osint = _journey(w, OSINT_WF, _osint_arguments(w, w.osint_session), "osint-journey",
                       {"boundary_name": "Mitte", "claim_id": "claim:berlin-mitte-1"})
    w.runs_after_osint = w.conn.execute("SELECT COUNT(*) FROM source_pack_runs").fetchone()[0]

    # Research second: same spatial binding, no re-ingestion.
    w.coordinator.select("science", "^1.0.0")
    w.science_receipt = w.coordinator.activate("activate-science")
    w.shared_plan = w.coordinator.active()["plan"]
    w.research_session = _session(w, "research-place")
    w.research = _journey(w, RESEARCH_WF, _research_arguments(w.research_session), "research-journey",
                          {"boundary_name": "Mitte", "topic": "Berlin schools"})
    w.coordinator.pin_run(w.research["workflow_run_id"])
    w.pre_disturbance = {"osint": copy.deepcopy(w.osint), "research": copy.deepcopy(w.research)}
    yield w
    w.conn.close()
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


# ------------------------------------------------------------ C08.1 provider


def test_geospatial_descriptor_validates_registers_and_is_explained(world):
    descriptor = next(d for d in provider_descriptors() if d["id"] == "geospatial.core")
    assert validate_provider_descriptor(descriptor) == []  # tools checked against the generated catalog
    assert {op["side_effect"] for op in descriptor["operations"]} == {"read-only", "local-mutation"}
    assert all(op["readiness_probe"] for op in descriptor["operations"])
    assert descriptor["source_packs"][0]["pack_id"] == world.source["pack_id"]
    stores = {s["record_type"]: s["store"] for s in descriptor["stores"]}
    assert stores["feature"] == "src.kb.geospatial_features" and stores["place"] == "src.kb.geospatial"
    relation = next(c for c in descriptor["capabilities"] if c["id"] == "geospatial.spatial-relation")
    assert relation["semantic_constraints"]["distance_units"] == "meters"
    explained = world.coordinator.view().explain()
    feature = next(c for c in explained["capabilities"] if c["capability"] == "geospatial.feature-query")
    assert feature["provider"] == "geospatial.core" and feature["reason"] == "explicit"


def test_probes_report_local_ready_with_the_fixture_store_loaded(world):
    readiness = world.coordinator.readiness(scopes=SCOPES)
    assert validate_readiness(readiness) == []
    within = next(o for o in readiness["operations"] if o["operation"] == "features-within")
    assert within["state"] == "ready" and within["blockers"] == []


# ------------------------------------------------------------ C08.2 templates


def test_both_templates_validate_consume_the_same_capability_and_declare_no_store():
    for template in (OSINT_WF, RESEARCH_WF):
        assert validate_workflow_template(template) == []
        assert "stores" not in template and all("store" not in step for step in template["steps"])
        assert {s["effect"] for s in template["steps"]} <= {"read-only", "local-mutation", "acquisition"}
    assert {s["effect"] for s in RESEARCH_WF["steps"]} <= {"read-only", "local-mutation"}
    spatial = {r["capability"] for r in OSINT_WF["requires"]} & {r["capability"] for r in RESEARCH_WF["requires"]}
    assert "geospatial.feature-query" in spatial
    for bundle in ("osint", "science"):
        profile = json.loads((berlin.ROOT / f"packs/{bundle}/composition.json").read_text())["contributes"]["profiles"]
        assert set(profile[0]) <= {"id", "description", "vocabulary", "workflow_defaults", "source_packs"}


# ------------------------------------------------------------ C08.3 shared binding


def test_one_geospatial_binding_consumed_by_both_roots(world):
    bindings = [b for b in world.shared_plan["bindings"] if b["provider"] == "geospatial.core"]
    assert len(bindings) == 1 and bindings[0]["consumers"] == ["osint", "science"]
    assert [p["id"] for p in world.shared_plan["providers"]].count("geospatial.core") == 1
    owners = [s for d in world.coordinator.installed("provider") for s in d["stores"] if s["record_type"] == "feature"]
    assert len(owners) == 1


def test_enabling_research_triggers_no_reingestion_and_identities_match(world):
    assert world.conn.execute("SELECT COUNT(*) FROM source_pack_runs").fetchone()[0] == world.runs_after_osint == 1
    osint_refs = world.osint["recipe_run"]["outputs"]["locate"]["references"]
    research_refs = world.research["recipe_run"]["outputs"]["locate"]["references"]
    assert osint_refs and osint_refs == research_refs
    assert {r["owner_capability"] for r in osint_refs} == {"geospatial.feature-query"}
    assert all(r["revision_id"] and r["namespace"] and r["record_kind"] == "feature" for r in osint_refs)


def test_required_provider_missing_or_ambiguous_blocks_before_execution(world):
    without = [d for d in provider_descriptors() if d["id"] != "geospatial.core"]
    missing = resolve([{"pack": "osint", "range": "^1.0.0"}], list(world.bundles.values()), without)
    assert missing.failure.code == "missing_contract"
    assert "geospatial.feature-query" in missing.failure.message and "osint" in missing.failure.message
    test_a_second_semantically_different_provider_is_an_ambiguity(world)


def test_contract_or_ontology_conflict_rejects_the_composition(world):
    projected = copy.deepcopy(next(d for d in provider_descriptors() if d["id"] == "geospatial.core"))
    projected["capabilities"][0]["semantic_constraints"]["crs"] = "EPSG:3857"
    others = [d for d in provider_descriptors() if d["id"] != "geospatial.core"]
    conflict = resolve([{"pack": "osint", "range": "^1.0.0"}], list(world.bundles.values()), others + [projected])
    assert conflict.failure.code == "semantic_mismatch" and conflict.failure.details["constraint"] == "crs"
    squatter = copy.deepcopy(next(d for d in provider_descriptors() if d["id"] == "osint.core"))
    squatter["stores"] = [{"record_type": "feature", "store": "src.osint.features", "tables": ["osint_features"]}]
    owners = resolve([{"pack": "osint", "range": "^1.0.0"}], list(world.bundles.values()),
                     [d for d in provider_descriptors() if d["id"] != "osint.core"] + [squatter])
    assert owners.failure.code == "conflicting_store_owner"  # no silent overwrite of the feature store


def test_a_second_semantically_different_provider_is_an_ambiguity(world):
    rival = copy.deepcopy(next(d for d in provider_descriptors() if d["id"] == "geospatial.core"))
    rival["id"] = "geospatial.topology"
    rival["capabilities"][0]["semantic_constraints"]["containment"] = "shapely covers; boundary edges excluded"
    rival["stores"] = []
    bundles = copy.deepcopy(world.bundles)
    for capability in bundles["geospatial"]["contributes"]["capabilities"]:
        capability.pop("provider", None)  # no configured choice
    result = resolve([{"pack": "osint", "range": "^1.0.0"}], list(bundles.values()),
                     provider_descriptors() + [rival])
    assert result.failure.code == "ambiguous_binding"
    assert result.failure.details["candidates"] == ["geospatial.core", "geospatial.topology"]


# ------------------------------------------------------------ C08.4 journeys


def test_both_journeys_complete_with_owner_receipts_through_the_dispatcher(world):
    for name, session in (("osint", world.osint_session), ("research", world.research_session)):
        journey = world.pre_disturbance[name]
        receipt = journey["recipe_run"]
        assert journey["status"] == "completed" and receipt["actions_executed"] is True
        assert receipt["execution_mode"] == "composition-dispatch" and receipt["dispatch"]["steps"]
        assert receipt["outputs"]["locate"]["total_members"] == 86
        assert receipt["outputs"]["record"]["receipt"]["owner"] == "intake-session"
        assert world.bindings.binding("recipe_run", receipt["run_id"])["workflow_id"] in TEMPLATES
        state = world.intake.inspect("research", session, principal_id="alice", scopes=INTAKE_SCOPES)
        assert state["data"]["artifacts"]
    acquired = world.pre_disturbance["osint"]["recipe_run"]["outputs"]["acquire"]
    assert acquired["receipt"]["owner"] == "source-pack-runtime"
    assert acquired["provider_input"] == "fixture" and acquired["tool_execution"] == "real"


def test_optional_acquisition_unavailable_runs_local_analysis_with_missing_source_coverage(world):
    store = SourcePackStore(world.conn, initialize=False)
    store.set_enabled(world.source["pack_id"], False, principal_id="operator")
    try:
        session = _session(world, "osint-offline")
        offline = _journey(world, OSINT_WF, _osint_arguments(world, session, run_key="osint-offline"),
                           "osint-offline", {"boundary_name": "Mitte", "claim_id": "claim:berlin-mitte-1"})
    finally:
        store.set_enabled(world.source["pack_id"], True, principal_id="operator")
    assert offline["status"] == "completed" and offline["steps"]["acquire"] == "omitted"
    receipt = offline["recipe_run"]
    assert receipt["outputs"]["locate"]["total_members"] == 86
    assert [o["step_id"] for o in receipt["omissions"]] == ["acquire"]
    artifact = world.intake.inspect("research", session, principal_id="alice", scopes=INTAKE_SCOPES)
    coverage = artifact["data"]["artifacts"]["location-finding"]["coverage"]
    assert coverage["acquisition"] == "omitted" and coverage["missing_sources"]


# ------------------------------------------------------------ C08.5 disturbances


def test_disabling_osint_keeps_research_spatial_operations_and_evidence(world):
    world.coordinator.disable("osint", "disable-osint")
    plan = world.coordinator.active()["plan"]
    geo = next(b for b in plan["bindings"] if b["capability"] == "geospatial.feature-query")
    assert geo["consumers"] == ["science"] and "osint" not in {p["id"] for p in plan["packs"]}
    rerun = _journey(world, RESEARCH_WF, _research_arguments(world.research_session), "research-after-disable",
                     {"boundary_name": "Mitte", "topic": "Berlin schools"})
    assert rerun["status"] == "completed" and rerun["recipe_run"]["outputs"]["locate"]["total_members"] == 86
    kept = world.intake.inspect("research", world.osint_session, principal_id="alice", scopes=INTAKE_SCOPES)
    refs = kept["data"]["artifacts"]["location-finding"]["references"]
    assert refs and all(world.features.feature("global", r["feature_id"], scopes=SCOPES) for r in refs[:3])


def test_source_revision_preview_blocks_incompatible_and_keeps_historical_pins(world):
    upgrade = SourcePackUpgradeStore(world.conn)
    current = SourcePackStore(world.conn, initialize=False).status(world.source["pack_id"])
    major = copy.deepcopy(berlin.raw_manifest())
    major["version"] = "2.0.0"
    preview = upgrade.preview_impact(major, principal_id="operator", scopes={"operator"})
    dependents = [e for e in preview["effects"] if e["kind"] == "composition"]
    assert {e["role"] for e in dependents} >= {"active", "pinned-run"}
    active = next(e for e in dependents if e["role"] == "active")
    assert active["candidate_compatible"] is False and active["id"] == world.coordinator.active()["plan_digest"]
    with pytest.raises(SourcePackError) as blocked:
        upgrade.apply(major, preview_hash=preview["preview"]["preview_hash"], impact_hash=preview["impact_hash"],
                      apply_key="berlin-major", principal_id="operator", scopes={"operator"},
                      dns_resolver=berlin.PUBLIC_DNS)
    assert blocked.value.code == "composition_incompatible" and active["id"] in str(blocked.value)
    assert SourcePackStore(world.conn, initialize=False).status(world.source["pack_id"])["version"] == current["version"]
    reader = upgrade.preview_impact(major, principal_id="analyst", scopes={"knowledge:read"})
    assert not [e for e in reader["effects"] if e["kind"] == "composition"]
    pinned = world.bindings.binding("workflow_run", world.research["workflow_run_id"])
    assert pinned["plan_digest"] == world.shared_plan["digest"]


def test_provider_revision_requires_a_new_plan_while_history_keeps_its_pin(world):
    revised = copy.deepcopy(next(d for d in provider_descriptors() if d["id"] == "geospatial.core"))
    revised["version"] = "1.1.0"
    revised["capabilities"][0]["contract"]["version"] = "1.1.0"
    world.coordinator.install(revised)
    preview = world.coordinator.preview(upgrade={"geospatial.core"})
    assert preview["affected_consumers"] == ["science"]
    world.coordinator.activate("provider-1.1.0", upgrade={"geospatial.core"})
    active = world.coordinator.active()["plan"]
    assert next(p for p in active["providers"] if p["id"] == "geospatial.core")["version"] == "1.1.0"
    history = world.bindings.binding("recipe_run", world.research["recipe_run"]["run_id"])
    assert history["plan_digest"] == world.shared_plan["digest"] != active["digest"]
    replay = world.bindings.resume("recipe_run", world.research["recipe_run"]["run_id"],
                                   world.coordinator.installed("pack"), world.coordinator.installed("provider"))
    assert replay["status"] == "current"  # the pinned 1.0.0 descriptor is retained, so history replays


def test_access_revoked_after_preflight_is_rechecked_everywhere(world):
    dispatcher = _dispatcher(world, plan=world.shared_plan)
    run_id = world.research["workflow_run_id"]
    allowed = {"value": True}

    def authorize(action, principal, step):
        return allowed["value"]

    preflight = world.bindings.preflight(world.coordinator.view(), RESEARCH_WF, conn=world.conn, scopes=SCOPES)
    assert all(o["state"] == "ready" for o in preflight["operations"] if o["operation"] == "features-within")
    assert dispatcher.read(run_id, principal="alice", scopes=SCOPES, authorize=authorize)["steps"]
    allowed["value"] = False
    step = next(s for s in RESEARCH_WF["steps"] if s["id"] == "locate")
    calls = [lambda: dispatcher.execute("revoked-run", step, _locate(), principal="alice", scopes=SCOPES,
                                        authorize=authorize),
             lambda: dispatcher.resume(run_id, RESEARCH_WF, _research_arguments(world.research_session),
                                       principal="alice", scopes=SCOPES, authorize=authorize),
             lambda: dispatcher.read(run_id, principal="alice", scopes=SCOPES, authorize=authorize),
             lambda: dispatcher.export(run_id, principal="alice", scopes=SCOPES, authorize=authorize)]
    for call in calls:
        with pytest.raises(DispatchError) as denied:
            call()
        assert denied.value.code == "unauthorized"


def test_crash_during_activation_keeps_the_previous_generation(world):
    before = world.coordinator.active()["id"]
    world.coordinator.select("osint", "^1.0.0")
    with pytest.raises(ActivationCrash):
        world.coordinator.activate("reenable-osint", crash_after="verified")
    restarted = CompositionCoordinator(world.conn, now=world.clock, legacy_config=lambda: ["news", "research"])
    summary = restarted.reconcile()
    assert summary["active_generation"] == before
    world.coordinator.deselect("osint")


def test_crash_after_a_workflow_mutation_reconciles_and_resumes_under_the_original_digest(world):
    plan = world.shared_plan  # the digest the interrupted run started under
    session = _session(world, "crash-after-mutation", mode="Exploration")
    handlers = _handlers(world)
    real_record = handlers[local_adapters.SESSION_TOOL]
    crash = {"armed": True}

    def record_then_crash(arguments, context):
        result = real_record(arguments, context)
        if crash["armed"]:
            crash["armed"] = False
            raise ProcessDeath("record")
        return result

    dispatcher = _dispatcher(world, plan=plan, handlers={**handlers, local_adapters.SESSION_TOOL: record_then_crash})
    run_id = "workflow-run:crash-after-mutation"
    world.bindings.bind("workflow_run", run_id, "research", RESEARCH_WF, plan)
    with pytest.raises(ProcessDeath):
        dispatcher.run_steps(run_id, RESEARCH_WF, _research_arguments(session), principal="alice", scopes=SCOPES)
    revisions = world.conn.execute("SELECT COUNT(*) FROM intake_session_revisions WHERE session_id=?",
                                   [session]).fetchone()[0]
    resumed = _dispatcher(world, plan=world.bindings.plan(world.bindings.binding("workflow_run", run_id)["plan_digest"]))
    outcome = resumed.resume(run_id, RESEARCH_WF, _research_arguments(session), principal="alice", scopes=SCOPES)
    assert outcome["steps"] == {"locate": "completed", "literature": "completed", "record": "completed"}
    assert outcome["results"]["record"]["_dispatch"]["reconciled"]
    assert world.conn.execute("SELECT COUNT(*) FROM intake_session_revisions WHERE session_id=?",
                              [session]).fetchone()[0] == revisions  # no second effect
    assert world.bindings.binding("workflow_run", run_id)["plan_digest"] == plan["digest"]
    assert world.coordinator.active()["plan"]["digest"] != plan["digest"]


def test_pre_disturbance_receipts_and_references_remain_valid(world):
    for name in ("osint", "research"):
        receipt = world.pre_disturbance[name]["recipe_run"]
        stored = world.recipes.status("research", receipt["run_id"], scopes=RECIPE_SCOPES)
        assert stored["receipt"]["receipt_hash"] == receipt["receipt_hash"]
        for ref in receipt["outputs"]["locate"]["references"][:3]:
            assert world.conn.execute("SELECT 1 FROM geospatial_feature_revisions WHERE revision_id=?",
                                      [ref["revision_id"]]).fetchone()


# ------------------------------------------------------------ C08.6 migration parity


def test_shadow_diff_for_the_three_bundles_is_fully_annotated():
    report = json.loads(SHADOW_REPORT.read_text())
    assert {"geospatial", "osint", "science"} <= set(report["disagreements"])
    for bundle in ("geospatial", "osint", "science"):
        for item in report["disagreements"][bundle]:
            assert item["annotation"] != "unreviewed", item
            assert item["annotation"].split(":")[0] in {"resolved", "intended"}


def test_after_cutover_legacy_calls_never_diverge(world):
    expected = world.coordinator._active_names()
    for name in ("osint", "research", "geospatial"):
        for call in (domain_registry.enable_pack, domain_registry.disable_pack):
            try:
                call(name)
            except domain_registry.CompositionAuthorityError:
                pass
            managed = {n for n in domain_registry._ENABLED if world.coordinator.manages(n)}
            assert managed == expected, (call.__name__, name)
        with pytest.raises(pack_install.PackInstallError):
            pack_install.install_manifest(PackManifest.from_dict(json.loads(
                (berlin.ROOT / f"packs/{'science' if name == 'research' else name}/pack.json").read_text())))


def test_rollback_and_recutover_leave_sources_cursors_and_evidence_identical(world):
    before = _snapshot(world.conn)
    for bundle in ("osint", "science", "geospatial"):
        result = world.coordinator.rollback_to_legacy(bundle, f"rollback-{bundle}")
        assert result["authority"] == "legacy"
    assert domain_registry.is_pack_enabled("research") or "research" in domain_registry._ENABLED
    assert "geospatial" not in domain_registry._ENABLED  # legacy config does not enable it
    domain_registry.enable_pack("geospatial")  # the legacy path is the authority again
    domain_registry.disable_pack("geospatial")
    assert _snapshot(world.conn) == before
    for bundle in ("osint", "science", "geospatial"):
        world.coordinator.cutover(bundle)
    world.coordinator.reconcile()
    assert _snapshot(world.conn) == before
    assert {"research", "geospatial"} <= domain_registry._ENABLED


def test_legacy_manifests_keep_identity_and_public_behavior_through_the_adapter(world):
    for bundle in ("osint", "science", "geospatial"):
        data = json.loads((berlin.ROOT / f"packs/{bundle}/pack.json").read_text())
        v1 = PackManifest.from_dict(data)
        view = v1_view(world.bundles[bundle])
        assert view["capabilities"][: len(v1.capabilities)] == v1.capabilities  # overlay adds no v1 names
        assert set(view["capabilities"]) - set(v1.capabilities) <= set(
            getattr(next((p for p in adapter_code_packs() if p.name in world.bundles[bundle]["adapter"]["legacy_names"]),
                         None), "capabilities", []))
        assert {k: view["schema_versions"][k] for k in v1.schema_versions} == v1.schema_versions
        assert data["name"] in world.bundles[bundle]["adapter"]["legacy_names"]
    # Sessions and reports made before the disturbances still resolve to the same records.
    osint = world.intake.inspect("research", world.osint_session, principal_id="alice", scopes=INTAKE_SCOPES)
    assert osint["session_id"] == world.osint_session and osint["data"]["artifacts"]["location-finding"]


def adapter_code_packs():
    from src.composition.adapter import code_packs

    return code_packs()
