"""Workflow templates, plan binding and authorized dispatch (C07, #1828-#1832)."""

from __future__ import annotations

import copy
import json

import pytest

from src.composition import bindings as registry
from src.composition import contracts as c
from src.composition import lifecycle as lc
from src.composition.dispatcher import DispatchContext
from src.composition.workflows import (
    WorkflowStore,
    adapt_stored_investigation_templates,
    templates_of,
    validate_template,
)
from src.kb.investigation_templates import InvestigationTemplateStore
from src.kb.research_recipes import RecipeError, ResearchRecipeStore
from tests.unit.composition.journey import (
    NAMESPACE,
    World,
    consumer_manifest,
    location_template,
)

CONSUMER = "osint@1.0.0"


@pytest.fixture(autouse=True)
def isolated():
    from src.domains import registry as domain_registry

    saved = domain_registry._AUTHORITY
    lc.reset_runtime()
    yield
    domain_registry.set_authority(saved)
    lc.reset_runtime()


def world_with(template=None, *, requires=None, name="osint"):
    template = template or location_template(name, f"{name}.location-investigation")
    world = World(consumers={name: consumer_manifest(name, template, requires=requires)})
    manifest = next(m for m in world.coordinator.store.manifests() if m["name"] == name)
    (tmpl,) = templates_of(manifest)
    return world, tmpl


def start(world, tmpl, session, *, run_key="run-1", place="Mitte", **kwargs):
    return world.dispatcher(**kwargs.pop("dispatcher", {})).start(
        tmpl, namespace=NAMESPACE, consumer=kwargs.pop("consumer", CONSUMER), parameters={"place": place},
        run_key=run_key, principal_id="analyst", session_id=session["session_id"], **kwargs)


# --------------------------------------------------------------------------- #
# C07.1 template contract and bindings
# --------------------------------------------------------------------------- #

def test_every_stored_investigation_template_adapts_without_losing_pins():
    world = World()
    store = InvestigationTemplateStore(world.conn)
    scopes = {"knowledge:projects:write", "knowledge:projects:read", f"namespace:{NAMESPACE}:write",
              f"namespace:{NAMESPACE}:read", "domain:research:read"}
    pins = [[{"pack_id": "geospatial-berlin", "version": "1.1.0"}],
            [{"pack_id": "geospatial-berlin", "version": "1.1.0"}, {"pack_id": "research-discovery", "version": "1.0.0"}]]
    for index, pinned in enumerate(pins):
        store.create(NAMESPACE, f"t{index}", {
            "name": f"Template {index}", "description": "Pinned", "parameters": {"place": "Where"},
            "questions": ["Where?"], "success_criteria": ["Found"],
            "scope": {"domains": ["research"], "namespaces": []},
            "source_packs": pinned, "report_outline": ["Findings"]}, principal_id="analyst", scopes=scopes)
    adapted = adapt_stored_investigation_templates(world.conn)
    assert len(adapted) == 2
    for template in adapted:
        origin = template["origin"]["investigation_template_id"]
        state = store.inspect(NAMESPACE, origin, principal_id="analyst", scopes=scopes)
        assert template["source_packs"] == state["definition"]["source_packs"]
        acquisitions = [s["arguments"] for s in template["steps"] if s["capability"] == "sources.acquire"]
        assert acquisitions == [{"pack_id": p["pack_id"], "version": p["version"]}
                                for p in state["definition"]["source_packs"]]


def test_undeclared_capabilities_and_store_declarations_fail_validation():
    template = location_template("osint", "osint.location-investigation")
    manifest = consumer_manifest("osint", None, requires=["sources.acquire"])
    with pytest.raises(c.CompositionError) as caught:
        validate_template(template, manifest=manifest)
    assert caught.value.code == "undeclared_capability"
    assert "spatial.points-within-boundary" in caught.value.details["capabilities"]
    with_store = copy.deepcopy(template)
    with_store["steps"][0]["arguments"]["stores"] = ["geospatial_features"]
    with pytest.raises(c.CompositionError) as caught:
        validate_template(with_store)
    assert caught.value.code == "template_declares_store"


