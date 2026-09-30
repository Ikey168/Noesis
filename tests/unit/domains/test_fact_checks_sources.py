"""Fact-checks source audit, declared sources and acquisition through the real adapter (#2664, #2675, #2679, #2683)."""

from __future__ import annotations

import json

import pytest

from src.ingestion import fact_checks_sources as fc
from src.ingestion.source_packs import SourcePackError, _digest, replay_native_fixture
from tests.unit import fact_checks_harness as h

PERSONAL = ("Chief Spokesperson", "images.example", "robinsample_fake", "Pat Reviewer", "staff/pat")


def fetch_all(source_id: str, version: str = "v1") -> tuple[list[dict], list[dict]]:
    adapter = h.adapter(source_id, version)
    records, receipts, cursor = [], [], None
    for _ in adapter.units:
        page = adapter.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=cursor)
        records += [r["fact_check_record"] for r in page.records]
        receipts.append(page.receipt)
        cursor = page.next_cursor
    return records, receipts


def test_the_audit_records_contracts_terms_minimisation_and_bounded_coverage():
    audit = (h.ROOT / "docs/development/fact-checks-evidence/source-audit.md").read_text()
    for source_id in h.SOURCES:
        assert f"`{source_id}`" in audit
    for needed in ("X-Goog-Api-Key", "NOESIS_GOOGLE_FACTCHECK_API_KEY", "vintage",
                   "absent-from-release", "absent-from-listing", "Retention", "Who may query", "Bounded first coverage",
                   "not re-verified live", "no truth verdicts by Noesis", "unverified-live", "SHA-256"):
        assert needed in audit, needed
    assert set(fc.PROVIDER_CONTRACTS) == set(fc.PROVIDERS)
    for contract in fc.PROVIDER_CONTRACTS.values():
        assert {"endpoints", "authentication", "rate_limits", "revisions", "licence", "access_decision"} <= set(contract)
    assert all(v["status"] == "unverified-live" for v in fc.LIVE_VERIFICATION.values())
    assert "claimant image" in fc.MINIMISATION["never_stored"]
    assert set(fc.BOUNDED_COVERAGE) >= set(fc.PROVIDERS)


def test_the_pack_declares_every_source_bounded_minimised_and_replaying_its_pinned_output():
    manifest = h.manifest()
    assert manifest["version"] == "1.2.0"
    ours = {s["source_id"]: s for s in manifest["sources"] if s["connector"] == "fact-checks"}
    assert set(ours) == set(h.SOURCES)
    for source_id, item in ours.items():
        declared = item["fact_checks"]
        assert declared["live_verification"] == "unverified-live"
        assert declared["minimisation"] == fc.MINIMISATION_POLICY
        assert declared["format"] == h.FORMATS[source_id]
        keyed = fc.FORMATS[declared["format"]]["keyed"]
        assert (item["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_GOOGLE_FACTCHECK_API_KEY"}) is keyed
        fixture = json.loads((h.ROOT / item["fixture"]["path"]).read_text())
        assert fixture == json.loads(json.dumps(h.source_pack_fixture(source_id)))  # generated from the harness
        assert _digest(list(replay_native_fixture(item, fixture))) == item["fixture"]["expected_output_hash"]


def test_google_reviews_are_keyed_by_publisher_review_url_and_claim_with_ratings_verbatim():
    records, receipts = fetch_all(h.GOOGLE)
    assert len(records) == 4 and {r["provider"] for r in records} == {"google-fact-check-tools"}
    widgets = [r for r in records if r["fields"]["claims"][0]["claim_text"] == h.C1]
    assert sorted(r["fields"]["claims"][0]["rating"]["text"] for r in widgets) == ["False", "Mostly accurate"]
    assert all(r["fields"]["claims"][0]["rating"]["value"] is None for r in widgets)  # the API publishes text only
    assert {r["publisher_key"] for r in widgets} == {"fact-check:publisher:factdesk.example",
                                                     "fact-check:publisher:northwind-verify.example"}
    assert all(r["record_key"].startswith(r["review_key"] + ":") for r in records)
    # the publisher-site search follows its page token within the page bound
    assert [q["path"].split("?")[1].count("pageToken") for q in receipts[1]["requests"]] == [0, 1]
    text = json.dumps([records, receipts])
    assert fc.FIXTURE_SECRET not in text and "X-Goog-Api-Key" not in text  # the key never reaches a receipt
    assert all(r["evidence_origin"] == "fixture" for r in receipts)


def test_the_google_key_is_required_and_sent_as_a_header_only():
    item = h.source(h.GOOGLE)
    with pytest.raises(SourcePackError) as refused:
        fc.FactChecksAdapter(item, transport=fc.fixture_transport(h.native_pages(h.GOOGLE))).fetch_page(
            {"operation": "selection", "parameters": {}}, cursor=None)
    assert refused.value.code == "authentication_failed"
    seen = []

    def spy(*, url, params, headers, timeout):
        seen.append((url, dict(params), dict(headers)))
        return fc.fixture_transport(h.native_pages(h.GOOGLE))(url=url, params=params, headers=headers,
                                                               timeout=timeout)

    fc.FactChecksAdapter(item, transport=spy, secret="k-123").fetch_page({"operation": "selection", "parameters": {}},
                                                                          cursor=None)
    assert all(h_["X-Goog-Api-Key"] == "k-123" and "key" not in p and "k-123" not in u for u, p, h_ in seen)


