"""OSINT + Research + Geospatial: the first composition, proven (C08, #1833-#1839).

Everything here uses the shipped manifests (``packs/*/pack.json`` plus
``composition.json``), the shipped provider descriptors and the real local
adapters. Provider input is fixture data: synthetic WFS pages served through
the source-pack runtime's real adapter. No tool execution is mocked. Live
provider availability and the quality of investigative conclusions are outside
what these tests establish.

Each acceptance-matrix row in docs/architecture/pack-workflow-composition.md
maps to exactly one test marked ``composition_acceptance``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.composition import contracts as c
from src.composition import deployment
from src.composition import lifecycle as lc
from src.composition.readiness import catalog_state
from src.composition.resolver import resolve
from src.composition.sources import source_pins
from src.composition.workflows import WorkflowStore, adapt_investigation_template, templates_of, validate_template
from src.domains import pack_install
from src.domains import registry as domain_registry
from src.domains.pack_format import PackManifest
from src.ingestion.source_pack_upgrades import SourcePackUpgradeStore
from src.ingestion.source_packs import SourcePackError, validate_source_pack
from src.kb.research_recipes import RecipeError, ResearchRecipeStore
from tests.unit.composition.journey import NAMESPACE, World
from tests.unit.geospatial_pack_helpers import PUBLIC_DNS

ROOT = Path(__file__).resolve().parents[3]
BUNDLES = ("geospatial", "osint", "science")
OSINT, SCIENCE = "osint@1.0.0", "science@1.2.0"
# Geospatial consumes its own transit capability (C09.3), so it is a consumer too.
GEOSPATIAL = "geospatial@1.0.0"
acceptance = pytest.mark.composition_acceptance


@pytest.fixture(autouse=True)
def isolated():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY,
             dict(pack_install._INSTALLED), dict(pack_install._TEMPLATES))
    lc.reset_runtime()
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])
    pack_install._INSTALLED.clear()
    pack_install._INSTALLED.update(saved[3])
    pack_install._TEMPLATES.clear()
    pack_install._TEMPLATES.update(saved[4])
    lc.reset_runtime()


def shipped(name: str) -> dict:
    known = c.provided_capabilities(c.load_providers())
    return c.load_pack(ROOT / "packs" / name, known_capabilities=known)


class Shipped(World):
    """The world with the three shipped bundles installed."""

    def __init__(self, roots=("osint", "science")):
        super().__init__()
        for name in BUNDLES:
            self.coordinator.store.install_manifest(shipped(name), principal_id="operator")
        for root in roots:
            self.select(root)
        if roots:
            assert self.coordinator.activate("gen-1", principal_id="operator")["status"] == "applied"

    def select(self, root):
        self.coordinator.store.select(root, shipped(root)["version"], principal_id="operator")

    def template(self, bundle):
        manifest = next(m for m in self.coordinator.store.manifests() if m["name"] == bundle)
        (template,) = templates_of(manifest)
        return template

    def run(self, bundle, consumer, session, *, run_key="run", place="Mitte", dispatcher=None, network="disabled"):
        dispatcher = dispatcher or self.dispatcher()
        return dispatcher.start(self.template(bundle), namespace=NAMESPACE, consumer=consumer,
                                parameters={"place": place}, run_key=run_key, principal_id="analyst",
                                session_id=session["session_id"], network=network)

    def snapshot(self):
        tables = ("source_pack_versions", "source_pack_current", "source_pack_checkpoints",
                  "source_pack_watermarks", "geospatial_feature_revisions", "geospatial_features",
                  "document_revision_records")
        return {t: sorted(map(repr, self.conn.execute(f"SELECT * FROM {t}").fetchall())) for t in tables}


# --------------------------------------------------------------------------- #
# C08.1 and C08.2: the provider and the two templates
# --------------------------------------------------------------------------- #

def test_geospatial_provider_is_registered_explained_and_locally_ready():
    world = Shipped()
    geo = next(p for p in world.coordinator.store.providers() if p["provider_id"] == "noesis.geospatial")
    tools = {b["id"].split(".", 1)[1] for cap in geo["capabilities"] for b in cap["bindings"] if b["kind"] == "mcp-tool"}
    assert {"calculate_spatial_relation", "store_geospatial_geometry", "record_geospatial_resolution",
            "revise_geospatial_place", "simplify_geospatial_geometry", "review_geospatial_resolution"} <= tools
    for capability in geo["capabilities"]:
        assert capability["effect"] in c.EFFECTS and capability["readiness"]["probe"]
        assert {"crs"} & set(capability["semantics"]) or "resolution" in capability["semantics"] or (
            "decision" in capability["semantics"] or "concurrency" in capability["semantics"])
    assert {s["record_kind"] for s in geo["stores"]} >= {"place", "geometry", "geospatial_feature"}
    assert geo["source_packs"][0]["pack_id"] == "geospatial-berlin"
    world.run("osint", OSINT, world.session("load"))  # fixture store loaded through acquisition
    readiness = world.coordinator.readiness(principal_id="analyst", scopes=world.grants["analyst"])
    spatial = [o for o in readiness["operations"] if o["provider_id"] == "noesis.geospatial"]
    assert spatial and {o["state"] for o in spatial} == {"ready"}
    from src.composition.readiness import explain

    explained = explain(world.coordinator.store.active_plan(), world.coordinator.store.providers(), readiness)
    item = next(i for i in explained["capabilities"] if i["capability"] == "spatial.points-within-boundary")
    assert item["provider_id"] == "noesis.geospatial" and item["consuming_packs"] == ["osint", "science"]


def test_both_templates_validate_alongside_investigation_templates():
    osint, science = shipped("osint"), shipped("science")
    for manifest in (osint, science):
        (template,) = templates_of(manifest)
        validate_template(template, manifest=manifest)
        assert "stores" not in json.dumps(template["steps"])
    (location,) = templates_of(osint)
    assert {s["effect"] for s in location["steps"]} == {"acquisition", "read-only", "local-mutation"}
    (place,) = templates_of(science)
    assert {s["effect"] for s in place["steps"]} <= {"read-only", "local-mutation"}
    legacy = adapt_investigation_template(
        {"name": "Location", "description": "Pinned", "parameters": {}, "questions": ["Where?"],
         "success_criteria": ["Found"], "scope": {"domains": [], "namespaces": []},
         "source_packs": location["source_packs"], "report_outline": ["Findings"]},
        template_id="investigation.location", version="1.0.0", owner_pack="osint")
    assert legacy["source_packs"] == location["source_packs"]
    for manifest in (osint, science):
        assert all(p.get("vocabulary") is not None for p in manifest["contributes"]["profiles"])
        assert all(set(p) <= {"profile_id", "description", "source_packs", "vocabulary",
                              "query_defaults", "workflow_templates"} for p in manifest["contributes"]["profiles"])


# --------------------------------------------------------------------------- #
# Acceptance matrix
# --------------------------------------------------------------------------- #

@acceptance
def test_two_packs_consume_geospatial_through_one_provider():
    """Row: Two packs consume Geospatial."""

    world = Shipped(roots=("osint",))
    osint_run = world.run("osint", OSINT, world.session("osint"))
    runs_before = world.conn.execute("SELECT count(*) FROM source_pack_runs").fetchone()[0]
    revisions_before = world.conn.execute("SELECT count(*) FROM geospatial_feature_revisions").fetchone()[0]
    world.select("science")
    world.coordinator.activate("gen-2", principal_id="operator")
    plan = world.coordinator.store.active_plan()
    spatial = {(b["consumer"], b["provider_id"], b["provider_version"]) for b in plan["bindings"]
               if b["capability"] == "spatial.points-within-boundary"}
    assert spatial == {(OSINT, "noesis.geospatial", "1.0.0"), (SCIENCE, "noesis.geospatial", "1.0.0")}
    assert [p["provider_id"] for p in plan["providers"]].count("noesis.geospatial") == 1
    science_run = world.run("science", SCIENCE, world.session("science"))
    assert world.conn.execute("SELECT count(*) FROM source_pack_runs").fetchone()[0] == runs_before
    assert world.conn.execute("SELECT count(*) FROM geospatial_feature_revisions").fetchone()[0] == revisions_before
    members = lambda run: {(m["feature_id"], m["revision_id"], m["document_id"])  # noqa: E731
                           for m in run["outputs"]["within"]["members"]}
    assert members(osint_run) == members(science_run) and len(members(osint_run)) == 2
    refs = lambda run: {(r["owner_capability"], r["record_kind"], r["record_id"], r["namespace"], r["revision"])  # noqa: E731
                        for r in run["outputs"]["evidence"]["references"]}
    assert refs(osint_run) == refs(science_run)


@acceptance
def test_osint_disabled_while_research_stays_active():
    """Row: OSINT disabled, Research active."""

    world = Shipped()
    session = world.session("before-disable")
    osint_run = world.run("osint", OSINT, session)
    world.coordinator.disable("osint", "disable-osint", principal_id="operator")
    assert ("noesis.geospatial", "1.0.0") in lc.runtime().providers
    science_run = world.run("science", SCIENCE, world.session("after-disable"))
    assert science_run["status"] == "completed" and science_run["outputs"]["within"]["total_members"] == 2
    view = world.dispatcher().read(osint_run["dispatch_run_id"], principal_id="analyst")
    assert all("redacted" not in r for step in view["steps"] for r in step["references"])
    state = world.intake.inspect(NAMESPACE, session["session_id"], principal_id="analyst",
                                 scopes=world.grants["analyst"])
    for ref in state["references"]:
        assert world.conn.execute(
            "SELECT 1 FROM document_revision_records WHERE document_id=? AND revision=?",
            [ref["id"], ref["version"]]).fetchone()


@acceptance
def test_missing_or_ambiguous_provider_blocks_before_execution():
    """Row: Required provider missing or ambiguous."""

    candidate = shipped("osint")
    providers = c.load_providers()
    rival = copy.deepcopy(next(p for p in providers if p["provider_id"] == "noesis.geospatial"))
    rival.pop("descriptor_hash")
    rival["provider_id"] = "example.alternate-spatial"
    rival["stores"] = []
    rival["source_packs"] = []
    for capability in rival["capabilities"]:
        capability["record_kinds"] = []
        capability["effect"] = "read-only" if capability["effect"] == "local-mutation" else capability["effect"]
    rival = c.validate_provider(rival)
    result = resolve(roots=[{"name": "osint", "range": "1.0.0"}], manifests=[candidate, shipped("geospatial")],
                     providers=[*providers, rival], contracts=c.contract_candidates(include_domain_packs=False))
    assert result["status"] == "ambiguous"
    names = {tuple(a["providers"]) for a in result["ambiguities"]}
    assert ("example.alternate-spatial@1.0.0", "noesis.geospatial@1.0.0") in names
    world = Shipped(roots=("osint",))
    world.coordinator.shutdown_provider("noesis.geospatial", "down", reason="maintenance", principal_id="operator")
    with pytest.raises(c.CompositionError) as blocked:
        world.run("osint", OSINT, world.session("blocked"))
    assert blocked.value.code == "preflight_blocked"
    assert "provider-shutdown" in blocked.value.details["blocking"][0]["kinds"]
    assert world.conn.execute("SELECT count(*) FROM composition_step_receipts").fetchone()[0] == 0


@acceptance
def test_optional_acquisition_unavailable_runs_local_analysis_with_missing_coverage():
    """Row: Optional acquisition unavailable."""

    world = Shipped(roots=("osint",))
    world.run("osint", OSINT, world.session("seed"))
    offline = world.dispatcher(inputs={"source_adapters": {}})
    run = world.run("osint", OSINT, world.session("offline"), run_key="offline", dispatcher=offline)
    assert run["status"] == "completed"
    assert "acquire" in {o["step_id"] for o in run["omissions"]}
    coverage = run["outputs"]["artifact"]["coverage"]
    assert coverage["acquisition"] == "unavailable" and coverage["missing_sources"] == ["geospatial-berlin"]
    assert run["outputs"]["within"]["total_members"] == 2


@acceptance
def test_contract_or_ontology_conflict_rejects_the_composition():
    """Row: Contract/ontology conflict."""

    world = Shipped()
    before = world.coordinator.store.active_generation()["generation"]
    planar = copy.deepcopy(next(p for p in world.coordinator.store.providers() if p["provider_id"] == "noesis.geospatial"))
    planar.pop("descriptor_hash")
    planar["version"] = "1.1.0"
    for capability in planar["capabilities"]:
        capability["version"] = "1.1.0"
        if capability["capability"] == "spatial.points-within-boundary":
            capability["semantics"]["containment"] = "planar-bounding-box"
    world.coordinator.store.install_provider(planar, principal_id="operator")
    world.conn.execute("DELETE FROM composition_providers WHERE provider_id='noesis.geospatial' AND version='1.0.0'")
    receipt = world.coordinator.activate("conflict", principal_id="operator", retain=False)
    assert receipt["status"] == "failed" and receipt["error"]["code"] == "incompatible_provider"
    assert "containment" in receipt["error"]["message"]
    assert world.coordinator.store.active_generation()["generation"] == before
    conflicting = shipped("osint")
    conflicting.pop("manifest_hash")
    conflicting["version"] = "1.0.1"
    conflicting.setdefault("references", {})["contracts"] = [{"name": "geospatial-geometry", "range": "^3.0.0"}]
    conflicting.pop("legacy_v1", None)
    with pytest.raises(c.CompositionError) as caught:
        resolve(roots=[{"name": "osint", "range": "1.0.1"}], manifests=[c.validate_manifest(conflicting), shipped("geospatial")],
                providers=c.load_providers(), contracts=c.contract_candidates(include_domain_packs=False))
    assert caught.value.code == "incompatible_range"


@acceptance
def test_upgrade_of_a_pinned_source_or_provider_keeps_history_and_needs_a_new_plan():
    """Row: Upgrade changes a pinned provider/source."""

    world = Shipped()
    run = world.run("osint", OSINT, world.session("history"))
    old_plan = world.coordinator.store.active_plan()
    upgrade = SourcePackUpgradeStore(world.conn)

    def bumped(version):
        value = copy.deepcopy(world.source_pack)
        value.pop("manifest_hash", None)
        value["version"] = version
        return validate_source_pack(value)

    args = lambda preview, key: {"preview_hash": preview["preview"]["preview_hash"],  # noqa: E731
                                 "impact_hash": preview["impact_hash"], "apply_key": key,
                                 "principal_id": "operator", "scopes": {"operator"},
                                 "dns_resolver": PUBLIC_DNS, "secret_available": lambda _: True}
    major = bumped("2.0.0")
    preview = upgrade.preview_impact(major, principal_id="operator", scopes={"operator"})
    dependents = [e for e in preview["effects"] if e["kind"] == "composition-plan"]
    assert {d["id"] for d in dependents} >= {old_plan["digest"]} and dependents[0]["consumers"]
    with pytest.raises(SourcePackError) as blocked:
        upgrade.apply(major, **args(preview, "major"))
    assert blocked.value.code == "composition_incompatible"
    minor = bumped("1.2.0")
    preview = upgrade.preview_impact(minor, principal_id="operator", scopes={"operator"})
    upgrade.apply(minor, accepted_license_sources=[s["source_id"] for s in minor["sources"]], **args(preview, "minor"))
    assert world.coordinator.store.plan(old_plan["digest"]) == old_plan
    assert WorkflowStore(world.conn).run_binding(run["dispatch_run_id"])["plan_digest"] == old_plan["digest"]
    resumed = world.dispatcher().resume(run["dispatch_run_id"], principal_id="analyst")
    assert resumed["status"] == "new-plan-required"
    world.coordinator.activate("after-upgrade", principal_id="operator")
    assert world.coordinator.store.active_plan()["source_packs"][0]["version"] == "1.2.0"
    revised = copy.deepcopy(next(p for p in world.coordinator.store.providers() if p["provider_id"] == "noesis.geospatial"))
    revised.pop("descriptor_hash")
    revised["version"] = "1.1.0"
    for capability in revised["capabilities"]:
        capability["version"] = "1.1.0"
    world.coordinator.store.install_provider(revised, principal_id="operator")
    # geospatial@1.0.0 contributes only noesis.geospatial@1.0.0, so rebinding
    # to 1.1.0 drops it (and its transit binding) from the closure.
    assert world.coordinator.preview(retain=False)["affected_consumers"] == [GEOSPATIAL, OSINT, SCIENCE]


@acceptance
def test_crash_during_activation_keeps_the_previous_generation():
    """Row: Crash during activation."""

    world = Shipped(roots=("osint",))
    world.select("science")

    def crash(stage):
        if stage == "verified":
            raise lc.Crash(stage)

    with pytest.raises(lc.Crash):
        lc.Coordinator(world.conn, contracts=world.coordinator.contracts(), fault=crash).activate(
            "crashing", principal_id="operator")
    lc.reset_runtime()
    report = lc.Coordinator(world.conn, contracts=world.coordinator.contracts()).reconcile()
    assert report["generation"] == 1 and report["abandoned"]
    assert {b["consumer"] for b in lc.runtime().bindings()} == {OSINT, GEOSPATIAL}
    run = world.run("osint", OSINT, world.session("after-crash"))
    assert run["status"] == "completed"


@acceptance
def test_crash_after_a_workflow_mutation_reconciles_from_the_owner_receipt(monkeypatch):
    """Row: Crash after a workflow mutation."""

    world = Shipped(roots=("osint",))
    session = world.session("crash")
    real = WorkflowStore.record_step

    def crash(self, run_id, step_id, **kwargs):
        if step_id == "artifact" and kwargs["status"] == "completed":
            monkeypatch.setattr(WorkflowStore, "record_step", real)
            raise lc.Crash("died after the session write")
        return real(self, run_id, step_id, **kwargs)

    monkeypatch.setattr(WorkflowStore, "record_step", crash)
    with pytest.raises(lc.Crash):
        world.run("osint", OSINT, session)
    written = world.intake.inspect(NAMESPACE, session["session_id"], principal_id="analyst",
                                   scopes=world.grants["analyst"])["revision"]
    run_id = world.conn.execute("SELECT run_id FROM composition_run_bindings").fetchone()[0]
    resumed = world.dispatcher().resume(run_id, principal_id="analyst")
    assert resumed["status"] == "completed" and resumed["outputs"]["artifact"]["adopted"] is True
    assert world.intake.inspect(NAMESPACE, session["session_id"], principal_id="analyst",
                                scopes=world.grants["analyst"])["revision"] == written


@acceptance
def test_access_revoked_after_preflight_is_rechecked_everywhere():
    """Row: Access revoked after preflight."""

    from src.composition import bindings as registry

    world = Shipped(roots=("science",))
    world.run("osint", OSINT, world.session("seed")) if False else None
    world.select("osint")
    world.coordinator.activate("gen-2", principal_id="operator")
    world.run("osint", OSINT, world.session("seed"))
    original = registry._BINDINGS["research.place-literature"]

    def revoke(ctx, arguments):
        world.grants["analyst"].discard("knowledge:geospatial:calculate")
        world.grants["analyst"].discard("knowledge:recipes:execute")
        return original.fn(ctx, arguments)

    registry._BINDINGS["research.place-literature"] = registry.RegisteredBinding(**{**original.__dict__, "fn": revoke})
    try:
        with pytest.raises(RecipeError) as denied:
            world.run("science", SCIENCE, world.session("revoked"))
        assert denied.value.code in {"authority_revoked", "unauthorized"}
    finally:
        registry._BINDINGS["research.place-literature"] = original
    run_id = world.conn.execute(
        "SELECT run_id FROM composition_run_bindings WHERE consumer=?", [SCIENCE]).fetchone()[0]
    dispatcher = world.dispatcher()
    with pytest.raises(c.CompositionError):
        dispatcher.resume(run_id, principal_id="analyst")
    world.grants["analyst"].discard("knowledge:recipes:read")
    with pytest.raises(c.CompositionError):
        dispatcher.read(run_id, principal_id="analyst")
    with pytest.raises(c.CompositionError):
        dispatcher.export(run_id, principal_id="analyst")


@acceptance
def test_several_consumers_on_one_account_share_its_limit():
    """Row: Several consumers acquire from one account."""

    world = Shipped(roots=("osint",))
    account = world.runtime.default_account("geospatial-berlin")
    first = world.run("osint", OSINT, world.session("a"))
    assert first["outputs"]["acquire"]["shared"]["joined"] is False
    used = sum(s["counts"]["pages"] for s in first["outputs"]["acquire"]["receipt"]["sources"])
    world.runtime.set_account_limit(account, window_ms=10**12, max_pages=used, max_bytes=10**9)
    joined = world.run("osint", OSINT, world.session("a2"), run_key="a2")
    assert joined["outputs"]["acquire"]["shared"]["joined"] is True  # joins never consume quota twice
    other_namespace = world.dispatcher()
    world.grants["analyst"] |= {"namespace:team:read", "namespace:team:write"}
    session = world.intake.create("team", "Deep Research", "b", intent="second consumer",
                                  principal_id="analyst", scopes=world.grants["analyst"])
    second = other_namespace.start(world.template("osint"), namespace="team", consumer=OSINT,
                                   parameters={"place": "Mitte"}, run_key="b", principal_id="analyst",
                                   session_id=session["session_id"])
    assert "acquire" in {o["step_id"] for o in second["omissions"]}  # the aggregate limit held
    assert world.runtime.account_state(account)["used_pages"] == used
    readiness = world.coordinator.readiness(principal_id="analyst", scopes=world.grants["analyst"])
    assert "aggregate-limit-exhausted" in {b["kind"] for o in readiness["operations"] for b in o["blockers"]}


@acceptance
def test_legacy_manifest_session_and_report_keep_identity_through_the_adapter():
    """Row: Legacy manifest/session/report."""

    from src.kb.authored_reports import AuthoredReportStore
    from tests.unit.kb.test_intake_creation import SCOPES, _report

    for name in BUNDLES:
        legacy = PackManifest.from_dict(json.loads((ROOT / "packs" / name / "pack.json").read_text()))
        assert c.v1_view(shipped(name)) == legacy.to_dict()
    world = World()
    session = world.session("legacy")
    report = AuthoredReportStore(world.conn).create("research", "legacy-report", _report(),
                                                   principal_id="alice", scopes=SCOPES)
    before_session = world.intake.inspect(NAMESPACE, session["session_id"], principal_id="analyst",
                                          scopes=world.grants["analyst"], revision=1)
    before_report = AuthoredReportStore(world.conn).inspect("research", report["report_id"],
                                                            principal_id="alice", scopes=SCOPES)
    for name in BUNDLES:
        world.coordinator.store.install_manifest(shipped(name), principal_id="operator")
    for root in ("osint", "science"):
        world.coordinator.store.select(root, shipped(root)["version"], principal_id="operator")
    world.coordinator.activate("gen-1", principal_id="operator")
    for name in BUNDLES:
        world.coordinator.cutover(name, f"cutover-{name}", principal_id="operator")
    assert world.intake.inspect(NAMESPACE, session["session_id"], principal_id="analyst",
                                scopes=world.grants["analyst"], revision=1) == before_session
    assert AuthoredReportStore(world.conn).inspect("research", report["report_id"], principal_id="alice",
                                                   scopes=SCOPES) == before_report
    assert domain_registry.get_pack("osint").capabilities == json.loads(
        (ROOT / "packs/osint/pack.json").read_text())["capabilities"]


@acceptance
def test_fixture_only_public_recipe_run_declares_fixture_execution():
    """Row: Fixture-only public recipe run."""

    world = World()
    recipes = ResearchRecipeStore(world.conn)
    recipe = recipes.register({
        "recipe_id": "public", "version": "1", "namespace": NAMESPACE, "inputs": {},
        "steps": [{"id": "s", "tool": "noesis-osint.corroborate", "input_schema": {"a": 1}, "output_schema": {"a": 1}}],
        "outputs": [], "compatibility": {}}, principal_id="p", scopes={"knowledge:recipes:write"})
    kwargs = dict(run_key="k", adapters={"s": lambda step, state: {"fixture": True}}, principal_id="p",
                  scopes={"knowledge:recipes:execute"}, execution_mode="caller-supplied-fixture")
    receipt = recipes.run(NAMESPACE, recipe["recipe_revision_id"], {}, actions_executed=False, **kwargs)
    assert receipt["execution_mode"] == "caller-supplied-fixture" and receipt["actions_executed"] is False
    with pytest.raises(RecipeError):
        recipes.run(NAMESPACE, recipe["recipe_revision_id"], {}, actions_executed=True, **kwargs)


# --------------------------------------------------------------------------- #
# C08.4 journeys and C08.6 migration parity
# --------------------------------------------------------------------------- #

def test_both_journeys_complete_with_owner_receipts_through_the_dispatcher():
    world = Shipped()
    osint_session, science_session = world.session("osint"), world.session("science")
    osint_run = world.run("osint", OSINT, osint_session)
    science_run = world.run("science", SCIENCE, science_session)
    for run, session in ((osint_run, osint_session), (science_run, science_session)):
        assert run["status"] == "completed" and run["actions_executed"] is True
        assert run["execution_mode"] == "composition-dispatch"
        binding = WorkflowStore(world.conn).run_binding(run["dispatch_run_id"])
        assert binding["session_id"] == session["session_id"] and binding["recipe_run_id"] == run["run_id"]
        assert run["outputs"]["within"]["receipt"]["receipt_id"]
        state = world.intake.inspect(NAMESPACE, session["session_id"], principal_id="analyst",
                                     scopes=world.grants["analyst"])
        assert state["revision"] == run["outputs"]["artifact"]["revision"]
    assert osint_run["outputs"]["acquire"]["receipt"]["status"] == "complete"
    assert osint_run["outputs"]["acquire"]["provider_input"] == "injected-transport"


def test_migration_parity_shadow_cutover_rollback_and_recutover():
    report = deployment.shadow_report()
    assert report == json.loads(deployment.SHADOW_REPORT.read_text())
    assert deployment.unannotated(report, deployment.load_annotations()) == []
    assert {"osint", "science"} <= set(report["bundles"])
    world = Shipped()
    world.run("osint", OSINT, world.session("parity"))
    before = world.snapshot()
    for name in BUNDLES:
        world.coordinator.cutover(name, f"cutover-{name}", principal_id="operator")
        assert domain_registry.is_pack_enabled(name)
        with pytest.raises(domain_registry.CompatibilityError):
            pack_install.uninstall(name)
    domain_registry.disable_pack("osint")
    assert "osint" not in world.coordinator.store.selection() and not domain_registry.is_pack_enabled("osint")
    domain_registry.enable_pack("osint")
    assert "osint" in world.coordinator.store.selection() and domain_registry.is_pack_enabled("osint")
    for name in BUNDLES:
        assert domain_registry.is_pack_enabled(name) == (
            name in {m["name"] for m in world.coordinator.store.active_plan()["manifests"]})
    for name in BUNDLES:
        world.coordinator.rollback(name, f"rollback-{name}", principal_id="operator")
        assert pack_install.installed_packs()[name] == shipped(name)["version"]
    assert world.snapshot() == before
    for name in BUNDLES:
        world.coordinator.cutover(name, f"recutover-{name}", principal_id="operator")
    assert world.snapshot() == before
    assert source_pins(world.conn)["geospatial-berlin"]["version"] == world.source_pack["version"]
    assert catalog_state({"state": "ready", "blockers": []}) == ("available", None)
