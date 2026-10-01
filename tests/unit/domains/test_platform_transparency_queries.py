"""Political ads by advertiser or election and moderation statements by platform, ground and period (#2626,
#2630)."""

from __future__ import annotations

import json

import pytest

from src.kb.platform_transparency_links import PlatformTransparencyLinks
from src.kb.platform_transparency_queries import PlatformTransparencyQueries
from src.kb.platform_transparency_records import forbidden_keys
from tests.unit import platform_transparency_harness as h


@pytest.fixture(scope="module")
def world():
    conn = h.accepted_world(version="v2")
    PlatformTransparencyLinks(conn).link_all(h.NS, principal_id="alice", scopes=h.SCOPES,
                                             ownership_namespace=h.OWN_NS)
    return conn


def test_ads_for_a_committee_reach_the_advertiser_through_accepted_identity_with_ranges_as_published(world):
    answer = PlatformTransparencyQueries(world).ads_for_advertiser(h.NS, "C00999901", scopes=h.SCOPES)
    assert answer["status"] == "answered" and answer["subjects"][0]["method"] == "published-id"
    ads = {a["ad_id"]: a for a in answer["ads"]}
    assert set(ads) == {"CR10000000000000000001", "CR10000000000000000002", "CR10000000000000000003",
                        "CR10000000000000000004"}
    revised = ads["CR10000000000000000001"]
    assert revised["impressions_as_published"] == "1M-10M"
    assert revised["spend_as_published"]["ranges"] == [{"currency": "USD", "lower_bound": "1000",
                                                       "upper_bound": "50000"}]
    assert [r["change"] for r in revised["revisions"]] == ["new", "revised"]
    removed = ads["CR10000000000000000002"]
    assert removed["listing_status"] == "not-returned" and removed["removal"]["revision_id"] == removed["revision_id"]
    assert answer["counts"] == {"ads": 4, "listed": 3, "not_returned": 1}
    assert all(a["citation"]["revision_id"] and a["citation"]["observed_at"] for a in answer["ads"])
    assert "never converted" in answer["ranges_note"]
    assert forbidden_keys(answer) == []
    text = json.dumps(answer["ads"])
    assert '"midpoint' not in text and "total_spend" not in text and "estimate" not in text


def test_as_of_answers_cite_the_revision_on_record_and_meta_ranges_stay_open_when_published_open(world):
    ask = PlatformTransparencyQueries(world)
    before = ask.ads_for_advertiser(h.NS, "C00999901", scopes=h.SCOPES, as_of="2099-11-20")
    assert {a["ad_id"]: a["impressions_as_published"] for a in before["ads"]}["CR10000000000000000001"] == "100k-1M"
    assert all(a["listing_status"] == "listed" for a in before["ads"]) and len(before["ads"]) == 3
    fund = ask.ads_for_advertiser(h.NS, h.META_FUND, scopes=h.SCOPES)
    open_range = {a["ad_id"]: a for a in fund["ads"]}["880000000000011"]["impressions_as_published"]
    assert open_range == {"lower_bound": "1000000", "upper_bound": None}
    funding = ask.ads_for_advertiser(h.NS, "Paid for by Example Holdings Ltd", scopes=h.SCOPES)
    assert {a["ad_id"] for a in funding["ads"]} == {"880000000000001", "880000000000002", "880000000000003"}


def test_ads_for_an_election_group_by_advertiser_and_a_subject_without_records_is_never_a_clean_bill(world):
    ask = PlatformTransparencyQueries(world)
    answer = ask.ads_for_election(h.NS, h.UK_ELECTION, scopes=h.SCOPES)
    assert answer["election"] == {"name": "UK Parliamentary General Election 2099", "date": "2099-05-07"}
    groups = {g["advertiser_key"]: g["ads"] for g in answer["advertisers"]}
    assert set(groups) == {"platform-transparency:google:advertiser:AR10000000000000000003",
                           "platform-transparency:meta:advertiser:999000001"}
    assert {a["link"]["basis"] for ads in groups.values() for a in ads} == {"declared-selection",
                                                                            "published-election-label"}
    none = ask.ads_for_advertiser(h.NS, "Unknown Example Org", scopes=h.SCOPES)
    assert none["status"] == "none_on_record" and "not evidence" in none["note"]
    assert ask.ads_for_election(h.NS, "xx-none:2099", scopes=h.SCOPES)["status"] == "none_on_record"


def test_moderation_counts_state_the_window_dump_versions_and_flags_as_published(world):
    ask = PlatformTransparencyQueries(world)
    answer = ask.moderation_statements(h.NS, "Exampla Social", scopes=h.SCOPES, start="2099-05-01", end="2099-05-03")
    assert answer["total"] == 5
    assert answer["by_ground"] == {"DECISION_GROUND_ILLEGAL_CONTENT": 2, "DECISION_GROUND_INCOMPATIBLE_CONTENT": 3}
    assert answer["automated_detection_as_published"] == {"No": 1, "Yes": 4}
    assert answer["automated_decision_as_published"] == {"AUTOMATED_DECISION_FULLY": 3,
                                                         "AUTOMATED_DECISION_PARTIALLY": 1, "NOT_AUTOMATED": 1}
    assert answer["by_category"]["STATEMENT_CATEGORY_VIOLENCE"] == 1  # the corrected revision counts
    assert answer["stored_window"]["days_without_dump"] == ["2099-05-03"]
    assert [(v["day"], v["revision_no"]) for v in answer["dump_versions"]] == [("2099-05-01", 2), ("2099-05-02", 1)]
    assert len(answer["withdrawn_from_republished_dumps"]) == 1 and "not the platform's totals" in answer["note"]
    ground = ask.moderation_statements(h.NS, "exampla-social", scopes=h.SCOPES, start="2099-05-01",
                                       end="2099-05-31", ground="DECISION_GROUND_ILLEGAL_CONTENT")
    assert ground["total"] == 2
    bundle = ask.evidence_bundle(answer)
    assert {a["id"] for a in bundle["sections"][0]["assertions"]} >= {
        "dump-sor-exampla-social-2099-05-01-light.zip", "counts"}
    assert len(bundle["bibliography"]) == 2
    empty = ask.moderation_statements(h.NS, "Northwind Video", scopes=h.SCOPES, start="2099-06-01", end="2099-06-02")
    assert empty["status"] == "none_on_record" and empty["stored_window"]["days_without_dump"] == [
        "2099-06-01", "2099-06-02"]


def test_takedown_notices_are_counted_without_the_notices_scope_and_bundles_cite_every_ad(world):
    ask = PlatformTransparencyQueries(world)
    counted = ask.takedown_notices(h.NS, "Exampla Social", scopes=h.SCOPES)
    assert counted["status"] == "counted" and counted["count"] == 3 and "notices" not in counted
    shown = ask.takedown_notices(h.NS, "Exampla Social", scopes=h.REVIEW_SCOPES)
    assert shown["status"] == "answered" and {n["sender_name_as_published"] for n in shown["notices"]} >= {
        "[Private]"}
    answer = ask.ads_for_advertiser(h.NS, "C00999901", scopes=h.SCOPES)
    bundle = ask.evidence_bundle(answer)
    cited = {a["citations"][0] for a in bundle["sections"][0]["assertions"]}
    assert cited == {a["revision_id"] for a in answer["ads"]}
    assert all("observed" in b["text"] and "source data as of" in b["text"] for b in bundle["bibliography"])
