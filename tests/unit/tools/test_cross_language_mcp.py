from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_cross_language_mcp_flow_and_authorization(tmp_path, monkeypatch):
    db = tmp_path / "cross-language.duckdb"
    scopes = {"knowledge:cross-language:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(db))
    )
    tools = asyncio.run(server.mcp.get_tools())
    names = {
        "record_language_text",
        "get_original_language_text",
        "record_multilingual_alias",
        "review_multilingual_alias",
        "align_cross_language_claims",
        "review_cross_language_alignment",
        "compare_cross_language_claims",
        "record_translation",
        "review_translation",
        "multilingual_search",
    }
    assert names <= tools.keys()
    denied = call(
        tools["record_language_text"],
        namespace="osint",
        object_type="claim",
        object_id="c1",
        original_text="Bonjour",
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.add("knowledge:cross-language:write")
    one = call(
        tools["record_language_text"],
        namespace="osint",
        object_type="claim",
        object_id="c1",
        original_text="Bonjour",
        language="fr",
        script="Latn",
    )
    two = call(
        tools["record_language_text"],
        namespace="osint",
        object_type="claim",
        object_id="c2",
        original_text="Hello",
        language="en",
        script="Latn",
    )
    aligned = call(
        tools["align_cross_language_claims"],
        namespace="osint",
        source_claim_id="c1",
        target_claim_id="c2",
        relation="translated",
        source_text_id=one["text_id"],
        target_text_id=two["text_id"],
        confidence=0.9,
    )
    translated = call(
        tools["record_translation"],
        namespace="osint",
        source_text_id=one["text_id"],
        target_language="en",
        translated_text="Hello",
        producer={"kind": "human", "id": "h1"},
        confidence=0.95,
    )
    assert call(tools["multilingual_search"], namespace="osint", query="Hello")[
        "results"
    ]
    assert (
        call(
            tools["compare_cross_language_claims"],
            namespace="osint",
            alignment_id=aligned["alignment_id"],
        )["source_text"]["original_text"]
        == "Bonjour"
    )
    assert translated["source_original_text"] == "Bonjour"
    scopes.add("knowledge:cross-language:review")
    reviewed = call(
        tools["review_translation"],
        namespace="osint",
        translation_id=translated["translation_id"],
        decision="accepted",
        reviewer_id="h2",
    )
    assert reviewed["status"] == "accepted"


def test_cross_language_catalog():
    assert _mutability("align_cross_language_claims") == "write"
    assert _mutability("multilingual_search") == "read"
    assert _required_scopes("knowledge_engine_mcp", "read", "multilingual_search") == [
        "knowledge:cross-language:read"
    ]
    assert _required_scopes("knowledge_engine_mcp", "write", "review_translation") == [
        "knowledge:cross-language:review"
    ]
    assert (
        "noesis-translation-record-v1"
        in server.knowledge_engine_capabilities.fn()["contracts"]
    )
