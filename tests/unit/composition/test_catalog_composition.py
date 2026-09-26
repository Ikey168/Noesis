"""Catalog discovery and readiness from the composition plan (C04, #1814-#1818)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import pytest

from src.composition import contracts as c
from src.composition import deployment
from src.composition.identifiers import FIXTURE, build_reference_catalog, preserved_identifiers
from src.composition.readiness import REASONS, CompositionView, assess, catalog_state, explain
from src.composition.resolver import resolve
from src.kb.geospatial import GeospatialStore
from tests.unit.composition.helpers import capability, manifest, provider, req

ROOT = Path(__file__).resolve().parents[3]
GEO_TOOL = "noesis-knowledge-engine.calculate_spatial_relation"
GEO_SCOPES = {"knowledge:geospatial:calculate", "knowledge:geospatial:write", "knowledge:read", "public"}


def _geo_world():
    geo = next(p for p in c.load_providers() if p["provider_id"] == "noesis.geospatial")
    geospatial = manifest("geospatial", providers=[("noesis.geospatial", "1.0.0")])
    osint = manifest("osint", requires=[req("spatial.relation")],
                     templates=[{"template_id": "location-investigation", "version": "1.0.0"}])
    research = manifest("research", requires=[req("spatial.relation"), req("spatial.resolution-record")])
    contracts = c.contract_candidates(include_domain_packs=False)
    plan = resolve(roots=[{"name": "osint", "range": "^1.0.0"}, {"name": "research", "range": "^1.0.0"}],
                   manifests=[geospatial, osint, research], providers=[geo], contracts=contracts)["plan"]
    return plan, [geo]


@pytest.fixture(scope="module")
def reference():
    return build_reference_catalog()


@pytest.fixture(scope="module")
def geo_conn():
    conn = duckdb.connect(":memory:")
    GeospatialStore(conn)
    return conn


# --------------------------------------------------------------------------- #
# C04.1 plan-derived mappings with preserved identifiers
# --------------------------------------------------------------------------- #

def test_golden_identifiers_match_the_committed_list(reference):
    assert preserved_identifiers(reference) == json.loads(FIXTURE.read_text())


def test_active_reference_composition_keeps_every_identifier(reference):
    candidate_set = deployment.candidates()
    plan = deployment.reference_plan(candidate_set)
    active = build_reference_catalog(
        composition={"plan": plan, "providers": candidate_set["providers"]},
        composition_mode="active",
    )
    before, after = preserved_identifiers(reference), preserved_identifiers(active)
    assert after["tools"].keys() == before["tools"].keys()
    assert after["servers"] == before["servers"]
    for tool_id, facts in before["tools"].items():
        assert after["tools"][tool_id]["required_data"] == facts["required_data"], tool_id


def test_composed_geospatial_attribution_comes_from_the_plan(reference, geo_conn):
    plan, providers = _geo_world()
    readiness = assess(plan, providers, conn=geo_conn, scopes=GEO_SCOPES, observed_at_ms=1)
    active = build_reference_catalog(
        composition={"plan": plan, "providers": providers, "readiness": readiness},
        composition_mode="active",
    )
    tool = next(t for t in active["tools"] if t["id"] == GEO_TOOL)
    legacy = next(t for t in reference["tools"] if t["id"] == GEO_TOOL)
    server = next(s for s in active["servers"] if s["name"] == "noesis-knowledge-engine")
    assert server["pack"] is None  # the stem table attributes no pack
    assert tool["packs"] == ["geospatial", "osint", "research"]
    assert tool["required_data"] == legacy["required_data"] == ["knowledge-engine-runtime"]
    assert tool["state"] == "available" and legacy["state"] == "unauthorized"
    assert {t["id"] for t in active["tools"]} == {t["id"] for t in reference["tools"]}
    untouched = next(t for t in active["tools"] if t["id"] == "noesis-osint.corroborate")
    assert untouched == next(t for t in reference["tools"] if t["id"] == "noesis-osint.corroborate")


# --------------------------------------------------------------------------- #
# C04.2 operation-specific readiness
# --------------------------------------------------------------------------- #

def _kinds(assessment, capability_id):
    op = next(o for o in assessment["operations"] if o["capability"] == capability_id)
    return op, {b["kind"] for b in op["blockers"]}


def test_each_blocked_state_has_its_own_blocker_kind(geo_conn):
    plan, providers = _geo_world()
    ready = assess(plan, providers, conn=geo_conn, scopes=GEO_SCOPES, observed_at_ms=5)
    op, kinds = _kinds(ready, "spatial.relation")
    assert op["state"] == "ready" and not kinds
    assert ready["plan_digest"] == plan["digest"] and ready["observed_at_ms"] == 5

    disabled = assess(plan, providers, conn=geo_conn, scopes=GEO_SCOPES,
                      disabled_providers={"noesis.geospatial"})
    op, kinds = _kinds(disabled, "spatial.relation")
    assert kinds == {"provider-disabled"} and catalog_state(op)[0] == "disabled"

    unauthorized = assess(plan, providers, conn=geo_conn, scopes={"knowledge:read"})
    op, kinds = _kinds(unauthorized, "spatial.relation")
    assert kinds == {"unauthorized"} and catalog_state(op)[0] == "unauthorized"

    empty = assess(plan, providers, conn=duckdb.connect(":memory:"), scopes=GEO_SCOPES,
                   probe_results={"geospatial.local-store": {"state": "blocked",
                                                             "blockers": [{"kind": "empty-data"}]}})
    op, kinds = _kinds(empty, "spatial.relation")
    assert kinds == {"empty-data"} and catalog_state(op)[0] == "empty"


def test_offline_acquisition_and_optional_omission_are_distinct():
    local = capability("spatial.relation")
    refresh = capability("spatial.source-refresh", effect="acquisition")
    refresh["readiness"] = {"probe": "geospatial.live-source"}
    geo = provider("noesis.geo", capabilities=[local, refresh])
    consumer = manifest("osint", requires=[req("spatial.relation"), req("spatial.source-refresh"),
                                           req("spatial.relation-extra", feature="extra")],
                        features=["extra"], providers=None)
    contracts = {"noesis-spatial-result-v1": ["1.0.0"], "noesis-geospatial-geometry-v2": ["2.0.0"]}
    extra = provider("noesis.extra", capabilities=[capability("spatial.relation-extra")])
    plan = resolve(roots=[{"name": "osint", "range": "^1.0.0"}], manifests=[consumer],
                   providers=[geo, extra], contracts=contracts)["plan"]
    readiness = assess(plan, [geo, extra], scopes={"operator"},
                       probe_results={"geospatial.local-store": {"state": "ready", "blockers": []},
                                      "geospatial.live-source": {"state": "ready", "blockers": []}})
    local_op, local_kinds = _kinds(readiness, "spatial.relation")
    remote_op, remote_kinds = _kinds(readiness, "spatial.source-refresh")
    assert local_op["state"] == "ready" and not local_kinds
    assert remote_kinds == {"unverified-live"} and remote_op["state"] == "degraded"
    omitted, kinds = _kinds(readiness, "spatial.relation-extra")
    assert omitted["state"] == "omitted" and not kinds
    assert readiness["optional_omissions"][0]["capability"] == "spatial.relation-extra"


def test_non_composed_tools_keep_legacy_state(reference, geo_conn):
    plan, providers = _geo_world()
    readiness = assess(plan, providers, conn=geo_conn, scopes=GEO_SCOPES)
    active = build_reference_catalog(
        composition={"plan": plan, "providers": providers, "readiness": readiness},
    )
    legacy = {t["id"]: t for t in reference["tools"]}
    bound = CompositionView(plan, providers).tools
    for tool in active["tools"]:
        if tool["id"] not in bound:
            assert tool == legacy[tool["id"]]


# --------------------------------------------------------------------------- #
# C04.3 explanations
# --------------------------------------------------------------------------- #

def test_geospatial_explanation_names_provider_pin_reason_and_consumers(geo_conn):
    plan, providers = _geo_world()
    readiness = assess(plan, providers, conn=geo_conn, scopes=GEO_SCOPES)
    explained = explain(plan, providers, readiness)
    item = next(i for i in explained["capabilities"] if i["capability"] == "spatial.relation")
    assert item["provider_id"] == "noesis.geospatial"
    assert item["pinned"]["version"] == "1.0.0"
    assert item["pinned"]["descriptor_hash"] == providers[0]["descriptor_hash"]
    assert item["selection_reasons"] == ["only-compatible"]
    assert item["consuming_packs"] == ["osint", "research"]
    assert item["required_data"] == ["knowledge-engine-runtime"]


def test_blocked_workflow_names_the_blocking_operation_and_kind(geo_conn):
    plan, providers = _geo_world()
    readiness = assess(plan, providers, conn=geo_conn, scopes=GEO_SCOPES,
                       disabled_providers={"noesis.geospatial"})
    template = {"template_id": "location-investigation", "version": "1.0.0", "consumer": "osint@1.0.0",
                "steps": [{"capability": "spatial.relation"}]}
    workflow = explain(plan, providers, readiness, templates=[template])["workflows"][0]
    assert workflow["state"] == "blocked"
    assert workflow["blocking"][0]["operation"] == GEO_TOOL
    assert workflow["blocking"][0]["blocker"]["kind"] == "provider-disabled"


def test_active_catalog_adds_explanations_without_renaming_fields(reference, geo_conn):
    plan, providers = _geo_world()
    readiness = assess(plan, providers, conn=geo_conn, scopes=GEO_SCOPES)
    active = build_reference_catalog(
        composition={"plan": plan, "providers": providers, "readiness": readiness})
    assert set(reference) <= set(active) and set(active) - set(reference) == {"composition"}
    from jsonschema import Draft7Validator

    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-mcp-catalog-v1.json").read_text())
    assert not list(Draft7Validator(schema).iter_errors(active))


# --------------------------------------------------------------------------- #
# C04.4 shadow mode
# --------------------------------------------------------------------------- #

def test_shadow_mode_output_is_byte_identical_and_records_disagreements(reference, geo_conn):
    plan, providers = _geo_world()
    readiness = assess(plan, providers, conn=geo_conn, scopes=GEO_SCOPES)
    sink: list = []
    shadow = build_reference_catalog(
        composition={"plan": plan, "providers": providers, "readiness": readiness},
        composition_mode="shadow", shadow_sink=sink)
    assert json.dumps(shadow, sort_keys=True) == json.dumps(reference, sort_keys=True)
    fields = {(d["tool"], d["field"]) for d in sink}
    assert (GEO_TOOL, "packs") in fields and (GEO_TOOL, "state") in fields


def test_committed_shadow_report_is_current_and_fully_annotated():
    report = deployment.shadow_report()
    committed = json.loads(deployment.SHADOW_REPORT.read_text())
    assert report == committed
    assert deployment.unannotated(report, deployment.load_annotations()) == []


# --------------------------------------------------------------------------- #
# C04.5 redaction
# --------------------------------------------------------------------------- #

def test_callers_see_only_their_own_consumers(geo_conn):
    plan, providers = _geo_world()
    for visible, hidden in ((["osint@1.0.0"], "research"), (["research@1.0.0"], "osint")):
        readiness = assess(plan, providers, conn=geo_conn, scopes=GEO_SCOPES, visible_consumers=visible)
        explained = explain(plan, providers, readiness, visible_consumers=visible)
        text = json.dumps([readiness, explained])
        assert hidden not in text
        view = CompositionView(plan, providers, readiness=readiness, visible_consumers=visible)
        assert hidden not in json.dumps(view.explanation(GEO_TOOL))
        assert hidden not in view.packs(GEO_TOOL)


def test_credential_values_never_appear_in_diagnostics(reference, geo_conn):
    secret = "hunter2-credential-value-7f3a"
    guarded = copy.deepcopy(provider("noesis.creds", capabilities=[capability("spatial.relation")]))
    guarded["capabilities"][0]["required_context"] = ["namespace", "credential"]
    guarded = c.validate_provider({k: v for k, v in guarded.items() if k != "descriptor_hash"},
                                  known_tools={GEO_TOOL}, registered_bindings=set(),
                                  registered_probes={"geospatial.local-store"})
    consumer = manifest("osint", requires=[req("spatial.relation")])
    plan = resolve(roots=[{"name": "osint", "range": "^1.0.0"}], manifests=[consumer], providers=[guarded],
                   contracts={"noesis-spatial-result-v1": ["1.0.0"], "noesis-geospatial-geometry-v2": ["2.0.0"]})["plan"]
    seen = []

    def credential_available(kind):
        seen.append(kind)
        return False  # a resolver holding `secret` answers only yes or no

    readiness = assess(plan, [guarded], conn=geo_conn, scopes={"operator"},
                       credential_available=credential_available,
                       probe_results={"geospatial.local-store": {"state": "blocked", "blockers": [
                           {"kind": "missing-credentials", "reason": secret, "credential_kind": "api-key"}]}})
    explained = explain(plan, [guarded], readiness)
    catalog = build_reference_catalog(composition={"plan": plan, "providers": [guarded], "readiness": readiness})
    for payload in (readiness, explained, catalog):
        assert secret not in json.dumps(payload)
    op = readiness["operations"][0]
    assert {b["kind"] for b in op["blockers"]} == {"missing-credentials"}
    assert all(b["reason"] == REASONS["missing-credentials"] for b in op["blockers"])
    assert seen == ["noesis.creds"]
