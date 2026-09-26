"""Catalog discovery and operation readiness from the composition plan (C04)."""

from __future__ import annotations

import asyncio
import copy
import json

import duckdb
import pytest

from src.composition.contracts import validate_readiness
from src.composition.readiness import CompositionView, assess, redact
from src.composition.resolver import resolve
from src.composition.shadow import SHADOW_REPORT, registered_view, render, report
from src.mcp_host.catalog import build_catalog
from tests.unit import composition_corpus as corpus

SCOPES = {"public", "knowledge:read", "operator", "knowledge:geospatial:read", "knowledge:geospatial:write",
          "knowledge:geospatial:calculate"}
SPATIAL_TOOL = "noesis-knowledge-engine.calculate_spatial_relation"
SECRET = "sk-live-SECRET-7f3a91"


def _catalog(**kwargs):
    defaults = {"enabled_pack_names": {"news", "research", "osint", "geospatial"}, "configured_backends": set(),
                "granted_scopes": SCOPES}
    return asyncio.run(build_catalog(**{**defaults, **kwargs}))


def _refresh_descriptor():
    """geospatial.core with a remote refresh next to its local spatial query."""

    body = corpus.spatial_provider(version="1.1.0")
    body["operations"].append({"id": "refresh-places", "tool": "noesis-knowledge-engine.import_geospatial_features",
                               "side_effect": "acquisition", "idempotency": {"supported": True},
                               "readiness_probe": "places", "required_scopes": ["knowledge:geospatial:write"],
                               "required_context": ["namespace", "network", "credentials"]})
    body["capabilities"][0]["operations"].append("refresh-places")
    return body


def _world(features=None):
    world = corpus.resolver_world()
    osint = corpus.pack("osint", requires=[corpus.SPATIAL], features=[corpus.MEDIA_FEATURE],
                        aliases={"neuronews-osint": "osint"},
                        contributes_extra={"workflow_templates": [{"id": "osint.location-investigation",
                                                                   "version": "1.0.0"}]})
    science = corpus.pack("science", requires=[corpus.SPATIAL], aliases={"research": "science"},
                          contributes_extra={"workflow_templates": [{"id": "science.site-study",
                                                                     "version": "1.0.0"}]})
    packs = [osint, science] + world["packs"][2:]
    providers = [world["providers"][0], _refresh_descriptor()]
    roots = [{"pack": "osint", "range": "^1.0.0", **({"features": features} if features else {})},
             {"pack": "science", "range": "^1.0.0"}]
    plan = resolve(roots, packs, providers).plan
    return CompositionView(plan, providers, packs)


@pytest.fixture(scope="module")
def legacy():
    return _catalog()


# ------------------------------------------------------------ C04.1 mappings


def _projection(catalog):
    return ({t["id"]: (t["required_data"], t["state"], t["required_scopes"]) for t in catalog["tools"]},
            {s["name"]: (s.get("aliases"), s.get("pack")) for s in catalog["servers"]})


def test_golden_registered_bundles_keep_tool_ids_aliases_and_required_data(legacy):
    view = registered_view()
    composed = _catalog(composition=view)
    tools, servers = _projection(composed)
    legacy_tools, legacy_servers = _projection(legacy)
    assert set(tools) == set(legacy_tools) and servers == legacy_servers  # tool IDs and aliases preserved
    managed = set(view.tools)
    assert managed  # OSINT + Research + Geospatial bindings (C08)
    assert {t: v for t, v in tools.items() if t not in managed} == {
        t: v for t, v in legacy_tools.items() if t not in managed}
    for tool in composed["tools"]:
        assert ("composition" in tool) == (tool["id"] in managed)
        if tool["id"] in managed:
            assert tool["required_data"] == view.tools[tool["id"]].required_data
            assert tool["required_scopes"] == legacy_tools[tool["id"]][2]  # a pack is not a grant
    assert "composition" not in legacy


