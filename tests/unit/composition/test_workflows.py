"""Workflow templates, plan-bound runs, dispatcher, reconciliation and local adapters (C07)."""

from __future__ import annotations

import copy
import json

import duckdb
import pytest

from src.composition import local_adapters
from src.composition.contracts import CompositionContractError, validate_readiness
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.workflows import (
    DispatchError,
    SimulatedCrash,
    WorkflowBindings,
    WorkflowDispatcher,
    from_investigation_template,
    require_workflow,
    run_workflow,
    seal_workflow,
)
from src.ingestion.source_pack_runtime import FixturePageAdapter, SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore
from src.kb.geospatial import GeospatialStore
from src.kb.intake_modes import IntakeStore
from src.kb.investigation_templates import InvestigationTemplateStore
from src.kb.research_recipes import RecipeError, ResearchRecipeStore
from tests.unit import composition_corpus as corpus
from tests.unit.ingestion.test_source_pack_upgrades import pack as research_source_pack

GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate"}
INTAKE_SCOPES = {"knowledge:intake:read", "knowledge:intake:write", "namespace:research:read",
                 "namespace:research:write"}
RECIPE_SCOPES = {"knowledge:recipes:write", "knowledge:recipes:execute", "knowledge:recipes:read"}
SCOPES = GEO_SCOPES | INTAKE_SCOPES | {"operator", "knowledge:read"}


def _descriptor(provider_id, capability, contract, operations, probes):
    body = {"contract": "noesis-provider-descriptor-v1", "id": provider_id, "version": "1.0.0",
            "implementation": {"name": provider_id, "version": "1.0.0", "server": "noesis-knowledge-engine"},
            "capabilities": [{"id": capability, "contract": {"name": contract, "version": "1.0.0"},
                              "operations": [op["id"] for op in operations], "semantic_constraints": {}}],
            "operations": operations, "stores": [], "readiness_probes": probes}
    return body


def descriptors():
    geo = corpus.spatial_provider(version="1.1.0")
    geo["capabilities"].append({"id": "geospatial.evidence-geolocation",
                                "contract": {"name": "noesis-evidence-geolocation", "version": "1.0.0"},
                                "operations": ["geolocate-evidence"], "semantic_constraints": {}})
    geo["operations"].append({"id": "geolocate-evidence", "tool": "noesis-osint.image_provenance",
                              "side_effect": "read-only", "idempotency": {"supported": True},
                              "readiness_probe": "geometries", "required_scopes": ["knowledge:geospatial:read"],
                              "gates": ["osint-review"]})
    sessions = _descriptor("intake.sessions", "intake.session-artifacts", "noesis-intake-artifact", [
        {"id": "record-artifact", "tool": local_adapters.SESSION_TOOL, "side_effect": "local-mutation",
         "idempotency": {"supported": True, "key": "command_key"}, "readiness_probe": "sessions",
         "required_scopes": ["knowledge:intake:write"]}],
        [{"id": "sessions", "kind": "table-exists", "target": "intake_sessions"}])
    sources = _descriptor("sources.research", "sources.scholarly-acquisition", "noesis-source-acquisition", [
        {"id": "acquire", "tool": local_adapters.SOURCE_TOOL, "side_effect": "acquisition",
         "idempotency": {"supported": False, "receipt": "source_pack_runs"}, "readiness_probe": "runs",
         "required_scopes": ["operator"], "required_context": ["namespace"]}],
        [{"id": "runs", "kind": "table-exists", "target": "source_pack_runs"}])
    return [geo, sessions, sources]


REQUIRES = [corpus.SPATIAL,
            corpus.req("geospatial.evidence-geolocation", "noesis-evidence-geolocation"),
            corpus.req("intake.session-artifacts", "noesis-intake-artifact"),
            corpus.req("sources.scholarly-acquisition", "noesis-source-acquisition")]


