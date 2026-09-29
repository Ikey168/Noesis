"""Sanctions-list source contracts, parsers and the runtime adapter (#1910, #1920, #1927).

All list files are authored in the providers' documented formats and name
fictional parties; no test touches the network.
"""

from __future__ import annotations

import copy
from functools import partial

import pytest

from src.ingestion.sanctions_sources import (
    FORMATS,
    PROVIDER_CONTRACTS,
    SanctionsFormatError,
    SanctionsListAdapter,
    fixture_transport,
    parse_snapshot,
)
from src.ingestion.source_pack_runtime import (
    HTTPSPageAdapter,
    RuntimeAdapterFactory,
    _validate_redirect,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit.sanctions_harness import (
    FIXTURES,
    ROOT,
    SOURCES,
    list_page,
    manifest,
    source,
)


def parse(fmt, name):
    return parse_snapshot(fmt, (FIXTURES / name).read_bytes())


def test_access_decisions_are_recorded_and_the_sanctions_map_is_not_scraped():
    decisions = {k: v["access_decision"] for k, v in PROVIDER_CONTRACTS.items()}
    assert decisions == {
        "eu-fsf": "unverified-live",
        "eu-sanctions-map": "not-implemented",
        "un-sc": "unverified-live",
        "ofac-sls": "unverified-live",
        "uk-sanctions-list": "unverified-live",
    }
    assert "never scraped" in PROVIDER_CONTRACTS["eu-sanctions-map"]["reason"]
    for key, contract in PROVIDER_CONTRACTS.items():
        if key == "eu-sanctions-map":
            continue
        for field in (
            "format",
            "authentication",
            "rate_limits",
            "pagination",
            "cadence",
            "publication_model",
            "revisions",
            "delistings",
            "identifiers",
            "aliases",
            "legal_act_reference",
            "terms",
        ):
            assert contract[field], (key, field)
    assert set(FORMATS.values()) == {"eu", "un", "ofac", "uk"}


def test_list_sources_are_pinned_bounded_and_replay_offline():
    value = manifest()
    lists = {
        s["source_id"]: s
        for s in value["sources"]
        if s["connector"] == "sanctions-list"
    }
    assert set(lists) == set(SOURCES.values())
    for item in lists.values():
        assert item["mapping"]["target_schema"] == "noesis-sanctions-record-v1"
        assert item["endpoint"].startswith("https://") and item["auth"] == {
            "kind": "none"
        }
        assert item["budgets"]["max_pages"] == 1 and item["license"][
            "terms_url"
        ].startswith("https://")
    result = SourcePackConformance(ROOT).offline(value)
    assert result["valid"]


def test_eu_fsf_entries_keep_reference_numbers_aliases_and_acts_as_written():
    snapshot = parse("eu-fsf-xml-1.1", "eu_fsf_2026-03-01.xml")
    assert snapshot["publication_date"] == "2026-03-01" and snapshot["entry_count"] == 3
    person = next(e for e in snapshot["entries"] if e["entry_id"] == "EU.9003.03")
    assert person["party_kind"] == "person" and person["dates_of_birth"] == [
        "1970-01-01"
    ]
    assert {n["language"] for n in person["names"]} == {
        "EN",
        "RU",
    }  # transliterations kept as stated
    assert person["identifiers"] == [
        {
            "kind": "passport",
            "value": "X1234567",
            "source_type": "National passport",
            "country": "RU",
            "note": None,
        }
    ]
    assert person["cross_references"]["un_reference"] == "QDi.902"
    vessel = next(e for e in snapshot["entries"] if e["entry_id"] == "EU.9002.02")
    assert (
        vessel["identifiers"][0]["kind"] == "imo"
        and vessel["identifiers"][0]["value"] == "9999991"
    )
    basis = vessel["legal_basis"][0]
    assert basis["celex"] == "32014R0269" and basis["role"] == "amending-act"
    assert (
        basis["entry_into_force"] == "2026-02-21"
        and vessel["listed_on"] == "2026-02-20"
    )
    assert vessel["programmes"] == [{"code": "UKR", "name": None}]


def test_un_ofac_and_uk_keep_their_own_identifiers_quality_flags_and_regimes():
    un = parse("un-sc-xml", "un_sc_2026-03-10.xml")
    person = next(e for e in un["entries"] if e["entry_id"] == "QDi.902")
    assert {
        (n["name"], n["quality"]) for n in person["names"] if n["kind"] == "alias"
    } == {("Ivan Fiktsional", "good"), ("The Example", "low")}
    assert (
        person["listed_on"] == "2025-10-01"
        and person["programmes"][0]["code"] == "Fixture Regime"
    )
    assert (
        person["legal_basis"] == []
    )  # resolutions are not CELLAR works; nothing is invented
    ofac = parse("ofac-sdn-xml", "ofac_sdn_2026-06-05.xml")
    assert ofac["publication_date"] == "2026-06-05"
    vessel = next(e for e in ofac["entries"] if e["entry_id"] == "99001")
    assert vessel["party_kind"] == "vessel" and {
        i["kind"] for i in vessel["identifiers"]
    } == {"imo", "call_sign"}
    assert ("STAR OF FICTION", "f.k.a.", "strong") in {
        (n["name"], n["kind"], n["quality"]) for n in vessel["names"]
    }
    assert (
        vessel["programmes"] == [{"code": "FIXTURE-EO", "name": None}]
        and vessel["listed_on"] is None
    )
    uk = parse("uk-sanctions-list-xml", "uk_sanctions_2026-03-15.xml")
    ship = next(e for e in uk["entries"] if e["entry_id"] == "RUS9002")
    assert ship["party_kind"] == "vessel" and ship["identifiers"][0] == {
        "kind": "imo",
        "value": "9999991",
        "source_type": "IMO number",
        "country": None,
        "note": None,
    }
    entity = next(e for e in uk["entries"] if e["entry_id"] == "RUS9001")
    assert entity["cross_references"]["ofsi_group_id"] == "99901"
    assert ("OOO Examplar Freight", "low quality") in {
        (n["name"], n["quality"]) for n in entity["names"]
    }


def test_parsers_refuse_drift_duplicates_and_entities():
    with pytest.raises(SanctionsFormatError) as caught:
        parse_snapshot("un-sc-xml", (FIXTURES / "eu_fsf_2026-03-01.xml").read_bytes())
    assert caught.value.code == "schema_drift"
    raw = (
        (FIXTURES / "ofac_sdn_2026-03-05.xml")
        .read_text()
        .replace("<uid>99002</uid>", "<uid>99001</uid>")
    )
    with pytest.raises(SanctionsFormatError) as caught:
        parse_snapshot("ofac-sdn-xml", raw.encode())
    assert caught.value.code == "schema_drift"
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><sdnList>&a;</sdnList>'
    )
    with pytest.raises(SanctionsFormatError):
        parse_snapshot("ofac-sdn-xml", bomb)
    with pytest.raises(SanctionsFormatError) as caught:
        parse_snapshot("unknown-format", b"<x/>")
    assert caught.value.code == "unsupported_format"


