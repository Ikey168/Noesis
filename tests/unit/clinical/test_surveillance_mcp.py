"""Surveillance MCP entry points: catalog registration, exact scopes and the never-sentence (#2027)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.surveillance import NEVER_SENTENCE
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import surveillance_fixture_builder as fb
from tests.unit.clinical import surveillance_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.clinical import (
    SURVEILLANCE_SCOPES,
    SURVEILLANCE_TOOLS,
    SURVEILLANCE_WRITES,
)

NS_READ = f"namespace:{h.NS}:read"
NS_WRITE = f"namespace:{h.NS}:write"


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("surveillance") / "surveillance-mcp.duckdb")
    env = h.Env(path)
    env.load_all()
    h.import_boundaries(env.conn)
    h.align_terms(env)
    h.resolve(env)
    h.seed_documents(env)
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    feature = env.conn.execute(
        "SELECT feature_id FROM geospatial_features WHERE native_id='cntr.DE'"
    ).fetchone()[0]
    env.conn.close()
    patch = pytest.MonkeyPatch()
    state = {"principal": "alice", "scopes": set()}
    patch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    patch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    yield (
        tool_map(server.mcp),
        state,
        {"series_id": germany["series_id"], "feature_id": feature},
    )
    patch.undo()


def call(tools, state, name, scopes, **kwargs):
    state["scopes"] = set(scopes)
    return tools[name].fn(**kwargs)


def test_tools_are_registered_in_the_catalog_with_every_scope_they_always_use(mcp_env):
    tools, _, _ = mcp_env
    assert (
        SURVEILLANCE_TOOLS <= set(tools)
        and set(SURVEILLANCE_SCOPES) == SURVEILLANCE_TOOLS
    )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in SURVEILLANCE_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in SURVEILLANCE_WRITES else "read"), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == SURVEILLANCE_SCOPES[name]
        )
        assert by_name[name]["required_scopes"] == SURVEILLANCE_SCOPES[name]
    for name in ("surveillance_boundary_series", "replay_surveillance_query"):
        assert "conditional scope" in tools[name].description.lower()


def test_reads_work_with_exactly_their_declared_scopes_and_carry_the_never_sentence(
    mcp_env,
):
    tools, state, ids = mcp_env
    series_id, feature_id = ids["series_id"], ids["feature_id"]

    def read(name, **kwargs):
        result = call(
            tools,
            state,
            name,
            {*SURVEILLANCE_SCOPES[name], NS_READ},
            namespace=h.NS,
            **kwargs,
        )
        assert result.get("ok") is not False, (name, result)
        assert result["boundary"] == NEVER_SENTENCE, name
        return result

    assert (
        read("surveillance_readiness")["providers"]["who-gho"]["releases"] == 2
    )  # one per indicator
    assert (
        len(read("list_surveillance_series", provider="rki-open-data")["series"]) == 3
    )
    values = read("surveillance_series_values", series_id=series_id)
    assert values["kind"] == "observation" and values["values"]
    history_key = next(
        s for s in read("list_surveillance_series", provider="rki-open-data")["series"]
    )["definition_key"]
    assert (
        len(
            read("surveillance_definition_history", definition_key=history_key)[
                "revisions"
            ]
        )
        == 2
    )
    expansion = read("expand_surveillance_condition", condition="tuberculosis")
    assert expansion["series"] and expansion["unmapped_terms"]
    boundary = read(
        "surveillance_boundary_series", feature_id=feature_id, condition="tuberculosis"
    )
    replayed = read("replay_surveillance_query", receipt=boundary["receipt"])
    assert replayed["status"] == "reproduced"
    assert read("list_surveillance_resolutions")["resolutions"]
    assert (
        read("compare_surveillance_vintages", series_id=series_id)["status"]
        == "single_vintage"
    )
    assert read("surveillance_reporting_delay", series_id=series_id)["periods"]
    assert read("surveillance_pin_status")["pins"] == []
    assert read("surveillance_series_links", series_id=series_id)["publications"] == []
    assert read("surveillance_series_claims", series_id=series_id)["claims"] == []
    # Naming a boundary also needs the Geospatial read scope, checked at call time.
    named = call(
        tools,
        state,
        "surveillance_boundary_series",
        {*SURVEILLANCE_SCOPES["surveillance_boundary_series"], NS_READ},
        namespace=h.NS,
        boundary_name="Bayern",
        boundary_collection="bkg:vg250:lan",
        geo_namespace="geo",
    )
    assert named["ok"] is False and named["error"]["code"] == "unauthorized"
    named = call(
        tools,
        state,
        "surveillance_boundary_series",
        {
            *SURVEILLANCE_SCOPES["surveillance_boundary_series"],
            NS_READ,
            "knowledge:geospatial:read",
        },
        namespace=h.NS,
        boundary_name="Bayern",
        boundary_collection="bkg:vg250:lan",
        geo_namespace="geo",
    )
    assert named["boundary_name_resolution"]["status"] == "resolved"
    other = dict(
        boundary["receipt"],
        request={**boundary["receipt"]["request"], "namespace": "elsewhere"},
    )
    refused = call(
        tools,
        state,
        "replay_surveillance_query",
        {*SURVEILLANCE_SCOPES["replay_surveillance_query"], NS_READ},
        namespace=h.NS,
        receipt=other,
    )
    assert refused["ok"] is False and refused["error"]["code"] == "invalid_receipt"


def test_writes_work_with_exactly_their_declared_scopes_and_are_refused_without(
    mcp_env,
):
    tools, state, ids = mcp_env

    def write(name, **kwargs):
        denied = call(tools, state, name, {NS_WRITE, NS_READ}, namespace=h.NS, **kwargs)
        assert denied["ok"] is False and denied["error"]["code"] == "unauthorized", name
        result = call(
            tools,
            state,
            name,
            {*SURVEILLANCE_SCOPES[name], NS_WRITE, NS_READ},
            namespace=h.NS,
            **kwargs,
        )
        assert result.get("ok") is not False, (name, result)
        assert result["boundary"] == NEVER_SENTENCE
        return result

    assert (
        write("import_surveillance_export", export=fb.ecdc_export())["status"]
        == "unchanged"
    )
    mesh, icd = h.raw("mesh_tuberculosis.json"), h.raw("icd10_who_subset.json")
    aligned = write(
        "align_surveillance_terms",
        mesh_version=mesh["version"],
        icd={"system": "who", "version": icd["version"]},
        curations=h.raw("term_curations.json")["curations"],
    )
    assert aligned["unmapped"]
    resolved = write(
        "resolve_surveillance_geographies", geo_namespace="geo", collections=h.LAND_NUTS
    )
    assert resolved["matched"] == [] and resolved["unresolved"] == []
    linked = write("link_surveillance_series", observation="mcp-link")
    assert {link["evidence_kind"] for link in linked["links"]} == {"dataset-citation"}
    vintage = call(
        tools,
        state,
        "surveillance_series_values",
        {*SURVEILLANCE_SCOPES["surveillance_series_values"], NS_READ},
        namespace=h.NS,
        series_id=ids["series_id"],
    )["vintage"]["vintage_id"]
    pinned = write(
        "pin_surveillance_vintages", view_key="mcp-view", vintage_ids=[vintage]
    )
    assert pinned["pins"][0]["state"] == "current"
    created = write(
        "create_surveillance_monitor",
        request_key="mcp-monitor",
        watch={"series_id": ids["series_id"]},
        thresholds=[{"value": "250", "unit": "deaths"}],
    )
    run = write(
        "run_surveillance_monitor",
        subscription_id=created["subscription_id"],
        watermark=1,
    )
    assert "threshold-exceeded" in {n["kind"] for n in run["notifications"]}
    polled = call(
        tools,
        state,
        "poll_surveillance_monitor",
        {*SURVEILLANCE_SCOPES["poll_surveillance_monitor"], NS_READ},
        namespace=h.NS,
        subscription_id=created["subscription_id"],
    )
    assert polled["events"] and polled["boundary"] == NEVER_SENTENCE
    resolution = next(
        r
        for r in call(
            tools,
            state,
            "list_surveillance_resolutions",
            {"knowledge:clinical:read", NS_READ},
            namespace=h.NS,
        )["resolutions"]
        if r["state"] == "matched"
    )
    reviewed = call(
        tools,
        state,
        "review_surveillance_resolution",
        {*SURVEILLANCE_SCOPES["review_surveillance_resolution"], NS_WRITE},
        namespace=h.NS,
        resolution_id=resolution["resolution_id"],
        decision="accept",
        reason="checked",
    )
    assert reviewed.get("ok") is not False and reviewed["review_state"] == "accepted"


def test_expand_clinical_question_lists_surveillance_series_when_they_are_held(mcp_env):
    tools, state, _ = mcp_env
    result = call(
        tools,
        state,
        "expand_clinical_question",
        {"knowledge:clinical:read", NS_READ},
        namespace=h.NS,
        question={"condition": "tuberculosis", "intervention": "screening"},
    )
    assert result["surveillance"]["series"] and result["surveillance"]["mesh_ids"]
