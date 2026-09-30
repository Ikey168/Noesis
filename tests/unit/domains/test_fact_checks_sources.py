"""Fact-checks acquisition: Google claim search, Data Commons feed and IFCN listing adapters (#2664, #2675, #2679, #2683)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.ingestion.fact_checks_sources import (
    BOUNDED_COVERAGE,
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    FactChecksAdapter,
    fixture_transport,
    minimisation_violations,
    replay_native_fixture,
)
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    validate_source_pack,
)
from tests.unit import fact_checks_harness as h

ROOT = Path(__file__).resolve().parents[3]


def records(source_id, version="v1"):
    fetcher = h.adapter(source_id, version)
    out, cursor = [], None
    for _ in fetcher.units:
        page = fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=cursor)
        out += [r["fact_check_record"] for r in page.records]
        cursor = page.next_cursor
        if cursor is None:
            break
    return out


def by_site(items, site):
    return [r for r in items if r["publisher_site"] == site]


def test_audit_records_every_source_decision_and_the_minimisation_policy():
    assert set(LIVE_VERIFICATION) == set(PROVIDER_CONTRACTS)
    assert all(v["status"] in {"unverified-live", "documented-not-acquired"} for v in LIVE_VERIFICATION.values())
    for contract in PROVIDER_CONTRACTS.values():
        assert {"endpoints", "authentication", "licence", "rate_limits", "revisions", "access_decision"} <= set(contract)
    assert PROVIDER_CONTRACTS["ifcn"]["requires_terms_confirmation"] is True
    assert MINIMISATION["never_stored"] and MINIMISATION["stored"]
    assert "claimant:read" in MINIMISATION["query_scope"]
    assert set(BOUNDED_COVERAGE) >= {"google", "datacommons", "ifcn"}
    audit = (ROOT / "docs/development/fact-checks-evidence/source-audit.md").read_text()
    for url in ("https://developers.google.com/fact-check/tools/api", "https://datacommons.org/factcheck/download",
                "https://ifcncodeofprinciples.poynter.org/signatories"):
        assert url in audit
    assert "2026-09-30" in audit and "_unverified_" in audit


def test_source_pack_entries_state_live_verification_minimisation_and_pinned_fixtures():
    pack = h.manifest()
    assert pack["version"] == "1.2.0"
    sources = {s["source_id"]: s for s in pack["sources"] if s["connector"] == "fact-checks"}
    assert set(sources) == set(h.SOURCES)
    for source in sources.values():
        assert source["fact_checks"]["live_verification"] == "unverified-live"
        assert source["fact_checks"]["minimisation"] == "fact-checks-minimisation-v1"
        assert source["mapping"]["target_schema"] == "noesis-fact-check-record-v1"
    assert sources[h.GOOGLE]["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_GOOGLE_FACTCHECK_API_KEY"}
    result = SourcePackConformance(ROOT).offline(pack, runners={"fact-checks": replay_native_fixture})
    assert all(item["valid"] for item in result["sources"] if item["source_id"] in h.SOURCES)


def test_google_claim_search_keeps_claims_as_quoted_claimants_as_named_and_ratings_verbatim():
    items = records(h.GOOGLE)
    assert len(items) == 4  # two pages of the topic search plus one publisher-site search
    (seals,) = [r for r in by_site(items, "factcheck.example.org") if "seals" in r["locator"]]
    claim = seals["fields"]["claims"][0]
    assert claim["claim_text_as_quoted"] == h.SEALS
    assert claim["claimant_as_named"] == "Mayor Alex Example"
    assert claim["rating"] == {"textual_rating": "False", "rating_value": None, "best_rating": None,
                               "worst_rating": None, "rating_name": None}
    assert seals["record_key"].startswith("fact-checks:review:factcheck.example.org:")
    assert seals["fields"]["review_date"] == "2025-03-10T00:00:00Z" and seals["effective_on"] == "2025-03-10"
    (photo,) = by_site(items, "claimwatch.example.com")
    assert photo["fields"]["claims"][0]["rating"]["textual_rating"] == "Altered photo"
    assert all(minimisation_violations(r) == [] for r in items)


def test_google_key_travels_in_a_header_and_never_in_a_receipt_or_record():
    seen = []
    replay = fixture_transport(h.native_pages(h.GOOGLE))

    def transport(**kwargs):
        seen.append(kwargs)
        return replay(**kwargs)

    fetcher = FactChecksAdapter(h.source(h.GOOGLE), transport=transport, secret="secret-value-123")
    page = fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=None)
    assert all(call["headers"]["X-Goog-Api-Key"] == "secret-value-123" for call in seen)
    assert all("key" not in call["params"] for call in seen)
    assert "secret-value-123" not in json.dumps(page.receipt) + json.dumps([dict(r) for r in page.records])
    assert [r["path"].split("?")[0] for r in page.receipt["requests"]] == ["/claims:search", "/claims:search"]
    with pytest.raises(SourcePackError) as missing:
        FactChecksAdapter(h.source(h.GOOGLE), transport=transport).fetch_page(
            {"operation": "selection", "parameters": {}, "limit": 500}, cursor=None)
    assert missing.value.code == "authentication_failed"


def test_a_unit_longer_than_its_page_bound_is_budget_exhausted_never_truncated():
    body = json.dumps({"claims": [], "nextPageToken": "again"})

    def transport(**kwargs):
        return {"status": 200, "headers": {}, "content": body.encode(), "origin": "fixture"}

    fetcher = FactChecksAdapter(h.source(h.GOOGLE), transport=transport, secret="k")
    with pytest.raises(SourcePackError) as exhausted:
        fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=None)
    assert exhausted.value.code == "budget_exhausted"


def test_datacommons_release_is_a_filtered_vintage_with_publisher_scales_and_withheld_personal_fields():
    items = records(h.DATACOMMONS)
    assert sorted(r["publisher_site"] for r in items) == ["claimwatch.example.com", "factcheck.example.org",
                                                          "verifica.example.net"]  # other site and 2024 item dropped
    (seals,) = by_site(items, "factcheck.example.org")
    claim = seals["fields"]["claims"][0]
    assert claim["rating"] == {"textual_rating": "False", "rating_value": "1", "best_rating": "5",
                               "worst_rating": "1", "rating_name": None}
    assert claim["claimant_same_as"] == ["https://www.wikidata.org/wiki/Q999999901"]
    assert claim["appearance_urls"] == [h.APPEARANCE]
    assert set(seals["minimisation"]["withheld"]) >= {"itemReviewed.author.jobTitle", "itemReviewed.author.image",
                                                      "itemReviewed.firstAppearance.author", "reviewRating.image"}
    text = json.dumps(items)
    for withheld in ("Mayor of Example Bay", "mayor.jpg", "Sam Placeholder", "Reviewer Placeholder", "false.png"):
        assert withheld not in text
    (verifica,) = by_site(items, "verifica.example.net")
    assert verifica["fields"]["publisher"] == {"name_as_published": None, "site": "verifica.example.net"}
    assert verifica["fields"]["claims"][0]["rating"]["rating_value"] == 2  # a number stays a number
    assert all(r["vintage"].startswith("2025-06-01T00:00:00Z|") for r in items)
    # the same review from Google and Data Commons shares one record key (canonical review URL)
    google = [r for r in records(h.GOOGLE) if "seals" in r["locator"] and r["publisher_site"] == "factcheck.example.org"]
    assert google[0]["record_key"] == seals["record_key"]


def test_ifcn_listing_keeps_status_as_published_and_refuses_a_live_fetch_without_terms_confirmation():
    items = {r["record_key"]: r for r in records(h.IFCN)}
    assert set(items) == {"fact-checks:ifcn:example-fact-check", "fact-checks:ifcn:verifica-example",
                          "fact-checks:ifcn:claimwatch-example"}
    verifica = items["fact-checks:ifcn:verifica-example"]["fields"]
    assert verifica["status_as_published"] == "Under renewal"
    assert verifica["status_date_as_published"] == "2025-02-01" and verifica["status_date_label"] == "Expires on"
    assert verifica["site"] == "verifica.example.net"
    assert items["fact-checks:ifcn:example-fact-check"]["minimisation"]["withheld"] == ["signatory logo image"]
    with pytest.raises(SourcePackError) as refused:
        FactChecksAdapter(h.source(h.IFCN))  # no transport: a live fetch
    assert refused.value.code == "licensing"
    confirmed = h.source(h.IFCN)
    confirmed["fact_checks"]["terms_confirmation"] = "operator read the site terms on 2099-01-01"
    assert FactChecksAdapter(confirmed).describe()["fact_checks"]["provider"] == "ifcn"


def test_a_listing_without_profile_links_is_schema_drift_not_an_empty_listing():
    pages = [{**h.native_pages(h.IFCN)[0], "body": "<html><body><p>maintenance</p></body></html>"}]
    fetcher = FactChecksAdapter(h.source(h.IFCN), transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as drift:
        fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=None)
    assert drift.value.code == "schema_drift"


def test_declarations_are_bounded():
    pack = json.loads(h.PACK.read_text())
    for mutate, message in (
            (lambda s: s["fact_checks"]["selection"]["queries"][0].update(max_age_days=4000), "maxAgeDays"),
            (lambda s: s["fact_checks"].update(minimisation="none"), "minimisation"),
            (lambda s: s.update(auth={"kind": "none"}), "secret")):
        broken = copy.deepcopy(pack)
        source = next(s for s in broken["sources"] if s["source_id"] == h.GOOGLE)
        mutate(source)
        with pytest.raises(SourcePackError, match=message):
            FactChecksAdapter(next(s for s in validate_source_pack(broken)["sources"] if s["source_id"] == h.GOOGLE),
                              transport=fixture_transport([]))
    feed = copy.deepcopy(pack)
    release = next(s for s in feed["sources"] if s["source_id"] == h.DATACOMMONS)["fact_checks"]["selection"]
    release["releases"][0]["to"] = "2027-12-31"
    with pytest.raises(SourcePackError, match="review window"):
        FactChecksAdapter(next(s for s in validate_source_pack(feed)["sources"] if s["source_id"] == h.DATACOMMONS),
                          transport=fixture_transport([]))


def test_a_redirect_to_another_host_is_a_network_policy_failure():
    pages = [{**p, "final_url": "https://elsewhere.example.com/x"} for p in h.native_pages(h.DATACOMMONS)]
    fetcher = FactChecksAdapter(h.source(h.DATACOMMONS), transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as blocked:
        fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=None)
    assert blocked.value.code == "network_policy"
