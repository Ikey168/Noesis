"""Platform-transparency source audit, declared sources and acquisition through the real adapter (#2585, #2596,
#2600, #2604, #2611)."""

from __future__ import annotations

import json

import pytest

from src.ingestion import platform_transparency_sources as pt
from src.ingestion.source_packs import SourcePackError, _digest, replay_native_fixture
from tests.unit import platform_transparency_harness as h

PERSONAL = ("PLACEHOLDER", "placeholder_user", "Placeholder Notifier", "Placeholder notice body", "1 Placeholder",
            "exampla.example/post", "northwind.example/track", "-placeholder", "FIXTURE-TOKEN-NOT-REAL",
            "access_token", "Placeholder creative", "Exampleshire", "Placeholder terms explanation")


def fetch_all(source_id: str, version: str = "v1", secret: str | None = pt.FIXTURE_SECRET):
    adapter = h.adapter(source_id, version, secret=secret)
    records, receipts, cursor = [], [], None
    for _ in adapter.units:
        page = adapter.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=cursor)
        records += [r["platform_transparency_record"] for r in page.records]
        receipts.append(page.receipt)
        cursor = page.next_cursor
    return records, receipts


def by_key(records):
    return {r["record_key"]: r for r in records}


def test_the_audit_records_contracts_terms_minimisation_and_the_gated_source():
    audit = (h.ROOT / "docs/development/platform-transparency-evidence/source-audit.md").read_text()
    for source_id in h.SOURCES:
        assert f"`{source_id}`" in audit
    for needed in ("not re-verified live", "Authorization: Bearer", "access_token", "X-Authentication-Token",
                   "Retention", "Who may query", "Bounded first coverage", "gated-not-granted", "not implemented",
                   "decision_facts", "puid", "demographic_distribution", "point estimates"):
        assert needed in audit, needed
    assert set(pt.PROVIDER_CONTRACTS) == {"dsa-transparency-database", "meta-ad-library", "google-political-ads",
                                          "lumen"}
    for contract in pt.PROVIDER_CONTRACTS.values():
        assert {"endpoints", "authentication", "rate_limits", "revisions", "licence", "access_decision"} <= set(
            contract)
    assert pt.LIVE_VERIFICATION["lumen"]["status"] == "gated-not-granted"
    assert all(v["status"] != "verified-live" for v in pt.LIVE_VERIFICATION.values())
    assert "decision_facts" in pt.MINIMISATION["never_stored"]["statement-of-reasons"][0]
    assert "never a match target" in pt.MINIMISATION["individuals"]
    assert pt.source_contracts()["decision_document"].endswith("source-audit.md")


def test_the_pack_declares_every_source_bounded_minimised_and_replaying_its_pinned_output():
    manifest = h.manifest()
    assert manifest["pack_id"] == "osint-platform-transparency" and manifest["version"] == "1.0.0"
    ours = {s["source_id"]: s for s in manifest["sources"]}
    assert set(ours) == set(h.SOURCES)
    for source_id, item in ours.items():
        declared = item["platform_transparency"]
        assert declared["minimisation"] == pt.MINIMISATION_POLICY and declared["format"] == h.FORMATS[source_id]
        assert declared["live_verification"] == ("gated-not-granted" if source_id == "lumen-notices"
                                                 else "unverified-live")
        keyed = pt.FORMATS[declared["format"]]["keyed"]
        assert (item["auth"]["kind"] == "required-secret") is keyed
        fixture = json.loads((h.ROOT / item["fixture"]["path"]).read_text())
        assert fixture == json.loads(json.dumps(h.source_pack_fixture(source_id)))  # generated from the harness
        assert _digest(list(replay_native_fixture(item, fixture))) == item["fixture"]["expected_output_hash"]


def test_dsa_dumps_key_statements_by_platform_and_uuid_record_the_dump_version_and_drop_free_text():
    records, receipts = fetch_all("dsa-sor-dumps")
    found = by_key(records)
    dump = found["platform-transparency:dsa:dump:exampla-social:2099-05-01:light"]
    assert dump["record_kind"] == "dump-release" and dump["fields"]["file_name"] == \
        "sor-exampla-social-2099-05-01-light.zip"
    assert len(dump["fields"]["sha256"]) == 64 and dump["fields"]["statements"] == 5
    statement = found["platform-transparency:dsa:sor:exampla-social:00000000-0000-4000-8000-00000000a001"]
    fields = statement["fields"]
    assert fields["decision_visibility"] == ["DECISION_VISIBILITY_CONTENT_REMOVED"]
    assert fields["decision_ground"] == "DECISION_GROUND_ILLEGAL_CONTENT"
    assert fields["automated_detection"] == "Yes" and fields["automated_decision"] == "AUTOMATED_DECISION_PARTIALLY"
    assert fields["content_type"] == ["CONTENT_TYPE_TEXT"] and fields["application_date"] == "2099-05-01 12:00:00"
    assert {"decision_facts", "illegal_content_explanation", "puid", "source_identity"} <= set(
        statement["minimisation"]["withheld"])
    assert not any("other-platform" in k for k in found)  # another platform's row is never stored
    assert receipts[0]["requests"][0]["path"] == "/sor-exampla-social-2099-05-01-light.zip"
    assert receipts[0]["complete_listings"] == [{"selection_key": "dsa:exampla-social:2099-05-01:light",
                                                 "record_kind": "statement-of-reasons"}]
    assert not [p for p in PERSONAL if p in json.dumps([records, receipts])]