def plan():
    science = corpus.pack("science", requires=REQUIRES, aliases={"research": "science"})
    result = resolve([{"pack": "science", "range": "^1.0.0"}], [science], descriptors())
    assert result.ok, result.failure
    return result.plan, science


def workflow(**overrides):
    body = {"contract": "noesis-workflow-template-v1", "id": "science.place-evidence", "version": "1.0.0",
            "description": "Place evidence for a research question", "pack": "science",
            "parameters": {"topic": {"type": "string", "required": True}},
            "requires": REQUIRES,
            "steps": [
                {"id": "acquire", "capability": "sources.scholarly-acquisition", "operation": "acquire",
                 "effect": "acquisition"},
                {"id": "locate", "capability": "geospatial.spatial-relation",
                 "operation": "calculate-spatial-relation", "effect": "read-only", "depends_on": ["acquire"]},
                {"id": "artifact", "capability": "intake.session-artifacts", "operation": "record-artifact",
                 "effect": "local-mutation", "depends_on": ["locate"]},
            ],
            "artifacts": [{"id": "place-evidence", "kind": "session-artifact"}]}
    body.update(overrides)
    return seal_workflow(body)


class Calls:
    def __init__(self):
        self.count = {}

    def handler(self, tool, result=None, *, effect=None, fail_times=0):
        def run(arguments, context):
            self.count[tool] = self.count.get(tool, 0) + 1
            if self.count[tool] <= fail_times:
                raise RuntimeError("transient owner failure")
            if effect:
                context.record_effect(effect)
            return dict(result or {"ok": True, "arguments": arguments})
        return run


def _dispatcher(conn, handlers=None, **options):
    p, _ = plan()
    calls = Calls()
    default = {local_adapters.SOURCE_TOOL: calls.handler(local_adapters.SOURCE_TOOL, effect="acquisition"),
               corpus.GEOSPATIAL_TOOLS[0]: calls.handler(corpus.GEOSPATIAL_TOOLS[0], effect="read-only"),
               local_adapters.SESSION_TOOL: calls.handler(local_adapters.SESSION_TOOL, effect="local-mutation"),
               "noesis-osint.image_provenance": calls.handler("noesis-osint.image_provenance")}
    return WorkflowDispatcher(conn, p, descriptors(), {**default, **(handlers or {})}, **options), calls


# ------------------------------------------------------------ C07.1


def test_workflow_template_validates_and_rejects_undeclared_capabilities():
    assert require_workflow(workflow())
    bad = workflow(steps=[{"id": "x", "capability": "legal.citation-lookup", "operation": "lookup",
                           "effect": "read-only"}])
    with pytest.raises(CompositionContractError) as caught:
        require_workflow(bad)
    assert caught.value.code == "undeclared_capability"
    cyclic = workflow(steps=[{**workflow()["steps"][0], "depends_on": ["locate"]}, workflow()["steps"][1]])
    with pytest.raises(CompositionContractError) as cycle:
        require_workflow(cyclic)
    assert cycle.value.code == "cycle"


def test_investigation_templates_adapt_without_losing_source_pack_pins():
    conn = duckdb.connect(":memory:")
    SourcePackStore(conn).install(json.loads((corpus.ROOT / "config/source_packs/political.json").read_text()),
                                  principal_id="alice")
    from tests.unit.kb.test_investigation_templates import AUTH, DEFINITION

    store = InvestigationTemplateStore(conn)
    definitions = [DEFINITION, {**DEFINITION, "name": "Second", "questions": ["Who decided on ${topic}?"]}]
    for index, definition in enumerate(definitions):
        created = store.create("r", f"template-{index}", definition, **AUTH)
        state = store.inspect("r", created["template_id"], **AUTH)
        adapted = from_investigation_template(state)
        assert adapted["source_packs"] == definition["source_packs"]
        assert adapted["questions"] == definition["questions"]
        assert adapted["artifacts"][0]["sections"] == definition["report_outline"]
        assert adapted["adapter"]["template_id"] == created["template_id"]
        with pytest.raises(CompositionContractError):
            from_investigation_template(state, steps=[{"id": "s", "capability": "geospatial.spatial-relation",
                                                       "operation": "calculate-spatial-relation",
                                                       "effect": "read-only"}])
        declared = from_investigation_template(state, requires=[corpus.SPATIAL], steps=[
            {"id": "s", "capability": "geospatial.spatial-relation", "operation": "calculate-spatial-relation",
             "effect": "read-only"}])
        assert declared["requires"][0]["capability"] == "geospatial.spatial-relation"


