"""Fact-checks linked to news articles, claim timelines, OSINT corroboration and source identities (#2693)."""

from __future__ import annotations

from src.kb.fact_checks_links import FactCheckLinks
from src.kb.fact_checks_records import forbidden_keys
from tests.unit import fact_checks_harness as h


def test_links_record_their_basis_and_point_at_specific_revisions():
    conn = h.accepted_world()
    result = FactCheckLinks(conn).link(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert result["status"] == "linked" and result["unavailable"] == []
    kinds = {link["link_kind"] for link in result["links"]}
    assert kinds == {"news-article", "claim-timeline", "osint-corroboration", "source-identity"}
    (article,) = [link for link in result["links"] if link["link_kind"] == "news-article"]
    assert article["basis"]["kind"] == "citation" and article["basis"]["url_rules"] == "wa-canon-v1"
    assert article["target_key"] == "doc-widgets" and article["target_revision"] == "document-revision:doc-widgets:2"
    assert article["record_revision_id"].startswith("fc-rev:") and article["source_id"] == h.DATACOMMONS
    for link in result["links"]:
        assert link["record_revision_id"] and link["target_revision"]
        if link["link_kind"] != "news-article":
            assert link["basis"]["kind"] == "accepted-match" and link["basis"]["decision_id"]
    timeline = [link for link in result["links"] if link["link_kind"] == "claim-timeline"]
    assert {link["target_key"] for link in timeline} == {"claim-widgets"}  # the rejected claim is never linked
    assert all(link["target_revision"].startswith("claim-state:") for link in timeline)
    assert forbidden_keys(result) == []
    again = FactCheckLinks(conn).link(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert again["created"] == 0 and len(again["links"]) == len(result["links"])


def test_missing_targets_and_absent_providers_are_reported_not_dropped():
    conn = h.accepted_world()
    result = FactCheckLinks(conn).link(h.NS, principal_id="alice", scopes=h.SCOPES)
    missing = [m for m in result["missing_targets"] if m["kind"] == "news-article"]
    assert {m["cited_url"] for m in missing} == {"https://news.example/2099/06/28/bus-fares"}
    bare = h.world(news=False, sources=False)
    bare_result = FactCheckLinks(bare).link(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert bare_result["status"] == "none_linked"
    assert {u["provider"] for u in bare_result["unavailable"]} == {"news.core"}
    assert bare_result["missing_targets"]  # every cited URL is reported


def test_links_are_listed_by_kind_record_and_target():
    conn = h.accepted_world()
    links = FactCheckLinks(conn)
    links.link(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert {link["link_kind"] for link in links.links(h.NS, scopes=h.READ_ONLY, target_key="claim-widgets")} == {
        "claim-timeline", "osint-corroboration"}
    assert all(link["notice"] for link in links.links(h.NS, scopes=h.READ_ONLY, kind="source-identity"))