def test_session_binding_is_stored_beside_unchanged_session_state():
    world, tmpl = world_with()
    session = world.session("s-bind")
    before = world.intake.inspect(NAMESPACE, session["session_id"], principal_id="analyst",
                                  scopes=world.grants["analyst"], revision=1)
    plan = world.coordinator.store.active_plan()
    binding = WorkflowStore(world.conn).bind_session(
        NAMESPACE, session["session_id"], template=tmpl, plan=plan, profile=None,
        principal_id="analyst", scopes=world.grants["analyst"])
    after = world.intake.inspect(NAMESPACE, session["session_id"], principal_id="analyst",
                                 scopes=world.grants["analyst"], revision=1)
    assert before == after
    assert world.intake.inspect(NAMESPACE, session["session_id"], principal_id="analyst",
                                scopes=world.grants["analyst"])["revision"] == 1
    assert binding["mode"] == "Deep Research" and binding["template_id"] == tmpl["template_id"]
    assert binding["plan_digest"] == plan["digest"]


# --------------------------------------------------------------------------- #
# C07.2 plan-bound runs
# --------------------------------------------------------------------------- #

def test_run_records_plan_digest_and_preflight_observation():
    world, tmpl = world_with()
    session = world.session("s-run")
    receipt = start(world, tmpl, session)
    binding = WorkflowStore(world.conn).run_binding(receipt["dispatch_run_id"])
    plan = world.coordinator.store.active_plan()
    assert binding["plan_digest"] == plan["digest"] == receipt["plan_digest"]
    assert binding["preflight"]["plan_digest"] == plan["digest"]
    assert binding["preflight"]["observed_at_ms"] > 0
    assert binding["recipe_run_id"] == receipt["run_id"]


def test_resume_after_provider_change_requires_a_new_plan():
    world, tmpl = world_with()
    session = world.session("s-resume")
    receipt = start(world, tmpl, session)
    revised = json.loads(json.dumps(next(p for p in world.coordinator.store.providers()
                                         if p["provider_id"] == "noesis.documents")))
    revised.pop("descriptor_hash")
    revised["description"] = "revised descriptor"
    world.conn.execute("DELETE FROM composition_providers WHERE provider_id='noesis.documents'")
    world.coordinator.store.install_provider(revised, principal_id="operator")
    result = world.dispatcher().resume(receipt["dispatch_run_id"], principal_id="analyst")
    assert result["status"] == "new-plan-required" and result["resumed"] is False
    assert result["changes"][0]["id"] == "noesis.documents@1.0.0"


# --------------------------------------------------------------------------- #
# C07.3 dispatcher authority, effects and gates
# --------------------------------------------------------------------------- #

def test_revoked_authority_is_rechecked_on_execute_resume_read_and_export():
    world, tmpl = world_with()
    session = world.session("s-revoke")
    original = registry._BINDINGS["evidence.document-references"]

    def revoking(ctx, arguments):
        world.grants["analyst"].discard("knowledge:recipes:execute")
        return original.fn(ctx, arguments)

    registry._BINDINGS["evidence.document-references"] = registry.RegisteredBinding(
        **{**original.__dict__, "fn": revoking})
    try:
        with pytest.raises(RecipeError) as denied:
            start(world, tmpl, session)
        assert denied.value.code == "authority_revoked"
    finally:
        registry._BINDINGS["evidence.document-references"] = original
    run_id = world.conn.execute("SELECT run_id FROM composition_run_bindings").fetchone()[0]
    dispatcher = world.dispatcher()
    with pytest.raises(c.CompositionError) as caught:
        dispatcher.resume(run_id, principal_id="analyst")
    assert caught.value.code == "authority_revoked"
    world.grants["analyst"].add("knowledge:recipes:execute")
    completed = dispatcher.resume(run_id, principal_id="analyst")
    assert completed["status"] == "completed"
    world.grants["analyst"].discard(f"namespace:{NAMESPACE}:read")
    view = dispatcher.read(run_id, principal_id="analyst")
    assert all(ref == {"redacted": True} for step in view["steps"] for ref in step["references"]
               if step["step_id"] == "evidence")
    exported = dispatcher.export(run_id, principal_id="analyst")
    assert exported["redacted"] is True and "outputs" not in exported
    world.grants["analyst"].discard("knowledge:recipes:read")
    with pytest.raises(c.CompositionError):
        dispatcher.read(run_id, principal_id="analyst")
    with pytest.raises(c.CompositionError):
        dispatcher.export(run_id, principal_id="analyst")