def test_composed_geospatial_tool_takes_pack_and_data_from_the_plan(legacy):
    conn = duckdb.connect(":memory:")
    catalog = _catalog(composition=_world(), conn=conn)
    tool = next(t for t in catalog["tools"] if t["id"] == SPATIAL_TOOL)
    assert tool["composition"] == {"capability": "geospatial.spatial-relation", "provider": "geospatial.core",
                                   "provider_version": "1.1.0", "operation": "calculate-spatial-relation",
                                   "pack": "geospatial", "reason": "explicit"}
    assert tool["required_data"] == ["geometries"] and tool["state"] == "unavailable"
    server = next(s for s in catalog["servers"] if s["name"] == "noesis-knowledge-engine")
    assert server["pack"] is None  # the stem table has no entry; attribution came from the plan
    legacy_tool = next(t for t in legacy["tools"] if t["id"] == SPATIAL_TOOL)
    assert "composition" not in legacy_tool and legacy_tool["required_data"] == ["knowledge-engine-runtime"]
    untouched = next(t for t in catalog["tools"] if t["id"] == "noesis-market.market_readiness")
    assert untouched == next(t for t in legacy["tools"] if t["id"] == "noesis-market.market_readiness")


# ------------------------------------------------------------ C04.4 shadow


def test_shadow_mode_returns_the_legacy_catalog_byte_for_byte(legacy):
    sink = []
    shadowed = _catalog(composition=_world(), shadow_sink=sink, conn=None)
    assert json.dumps(shadowed, sort_keys=True) == json.dumps(legacy, sort_keys=True)
    fields = {(d["tool"], d["field"]) for d in sink}
    assert {(SPATIAL_TOOL, "pack"), (SPATIAL_TOOL, "required_data"), (SPATIAL_TOOL, "state")} <= fields
    grouped = report(sink, _world())["disagreements"]
    assert set(grouped) == {"geospatial"}
    assert all(d["annotation"].split(":")[0] in {"resolved", "intended"} for d in grouped["geospatial"])


def test_committed_shadow_report_for_registered_bundles():
    sink = []
    enabled = json.loads((corpus.ROOT / "config/domain_packs.json").read_text())["enabled_packs"]
    view = registered_view()
    _catalog(composition=view, shadow_sink=sink, enabled_pack_names=enabled,
             granted_scopes={"public", "knowledge:read", "operator"})
    assert SHADOW_REPORT.read_text() == render(report(sink, view))


# ------------------------------------------------------------ C04.2 readiness


def _conn(*, geometries=True, places_rows=1):
    conn = duckdb.connect(":memory:")
    if geometries:
        conn.execute("CREATE TABLE geospatial_geometries (id INTEGER)")
    conn.execute("CREATE TABLE geospatial_places (id INTEGER)")
    for index in range(places_rows):
        conn.execute("INSERT INTO geospatial_places VALUES (?)", [index])
    return conn


def _ops(document):
    assert validate_readiness(document) == []
    return {o["operation"]: o for o in document["operations"]}


def _kinds(entry):
    return [b["kind"] for b in entry["blockers"]]


def test_local_query_ready_while_remote_refresh_is_unavailable():
    ops = _ops(assess(_world(), conn=_conn(), scopes=SCOPES, now_ms=lambda: 1))
    assert ops["calculate-spatial-relation"]["state"] == "ready" and not ops["calculate-spatial-relation"]["blockers"]
    refresh = ops["refresh-places"]
    assert refresh["state"] == "blocked"
    assert _kinds(refresh) == ["missing_credentials", "unverified_live_access"]


@pytest.mark.parametrize(("setup", "operation", "kind"), [
    ({"disabled_providers": {"geospatial.core"}}, "calculate-spatial-relation", "disabled_provider"),
    ({"scopes": {"knowledge:read"}}, "calculate-spatial-relation", "unauthorized"),
    ({"conn": "no-geometries"}, "calculate-spatial-relation", "inaccessible_data"),
    ({"credentials": {"geospatial.core": SECRET}}, "refresh-places", "unverified_live_access"),
    ({"failures": {"geospatial.core.calculate-spatial-relation": "timeout"}}, "calculate-spatial-relation",
     "failed_execution"),
])
def test_each_state_has_its_own_blocker_kind(setup, operation, kind):
    options = {"conn": _conn(), "scopes": SCOPES, "now_ms": lambda: 1, **setup}
    if options["conn"] == "no-geometries":
        options["conn"] = _conn(geometries=False)
    entry = _ops(assess(_world(), **options))[operation]
    assert entry["state"] == "blocked" and kind in _kinds(entry)


def test_empty_data_blocker_from_a_row_probe():
    view = _world()
    for tool in list(view.tools.values()):
        if tool.operation == "record-resolution":
            probes = ({"id": "places", "kind": "table-rows", "target": "geospatial_places"},)
            view.tools[tool.tool] = type(tool)(**{**tool.__dict__, "probes": probes})
    entry = _ops(assess(view, conn=_conn(places_rows=0), scopes=SCOPES, now_ms=lambda: 1))["record-resolution"]
    assert _kinds(entry) == ["empty_data"] and entry["state"] == "blocked"


