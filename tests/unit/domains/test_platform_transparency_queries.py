"""Platform-transparency answers: ads by advertiser or election, moderation statements by platform, ground and
period (#2626, #2630)."""

from __future__ import annotations

import json

from src.kb.platform_transparency_links import PlatformTransparencyLinks
from src.kb.platform_transparency_queries import PlatformTransparencyQueries
from src.kb.platform_transparency_records import forbidden_keys
from tests.unit import platform_transparency_harness as h

FUND = f"platform-transparency:google:advertiser:{h.FUND}"
CAMPAIGN = f"platform-transparency:google:advertiser:{h.CAMPAIGN}"
POINT_WORDS = ("midpoint", "point estimate", "estimated spend", "coordinated", "profile")


def no_point_estimates(answer):
    assert forbidden_keys(answer) == []
    text = json.dumps({k: v for k, v in answer.items() if k not in {"exclusions", "ranges_notice"}}).lower()
    assert not [w for w in POINT_WORDS if w in text], text


def ask_world(version="v1"):
    conn = h.accepted_world(version=version)
    h.link_campaign_finance_contests(conn)
    links = PlatformTransparencyLinks(conn)
    links.link_elections(h.NS, principal_id="alice", scopes=h.SCOPES, elections_namespace=h.CF_NS,
                         campaign_finance_namespace=h.CF_NS)
    return conn, PlatformTransparencyQueries(conn)


def test_ads_by_page_id_keep_ranges_as_published_and_cite_each_revision():
    _conn, ask = ask_world()
    answer = ask.ads_by_advertiser(h.NS, h.PARTY_PAGE, scopes=h.SCOPES)
    assert answer["status"] == "answered"
    (group,) = answer["advertisers"]
    assert group["identity"]["state"] == "matched" and group["path"][0]["via"] == "subject key or published id"
    ads = {a["ad_id"]: a for a in group["ads"]}
    assert ads["990000000000102"]["spend_range_as_published"]["as_published"] == {"lower_bound": "1000",
                                                                                  "upper_bound": "1499"}
    assert ads["990000000000102"]["currency_as_published"] == "GBP"
    for ad in group["ads"]:
        assert ad["citation"]["revision_id"] == ad["revision"]["revision_id"] and ad["citation"]["observed_at"]
        assert ad["citation"]["source_id"] == h.META
    no_point_estimates(answer)


def test_ads_reached_from_a_committee_id_only_through_accepted_identity_with_the_path():
    _conn, ask = ask_world()
    answer = ask.ads_by_advertiser(h.NS, "C00999903", scopes=h.SCOPES)
    (group,) = answer["advertisers"]
    assert group["subject_key"] == FUND
    assert [s["step"] for s in group["path"]] == ["record", "identity"]
    assert group["path"][1]["method"] == "published-id"
    assert sorted(a["ad_id"] for a in group["ads"]) == ["CR00000000000000000101", "CR00000000000000000102",
                                                       "CR00000000000000000103"]
    ad = next(a for a in group["ads"] if a["ad_id"] == "CR00000000000000000101")
    assert ad["impressions_range_as_published"] == {"as_published": "10k-100k"}
    assert ad["citation"]["source_as_of"] == "2099-06-01T00:00:00Z"  # the data refresh date of the record
    no_point_estimates(answer)
    by_name = ask.ads_by_advertiser(h.NS, "Example Holdings Ltd", scopes=h.SCOPES)
    assert by_name["advertisers"][0]["path"][0]["via"] == "declared name as published"


def test_removed_ads_are_shown_with_their_removal_revision_and_as_of_answers_differ():
    _conn, ask = ask_world(version="v2")
    answer = ask.ads_by_advertiser(h.NS, h.HOLDINGS_PAGE, scopes=h.SCOPES)
    (ad,) = answer["advertisers"][0]["ads"]
    assert ad["listing_state"] == "not-returned" and ad["removal"]["revision_id"] == ad["revision"]["revision_id"]
    assert [r["change"] for r in ad["revisions"]] == ["new", "not-returned"]
    assert "did not state why" in answer["removal_notice"]
    earlier = ask.ads_by_advertiser(h.NS, h.HOLDINGS_PAGE, scopes=h.SCOPES, as_of=h.V1_MS + 1)
    assert earlier["advertisers"][0]["ads"][0]["listing_state"] == "listed"
    party = ask.ads_by_advertiser(h.NS, h.PARTY_PAGE, scopes=h.SCOPES, as_of=h.V1_MS + 1)
    assert len(party["advertisers"][0]["ads"]) == 2  # the ad first returned by the later acquisition is not yet known
    assert answer["advertisers"][0]["ads_not_returned"] == 1


def test_an_advertiser_with_no_records_is_none_on_record():
    _conn, ask = ask_world()
    assert ask.ads_by_advertiser(h.NS, "999999999999999", scopes=h.SCOPES)["status"] == "none_on_record"
    civic = ask.ads_by_advertiser(h.NS, h.CIVIC, scopes=h.SCOPES)
    assert civic["status"] == "none_on_record" and civic["advertisers"][0]["identity"]["state"] == "unmatched"