def test_intake_sessions_store_the_template_reference_alongside_existing_state():
    conn = duckdb.connect(":memory:")
    intake = IntakeStore(conn)
    session = intake.create("research", "Deep Research", "place-study", intent="Where did it happen?",
                            principal_id="alice", scopes=INTAKE_SCOPES)
    stored = "SELECT revision, content_json FROM intake_sessions WHERE session_id=?"
    before = conn.execute(stored, [session["session_id"]]).fetchone()
    p, _ = plan()
    bindings = WorkflowBindings(conn)
    bound = bindings.bind("intake_session", session["session_id"], "research", workflow(), p,
                          mode="Deep Research", profile="science.default")
    assert bound["workflow_id"] == "science.place-evidence" and bound["plan_digest"] == p["digest"]
    assert bound["mode"] == "Deep Research" and bound["profile"] == "science.default"
    assert conn.execute(stored, [session["session_id"]]).fetchone() == before


# ------------------------------------------------------------ C07.2


def test_run_binding_records_digest_preflight_and_resume_requires_new_plan():
    conn = duckdb.connect(":memory:")
    p, science = plan()
    bindings = WorkflowBindings(conn, now=lambda: 5)
    view = CompositionView(p, descriptors(), [science])
    preflight = bindings.preflight(view, workflow(), conn=conn, scopes=SCOPES, now_ms=lambda: 42)
    assert validate_readiness(preflight) == []
    assert preflight["observed_at_ms"] == 42 and preflight["plan_digest"] == p["digest"]
    assert {o["operation"] for o in preflight["operations"]} == {"acquire", "calculate-spatial-relation",
                                                                  "record-artifact"}
    bound = bindings.bind("recipe_run", "recipe-run:1", "research", workflow(), p, preflight=preflight)
    assert bound["resolver_version"] == p["resolver_version"] and bound["preflight"]["observed_at_ms"] == 42
    assert bindings.resume("recipe_run", "recipe-run:1", [science], descriptors())["status"] == "current"
    changed = descriptors()
    changed[0]["capabilities"][0]["semantic_constraints"]["distance_units"] = "kilometers"
    resumed = bindings.resume("recipe_run", "recipe-run:1", [science], changed)
    assert resumed["status"] == "new_plan_required" and resumed["plan_digest"] == p["digest"]
    assert bindings.binding("recipe_run", "recipe-run:1")["plan_digest"] == p["digest"]  # not rebound
    with pytest.raises(DispatchError) as rebind:
        bindings.bind("recipe_run", "recipe-run:1", "research", workflow(), {**p, "digest": "x"})
    assert rebind.value.code == "invalid_plan"


# ------------------------------------------------------------ C07.3