def test_optional_omissions_are_reported_not_blocking():
    ops = _ops(assess(_world(features=["imagery"]), conn=_conn(), scopes=SCOPES, now_ms=lambda: 1))
    entry = ops["calculate-spatial-relation"]
    assert entry["state"] == "ready" and entry["blockers"] == []
    assert entry["optional_omissions"][0]["feature"] == "imagery"


def test_assessment_records_observation_time_and_plan_digest_only():
    view = _world()
    document = assess(view, conn=_conn(), scopes=SCOPES, now_ms=lambda: 42)
    assert document["observed_at_ms"] == 42 and document["plan_digest"] == view.plan["digest"]
    assert "observed_at_ms" not in json.dumps(view.plan)


# ------------------------------------------------------------ C04.3 explanations


def test_capability_explanation_names_provider_pin_reason_and_consumers():
    view = _world()
    explained = view.explain(assess(view, conn=_conn(), scopes=SCOPES, now_ms=lambda: 1))
    (capability,) = explained["capabilities"]
    assert capability["provider"] == "geospatial.core"
    assert capability["pinned"]["version"] == "1.1.0" and capability["pinned"]["content_hash"].startswith("sha256:")
    assert capability["reason"] == "explicit" and capability["consumers"] == ["osint", "science"]
    assert {o["operation"]: o["required_data"] for o in capability["operations"]}["calculate-spatial-relation"] == [
        "geometries"]


def test_blocked_workflow_names_blocking_operation_and_kind():
    view = _world()
    explained = view.explain(assess(view, conn=_conn(geometries=False), scopes=SCOPES, now_ms=lambda: 1))
    workflow = next(w for w in explained["workflows"] if w["workflow"] == "osint.location-investigation")
    assert workflow["state"] == "blocked"
    assert {"capability": "geospatial.spatial-relation", "operation": "calculate-spatial-relation",
            "kind": "inaccessible_data"} in [{k: b[k] for k in ("capability", "operation", "kind")}
                                            for b in workflow["blocked_by"]]


def test_catalog_exposes_explanations_without_changing_existing_fields(legacy):
    view = _world()
    catalog = _catalog(composition=view, composition_context={"visible_packs": ["osint", "geospatial"]})
    assert catalog["composition"]["capabilities"][0]["consumers"] == ["osint"]
    assert set(legacy) <= set(catalog) and set(catalog) - set(legacy) == {"composition"}


# ------------------------------------------------------------ C04.5 redaction


def test_two_callers_see_only_their_own_selections_and_no_inaccessible_identifiers():
    view = _world()
    selections = {"alice": ["osint.location-investigation"], "bob": ["science.site-study"]}
    alice = view.explain(visible_packs={"osint", "geospatial"}, principal="alice", selections=selections)
    bob = view.explain(visible_packs={"science", "geospatial"}, principal="bob", selections=selections)
    assert alice["selected_workflows"] == ["osint.location-investigation"]
    assert bob["selected_workflows"] == ["science.site-study"]
    assert alice["capabilities"][0]["consumers"] == ["osint"] and bob["capabilities"][0]["consumers"] == ["science"]
    assert "science" not in json.dumps(alice) and "osint" not in json.dumps(bob["workflows"])
    readiness = assess(view, conn=_conn(), scopes=SCOPES, namespace="team-alice",
                       accessible_namespaces={"team-bob"}, now_ms=lambda: 1)
    text = json.dumps(readiness)
    assert "inaccessible_data" in text
    for identifier in ("geospatial_geometries", "geospatial_places", "team-bob"):
        assert identifier not in text


def test_credential_values_never_appear_in_diagnostics():
    view = _world()
    readiness = assess(view, conn=_conn(), scopes=SCOPES, credentials={"geospatial.core": SECRET},
                       failures={"geospatial.core.refresh-places": f"401 for token {SECRET}"}, now_ms=lambda: 1)
    explained = redact(view.explain(readiness), secrets=[SECRET])
    for payload in (readiness, explained):
        assert SECRET not in json.dumps(payload) and SECRET[:12] not in json.dumps(payload)
    assert "[redacted]" in json.dumps(readiness)
    assert redact({"api_key": SECRET, "nested": [{"token": "x"}]}) == {"nested": [{}]}
    assert validate_readiness(copy.deepcopy(readiness)) == []