def test_ads_for_an_election_are_grouped_by_advertiser_with_link_basis():
    _conn, ask = ask_world()
    uk = ask.ads_for_election(h.NS, "gb-general:2099-05-07", scopes=h.SCOPES)
    assert uk["status"] == "answered"
    subjects = {g["subject_key"] for g in uk["advertisers"]}
    assert f"platform-transparency:meta:advertiser:{h.PARTY_PAGE}" in subjects
    for group in uk["advertisers"]:
        for ad in group["ads"]:
            assert ad["link"]["basis"]["via"] == "party list" and ad["citation"]["revision_id"]
    us = ask.ads_for_election(h.NS, "us-us-president:2099", scopes=h.SCOPES)
    assert {g["subject_key"] for g in us["advertisers"]} == {FUND, CAMPAIGN}
    no_point_estimates(us)
    assert ask.ads_for_election(h.NS, "de-bt:2099-03-01", scopes=h.SCOPES)["status"] == "none_on_record"


def test_moderation_counts_state_the_window_the_dump_versions_and_the_flags_as_published():
    _conn, ask = ask_world()
    answer = ask.moderation_statements(h.NS, "Example Video", scopes=h.SCOPES, start="2099-05-01", end="2099-05-03")
    assert answer["status"] == "answered" and answer["platform"] == "example-video"
    assert answer["statements_counted"] == 6
    counts = answer["counts"]
    assert counts["by_decision_ground"] == {"DECISION_GROUND_ILLEGAL_CONTENT": 2,
                                           "DECISION_GROUND_INCOMPATIBLE_CONTENT": 4}
    assert counts["by_decision_type"]["decision_visibility"] == {"DECISION_VISIBILITY_CONTENT_DEMOTED": 1,
                                                                 "DECISION_VISIBILITY_CONTENT_REMOVED": 3}
    assert counts["by_decision_type"]["decision_account"] == {"DECISION_ACCOUNT_SUSPENDED": 1}
    assert counts["automated_detection_as_published"] == {"No": 2, "Yes": 4}
    assert counts["automated_decision_as_published"] == {"AUTOMATED_DECISION_FULLY": 3,
                                                         "AUTOMATED_DECISION_NOT_AUTOMATED": 2,
                                                         "AUTOMATED_DECISION_PARTIALLY": 1}
    window = answer["stored_window"]
    assert window["days_with_a_stored_dump"] == ["2099-05-01", "2099-05-02"]
    assert window["days_without_a_stored_dump"] == ["2099-05-03"] and window["complete"] is False
    assert [d["date"] for d in answer["dump_versions"]] == ["2099-05-01", "2099-05-02"]
    assert all(len(d["sha1_as_published"]) == 40 and d["citation"]["revision_id"] for d in answer["dump_versions"])
    assert "not the platform's totals" in answer["count_notice"]
    illegal = ask.moderation_statements(h.NS, "example-video", scopes=h.SCOPES, start="2099-05-01",
                                        end="2099-05-02", ground="DECISION_GROUND_ILLEGAL_CONTENT",
                                        include_statements=True)
    assert illegal["statements_counted"] == 2 and all(s["citation"]["dump_key"] for s in illegal["statements"])
    assert "synthetic-content-id" not in json.dumps(illegal) and "Flagger" not in json.dumps(illegal)
    none = ask.moderation_statements(h.NS, "example-video", scopes=h.SCOPES, start="2099-06-01", end="2099-06-02")
    assert none["status"] == "none_on_record" and none["counts"] is None


def test_moderation_as_of_uses_the_dump_version_then_available():
    _conn, ask = ask_world(version="v2")
    now = ask.moderation_statements(h.NS, "example-video", scopes=h.SCOPES, start="2099-05-01", end="2099-05-01")
    then = ask.moderation_statements(h.NS, "example-video", scopes=h.SCOPES, start="2099-05-01", end="2099-05-01",
                                     as_of=h.V1_MS + 1)
    assert now["dump_versions"][0]["revision_no"] == 2 and then["dump_versions"][0]["revision_no"] == 1
    assert now["counts"]["automated_decision_as_published"]["AUTOMATED_DECISION_FULLY"] == 3
    assert then["counts"]["automated_decision_as_published"]["AUTOMATED_DECISION_FULLY"] == 2


def test_evidence_bundles_cite_every_item_with_source_revision_and_as_of_time():
    _conn, ask = ask_world()
    for answer in (ask.ads_by_advertiser(h.NS, h.PARTY_PAGE, scopes=h.SCOPES),
                   ask.moderation_statements(h.NS, "example-video", scopes=h.SCOPES, start="2099-05-01",
                                             end="2099-05-02", include_statements=True)):
        bundle = ask.evidence_bundle(answer)
        assertions = bundle["sections"][0]["assertions"]
        assert assertions and all(a["citations"] and a["dependencies"][0]["revision"] for a in assertions)
        cited = {b["id"] for b in bundle["bibliography"]}
        assert {c for a in assertions for c in a["citations"]} == cited
        assert all("source " in b["text"] and "recorded " in b["text"] for b in bundle["bibliography"])
        assert bundle["exclusions"] and "never converted" in bundle["ranges_notice"]