def test_read_only_step_cannot_perform_a_mutation():
    template = location_template("osint", "osint.location-investigation", effect_override="read-only")
    world, tmpl = world_with(template)
    session = world.session("s-effect")
    with pytest.raises(c.CompositionError) as caught:
        start(world, tmpl, session)
    assert caught.value.code == "effect_violation"
    assert caught.value.details["attempted"] == "local-mutation"
    assert world.conn.execute("SELECT count(*) FROM composition_step_receipts").fetchone()[0] == 0
    ctx = DispatchContext(conn=None, namespace=NAMESPACE, principal_id="analyst", scopes=set(),
                          run_id="r", step_id="artifact", idempotency_key="k", declared_effect="read-only")
    with pytest.raises(RecipeError) as runtime_check:
        ctx.require_effect("local-mutation")
    assert runtime_check.value.code == "effect_violation" and "local-mutation" in runtime_check.value.message


def test_shared_capability_cannot_bypass_the_osint_gate(monkeypatch):
    geolocate = {"id": "geolocate", "capability": "osint.event-geolocation", "range": "^1.0.0",
                 "effect": "read-only", "arguments": {"topic": {"$param": "place"}}}
    template = location_template("research", "research.place-evidence", extra_steps=[geolocate])
    requires = ["sources.acquire", "spatial.points-within-boundary", "evidence.document-references",
                "intake.session-artifact", "osint.event-geolocation"]
    world, tmpl = world_with(template, requires=requires, name="research")
    world.conn.execute("CREATE TABLE argument_claims (claim_id TEXT, document_id TEXT, claim_text TEXT)")
    world.conn.execute("INSERT INTO argument_claims VALUES ('c1','d1','Protest reported in Berlin')")
    session = world.session("s-gate")
    monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", "off")
    with pytest.raises(RecipeError) as denied:
        start(world, tmpl, session, consumer="research@1.0.0")
    assert denied.value.code == "gate_denied"
    monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", "on")
    receipt = start(world, tmpl, session, consumer="research@1.0.0", run_key="run-2")
    assert receipt["status"] == "completed"


# --------------------------------------------------------------------------- #
# C07.4 retries, receipts and unknown outcomes
# --------------------------------------------------------------------------- #

def test_crash_after_an_effect_with_an_owner_receipt_adopts_it_on_resume(monkeypatch):
    world, tmpl = world_with()
    session = world.session("s-crash")
    real = WorkflowStore.record_step
    crashed = {"done": False}

    def crash_once(self, run_id, step_id, **kwargs):
        if step_id == "artifact" and kwargs["status"] == "completed" and not crashed["done"]:
            crashed["done"] = True
            raise lc.Crash("worker died after the intake write")
        return real(self, run_id, step_id, **kwargs)

    monkeypatch.setattr(WorkflowStore, "record_step", crash_once)
    with pytest.raises(lc.Crash):
        start(world, tmpl, session)
    revision_after_crash = world.intake.inspect(
        NAMESPACE, session["session_id"], principal_id="analyst", scopes=world.grants["analyst"])["revision"]
    run_id = world.conn.execute("SELECT run_id FROM composition_run_bindings").fetchone()[0]
    receipt = world.dispatcher().resume(run_id, principal_id="analyst")
    assert receipt["status"] == "completed"
    assert receipt["outputs"]["artifact"]["adopted"] is True
    revision_final = world.intake.inspect(
        NAMESPACE, session["session_id"], principal_id="analyst", scopes=world.grants["analyst"])["revision"]
    assert revision_final == revision_after_crash  # no second effect


