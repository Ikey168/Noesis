from __future__ import annotations

import asyncio
import base64
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _register(tools, native="figure"):
    return _call(
        tools["register_multimodal_asset"],
        namespace="osint",
        source_id="report:1",
        native_id=native,
        version="1",
        asset_type="image",
        media_type="image/png",
        bytes_base64=base64.b64encode(native.encode()).decode(),
        perceptual_hash="same",
        metadata={"width": 10, "height": 10},
        segments=[
            {
                "kind": "region",
                "locator": {"region": {"x": 0, "y": 0, "width": 5, "height": 5}},
            }
        ],
    )


def test_multimodal_mcp_binary_search_extract_citations_provenance_and_auth(
    tmp_path, monkeypatch
):
    database = tmp_path / "media.duckdb"
    scopes = {"knowledge:multimodal:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "register_multimodal_asset",
        "get_multimodal_asset",
        "search_multimodal_assets",
        "get_multimodal_segment",
        "extract_multimodal_observations",
        "replay_multimodal_extraction",
        "link_cross_modal_evidence",
        "record_media_transformation",
        "assess_media_authenticity",
        "inspect_media_provenance",
    }
    assert (
        expected <= tools.keys() and _register(tools)["error"]["code"] == "unauthorized"
    )
    scopes.add("knowledge:multimodal:write")
    first = _register(tools)
    second = _register(tools, "mirror")
    assert first["asset_id"] in second["duplicate_asset_ids"]
    assert _call(tools["search_multimodal_assets"], namespace="osint", query="figure")[
        "items"
    ]
    assert _call(
        tools["get_multimodal_segment"],
        namespace="osint",
        asset_id=first["asset_id"],
        segment_id=first["segments"][0]["segment_id"],
    )["evidence_locator"]
    denied = _call(
        tools["extract_multimodal_observations"],
        namespace="osint",
        asset_id=first["asset_id"],
        extractor="ocr",
        observations=[],
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.add("knowledge:multimodal:extract")
    receipt = _call(
        tools["extract_multimodal_observations"],
        namespace="osint",
        asset_id=first["asset_id"],
        extractor="ocr",
        observations=[
            {
                "kind": "text",
                "value": "caption",
                "locator": {"region": {"x": 0}},
                "confidence": 0.8,
            }
        ],
    )
    assert _call(
        tools["replay_multimodal_extraction"],
        namespace="osint",
        extraction_id=receipt["extraction_id"],
    )["deterministic"]
    link = _call(
        tools["link_cross_modal_evidence"],
        namespace="osint",
        observation_id=receipt["items"][0]["observation_id"],
        target_type="event",
        target_id="event:1",
        relation="depicts",
        stance="mentions",
        confidence=0.8,
    )
    assert link["verification_status"] == "unverified-extraction"
    _call(
        tools["record_media_transformation"],
        namespace="osint",
        parent_asset_id=first["asset_id"],
        child_asset_id=second["asset_id"],
        operation="mirror",
        parameters={},
    )
    denied_review = _call(
        tools["assess_media_authenticity"],
        namespace="osint",
        asset_id=second["asset_id"],
        finding="inconclusive",
        confidence=0.5,
    )
    assert denied_review["error"]["code"] == "unauthorized"
    scopes.add("knowledge:multimodal:review")
    _call(
        tools["assess_media_authenticity"],
        namespace="osint",
        asset_id=second["asset_id"],
        finding="inconclusive",
        confidence=0.5,
        uncertainty="offline fixture",
    )
    provenance = _call(
        tools["inspect_media_provenance"],
        namespace="osint",
        asset_id=second["asset_id"],
    )
    assert provenance["transformations"] and provenance["authenticity"]


def test_multimodal_catalog_scopes_and_capabilities():
    assert _mutability("extract_multimodal_observations") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "extract_multimodal_observations"
    ) == ["knowledge:multimodal:extract"]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "inspect_media_provenance"
    ) == ["knowledge:multimodal:read"]
    capabilities = server.knowledge_engine_capabilities.fn()
    assert "noesis-multimodal-asset-v1" in capabilities["contracts"]
    assert (
        "media-transformation-and-authenticity-provenance" in capabilities["features"]
    )
