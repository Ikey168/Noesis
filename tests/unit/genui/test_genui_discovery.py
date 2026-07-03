"""Unit tests for the discovery-derived panel catalog (src/genui/discovery.py).

Covers the ADR-001 annotation validator, the merge-over-static semantics,
and the two R2 litmus properties: a new annotated server surfaces a new
panel type, and with nothing discovered the output is byte-identical to
the static catalog. All host access goes through fakes.
"""

from typing import Dict, List

import pytest

from src.genui.catalog import PANEL_CATALOG, panel_catalog_dict
from src.genui.discovery import (
    discovered_panel_defs,
    merged_catalog,
    merged_catalog_dict,
    panel_def_from_annotation,
)


def make_tool(panel=None, name="some_tool", has_output_schema=True, meta=None):
    tool = {
        "name": name,
        "description": "a tool description",
        "meta": meta if meta is not None else ({"panel": panel} if panel is not None else {}),
        "has_output_schema": has_output_schema,
    }
    return tool


GOOD = {
    "type": "custom_widget",
    "title": "Citation graph",
    "description": "Paper citation network.",
    "endpoint": "/api/v1/research/citations",
    "facets": ["entities", "library"],
    "tables": ["documents"],
    "ui_flag": "research",
    "default_span": 6,
    "topic_param": "topic",
    "days_param": "days",
    "max_days": 90,
}


class FakeHost:
    def __init__(self, tools_by_server: Dict[str, List[dict]]):
        self._tools = tools_by_server

    def tools(self, server=None):
        if server is not None:
            return {server: self._tools.get(server, [])}
        return dict(self._tools)


@pytest.fixture
def fake_host(monkeypatch):
    def _install(tools_by_server):
        host = FakeHost(tools_by_server)
        monkeypatch.setattr("src.mcp_host.get_host", lambda: host)
        return host

    return _install


@pytest.fixture
def no_host(monkeypatch):
    monkeypatch.setattr("src.mcp_host.get_host", lambda: None)


# ---------------------------------------------------------------------------
# Annotation validation (ADR-001 rules)
# ---------------------------------------------------------------------------


def test_valid_annotation_builds_panel_def():
    panel = panel_def_from_annotation("research", make_tool(GOOD))
    assert panel is not None
    assert panel.type == "custom_widget"
    assert panel.title == "Citation graph"
    assert panel.endpoint == "/api/v1/research/citations"
    assert panel.facets == ("entities", "library")
    assert panel.tables == ("documents",)
    assert panel.ui_flag == "research"
    assert panel.default_span == 6
    assert panel.topic_param == "topic"
    assert panel.source_type_param is None
    assert panel.days_param == "days"
    assert panel.max_days == 90


def test_title_and_description_default():
    panel = panel_def_from_annotation(
        "s", make_tool({"type": "my_new_panel", "facets": ["overview"]})
    )
    assert panel.title == "My New Panel"
    assert panel.description == "a tool description"
    assert panel.default_span == 6


def test_unannotated_tool_is_invisible():
    assert panel_def_from_annotation("s", make_tool(meta={})) is None
    assert panel_def_from_annotation("s", make_tool(meta={"other": 1})) is None


def test_output_schema_is_required():
    assert (
        panel_def_from_annotation("s", make_tool(GOOD, has_output_schema=False)) is None
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"type": None},
        {"type": ""},
        {"type": "Bad-Type"},
        {"type": "x" * 80},
        {"facets": []},
        {"facets": "overview"},
        {"facets": [1]},
        {"default_span": 2},
        {"default_span": 13},
        {"default_span": "6"},
        {"default_span": True},
        {"tables": "documents"},
        {"tables": [1]},
        {"max_days": 0},
        {"max_days": "30"},
        {"max_days": True},
    ],
)
def test_malformed_blocks_are_skipped(mutation):
    block = {**GOOD, **mutation}
    assert panel_def_from_annotation("s", make_tool(block)) is None


def test_non_dict_block_is_skipped():
    assert panel_def_from_annotation("s", make_tool(meta={"panel": "claims"})) is None


# ---------------------------------------------------------------------------
# Discovery + merge semantics
# ---------------------------------------------------------------------------


def test_no_host_yields_no_discovery(no_host):
    assert discovered_panel_defs() == {}
    assert merged_catalog_dict() == panel_catalog_dict()


def test_duplicate_type_first_server_wins(fake_host):
    fake_host(
        {
            "b-server": [make_tool({**GOOD, "title": "From B"})],
            "a-server": [make_tool({**GOOD, "title": "From A"})],
        }
    )
    defs = discovered_panel_defs()
    assert defs["custom_widget"][0].title == "From A"
    assert defs["custom_widget"][1] == "a-server"


def test_merge_overrides_in_place_and_appends_new(fake_host):
    override = {
        "type": "claims",
        "title": "Discovered claims",
        "facets": ["claims"],
        "default_span": 6,
    }
    fake_host({"srv": [make_tool(override), make_tool(GOOD, name="t2")]})

    merged = merged_catalog()
    types = [p.type for p, _ in merged]
    static_types = [p.type for p in PANEL_CATALOG]
    # Same order as static, with the new type appended at the end.
    assert types == static_types + ["custom_widget"]

    by_type = {p.type: (p, src) for p, src in merged}
    assert by_type["claims"][0].title == "Discovered claims"
    assert by_type["claims"][1] == "srv"
    assert by_type["custom_widget"][1] == "srv"
    # Untouched static entries carry no source server.
    assert by_type["kpi_row"][1] is None


def test_merged_dict_carries_source_only_when_discovery_contributes(fake_host):
    fake_host({"srv": [make_tool(GOOD)]})
    panels = merged_catalog_dict()
    by_type = {p["type"]: p for p in panels}
    assert by_type["custom_widget"]["source"] == "srv"
    assert by_type["claims"]["source"] == "static"
    # Every static field is still present alongside source.
    assert set(panel_catalog_dict()[0]) | {"source"} == set(by_type["claims"])


def test_servers_down_is_byte_identical(fake_host):
    # Host present but its caches are empty (all servers down / unannotated).
    fake_host({"srv": []})
    import json

    assert json.dumps(merged_catalog_dict(), sort_keys=True) == json.dumps(
        panel_catalog_dict(), sort_keys=True
    )
    assert merged_catalog_dict() == panel_catalog_dict()