def _register_publisher(calls):
    @registry.register_binding("test.external-publish", arguments_schema={"type": "object"})
    def publish(ctx, arguments):
        ctx.require_effect("external-publication")
        calls.append(ctx.idempotency_key)
        raise ConnectionError("connection dropped after the remote accepted the post")

    return publish


def test_crash_without_a_receipt_reports_unknown_outcome_and_never_retries_blindly():
    calls: list[str] = []
    _register_publisher(calls)
    try:
        publish_step = {"id": "publish", "capability": "test.publish", "range": "^1.0.0",
                        "effect": "external-publication", "depends_on": ["acquire"]}
        followup = {"id": "followup", "capability": "research.place-literature", "range": "^1.0.0",
                    "effect": "read-only", "depends_on": ["publish"], "arguments": {"place": "Mitte"}}
        template = location_template("osint", "osint.location-investigation",
                                     extra_steps=[publish_step, followup])
        requires = ["sources.acquire", "spatial.points-within-boundary", "evidence.document-references",
                    "intake.session-artifact", "test.publish", "research.place-literature"]
        publisher = {
            "contract": c.PROVIDER_CONTRACT, "provider_id": "example.publisher", "version": "1.0.0",
            "implementation": {"identity": "test", "version": "1"}, "stores": [],
            "capabilities": [{"capability": "test.publish", "version": "1.0.0",
                              "input_contract": {"name": "noesis-knowledge-source-v1", "version": "1.0.0"},
                              "output_contract": {"name": "noesis-knowledge-source-v1", "version": "1.0.0"},
                              "effect": "external-publication", "idempotent": False,
                              "execution_receipt": False,
                              "bindings": [{"kind": "registered", "id": "test.external-publish"}],
                              "readiness": {"probe": "intake.store"}}]}
        world = World()
        world.coordinator.store.install_provider(publisher, principal_id="operator")
        world.coordinator.store.install_manifest(consumer_manifest("osint", template, requires=requires),
                                                 principal_id="operator")
        world.coordinator.store.select("osint", "^1.0.0", principal_id="operator")
        world.coordinator.activate("gen-1", principal_id="operator")
        (tmpl,) = templates_of(next(m for m in world.coordinator.store.manifests() if m["name"] == "osint"))
        session = world.session("s-unknown")
        with pytest.raises(RecipeError) as stopped:
            start(world, tmpl, session)
        assert stopped.value.code == "reconciliation_required"
        assert stopped.value.details["stopped"] == ["followup", "publish"]
        assert len(calls) == 1  # never retried blindly
        receipts = {r["step_id"]: r["status"] for r in WorkflowStore(world.conn).step_receipts(
            world.conn.execute("SELECT run_id FROM composition_run_bindings").fetchone()[0])}
        assert receipts["publish"] == "unknown"
        assert receipts["artifact"] == "completed"  # independent steps still ran
        run_id = world.conn.execute("SELECT run_id FROM composition_run_bindings").fetchone()[0]
        with pytest.raises(RecipeError):
            world.dispatcher().resume(run_id, principal_id="analyst")
        assert len(calls) == 1
    finally:
        registry._BINDINGS.pop("test.external-publish", None)


