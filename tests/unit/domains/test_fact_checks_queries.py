"""Fact-checks of a claim or claimant as of a date, and fact-checks that cite a news article (#2698, #2703)."""

from __future__ import annotations

import json

import pytest

from src.kb.fact_checks_queries import FactCheckQueries
from src.kb.fact_checks_records import FactCheckError, forbidden_keys
from tests.unit import fact_checks_harness as h


@pytest.fixture(scope="module")
def ask():
    return FactCheckQueries(h.accepted_world())


def ratings(answer):
    return {f["publisher"]["domain"]: [s["rating_as_published"]["text"] for s in f["as_published_by_source"]]
            for f in answer["fact_checks"]}


def test_a_claim_reaches_its_fact_checks_through_accepted_matches_with_ratings_verbatim(ask):
    answer = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claim_id="claim-widgets", as_of="2099-08-31")
    assert answer["status"] == "answered" and forbidden_keys(answer) == []
    assert ratings(answer) == {"factdesk.example": ["False", "False (updated with the company's statement)"],
                               "northwind-verify.example": ["Mostly accurate"]}
    factdesk = next(f for f in answer["fact_checks"] if f["publisher"]["domain"] == "factdesk.example")
    scale = factdesk["as_published_by_source"][0]["rating_as_published"]
    assert scale["scale_as_published"] == "1 on a scale from 1 to 5" and scale["value"] == "1"
    assert {s["source_id"] for s in factdesk["as_published_by_source"]} == {h.GOOGLE, h.DATACOMMONS}
    for item in answer["fact_checks"]:
        for entry in item["as_published_by_source"]:
            assert entry["citation"]["revision_id"] and entry["citation"]["observed_at"]
    assert answer["identity"]["accepted"] and all(a["decision_id"] for a in answer["identity"]["accepted"])
    assert "Noesis issues no verdict" in answer["notice"]


def test_conflicting_ratings_are_shown_side_by_side_and_never_reconciled(ask):
    answer = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claim_id="claim-widgets")
    (group,) = answer["side_by_side"]["groups"]
    assert group["rating_texts_differ"] is True
    assert {p["publisher"] for p in group["publishers"]} == {"Exampla Fact Desk", "Northwind Verify"}
    assert "no rating is normalised" in answer["side_by_side"]["basis"]


def test_as_of_selects_the_review_revision_in_force_and_the_publisher_status_at_review_time(ask):
    early = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claim_id="claim-widgets", as_of="2099-06-05")
    assert ratings(early) == {"factdesk.example": ["False", "False"], "northwind-verify.example": ["Mostly accurate"]}
    before = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claim_id="claim-widgets", as_of="2099-06-03")
    assert set(ratings(before)) == {"factdesk.example"}
    northwind = next(f for f in early["fact_checks"] if f["publisher"]["domain"] == "northwind-verify.example")
    assert northwind["publisher_status_at_review"]["status"] == "expired"  # as known now
    known_then = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claim_id="claim-widgets", as_of="2099-06-05",
                                           known_at="2099-07-31")
    then = next(f for f in known_then["fact_checks"] if f["publisher"]["domain"] == "northwind-verify.example")
    assert then["publisher_status_at_review"]["status"] == "verified"
    assert then["publisher_status_at_review"]["citation"]["revision_no"] == 1


def test_claimants_by_accepted_identifier_or_by_name_as_published(ask):
    by_entity = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claimant="ent-wikidata-q99999901")
    assert [f["publisher"]["domain"] for f in by_entity["fact_checks"]] == ["factdesk.example"]
    assert by_entity["identity"]["unreviewed_candidates_not_used"]  # the name-only candidate is not used
    by_name = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claimant="Robin Sample")
    assert "not an identity match" in by_name["subject"]["basis"]
    assert {f["publisher"]["domain"] for f in by_name["fact_checks"]} == {"factdesk.example",
                                                                         "northwind-verify.example"}


def test_a_subject_with_no_records_is_none_on_record(ask):
    assert ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claim_id="claim-unknown")["status"] == "none_on_record"
    assert ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claimant="Nobody Anywhere")["status"] == "none_on_record"
    with pytest.raises(FactCheckError):
        ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES)


def test_fact_checks_citing_an_article_use_the_stated_url_rule_and_archived_captures(ask):
    answer = ask.citing(h.NS, scopes=h.SCOPES, url="http://news.example/2099/05/31/widget-exports/#comments")
    rule = answer["subject"]["url_rule"]
    assert rule["rules"] == "wa-canon-v1" and rule["canonical_url"] == h.NEWS_ARTICLE
    assert set(rule["applied"]) == {"scheme-equivalence", "strip-trailing-slash", "drop-fragment"}
    assert rule["rule_text"]
    (item,) = answer["fact_checks"]
    assert item["cites_target_as"][0]["match"] == "canonical-url"
    assert item["cites_target_as"][0]["cited_as_published"].startswith("https://www.news.example/")
    assert item["archived_captures"]["status"] == "answered"
    assert item["archived_captures"]["closest"]["archive_id"] == "internet-archive"
    by_document = ask.citing(h.NS, scopes=h.SCOPES, document_id="doc-widgets")
    assert [f["record_key"] for f in by_document["fact_checks"]] == [item["record_key"]]
    assert item["as_published_by_source"][0]["citation"]["revision_id"]


def test_social_appearances_match_only_a_supplied_url_and_never_expose_it(ask):
    answer = ask.citing(h.NS, scopes=h.SCOPES, url=h.SOCIAL_POST)
    (item,) = answer["fact_checks"]
    assert {c["match"] for c in item["cites_target_as"]} == {"url-digest"}
    assert {c["role"] for c in item["cites_target_as"]} == {"appearance", "first_appearance"}
    assert "robinsample_fake" not in json.dumps(answer["fact_checks"])
    assert ask.citing(h.NS, scopes=h.SCOPES, url="https://news.example/none")["status"] == "none_on_record"


def test_evidence_bundle_cites_every_item_with_source_revision_and_as_of_time(ask):
    answer = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claim_id="claim-widgets")
    bundle = ask.evidence_bundle(answer)
    cited = {c for a in bundle["sections"][0]["assertions"] for c in a["citations"]}
    assert cited == {b["id"] for b in bundle["bibliography"]}
    assert all("observed" in b["text"] and "source" in b["text"] for b in bundle["bibliography"])
    assert any("IFCN status" in a["text"] for a in bundle["sections"][0]["assertions"])
    assert "no truth verdicts by Noesis" in bundle["exclusions"]


def test_missing_providers_degrade_gracefully():
    conn = h.world(news=False, sources=False)
    bare = FactCheckQueries(conn)
    answer = bare.citing(h.NS, scopes=h.SCOPES, url=h.NEWS_ARTICLE)
    assert answer["fact_checks"][0]["archived_captures"]["status"] == "unavailable"
    with pytest.raises(FactCheckError) as refused:
        bare.citing(h.NS, scopes=h.SCOPES, document_id="doc-widgets")
    assert refused.value.code == "provider_absent"
    only_reviews = h.connection()
    h.apply(only_reviews, h.GOOGLE)
    answer = FactCheckQueries(only_reviews).for_claim_or_claimant(h.NS, scopes=h.SCOPES, claimant="Robin Sample")
    assert answer["fact_checks"][0]["publisher_status_at_review"]["status"] == "unavailable"