def test_revoked_access_is_rechecked_on_execute_resume_read_and_export():
    conn = duckdb.connect(":memory:")
    dispatcher, _ = _dispatcher(conn)
    allowed = {"value": True}

    def authorize(action, principal, step):
        return allowed["value"]

    wf = workflow()
    dispatcher.run_steps("run-1", wf, {}, principal="alice", scopes=SCOPES, authorize=authorize)
    assert dispatcher.read("run-1", principal="alice", scopes=SCOPES, authorize=authorize)["steps"]
    allowed["value"] = False
    for call in (lambda: dispatcher.execute("run-2", wf["steps"][0], {}, principal="alice", scopes=SCOPES,
                                            authorize=authorize),
                 lambda: dispatcher.resume("run-1", wf, {}, principal="alice", scopes=SCOPES, authorize=authorize),
                 lambda: dispatcher.read("run-1", principal="alice", scopes=SCOPES, authorize=authorize),
                 lambda: dispatcher.export("run-1", principal="alice", scopes=SCOPES, authorize=authorize)):
        with pytest.raises(DispatchError) as denied:
            call()
        assert denied.value.code == "unauthorized"


def test_read_redacts_and_export_denies_restricted_evidence():
    conn = duckdb.connect(":memory:")
    evidence = [{"kind": "geometry", "id": "g1", "restrictions": ["no-redistribution"]},
                {"kind": "geometry", "id": "g2", "restrictions": []}]
    dispatcher, _ = _dispatcher(conn, {corpus.GEOSPATIAL_TOOLS[0]: lambda a, c: {"evidence": evidence}})
    dispatcher.run_steps("run-1", workflow(), {}, principal="alice", scopes=SCOPES)
    read = dispatcher.read("run-1", principal="alice", scopes=SCOPES, evidence_visible=lambda e: e["id"] != "g2")
    locate = next(s for s in read["steps"] if s["id"] == "locate")
    assert locate["result"]["evidence_withheld"] == 1 and read["output_restrictions"] == ["no-redistribution"]
    with pytest.raises(DispatchError) as denied:
        dispatcher.export("run-1", principal="alice", scopes=SCOPES,
                          evidence_exportable=lambda e: not e["restrictions"])
    assert denied.value.code == "export_denied"


def test_effects_are_enforced_before_and_during_the_call():
    conn = duckdb.connect(":memory:")
    sneaky = {corpus.GEOSPATIAL_TOOLS[0]: lambda a, c: (c.record_effect("local-mutation"), {})[1]}
    dispatcher, _ = _dispatcher(conn, sneaky)
    locate = workflow()["steps"][1]
    with pytest.raises(DispatchError) as violation:
        dispatcher.execute("run-1", locate, {}, principal="alice", scopes=SCOPES)
    assert violation.value.code == "effect_violation" and "local-mutation" in str(violation.value)
    with pytest.raises(DispatchError) as mismatch:
        dispatcher.execute("run-1", {**locate, "id": "x", "effect": "local-mutation"}, {}, principal="alice",
                           scopes=SCOPES)
    assert mismatch.value.code == "effect_mismatch" and "read-only" in str(mismatch.value)


def test_unbound_and_unregistered_targets_are_rejected_before_any_call():
    conn = duckdb.connect(":memory:")
    dispatcher, calls = _dispatcher(conn)
    with pytest.raises(DispatchError) as unbound:
        dispatcher.execute("run-1", {"id": "x", "capability": "legal.citation", "operation": "lookup",
                                     "effect": "read-only"}, {}, principal="alice", scopes=SCOPES)
    assert unbound.value.code == "unbound_operation"
    del dispatcher.handlers[corpus.GEOSPATIAL_TOOLS[0]]
    with pytest.raises(DispatchError) as unregistered:
        dispatcher.execute("run-1", workflow()["steps"][1], {}, principal="alice", scopes=SCOPES)
    assert unregistered.value.code == "unregistered_target" and calls.count == {}


def test_research_cannot_reach_an_osint_gated_operation_through_the_shared_capability(monkeypatch):
    conn = duckdb.connect(":memory:")
    step = {"id": "geolocate", "capability": "geospatial.evidence-geolocation", "operation": "geolocate-evidence",
            "effect": "read-only"}
    monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", "off")
    dispatcher, calls = _dispatcher(conn)
    with pytest.raises(DispatchError) as denied:
        dispatcher.execute("research-run", step, {}, principal="alice", scopes=SCOPES | {"osint:all"})
    assert denied.value.code == "gate_denied" and calls.count == {}
    passed, _ = _dispatcher(conn, gates={"osint-review": lambda principal, scopes: True})
    assert passed.execute("research-run", step, {}, principal="alice", scopes=SCOPES)["ok"]


