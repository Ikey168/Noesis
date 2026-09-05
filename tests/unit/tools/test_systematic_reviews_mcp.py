import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_screening_public_tools_preserve_independent_reviews(tmp_path, monkeypatch):
    path = str(tmp_path / "review.duckdb")
    actor = ["coordinator"]
    scopes = {"knowledge:reviews:read", "knowledge:reviews:write", "namespace:r:write"}
    monkeypatch.setattr(server, "_context", lambda: (actor[0], scopes))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    content = {"question": "Which studies qualify?", "inclusion": ["Controlled"], "exclusion": ["Editorial"],
        "databases": ["Literature"], "search_expressions": ["Controlled study"], "date_from": "2020-01-01", "date_to": "2026-09-05",
        "reviewers": ["alice", "bob"], "fields": ["population"]}
    protocol = tools["create_review_protocol"].fn(namespace="r", request_key="p", content=content)
    pid = protocol["protocol_id"]
    item = tools["add_review_candidate"].fn(namespace="r", protocol_id=pid, protocol_revision=1,
        publication_id="paper", source_revision="v1", source_namespace="r", search_run_id="search", study_id="study",
        title="Study", abstract="Abstract", full_text_available=False)
    cid = item["candidate_id"]
    actor[0] = "alice"
    tools["screen_review_candidate"].fn(namespace="r", candidate_id=cid, stage="title_abstract", expected_revision=0, decision="include", reason="Eligible")
    actor[0] = "bob"
    listed = tools["list_review_candidates"].fn(namespace="r", protocol_id=pid)
    assert listed["candidates"][0]["screening"]["title_abstract"] == {"own_decisions": [], "other_reviews_hidden": True}
    tools["screen_review_candidate"].fn(namespace="r", candidate_id=cid, stage="title_abstract", expected_revision=0, decision="include", reason="Eligible")
    actor[0] = "coordinator"
    assert tools["export_systematic_review"].fn(namespace="r", protocol_id=pid)["counts"] == {"full_text_unavailable": 1}
    for name in ("amend_review_protocol", "screen_review_candidate", "adjudicate_review_candidate", "extract_review_field", "review_study_field"):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == ["knowledge:reviews:write"]
