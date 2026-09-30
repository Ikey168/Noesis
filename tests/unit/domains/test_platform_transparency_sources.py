"""Platform-transparency acquisition: DSA dumps, Meta Ad Library and Google political ads (#2596, #2600, #2604, #2611).

Authored responses replay through the real adapter; nothing here is live.
"""

from __future__ import annotations

import base64
import copy
import json

import pytest

from src.ingestion.platform_transparency_sources import (
    FIXTURE_SECRET,
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    WITHHELD_KEYS,
    PlatformTransparencyAdapter,
    fixture_transport,
    minimisation_violations,
    platform_transparency_declaration,
)
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    native_connector_module,
)
from tests.unit import platform_transparency_harness as h


def records(source_id: str, version: str = "v1") -> list[dict]:
    fetcher = h.adapter(source_id, version)
    out, cursor = [], None
    for _ in fetcher.units:
        page = fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 600}, cursor=cursor)
        out += [item["platform_transparency_record"] for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            break
    return out


def test_pack_declares_every_source_bounded_unverified_live_and_minimised():
    sources = [s for s in h.manifest()["sources"] if s["connector"] == "platform-transparency"]
    assert sorted(s["source_id"] for s in sources) == sorted(h.SOURCES)
    for item in sources:
        declared = platform_transparency_declaration(item)
        assert declared["live_verification"] == "unverified-live"
        assert declared["minimisation"] == "platform-transparency-minimisation-v1"
        assert declared["decision"].startswith("docs/development/platform-transparency-evidence/source-audit.md#")
        assert item["update_cadence"].startswith("on explicit selection only")
    assert {p: v["status"] for p, v in LIVE_VERIFICATION.items()} == {
        "dsa-transparency-db": "unverified-live", "meta-ad-library": "unverified-live",
        "google-political-ads": "unverified-live", "lumen": "not-implemented"}
    assert native_connector_module("platform-transparency").ADAPTERS["platform-transparency"] is \
        PlatformTransparencyAdapter


def test_lumen_is_recorded_as_not_implemented_with_its_reason():
    lumen = PROVIDER_CONTRACTS["lumen"]
    assert lumen["access_decision"] == "not-implemented" and lumen["formats"] == []
    assert "no research token" in lumen["reason"] and "Terms of Use" in lumen["reason"]
    item = h.source(h.DSA)
    item["platform_transparency"]["provider"] = "lumen"
    with pytest.raises(SourcePackError):
        platform_transparency_declaration(item)
    assert not [s for s in h.manifest()["sources"] if "lumen" in s["source_id"]]


def test_offline_conformance_replays_every_pinned_fixture():
    pack = h.manifest()
    pack["sources"] = [s for s in pack["sources"] if s["connector"] == "platform-transparency"]
    report = SourcePackConformance(h.ROOT).offline(pack)
    assert report["valid"], report["sources"]
    assert {r["source_id"]: r["records"] for r in report["sources"]} == {h.DSA: 10, h.META: 8, h.GOOGLE: 10}


def test_dsa_statements_are_keyed_by_platform_and_uuid_with_the_dump_version_and_minimised():
    rows = records(h.DSA)
    dumps = [r for r in rows if r["record_kind"] == "dump-release"]
    assert [d["record_key"] for d in dumps] == [
        "platform-transparency:dsa:dump:example-video:2099-05-01:light",
        "platform-transparency:dsa:dump:example-video:2099-05-02:light",
        "platform-transparency:dsa:dump:example-market:2099-05-01:light"]
    assert all(d["fields"]["sha1_verified"] and len(d["fields"]["sha1_as_published"]) == 40 for d in dumps)
    statements = [r for r in rows if r["record_kind"] == "statement-of-reasons"]
    first = next(s for s in statements if s["fields"]["uuid"].endswith("0002"))
    assert first["record_key"] == "platform-transparency:dsa:sor:example-video:0a000000-0000-4000-8000-000000000002"
    assert first["dump_key"] == dumps[0]["record_key"]
    assert first["fields"]["decision_ground"] == "DECISION_GROUND_ILLEGAL_CONTENT"
    assert first["fields"]["automated_detection"] == "No"
    assert first["fields"]["automated_decision"] == "AUTOMATED_DECISION_NOT_AUTOMATED"
    assert first["fields"]["decision_visibility"] == ["DECISION_VISIBILITY_CONTENT_REMOVED"]
    assert first["minimisation"]["withheld"] == ["platform_uid", "source_identity"]
    text = json.dumps(rows)
    assert "Example Flagger Association" not in text and "synthetic-content-id" not in text
    assert "free text a notifier wrote" not in text
    assert all(minimisation_violations(r) == [] for r in rows)


def test_a_dump_that_does_not_match_its_published_checksum_is_refused():
    pages = h.native_pages(h.DSA)
    pages[1] = {**pages[1], "body": "0" * 40 + "  sor-example-video-2099-05-01-light.zip\n"}
    fetcher = PlatformTransparencyAdapter(h.source(h.DSA), transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as refused:
        fetcher.fetch_page({"operation": "selection", "parameters": {}}, cursor=None)
    assert refused.value.code == "schema_drift" and "checksum_mismatch" in str(refused.value)


def test_a_dump_above_its_statement_cap_is_budget_exhausted_never_truncated(monkeypatch):
    from src.ingestion import platform_transparency_sources as sources

    monkeypatch.setattr(sources, "DSA_ROW_CAP", 2)
    with pytest.raises(SourcePackError) as refused:
        h.adapter(h.DSA).fetch_page({"operation": "selection", "parameters": {}}, cursor=None)
    assert refused.value.code == "budget_exhausted"


def test_meta_ads_keep_ranges_and_currency_exactly_as_published_and_drop_withheld_fields():
    rows = records(h.META)
    ads = {r["fields"]["ad_id"]: r for r in rows if r["record_kind"] == "ad"}
    assert sorted(ads) == ["990000000000101", "990000000000102", "990000000000201", "990000000000301"]
    ad = ads["990000000000102"]
    assert ad["fields"]["spend"] == {"lower_bound": "1000", "upper_bound": "1499",
                                     "as_published": {"lower_bound": "1000", "upper_bound": "1499"}}
    assert ad["fields"]["currency"] == "GBP"
    assert ad["fields"]["advertiser_as_declared"] == "Example Party"
    assert ad["fields"]["funding_entity_as_declared"] == "Example Party"
    assert ad["minimisation"]["withheld"] == ["ad_snapshot_url", "demographic_distribution"]
    open_ended = ads["990000000000201"]["fields"]["impressions"]
    assert open_ended["lower_bound"] == "1000000" and open_ended["upper_bound"] is None  # never filled in
    assert ad["locator"] == "https://www.facebook.com/ads/library/?id=990000000000102"
    assert "access_token" not in json.dumps(rows) and "FIXTURE" not in json.dumps(rows)
    listing = next(r for r in rows if r["record_kind"] == "listing")
    assert listing["fields"]["ad_keys"] == sorted(r["record_key"] for r in ads.values())


def test_meta_pagination_follows_the_cursor_and_the_token_travels_only_in_the_header():
    seen = []
    pages = h.native_pages(h.META)
    replay = fixture_transport(pages)

    def transport(**kwargs):
        seen.append(kwargs)
        return replay(**kwargs)

    fetcher = PlatformTransparencyAdapter(h.source(h.META), transport=transport, secret=FIXTURE_SECRET)
    page = fetcher.fetch_page({"operation": "selection", "parameters": {}}, cursor=None)
    assert len(seen) == 2 and seen[1]["params"]["after"] == "QUZURVIx"
    assert all(s["headers"]["Authorization"] == f"Bearer {FIXTURE_SECRET}" for s in seen)
    assert all(FIXTURE_SECRET not in json.dumps(s["params"]) for s in seen)
    assert FIXTURE_SECRET not in json.dumps(page.receipt) and FIXTURE_SECRET not in json.dumps(page.records)
    assert [r["method"] for r in page.receipt["requests"]] == ["GET", "GET"]
    without = PlatformTransparencyAdapter(h.source(h.META), transport=replay)
    with pytest.raises(SourcePackError) as refused:
        without.fetch_page({"operation": "selection", "parameters": {}}, cursor=None)
    assert refused.value.code == "authentication_failed"


def test_meta_units_longer_than_their_page_bound_are_budget_exhausted(monkeypatch):
    from src.ingestion import platform_transparency_sources as sources

    monkeypatch.setattr(sources, "META_MAX_PAGES", 1)
    with pytest.raises(SourcePackError) as refused:
        h.adapter(h.META).fetch_page({"operation": "selection", "parameters": {}}, cursor=None)
    assert refused.value.code == "budget_exhausted"


@pytest.mark.parametrize("change", [
    lambda s: s["pages"][0].update(page_ids=[str(10**14 + i) for i in range(11)]),
    lambda s: s["pages"][0].update(**{"from": "2098-01-01", "to": "2099-05-31"}),
    lambda s: s["pages"][0].update(countries=["gb"]),
    lambda s: s.pop("api_version"),
])
def test_unbounded_meta_selections_are_refused(change):
    item = h.source(h.META)
    change(item["platform_transparency"]["selection"])
    with pytest.raises(SourcePackError):
        platform_transparency_declaration(item)


def test_the_global_dump_and_undeclared_hosts_are_refused():
    item = h.source(h.DSA)
    item["platform_transparency"]["selection"]["dumps"][0]["platform"] = "global"
    with pytest.raises(SourcePackError):
        platform_transparency_declaration(item)
    pages = copy.deepcopy(h.native_pages(h.DSA))
    pages[0]["final_url"] = "https://storage.example.invalid/sor-example-video-2099-05-01-light.zip"
    fetcher = PlatformTransparencyAdapter(h.source(h.DSA), transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as refused:
        fetcher.fetch_page({"operation": "selection", "parameters": {}}, cursor=None)
    assert refused.value.code == "network_policy"


def test_google_ads_keep_published_ids_spend_range_bucket_and_refresh_date_per_record():
    rows = records(h.GOOGLE)
    advertiser = next(r for r in rows if r["record_key"].endswith(h.FUND) and r["record_kind"] == "advertiser")
    assert advertiser["fields"]["public_ids"] == ["C00999903"]
    assert advertiser["source_as_of"] == "2099-06-01T00:00:00Z"
    ad = next(r for r in rows if r["record_key"] == "platform-transparency:google:ad:CR00000000000000000103")
    assert ad["fields"]["spend"] == {"lower_bound": "0", "upper_bound": "100", "currency": "USD",
                                     "as_published": {"spend_range_min_usd": "0", "spend_range_max_usd": "100"}}
    assert ad["fields"]["impressions"] == {"as_published": "≤ 10k"}
    assert ad["source_as_of"] == "2099-06-01T00:00:00Z" and ad["native_revision"] == "refreshed:2099-06-01T00:00:00Z"
    assert ad["fields"]["funding_entity_as_declared"] is None
    civic = [r for r in rows if r["unit_key"] == f"google:{h.CIVIC}:US"]
    assert [r["record_kind"] for r in civic] == ["advertiser", "listing"]
    assert civic[-1]["fields"]["ad_keys"] == []


def test_google_queries_are_fixed_parameterised_posts_and_more_rows_than_one_page_is_budget_exhausted():
    seen = []
    replay = fixture_transport(h.native_pages(h.GOOGLE))

    def transport(**kwargs):
        seen.append(kwargs)
        return replay(**kwargs)

    fetcher = PlatformTransparencyAdapter(h.source(h.GOOGLE), transport=transport, secret=FIXTURE_SECRET)
    fetcher.fetch_page({"operation": "selection", "parameters": {}}, cursor=None)
    posts = [json.loads(s["body"]) for s in seen if s.get("method") == "POST"]
    assert len(posts) == 2 and all(p["parameterMode"] == "NAMED" for p in posts)
    assert all("@advertiser_id" in p["query"] and h.FUND not in p["query"] for p in posts)
    assert all("targeting" not in p["query"] for p in posts)
    pages = h.native_pages(h.GOOGLE)
    for page in pages:
        if isinstance(page.get("body"), dict) and page["body"].get("kind") == "bigquery#queryResponse":
            page["body"] = {**page["body"], "pageToken": "more"}
    fetcher = PlatformTransparencyAdapter(h.source(h.GOOGLE), transport=fixture_transport(pages),
                                          secret=FIXTURE_SECRET)
    with pytest.raises(SourcePackError) as refused:
        fetcher.fetch_page({"operation": "selection", "parameters": {}}, cursor=None)
    assert refused.value.code == "budget_exhausted"


def test_the_minimisation_decision_names_what_is_withheld_and_never_stored():
    assert MINIMISATION["policy"] == "platform-transparency-minimisation-v1"
    assert {"platform_uid", "source_identity", "ad_snapshot_url", "demographic_distribution"} <= WITHHELD_KEYS
    assert "never converted to a midpoint" in MINIMISATION["ranges"]
    assert "never to a natural person" in MINIMISATION["persons"]
    fixture = json.loads((h.ROOT / "tests/fixtures/source_packs/osint-platform-transparency-dsa.json").read_text())
    raw = base64.b64decode(fixture["native_pages"][0]["body_base64"])
    assert b"synthetic-content-id-0001" in raw  # the native dump carries it; the records never do