# ------------------------------------------------------------ C07.4


def test_crash_after_effect_with_owner_receipt_is_adopted_without_a_second_effect():
    conn = duckdb.connect(":memory:")
    owner = {}
    lookup = {local_adapters.SOURCE_TOOL: lambda key: owner.get(key)}
    dispatcher, calls = _dispatcher(conn, receipt_lookup=lookup)
    acquire = workflow()["steps"][0]
    with pytest.raises(SimulatedCrash):
        dispatcher.execute("run-1", acquire, {"q": 1}, principal="alice", scopes=SCOPES, crash_after_effect=True)
    key = dispatcher._row("run-1", "acquire")["idempotency_key"]
    owner[key] = {"result": {"documents": 3}, "receipt_id": "owner:1"}
    resumed = dispatcher.execute("run-1", acquire, {"q": 1}, principal="alice", scopes=SCOPES, action="resume")
    assert resumed["documents"] == 3 and resumed["_dispatch"]["reconciled"]
    assert calls.count[local_adapters.SOURCE_TOOL] == 1


def test_crash_without_receipt_is_an_unknown_outcome_that_stops_only_that_step():
    conn = duckdb.connect(":memory:")
    dispatcher, calls = _dispatcher(conn, receipt_lookup={local_adapters.SOURCE_TOOL: lambda key: None})
    wf = workflow(steps=[workflow()["steps"][0],
                         {**workflow()["steps"][1], "depends_on": []},
                         workflow()["steps"][2]])
    with pytest.raises(SimulatedCrash):
        dispatcher.execute("run-1", wf["steps"][0], {}, principal="alice", scopes=SCOPES, crash_after_effect=True)
    outcome = dispatcher.resume("run-1", wf, {}, principal="alice", scopes=SCOPES)
    assert outcome["steps"] == {"acquire": "unknown", "locate": "completed", "artifact": "completed"}
    assert calls.count[local_adapters.SOURCE_TOOL] == 1
    again = dispatcher.resume("run-1", wf, {}, principal="alice", scopes=SCOPES)
    assert again["steps"]["acquire"] == "unknown" and calls.count[local_adapters.SOURCE_TOOL] == 1


def test_retries_only_where_the_owner_declares_idempotency_or_a_receipt():
    conn = duckdb.connect(":memory:")
    flaky = Calls()
    dispatcher, _ = _dispatcher(conn, {
        local_adapters.SESSION_TOOL: flaky.handler(local_adapters.SESSION_TOOL, fail_times=1)})
    assert dispatcher.execute("run-1", workflow()["steps"][2], {}, principal="alice", scopes=SCOPES)["ok"]
    assert flaky.count[local_adapters.SESSION_TOOL] == 2
    descs = descriptors()
    descs[0]["operations"][0]["idempotency"] = {"supported": False, "receipt": ""}
    p, _ = plan()
    once = Calls()
    strict = WorkflowDispatcher(conn, p, descs, {corpus.GEOSPATIAL_TOOLS[0]: once.handler(
        corpus.GEOSPATIAL_TOOLS[0], fail_times=1)})
    with pytest.raises(DispatchError) as failed:
        strict.execute("run-2", workflow()["steps"][1], {}, principal="alice", scopes=SCOPES)
    assert failed.value.code == "step_failed" and once.count[corpus.GEOSPATIAL_TOOLS[0]] == 1


