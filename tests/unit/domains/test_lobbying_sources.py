"""Register export parsing and the lobbying-register connector (#1925, #1944, #1953, #1962)."""

from __future__ import annotations

import functools
import json

import pytest

from src.ingestion.lobbying_sources import (
    FORMATS,
    PROVIDER_CONTRACTS,
    LobbyingFormatError,
    LobbyingRegisterAdapter,
    parse_export,
    parse_range_text,
    reference,
    reference_key,
    references_in_text,
    spend_range,
)
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import lobbying_harness as h


def parse(fmt, name, **kw):
    return parse_export(fmt, (h.FIXTURES / name).read_bytes(), **kw)


def by_id(export):
    return {e["native_id"]: e for e in export["entries"]}


def test_access_decisions_cover_every_audited_source_and_none_is_live():
    assert set(PROVIDER_CONTRACTS) == {
        "eu-transparency-register",
        "de-lobbyregister",
        "ep-meetings",
        "ec-meetings",
        "uk-orcl",
        "us-lda",  # added by the legislation feature (#2208, LT08)
        "bundestag-party-financing",
        "integrity-watch-eu",
    }
    for contract in PROVIDER_CONTRACTS.values():
        assert contract["access_decision"] in {"unverified-live", "not-implemented"}
        assert contract["reason"]
    assert (
        PROVIDER_CONTRACTS["integrity-watch-eu"]["access_decision"] == "not-implemented"
    )
    assert "aggregator" in PROVIDER_CONTRACTS["integrity-watch-eu"]["publisher"]
    assert (
        PROVIDER_CONTRACTS["bundestag-party-financing"]["access_decision"]
        == "not-implemented"
    )
    implemented = {
        c["format"]
        for c in PROVIDER_CONTRACTS.values()
        if c["access_decision"] != "not-implemented"
    }
    assert implemented == set(FORMATS)


def test_eu_transparency_register_entries_as_filed():
    export = parse("eu-tr-xml", "eu_tr_2099-01-15.xml")
    assert (
        export["publication_date"] == "2099-01-15"
        and export["full_export"]
        and export["entry_count"] == 3
    )
    entries = by_id(export)
    assoc = entries["000000000101-01"]
    assert assoc["name"] == "Fictional Grid Association" and assoc[
        "section"
    ].startswith("II")
    assert {"scheme": "lei", "value": "5299000FIXTURE000017", "country": "DE"} in assoc[
        "identifiers"
    ]
    files = [i for i in assoc["interests"] if i["kind"] == "legislative_file"]
    assert files[0]["references"][0]["key"] == "eu-procedure:2099/0101(cod)"
    assert files[0]["references"][0]["extracted_from_text"] is False
    (costs,) = assoc["spend"]
    assert (costs["lower"], costs["upper"], costs["currency"]) == (
        "100000",
        "199999",
        "EUR",
    )
    assert costs["period"] == {"start": "2098-01-01", "end": "2098-12-31"}
    assert not set(costs) & {"midpoint", "total", "estimate", "value"}
    grant = assoc["grants"][0]
    assert (
        grant["programme"] == "Fictional Research Programme"
        and "not confirmed" in grant["assertion"]
    )
    consultancy = entries["000000000202-02"]
    clients = {c["name"]: c for c in consultancy["clients"]}
    assert clients["Nordic Example Energy AB"]["spend"]["lower"] is None
    assert clients["Nordic Example Energy AB"]["spend"]["lower_open"] is True
    assert clients["Nordic Example Energy AB"]["spend"]["as_filed"] == "< 10 000"
    assert (
        clients["Fictional Grid Association"]["native_ids"][0]["value"]
        == "000000000101-01"
    )
    forum = entries["000000000303-03"]
    assert [
        i["references"] for i in forum["interests"] if i["kind"] == "legislative_file"
    ] == [[]]


