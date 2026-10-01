"""Platform-transparency links to elections, campaign-finance filings and lobbying registers (#2621)."""

from __future__ import annotations

from src.kb.platform_transparency_identity import PlatformTransparencyIdentity
from src.kb.platform_transparency_links import (
    PlatformTransparencyLinks,
    delivery_relation,
)
from tests.unit import platform_transparency_harness as h

FUND = f"platform-transparency:google:advertiser:{h.FUND}"
PARTY = f"platform-transparency:meta:advertiser:{h.PARTY_PAGE}"


def linked_world():
    conn = h.accepted_world()
    h.link_campaign_finance_contests(conn)
    return conn, PlatformTransparencyLinks(conn)


def test_campaign_finance_links_point_at_ad_and_filing_revisions_with_their_basis():
    _conn, links = linked_world()
    result = links.link_campaign_finance(h.NS, principal_id="alice", scopes=h.SCOPES,
                                         campaign_finance_namespace=h.CF_NS)
    assert result["status"] == "linked" and result["missing_targets"] == []
    fund = [link for link in result["links"] if link["subject_key"] == FUND]
    assert {link["target_key"] for link in fund} == {"campaign-finance:fec:filing:1500301",
                                                      "campaign-finance:fec:filing:1500310",
                                                      "campaign-finance:fec:filing:1500390"}
    for link in fund:
        assert link["basis"]["method"] == "published-id" and link["basis"]["identity_candidate_id"]
        assert link["record_revision_id"].startswith("pt-rev:") and link["target_revision"].startswith("cf-rev:")
        assert link["basis"]["target_citation"]["revision_id"] == link["target_revision"]
        assert "not evidence of coordination" in link["notice"]
    party = {link["target_key"] for link in result["links"] if link["subject_key"] == PARTY}
    assert party == {"campaign-finance:ukec:filing:donations:9901:q1-2099",
                     "campaign-finance:ukec:filing:spending:9901:uk-parliamentary-general-election-2099"}
    again = links.link_campaign_finance(h.NS, principal_id="alice", scopes=h.SCOPES,
                                        campaign_finance_namespace=h.CF_NS)
    assert again["linked"] == 0 and len(again["links"]) == len(result["links"])  # idempotent


def test_election_links_follow_party_lists_and_cited_campaign_finance_contest_links():
    _conn, links = linked_world()
    result = links.link_elections(h.NS, principal_id="alice", scopes=h.SCOPES, elections_namespace=h.CF_NS,
                                  campaign_finance_namespace=h.CF_NS)
    assert result["status"] == "linked"
    uk = [link for link in result["links"] if link["target_key"] == "gb-general:2099-05-07"]
    assert {link["subject_key"] for link in uk} == {PARTY, "platform-transparency:meta:funder:example-party:gb"}
    assert all(link["basis"]["via"] == "party list" and link["basis"]["contests"] for link in uk)
    assert {link["basis"]["delivery_relative_to_election_day"] for link in uk} == {"before"}
    us = [link for link in result["links"] if link["subject_key"] == FUND]
    assert {link["target_key"] for link in us} == {"us-us-president:2099"}
    assert all(link["basis"]["via"] == "campaign-finance contest link" and link["basis"]["campaign_finance_links"]
               for link in us)
    # an accepted UK committee match without a campaign-finance contest link is reported, not dropped
    assert {m["target_key"] for m in result["missing_targets"]} == {"campaign-finance:ukec:entity:9901"}


def test_lobbying_links_cite_the_register_revision_in_force():
    _conn, links = linked_world()
    result = links.link_lobbying(h.NS, principal_id="alice", scopes=h.SCOPES, lobbying_namespace=h.CF_NS)
    assert result["status"] == "linked"
    assert {link["target_key"] for link in result["links"]} == {"lobbying:uk-orcl:ORCL0099"}
    assert all(link["target_revision"] == link["basis"]["register_revision_id"] for link in result["links"])
    listed = links.links(h.NS, scopes=h.SCOPES, kind="lobbying")
    assert len(listed) == len(result["links"])


def test_missing_providers_degrade_gracefully_and_are_reported():
    conn = h.world(elections=False, lobbying=False, campaign_finance=False, ownership=False)
    links = PlatformTransparencyLinks(conn)
    assert links.link_elections(h.NS, principal_id="alice", scopes=h.SCOPES)["status"] == "elections_unavailable"
    assert links.link_lobbying(h.NS, principal_id="alice", scopes=h.SCOPES)["status"] == "lobbying_unavailable"
    assert links.link_campaign_finance(h.NS, principal_id="alice", scopes=h.SCOPES)["status"] == \
        "campaign_finance_unavailable"


def test_without_accepted_matches_nothing_is_linked_and_subjects_are_listed_unmatched():
    conn = h.world()
    PlatformTransparencyIdentity(conn).propose(h.NS, principal_id="alice", scopes=h.SCOPES,
                                               campaign_finance_namespace=h.CF_NS)
    result = PlatformTransparencyLinks(conn).link_campaign_finance(h.NS, principal_id="alice", scopes=h.SCOPES,
                                                                   campaign_finance_namespace=h.CF_NS)
    assert result["status"] == "none_on_record" and FUND in result["unmatched_subjects"]


def test_delivery_relation_is_stated_from_published_dates_only():
    assert delivery_relation({"ad_delivery_start_time": "2099-04-01", "ad_delivery_stop_time": "2099-05-01"},
                             "2099-05-07") == "before"
    assert delivery_relation({"date_range_start": "2099-05-01", "date_range_end": "2099-05-09"},
                             "2099-05-07") == "spanning"
    assert delivery_relation({"ad_delivery_start_time": "2099-05-08"}, "2099-05-07") == "after"
    assert delivery_relation({"ad_delivery_start_time": "2099-04-01"}, "2099-05-07") == \
        "started-before-no-stop-published"
    assert delivery_relation({}, "2099-05-07") is None