def test_cancellation_during_a_step_leaves_receipt_consistent_state():
    world, tmpl = world_with()
    session = world.session("s-cancel")
    original = registry._BINDINGS["geospatial.features-within"]
    holder = {}

    def cancelling(ctx, arguments):
        holder["snapshot"] = holder["dispatcher"].cancel(ctx.run_id, principal_id="analyst")
        return original.fn(ctx, arguments)

    registry._BINDINGS["geospatial.features-within"] = registry.RegisteredBinding(
        **{**original.__dict__, "fn": cancelling})
    try:
        dispatcher = world.dispatcher()
        holder["dispatcher"] = dispatcher
        with pytest.raises(RecipeError) as cancelled:
            dispatcher.start(tmpl, namespace=NAMESPACE, consumer=CONSUMER, parameters={"place": "Mitte"},
                             run_key="run-1", principal_id="analyst", session_id=session["session_id"])
        assert cancelled.value.code == "cancelled"
    finally:
        registry._BINDINGS["geospatial.features-within"] = original
    run_id = world.conn.execute("SELECT run_id FROM composition_run_bindings").fetchone()[0]
    receipts = {r["step_id"]: r["status"] for r in WorkflowStore(world.conn).step_receipts(run_id)}
    assert receipts == {"acquire": "completed", "within": "completed"}  # in-flight step finished
    assert {r["step_id"] for r in holder["snapshot"]["step_receipts"]} == {"acquire"}
    assert WorkflowStore(world.conn).run_binding(run_id)["status"] == "cancelled"


# --------------------------------------------------------------------------- #
# C07.5 local adapters and honest execution claims
# --------------------------------------------------------------------------- #

def test_local_journey_produces_owner_receipts_bound_to_session_and_run():
    world, tmpl = world_with()
    session = world.session("s-journey")
    receipt = start(world, tmpl, session)
    assert receipt["actions_executed"] is True and receipt["execution_mode"] == "composition-dispatch"
    outputs = receipt["outputs"]
    source_run = outputs["acquire"]["receipt"]
    assert source_run["status"] == "complete" and outputs["acquire"]["provider_input"] == "injected-transport"
    assert world.conn.execute("SELECT count(*) FROM source_pack_runs WHERE run_id=?",
                              [source_run["run_id"]]).fetchone()[0] == 1
    assert outputs["within"]["status"] == "complete" and outputs["within"]["total_members"] == 2
    assert outputs["within"]["receipt"]["receipt_id"]
    refs = outputs["evidence"]["references"]
    assert {r["record_kind"] for r in refs} == {"document"} and len(refs) == 2
    state = world.intake.inspect(NAMESPACE, session["session_id"], principal_id="analyst",
                                 scopes=world.grants["analyst"])
    assert state["revision"] == outputs["artifact"]["revision"]
    assert {r["id"] for r in state["references"]} == {r["record_id"] for r in refs}
    assert receipt["dispatch_run_id"] in json.dumps(state["data"])


def test_fixture_runs_cannot_claim_executed_actions():
    world = World()
    recipes = ResearchRecipeStore(world.conn)
    recipe = recipes.register({
        "recipe_id": "fixture", "version": "1", "namespace": NAMESPACE, "inputs": {},
        "steps": [{"id": "s", "tool": "t", "input_schema": {"a": 1}, "output_schema": {"a": 1}}],
        "outputs": [], "compatibility": {}}, principal_id="p", scopes={"knowledge:recipes:write"})
    common = dict(run_key="k", adapters={"s": lambda step, state: {"x": 1}}, principal_id="p",
                  scopes={"knowledge:recipes:execute"})
    with pytest.raises(RecipeError) as dishonest:
        recipes.run(NAMESPACE, recipe["recipe_revision_id"], {}, execution_mode="caller-supplied-fixture",
                    actions_executed=True, **common)
    assert dishonest.value.code == "dishonest_execution_claim"
    with pytest.raises(RecipeError) as forged:
        recipes.run(NAMESPACE, recipe["recipe_revision_id"], {}, execution_mode="composition-dispatch",
                    actions_executed=True, **common)
    assert forged.value.code == "dispatcher_required"
    honest = recipes.run(NAMESPACE, recipe["recipe_revision_id"], {}, execution_mode="caller-supplied-fixture",
                         actions_executed=False, **common)
    assert honest["actions_executed"] is False