def test_a_search_longer_than_its_page_bound_is_budget_exhausted_not_truncated():
    item = h.source(h.GOOGLE)
    endless = {"claims": [], "nextPageToken": "more"}

    def transport(*, url, params, headers, timeout):
        return {"status": 200, "headers": {}, "content": json.dumps(endless).encode(), "origin": "fixture"}

    with pytest.raises(SourcePackError) as exhausted:
        fc.FactChecksAdapter(item, transport=transport, secret="k").fetch_page(
            {"operation": "selection", "parameters": {}}, cursor=None)
    assert exhausted.value.code == "budget_exhausted"


def test_datacommons_release_is_a_filtered_vintage_with_scales_and_minimised_claimants():
    records, receipts = fetch_all(h.DATACOMMONS)
    assert sorted(r["fields"]["publisher"]["domain"] for r in records) == ["contoso-check.example", "factdesk.example"]
    assert {r["native_revision"] for r in records} == {"2099-07-15T00:00:00Z"}
    (widgets,) = [r for r in records if r["fields"]["claims"][0]["claim_text"] == h.C1]
    claim = widgets["fields"]["claims"][0]
    assert claim["rating"] == {"text": "False", "value": "1", "best": "5", "worst": "1",
                               "scale_as_published": "1 on a scale from 1 to 5"}
    assert claim["claimant"] == {"name_as_published": "Robin Sample", "type_as_published": "Person",
                                 "identifiers": [{"scheme": "wikidata", "value": "Q99999901"}]}
    social = [a for a in claim["appearances"] if a["platform_post"]]
    assert social == [{"host": "x.com", "url_sha256": fc.url_digest(h.SOCIAL_POST), "platform_post": True,
                       "url_withheld": True}]
    news = [a for a in claim["appearances"] if not a["platform_post"]]
    assert news[0]["url_canonical"] == h.NEWS_ARTICLE and "strip-www" in news[0]["canonical_rules"]
    assert claim["first_appearance"]["platform_post"] is True
    assert sorted(widgets["minimisation"]["withheld"]) == ["itemReviewed.author.image", "itemReviewed.author.jobTitle",
                                                            "itemReviewed.author.sameAs(x.com)",
                                                            "review.author(Person)"]
    assert receipts[0]["scope"]["complete"] is True and receipts[0]["withheld_fields"] == 4
    assert not [p for p in PERSONAL if p in json.dumps([records, receipts])]
    assert fc.minimisation_violations(widgets) == []


def test_the_same_review_from_the_api_and_a_release_shares_its_key():
    google, _ = fetch_all(h.GOOGLE)
    datacommons, _ = fetch_all(h.DATACOMMONS)
    shared = {r["record_key"] for r in google} & {r["record_key"] for r in datacommons}
    assert len(shared) == 1 and next(iter(shared)).startswith("fact-check:review:factdesk.example:")


def test_ifcn_listing_becomes_publisher_records_with_status_and_dates_as_published():
    records, receipts = fetch_all(h.IFCN)
    by_domain = {r["fields"]["domain"]: r for r in records}
    assert set(by_domain) == {"factdesk.example", "northwind-verify.example", "contoso-check.example"}
    assert by_domain["contoso-check.example"]["fields"]["ifcn_status"] == "under-review"
    assert by_domain["contoso-check.example"]["fields"]["status_as_published"] == "Under review"
    northwind = by_domain["northwind-verify.example"]
    assert northwind["fields"]["ifcn_status"] == "verified" and northwind["effective_on"] == "2097-05-10"
    assert northwind["locator"] == "https://ifcncodeofprinciples.poynter.org/profile/northwind-verify"
    later, _ = fetch_all(h.IFCN, "v2")
    expired = {r["fields"]["domain"]: r for r in later}["northwind-verify.example"]
    assert expired["fields"]["ifcn_status"] == "expired" and expired["effective_on"] == "2099-05-10"
    assert "contoso-check.example" not in {r["fields"]["domain"] for r in later}
    assert receipts[0]["scope"]["absence"] == "absent-from-listing"


@pytest.mark.parametrize("mutation", [
    lambda s: s["fact_checks"].update(minimisation="none"),
    lambda s: s["fact_checks"].update(live_verification="assumed"),
    lambda s: s["fact_checks"]["selection"].update(searches=[{"language": "en"}]),
    lambda s: s.update(auth={"kind": "none"}),
])
def test_declarations_must_be_bounded_minimised_keyed_and_state_their_live_status(mutation):
    item = h.source(h.GOOGLE)
    mutation(item)
    with pytest.raises(SourcePackError):
        fc.fact_checks_declaration(item)


def test_release_windows_are_bounded_and_listing_is_single():
    item = h.source(h.DATACOMMONS)
    item["fact_checks"]["selection"]["releases"][0]["to"] = "2101-12-31"
    with pytest.raises(SourcePackError):
        fc.fact_checks_declaration(item)
    item = h.source(h.IFCN)
    item["fact_checks"]["selection"]["listings"].append({"page": "signatories"})
    with pytest.raises(SourcePackError):
        fc.fact_checks_declaration(item)


def test_a_redirect_to_another_host_is_a_network_policy_failure():
    item = h.source(h.IFCN)

    def transport(*, url, params, headers, timeout):
        return {"status": 200, "headers": {}, "content": b"<html></html>", "origin": "fixture",
                "final_url": "https://elsewhere.example/signatories"}

    with pytest.raises(SourcePackError) as refused:
        fc.FactChecksAdapter(item, transport=transport).fetch_page({"operation": "selection", "parameters": {}},
                                                                   cursor=None)
    assert refused.value.code == "network_policy"