def test_adapter_emits_one_complete_snapshot_and_never_truncates():
    page, _ = list_page("eu", "eu_fsf_2026-03-01.xml")
    assert page.next_cursor is None and len(page.records) == 3
    headers = {r["sanctions_snapshot"]["file_sha256"] for r in page.records}
    assert len(headers) == 1 and page.receipt["entries"] == 3
    assert {r["id"] for r in page.records} == {
        "eu:EU.9001.01",
        "eu:EU.9002.02",
        "eu:EU.9003.03",
    }
    with pytest.raises(SourcePackError) as caught:
        list_page("eu", "eu_fsf_2026-03-01.xml", limit=2)
    assert caught.value.code == "budget_exhausted"


def test_adapter_uses_the_runtime_transport_and_refuses_other_hosts():
    item = source("ofac-sls")
    adapter = SanctionsListAdapter(item)
    assert (
        isinstance(adapter.transport, partial)
        and adapter.transport.func is HTTPSPageAdapter._request
    )
    assert adapter.transport.keywords == {"max_bytes": item["budgets"]["max_bytes"]}
    compiled = RuntimeAdapterFactory().compile(item)
    assert isinstance(compiled, SanctionsListAdapter)
    # OFAC exports may answer by redirecting to another host; the runtime refuses that.
    with pytest.raises(SourcePackError) as caught:
        _validate_redirect(
            item["endpoint"],
            "https://example-bucket.s3.amazonaws.com/sdn.xml",
            resolver=lambda _h: ["8.8.8.8"],
        )
    assert caught.value.code == "network_policy"
    with pytest.raises(SourcePackError) as caught:
        list_page(
            "ofac",
            "ofac_sdn_2026-03-05.xml",
            final_url="https://elsewhere.example.org/SDN.XML",
        )
    assert caught.value.code == "network_policy"


@pytest.mark.parametrize(
    "status,code",
    [
        (503, "source_unavailable"),
        (429, "rate_limited"),
        (403, "authentication_failed"),
        (404, "schema_drift"),
    ],
)
def test_adapter_classifies_http_failures(status, code):
    item = source("un-sc-consolidated")
    page = {
        "request": "/resources/xml/en/consolidated.xml",
        "status": status,
        "body": "",
    }
    adapter = SanctionsListAdapter(item, transport=fixture_transport([page]))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page(
            {"operation": "records", "parameters": {}, "limit": 10}, cursor=None
        )
    assert caught.value.code == code


def test_adapter_refuses_parameters_cursors_and_mismatched_formats():
    item = source("uk-sanctions-list")
    adapter = SanctionsListAdapter(item, transport=fixture_transport([]))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page(
            {"operation": "records", "parameters": {"q": "x"}}, cursor=None
        )
    assert caught.value.code == "parameter_forbidden"
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "records", "parameters": {}}, cursor="{}")
    assert caught.value.code == "cursor_drift"
    broken = copy.deepcopy(item)
    broken["sanctions"]["list"] = "eu"
    with pytest.raises(SourcePackError) as caught:
        SanctionsListAdapter(broken)
    assert caught.value.code == "invalid_mapping"