def test_cancellation_during_a_step_records_a_receipt_consistent_state():
    conn = duckdb.connect(":memory:")
    state = {"cancel": False}
    owner = {}

    def acquire(arguments, context):
        context.record_effect("acquisition")
        owner[context.idempotency_key] = {"result": {"documents": 1}}
        state["cancel"] = True
        return {"documents": 1}

    dispatcher, _ = _dispatcher(conn, {local_adapters.SOURCE_TOOL: acquire},
                                receipt_lookup={local_adapters.SOURCE_TOOL: lambda key: owner.get(key)})
    with pytest.raises(DispatchError) as cancelled:
        dispatcher.execute("run-1", workflow()["steps"][0], {}, principal="alice", scopes=SCOPES,
                           cancelled=lambda: state["cancel"])
    assert cancelled.value.code == "cancelled" and cancelled.value.details["owner_receipt"]
    row = dispatcher._row("run-1", "acquire")
    assert row["status"] == "cancelled" and row["owner_receipt"] == {"result": {"documents": 1}}
    resumed = dispatcher.execute("run-1", workflow()["steps"][0], {}, principal="alice", scopes=SCOPES)
    assert resumed["_dispatch"]["reconciled"]  # adopted from the owner, not re-run


# ------------------------------------------------------------ C07.5