def test_meta_ads_keep_ranges_as_published_follow_cursors_and_never_store_tokens_or_creatives():
    records, receipts = fetch_all("meta-ad-library-political")
    found = by_key(records)
    ad = found["platform-transparency:meta:ad:880000000000001"]
    fields = ad["fields"]
    assert fields["spend_range_as_published"] == {"lower_bound": "100", "upper_bound": "199", "currency": "GBP"}
    assert fields["impressions_range_as_published"] == {"lower_bound": "1000", "upper_bound": "4999"}
    assert fields["funding_entity_as_declared"] == ["Paid for by Example Holdings Ltd"]
    assert fields["advertiser_as_declared"] == "Example Holdings Ltd" and fields["delivery_stop"] is None
    assert fields["snapshot_url"] == "https://www.facebook.com/ads/archive/render_ad/?id=880000000000001"
    assert {"ad_creative_bodies", "demographic_distribution", "delivery_by_region"} <= set(
        ad["minimisation"]["withheld"])
    open_range = found["platform-transparency:meta:ad:880000000000011"]["fields"]["impressions_range_as_published"]
    assert open_range == {"lower_bound": "1000000", "upper_bound": None}  # open upper bound kept open
    assert "platform-transparency:meta:ad:880000000000003" in found  # second cursor page
    assert "platform-transparency:meta:ad:880000000000099" not in found  # another page's ad is never stored
    assert [len(r["requests"]) for r in receipts] == [2, 1, 1]
    assert all("after=C1" not in r["requests"][0]["path"] for r in receipts)
    assert "after=C1" in receipts[0]["requests"][1]["path"]
    assert found["platform-transparency:meta:advertiser:999000001"]["fields"]["funding_entities_as_declared"] == [
        "Paid for by Example Holdings Ltd"]
    assert not [p for p in PERSONAL if p in json.dumps([records, receipts])]
    with pytest.raises(SourcePackError) as refused:
        fetch_all("meta-ad-library-political", secret=None)
    assert refused.value.code == "authentication_failed"


def test_google_bundle_filters_declared_advertisers_keeps_spend_ranges_and_the_refresh_time_per_record():
    records, receipts = fetch_all("google-political-ads")
    found = by_key(records)
    advertiser = found["platform-transparency:google:advertiser:AR10000000000000000001"]
    assert advertiser["fields"]["fec_committee_ids_as_published"] == ["C00999901"]
    assert advertiser["fields"]["election_ids_declared"] == [h.US_ELECTION]
    ad = found["platform-transparency:google:ad:CR10000000000000000021"]
    assert ad["fields"]["spend_ranges_as_published"] == [
        {"currency": "GBP", "lower_bound": "800", "upper_bound": "40000"},
        {"currency": "USD", "lower_bound": "1000", "upper_bound": "50000"}]
    assert ad["fields"]["impressions_bucket_as_published"] == "100k-1M"
    assert all(r["data_as_of"] == "2099-11-10T06:00:00Z" for r in records)
    assert not any("AR10000000000000000009" in k or "CR10000000000000000091" in k for k in found)
    absent = found["platform-transparency:google:advertiser:AR10000000000000000004"]
    assert "not listed" in absent["fields"]["note"]
    listings = {(entry["selection_key"], entry["record_kind"]) for entry in receipts[0]["complete_listings"]}
    assert (f"google:{h.GOOGLE_ABSENT}", "ad") in listings


def test_lumen_is_gated_and_stores_only_permitted_fields_with_redactions_preserved():
    with pytest.raises(SourcePackError) as refused:
        fetch_all("lumen-notices", secret=None)
    assert refused.value.code == "authentication_failed" and "gated-not-granted" in str(refused.value)
    records, receipts = fetch_all("lumen-notices")
    notice = by_key(records)["platform-transparency:lumen:notice:99000002"]["fields"]
    assert notice["sender_name_as_published"] == "[Private]" and notice["title_as_published"].endswith("[REDACTED]")
    assert "[Private]" in notice["redactions_as_published"]
    assert notice["works_count"] == 1 and notice["infringing_url_count"] == 1
    assert {"body", "works", "sender_address"} <= set(receipts[0]["withheld_fields"])
    assert not [p for p in PERSONAL if p in json.dumps([records, receipts])]


def test_units_are_all_or_nothing_bounded_and_host_checked():
    item = h.source("meta-ad-library-political")
    item["platform_transparency"]["selection"]["units"][0]["delivery_date_max"] = "2101-01-01"
    with pytest.raises(SourcePackError):
        pt.PlatformTransparencyAdapter(item)
    item = h.source("dsa-sor-dumps")
    item["endpoint"] = "https://example.org"
    with pytest.raises(SourcePackError):
        pt.PlatformTransparencyAdapter(item)
    pages = h.native_pages("dsa-sor-dumps")
    pages[0]["final_url"] = "https://elsewhere.example/sor.zip"
    adapter = pt.PlatformTransparencyAdapter(h.source("dsa-sor-dumps"), transport=pt.fixture_transport(pages))
    with pytest.raises(SourcePackError) as moved:
        adapter.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=None)
    assert moved.value.code == "network_policy"
    original = pt.DSA_ROW_CAP
    try:
        pt.DSA_ROW_CAP = 2
        with pytest.raises(SourcePackError) as capped:
            fetch_all("dsa-sor-dumps")
        assert capped.value.code == "budget_exhausted"
    finally:
        pt.DSA_ROW_CAP = original
    with pytest.raises(SourcePackError):
        h.adapter("dsa-sor-dumps").fetch_page({"operation": "selection", "parameters": {"q": "x"}}, cursor=None)


def test_records_validate_against_the_published_record_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-platform-transparency-record-v2.json"
                         ).read_text())
    for source_id in h.SOURCES:
        for record in fetch_all(source_id)[0]:
            jsonschema.validate({**record, "evidence_origin": "fixture"}, schema)