def test_lobbyregister_entries_printed_papers_and_link_only_statements():
    export = parse("de-lobbyregister-json", "de_lobbyregister_2099-02-01.json")
    entry = by_id(export)["R009901"]
    assert (
        entry["legal_form"].startswith("Eingetragener Verein")
        and entry["native_version"] == "3"
    )
    project = next(i for i in entry["interests"] if i["kind"] == "regulatory_project")
    keys = {r["key"] for r in project["references"]}
    assert keys == {"de-regulatory-project:RV0099", "de-drucksache:21/9901"}
    assert all(not r["extracted_from_text"] for r in project["references"])
    (expense,) = entry["spend"]
    assert (expense["kind"], expense["lower"], expense["upper"]) == (
        "expenditure",
        "100001",
        "110000",
    )
    (paper,) = entry["documents"]
    assert (
        paper["retention"] == "link-only"
        and paper["text"] is None
        and paper["url"].startswith("https://")
    )
    kept = parse(
        "de-lobbyregister-json", "de_lobbyregister_2099-02-01.json", documents="text"
    )
    assert by_id(kept)["R009901"]["documents"][0]["text"].startswith("Authored fixture")
    assert (
        export["full_export"] is True
    )  # the export states a total equal to its entries
    capped = json.loads((h.FIXTURES / "de_lobbyregister_2099-02-01.json").read_text())
    capped["totalResultCount"] = 250
    assert (
        parse_export("de-lobbyregister-json", json.dumps(capped).encode())[
            "full_export"
        ]
        is False
    )
    del capped["totalResultCount"]
    assert (
        parse_export("de-lobbyregister-json", json.dumps(capped).encode())[
            "full_export"
        ]
        is False
    )
    consultancy = by_id(export)["R009902"]
    assert [c["name"] for c in consultancy["clients"]] == [
        "Fiktiver Netzverband e.V.",
        "Other Example GmbH",
    ]


def test_meeting_declarations_keep_the_organisation_string_beside_its_register_id():
    ep = parse("ep-meetings-csv", "ep_meetings_2099-02-10.csv")
    rows = sorted(ep["entries"], key=lambda e: (e["date"], e["official"]["id"]))
    rapporteur = rows[0]
    assert rapporteur["official"] == {
        "id": "ep-mep:990001",
        "name": "Alex Fictional",
        "role": "Rapporteur",
        "institution": "European Parliament",
        "committee": "ITRE",
    }
    assert rapporteur["references"][0]["key"] == "eu-procedure:2099/0101(cod)"
    (org,) = rapporteur["organisations"]
    assert org["name"] == "Fictional Grid Association" and org["register_ids"] == [
        {"scheme": "eu-tr", "value": "000000000101-01"}
    ]
    assert "000000000101-01" in org["as_declared"]
    free_text = rows[1]
    assert free_text["references"] == [] and free_text["subject"].startswith(
        "Exchange of views"
    )
    ec = parse("ec-meetings-json", "ec_meetings_2099-02-12.json")
    (meeting,) = ec["entries"]
    assert (
        meeting["official"]["id"] == "ec-official:cab-99"
        and meeting["published_on"] == "2099-02-12"
    )
    assert meeting["organisations"][0]["name"] == "Example Public Affairs SARL"


def test_uk_quarterly_returns_are_separate_revisions_of_one_registrant():
    export = parse("uk-orcl-csv", "uk_orcl_2099-04-30.csv")
    returns = [
        (e["native_id"], e["native_version"], [c["name"] for c in e["clients"]])
        for e in export["entries"]
    ]
    assert returns == [
        (
            "ORCL0099",
            "2098-Q4",
            ["Fictional Grid Association", "Sample Charging Operator SA"],
        ),
        ("ORCL0099", "2099-Q1", ["Fictional Grid Association", "Other Example GmbH"]),
    ]
    assert export["entries"][1]["period"] == {
        "return": "2099-Q1",
        "start": "2099-01-01",
        "end": "2099-03-31",
    }
    assert export["entries"][0]["identifiers"] == [
        {"scheme": "gb-coh", "value": "09990099", "country": "GB"}
    ]


def test_reference_keys_are_exact_and_scoped_to_their_jurisdiction():
    assert reference_key("eu-procedure", "2099/0101(COD)") == reference_key(
        "eu-procedure", "2099/0101-cod"
    )
    assert reference_key("eu-procedure", "fictional grid tariffs") is None
    assert reference_key("celex", "32099R0101") == "celex:32099R0101"
    assert (
        reference_key("de-drucksache", "BT-Drucksache 21/09901")
        == "de-drucksache:21/9901"
    )
    assert (
        reference("de-drucksache", "21/9901", source_field="x")["jurisdiction"] == "DE"
    )
    found = references_in_text(
        "On 2099/0101 (COD) and CELEX 32099R0101, not grid tariffs 2099",
        source_field="f",
    )
    assert {r["key"] for r in found} == {
        "eu-procedure:2099/0101(cod)",
        "celex:32099R0101",
    }
    assert all(r["extracted_from_text"] for r in found)


def test_spend_ranges_keep_open_bounds_and_refuse_inverted_or_currencyless_ranges():
    assert parse_range_text(">= 10 000 000") == ("10000000", None)
    assert parse_range_text("100 000 - 199 999") == ("100000", "199999")
    assert spend_range("costs", None, None, "EUR", None, None, as_filed="") is None
    assert spend_range("costs", "1", "2", "", None, None, as_filed="") is None
    with pytest.raises(LobbyingFormatError):
        spend_range("costs", "5", "1", "EUR", None, None, as_filed="5-1")