def _local_world():
    conn = duckdb.connect(":memory:")
    old = research_source_pack()
    SourcePackStore(conn).install(old, principal_id="operator", enable=True)
    clock = iter(range(10_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _delay: None)
    for source in old["sources"]:
        runtime.accept_license(old["pack_id"], source["source_id"], principal_id="operator")
    installed = runtime._manifest(old["pack_id"])[0]
    crossref = next(s for s in installed["sources"] if s["source_id"] == "crossref-works")
    fixture_input = {"crossref-works": FixturePageAdapter(crossref, [[{"id": "doi:10.1/berlin", "title": "Mitte"}]])}
    geo = GeospatialStore(conn, now=lambda: 1000)
    district = geo.store_geometry(
        "research", {"type": "Polygon", "coordinates": [[[13.3, 52.5], [13.45, 52.5], [13.45, 52.55],
                                                         [13.3, 52.55], [13.3, 52.5]]]},
        place_id=None, crs="EPSG:4326", precision_m=5, simplified_from=None, disputed=False,
        admin_hierarchy=["country:de"], source={"dataset": "offline-fixture", "revision": "1"},
        evidence=[{"citation": "map:1"}], principal_id="mapper", scopes=GEO_SCOPES)
    intake = IntakeStore(conn)
    session = intake.create("research", "Deep Research", "place-study", intent="Where did it happen?",
                            principal_id="alice", scopes=INTAKE_SCOPES)
    handlers = {
        local_adapters.SOURCE_TOOL: local_adapters.acquire_source(runtime, principal_id="operator",
                                                                  fixture_adapters=fixture_input),
        corpus.GEOSPATIAL_TOOLS[0]: local_adapters.spatial_relation(geo, principal_id="alice", scopes=GEO_SCOPES),
        local_adapters.SESSION_TOOL: local_adapters.session_artifact(intake, principal_id="alice",
                                                                     scopes=INTAKE_SCOPES),
    }
    lookups = {local_adapters.SOURCE_TOOL: local_adapters.source_run_receipt(conn, old["pack_id"])}
    selected = crossref
    arguments = {
        "acquire": {"request": {"pack_id": old["pack_id"], "run_key": "place-study",
                                "operation": selected["operations"][0], "source_ids": ["crossref-works"],
                                "required_sources": ["crossref-works"], "max_pages": 5, "max_results": 10}},
        "locate": {"namespace": "research", "operation": "contains",
                   "left_geometry_id": district["geometry_id"], "right": [13.4, 52.52]},
        "artifact": {"namespace": "research", "session_id": session["session_id"], "artifact_id": "place-evidence",
                     "artifact": {"geometry_id": district["geometry_id"], "claim": "inside Mitte"}},
    }
    return conn, handlers, lookups, arguments, intake, session


def test_bounded_local_journey_produces_owner_receipts_bound_to_session_and_recipe_run():
    conn, handlers, lookups, arguments, intake, session = _local_world()
    p, _ = plan()
    dispatcher = WorkflowDispatcher(conn, p, descriptors(), handlers, receipt_lookup=lookups)
    bindings = WorkflowBindings(conn)
    bindings.bind("intake_session", session["session_id"], "research", workflow(), p, mode="Deep Research")
    recipes = ResearchRecipeStore(conn)
    result = run_workflow(dispatcher, recipes, bindings, workflow(), namespace="research", run_key="journey-1",
                          parameters={"topic": "Mitte"}, arguments=arguments, principal="alice",
                          scopes=SCOPES, recipe_scopes=RECIPE_SCOPES)
    assert result["status"] == "completed", result
    receipt = result["recipe_run"]
    assert receipt["actions_executed"] is True and receipt["execution_mode"] == "composition-dispatch"
    assert [s["step_id"] for s in receipt["dispatch"]["steps"]] == ["acquire", "artifact", "locate"]
    outputs = receipt["outputs"]
    assert outputs["acquire"]["receipt"]["owner"] == "source-pack-runtime"
    assert outputs["acquire"]["provider_input"] == "fixture" and outputs["acquire"]["tool_execution"] == "real"
    assert outputs["locate"]["relation"]["result"]["contains"]
    assert outputs["artifact"]["receipt"]["owner"] == "intake-session"
    state = intake.inspect("research", session["session_id"], principal_id="alice", scopes=INTAKE_SCOPES)
    assert state["data"]["artifacts"]["place-evidence"]["claim"] == "inside Mitte"
    assert conn.execute("SELECT COUNT(*) FROM source_pack_runs").fetchone() == (1,)  # adopted, not re-run
    assert bindings.binding("recipe_run", receipt["run_id"])["plan_digest"] == p["digest"]
    assert bindings.binding("intake_session", session["session_id"])["plan_digest"] == p["digest"]


def test_fixture_runs_cannot_claim_dispatch_and_dispatch_mode_needs_the_dispatcher():
    conn = duckdb.connect(":memory:")
    recipes = ResearchRecipeStore(conn)
    from src.composition.workflows import recipe_for

    recipe = recipes.register(recipe_for(workflow(), "research"), principal_id="alice", scopes=RECIPE_SCOPES)
    fixture = {s["id"]: (lambda step, state: {"fixture": True}) for s in workflow()["steps"]}
    common = {"run_key": "r", "adapters": fixture, "principal_id": "alice", "scopes": RECIPE_SCOPES}
    with pytest.raises(RecipeError) as fixture_claim:
        recipes.run("research", recipe["recipe_revision_id"], {}, execution_mode="caller-supplied-fixture",
                    actions_executed=True, **common)
    assert fixture_claim.value.code == "invalid_execution_mode"
    with pytest.raises(RecipeError) as no_dispatcher:
        recipes.run("research", recipe["recipe_revision_id"], {}, execution_mode="composition-dispatch",
                    actions_executed=True, **common)
    assert no_dispatcher.value.code == "dispatch_required"
    dispatcher, _ = _dispatcher(conn)
    with pytest.raises(DispatchError) as undispatched:
        recipes.run("research", recipe["recipe_revision_id"], {}, execution_mode="composition-dispatch",
                    actions_executed=True, dispatch_attestation=dispatcher.attestation("never-ran"),
                    **{**common, "run_key": "r2"})
    assert undispatched.value.code == "not_dispatched"
    honest = recipes.run("research", recipe["recipe_revision_id"], {}, execution_mode="caller-supplied-fixture",
                         actions_executed=False, **{**common, "run_key": "r3"})
    assert honest["actions_executed"] is False and "dispatch" not in honest
    assert copy.deepcopy(honest)["execution_mode"] == "caller-supplied-fixture"
