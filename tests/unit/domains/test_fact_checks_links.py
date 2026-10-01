"""Fact-checks linked to news articles, argument claims, OSINT corroboration and claim timelines (#2693, FC07)."""

from __future__ import annotations

from src.kb.claim_timelines import READ_SCOPE as TIMELINE_READ
from src.kb.claim_timelines import WRITE_SCOPE as TIMELINE_WRITE
from src.kb.claim_timelines import ClaimTimelineStore
from src.kb.fact_checks_links import FactCheckLinks
from src.kb.fact_checks_records import forbidden_keys
from tests.unit import fact_checks_harness as h


def test_links_record_their_basis_point_at_revisions_and_report_missing_targets():
    conn = h.accepted_world()
    ClaimTimelineStore(conn).capture_state(
        h.NS, "claim-seals", principal_id="analyst", scopes={TIMELINE_READ, TIMELINE_WRITE}, source_id="doc-seals",
        source_revision_id="sha256:doc-seals", evidence=[{"citation": "doc-seals"}])
    result = FactCheckLinks(conn).link(h.NS, principal_id="alice", scopes=h.REVIEW_SCOPES)
    assert result["status"] == "linked"
    assert result["created"]["news-article"] == 2 and result["created"]["argument-claim"] == 2
    assert result["created"]["claim-timeline"] == 2
    links = FactCheckLinks(conn).links(h.NS, scopes=h.SCOPES)
    for link in links:
        assert link["revision_id"].startswith("fc-rev:") and link["target_revision"]
        assert link["basis"] in {"citation", "accepted-match"}
    news = [link for link in links if link["link_kind"] == "news-article"]
    assert {link["target_key"] for link in news} == {"document:doc-seals"}
    assert {link["basis"] for link in news} == {"citation"}
    assert any("drop-tracking-parameters" in link["detail"]["rules_applied"] for link in news)
    claims = [link for link in links if link["link_kind"] == "argument-claim"]
    assert all(link["basis"] == "accepted-match" and link["detail"]["match_id"] for link in claims)
    timelines = [link for link in links if link["link_kind"] == "claim-timeline"]
    assert all(link["target_revision"].startswith("claim-state:") for link in timelines)
    missing = {(m["kind"], m.get("url")) for m in result["missing_targets"]}
    assert ("news-article", "https://claimwatch.example.com/fact-check/turbine-photo") in missing
    assert forbidden_keys(links) == []
    again = FactCheckLinks(conn).link(h.NS, principal_id="alice", scopes=h.REVIEW_SCOPES)
    assert sum(again["created"].values()) == 0  # idempotent


def test_osint_corroboration_is_referenced_without_copying_grades():
    conn = h.accepted_world()
    FactCheckLinks(conn).link(h.NS, principal_id="alice", scopes=h.REVIEW_SCOPES)
    links = FactCheckLinks(conn).links(h.NS, scopes=h.SCOPES, kind="osint-corroboration")
    assert {link["target_key"] for link in links} == {"osint-corroboration:claim-seals"}
    for link in links:
        assert not set(link["detail"]) & {"credibility_grade", "source_reliability_grade", "grading", "claim",
                                          "verdict", "support", "contradict"}
        assert link["detail"]["result_digest"] == link["target_revision"]


def test_missing_providers_are_reported_not_dropped():
    conn = h.accepted_world(news=False)
    result = FactCheckLinks(conn).link(h.NS, principal_id="alice", scopes=h.REVIEW_SCOPES)
    assert result["status"] == "nothing_to_link" and sum(result["created"].values()) == 0
    assert {u["target"] for u in result["unavailable"]} == {"documents"}
    assert result["missing_targets"] and all(m["reason"] for m in result["missing_targets"])