def test_malformed_exports_are_provider_drift():
    with pytest.raises(LobbyingFormatError) as bad:
        parse_export("eu-tr-xml", b"<wrong/>")
    assert bad.value.code == "schema_drift"
    with pytest.raises(LobbyingFormatError):
        parse_export("ep-meetings-csv", b"mep_id,title\n1,x\n")
    with pytest.raises(LobbyingFormatError):
        parse_export(
            "uk-orcl-csv",
            (h.FIXTURES / "uk_orcl_2099-04-30.csv")
            .read_bytes()
            .replace(b"2098 Q4", b"late"),
        )


def test_adapter_marks_fixture_evidence_and_uses_the_runtime_transport_by_default():
    page, item = h.page("eu-tr", "eu_tr_2099-01-15.xml")
    assert page.receipt["evidence_origin"] == "fixture" and page.next_cursor is None
    header = page.records[0]["lobbying_export"]
    assert (
        header["evidence_origin"] == "fixture"
        and header["entry_count"] == 3
        and header["full_export"]
    )
    default = LobbyingRegisterAdapter(item)
    assert isinstance(default.transport, functools.partial)
    assert default.transport.func is HTTPSPageAdapter._request
    assert default.transport.keywords == {"max_bytes": item["budgets"]["max_bytes"]}
    live = LobbyingRegisterAdapter(
        item,
        transport=lambda **_: {
            "status": 200,
            "content": (h.FIXTURES / "eu_tr_2099-01-15.xml").read_bytes(),
        },
    )
    fetched = live.fetch_page(
        {"operation": "export", "parameters": {}, "limit": 10}, cursor=None
    )
    assert fetched.receipt["evidence_origin"] == "live"


def test_adapter_refuses_cross_host_redirects_errors_ad_hoc_queries_and_truncation():
    with pytest.raises(SourcePackError) as moved:
        h.page(
            "eu-tr", "eu_tr_2099-01-15.xml", final_url="https://data.example/export.xml"
        )
    assert moved.value.code == "network_policy"
    with pytest.raises(SourcePackError) as budget:
        h.page("eu-tr", "eu_tr_2099-01-15.xml", limit=2)
    assert budget.value.code == "budget_exhausted"
    with pytest.raises(SourcePackError) as drift:
        h.page("eu-tr", "eu_tr_2099-01-15.xml", body="<not-xml")
    assert drift.value.code == "schema_drift"
    item = h.source("eu-transparency-register")
    adapter = LobbyingRegisterAdapter(
        item, transport=lambda **_: {"status": 429, "headers": {"Retry-After": "3"}}
    )
    with pytest.raises(SourcePackError) as limited:
        adapter.fetch_page(
            {"operation": "export", "parameters": {}, "limit": 10}, cursor=None
        )
    assert limited.value.code == "rate_limited"
    with pytest.raises(SourcePackError) as query:
        adapter.fetch_page(
            {"operation": "export", "parameters": {"q": "x"}, "limit": 10}, cursor=None
        )
    assert query.value.code == "parameter_forbidden"


def test_a_bounded_selection_emits_only_named_entries():
    item = h.source("eu-transparency-register")
    item["lobbying"]["selection"] = {"native_ids": ["000000000101-01"]}
    fetched, _ = h.page("eu-tr", "eu_tr_2099-01-15.xml", item=item)
    assert [r["lobbying_entry"]["native_id"] for r in fetched.records] == [
        "000000000101-01"
    ]
    assert fetched.records[0]["lobbying_export"]["selection"] == ["000000000101-01"]


def test_the_source_pack_declares_pinned_fixtures_that_replay_offline():
    manifest = h.manifest()
    # Lobbying sources shipped in 1.1.0; 1.2.0 adds the elections feature's result sources (#1908); 1.3.0 adds the
    # legislation features' sources and the US LDA register whose filings name bills (#2208).
    assert manifest["version"] == "1.3.0"
    lobbying = [s for s in manifest["sources"] if s["connector"] == "lobbying-register"]
    assert {s["source_id"] for s in lobbying} == set(h.SOURCES.values()) | {"us-senate-lda"}
    for item in lobbying:
        assert item["mapping"]["target_schema"] == "noesis-lobbying-record-v1"
        assert (
            item["license"]["terms_url"].startswith("https://")
            and item["budgets"]["max_pages"] == 1
        )
    report = SourcePackConformance(h.ROOT).offline(manifest)
    assert report["valid"], [s for s in report["sources"] if not s["valid"]]
    fixture = json.loads((h.ROOT / lobbying[0]["fixture"]["path"]).read_text())
    assert "fictional" in fixture["note"]
