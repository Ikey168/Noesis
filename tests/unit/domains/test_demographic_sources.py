"""Demographic acquisition: Eurostat, UNHCR, IOM DTM, Destatis and Statistik BB through the real adapter (#1942, #1951, #1960)."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion.demographic_sources import (
    PROVIDER_CONTRACTS,
    DemographicsAdapter,
    demographic_declaration,
    fixture_transport,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import demographics_harness as h


def _series(page):
    return [r["demographic_series"] for r in page.records]


def test_every_provider_has_an_access_decision_and_nothing_is_assumed_live():
    assert set(PROVIDER_CONTRACTS) == {
        "eurostat",
        "unhcr",
        "iom-dtm",
        "bamf",
        "destatis",
        "statistik-bb",
    }
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert contract["access_decision"] in {"unverified-live", "not-implemented"}, (
            provider
        )
        assert contract["reason"] and contract["definitions"]
    assert PROVIDER_CONTRACTS["bamf"]["access_decision"] == "not-implemented"
    assert "never scraped" in PROVIDER_CONTRACTS["bamf"]["reason"]


def test_all_pinned_fixtures_replay_through_the_adapter():
    result = SourcePackConformance(h.ROOT).offline(h.manifest())
    ours = [s for s in result["sources"] if s["source_id"] in h.SOURCES.values()]
    assert len(ours) == 5 and all(s["valid"] for s in ours), ours


def test_eurostat_keeps_flags_code_lists_and_the_updated_vintage():
    page, _ = h.fetch("eurostat", 0)
    (series,) = _series(page)
    header = page.records[0]["demographic_release"]
    assert header["release_basis"] == "eurostat_dataset_updated"
    assert header["published_at"] == "2099-03-15T22:00:00"
    assert header["evidence_origin"] == "fixture"
    assert header["structure"]["status_labels"]["b"] == "break in time series"
    assert [o["flags"] for o in series["observations"]] == [[], [], ["p"]]
    assert series["definition"]["code_labels"]["sex"] == {
        "dimension_label": "Sex",
        "code": "T",
        "label": "Total",
    }
    # The definition comes from the declaration and code lists, never from the dataset title.
    assert "authored fixture" not in json.dumps(series["definition"])
    # A confidential cell stays unknown, with its flag.
    page, _ = h.fetch("eurostat", 4)
    (decisions,) = _series(page)
    assert decisions["observations"][1] == {
        "period": "2097",
        "value_text": None,
        "value": None,
        "flags": ["c", "not_published"],
        "flag_labels": {"c": "confidential", "not_published": "no value published"},
    }


def test_eurostat_refuses_a_declared_breakdown_the_code_lists_do_not_have():
    item = h.source("eurostat")
    item["demographics"]["documents"][0]["definition"] = {
        **item["demographics"]["documents"][0]["definition"],
        "breakdown": "country_of_birth",
    }
    with pytest.raises(SourcePackError) as refused:
        h.fetch("eurostat", 0, item=item)
    assert refused.value.code == "schema_drift" and "c_birth" in str(refused.value)


def test_eurostat_refuses_an_undeclared_unit_and_a_cube_without_updated_time():
    body = json.loads(h.body("eurostat_demo_pjan_de_2099-03.json"))
    body.pop("updated")
    with pytest.raises(SourcePackError) as refused:
        h.fetch("eurostat", 0, json.dumps(body))
    assert "updated" in str(refused.value)
    item = h.source("eurostat")
    item["demographics"]["documents"][0]["unit"] = {
        "code": "THS",
        "label": "thousand persons",
    }
    with pytest.raises(SourcePackError):
        h.fetch("eurostat", 0, item=item)


def test_unhcr_population_types_are_separate_series_with_coverage_notes():
    page, _ = h.fetch("unhcr", 0)
    series = _series(page)
    assert [s["indicator"] for s in series] == ["asylum_seekers", "refugees"]
    assert {s["definition"]["concept"] for s in series} == {
        "asylum_seeker_stock",
        "refugee_stock",
    }
    assert all(s["dimensions"] == {"country_of_origin": "XXO"} for s in series)
    assert all(s["geography"]["code"] == "DEU" for s in series)
    assert all(any("year-end stocks" in n for n in s["coverage_notes"]) for s in series)
    asylum = series[0]["observations"]
    assert asylum[1]["value"] is None and "not_published" in asylum[1]["flags"]
    header = page.records[0]["demographic_release"]
    assert (
        header["release_basis"] == "declared_publication"
        and header["published_on"] == "2099-06-12"
    )


def test_unhcr_refuses_a_multi_page_answer_and_a_response_without_a_release_date():
    body = json.loads(h.body("unhcr_population_2097_2098.json"))
    body["maxPages"] = 3
    with pytest.raises(SourcePackError) as refused:
        h.fetch("unhcr", 0, json.dumps(body))
    assert refused.value.code == "response_too_large"
    item = h.source("unhcr")
    item["demographics"]["documents"][0].pop("published_on")
    with pytest.raises(SourcePackError) as refused:
        h.fetch("unhcr", 0, item=item)
    assert "release date" in str(refused.value)
    page, _ = h.fetch(
        "unhcr",
        0,
        item=item,
        headers={"Last-Modified": "Mon, 15 Jun 2099 10:00:00 GMT"},
    )
    assert (
        page.records[0]["demographic_release"]["release_basis"] == "http_last_modified"
    )


def test_dtm_needs_its_key_and_keeps_rounds():
    with pytest.raises(SourcePackError) as refused:
        h.fetch("dtm", 0, secret=None)
    assert refused.value.code == "authentication_failed"
    page, _ = h.fetch("dtm", 0)
    north, south = _series(page)
    assert (
        north["geography"]["code"] == "XXA01"
        and north["geography"]["scheme"] == "cod-ab-pcode"
    )
    assert [o["round"] for o in north["observations"]] == ["7", "8"]
    assert south["observations"][0]["value"] == "4100"


def test_genesis_reads_the_table_version_ags_code_and_signs():
    page, _ = h.fetch("destatis", 0)
    series = _series(page)
    header = page.records[0]["demographic_release"]
    assert header["release_basis"] == "genesis_table_updated"
    assert header["published_at"] == "2099-06-20T08:00:00"
    assert {(s["geography"]["code"], s["dimensions"]["NAT"]) for s in series} == {
        ("09", "INSGESAMT"),
        ("09", "NATA"),
        ("11", "INSGESAMT"),
        ("11", "NATA"),
    }
    bayern = next(
        s
        for s in series
        if s["geography"]["code"] == "09" and s["dimensions"]["NAT"] == "INSGESAMT"
    )
    assert bayern["observations"][-1]["value"] is None and bayern["observations"][-1][
        "flags"
    ] == ["."]
    page, _ = h.fetch("destatis", 1)
    emigration = next(s for s in _series(page) if s["indicator"] == "BEV082")
    # "-" is the publisher's exact zero, kept with its sign.
    assert emigration["observations"][-1]["value"] == "0"
    assert emigration["observations"][-1]["value_text"] == "-"


def test_genesis_refuses_an_undeclared_value_column_and_a_table_mismatch():
    csv = h.body("genesis_12711-0005.csv").replace(
        "BEV082__Fortzuege_in_das_Ausland__Anzahl", "BEV099__Neu__Anzahl"
    )
    with pytest.raises(SourcePackError) as refused:
        h.fetch("destatis", 1, ("genesis_12711-0005_metadata.json", csv))
    assert "BEV099" in str(refused.value)
    meta = h.body("genesis_12711-0005_metadata.json").replace(
        '"Code": "12711-0005"', '"Code": "12711-0001"'
    )
    with pytest.raises(SourcePackError):
        h.fetch("destatis", 1, (meta, "genesis_12711-0005.csv"))


def test_berlin_districts_keep_code_and_census_base_and_refuse_undeclared_columns():
    page, _ = h.fetch("berlin", 0)
    series = _series(page)
    assert {s["geography"]["code"] for s in series} == {"001", "002", "003"}
    assert {s["definition"]["population_base"] for s in series} == {"census-2011"}
    mitte = next(
        s
        for s in series
        if s["geography"]["code"] == "001" and s["indicator"] == "residents"
    )
    assert mitte["observations"] == [
        {
            "period": "2098-12-31",
            "value_text": "381.000",
            "value": "381000",
            "flags": [],
            "flag_labels": {},
        }
    ]
    drifted = (
        h.body("statbb_bezirke_2098.csv")
        .replace("Auslaender", "Auslaender;Deutsche")
        .replace("110.100", "110.100;1")
    )
    with pytest.raises(SourcePackError):
        h.fetch("berlin", 0, drifted)


def test_network_policy_refuses_another_host_and_a_cross_host_redirect():
    with pytest.raises(SourcePackError) as refused:
        h.fetch("unhcr", 0, final_url="https://evil.example/population")
    assert refused.value.code == "network_policy"
    item = h.source("unhcr")
    item["demographics"]["documents"][0]["url"] = (
        "https://evil.example/population/v1/population/"
    )
    with pytest.raises(SourcePackError):
        demographic_declaration(item)


def test_the_default_transport_is_the_runtime_same_host_transport():
    from functools import partial

    from src.ingestion.source_pack_runtime import HTTPSPageAdapter

    adapter = DemographicsAdapter(h.source("eurostat"))
    assert isinstance(adapter.transport, partial)
    assert adapter.transport.func is HTTPSPageAdapter._request
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return {"status": 503, "headers": {}, "content": b""}

    adapter = DemographicsAdapter(h.source("eurostat"), transport=transport)
    with pytest.raises(SourcePackError) as refused:
        adapter.fetch_page({"operation": "release", "parameters": {}}, cursor=None)
    assert refused.value.code == "source_unavailable"
    assert calls[0]["timeout"] == 30 and calls[0]["url"].startswith(
        "https://ec.europa.eu/"
    )


def test_undeclared_parameters_and_manifest_mistakes_are_refused():
    adapter = DemographicsAdapter(h.source("eurostat"), transport=fixture_transport([]))
    with pytest.raises(SourcePackError) as refused:
        adapter.fetch_page(
            {"operation": "release", "parameters": {"geo": "FR"}}, cursor=None
        )
    assert refused.value.code == "parameter_forbidden"
    item = copy.deepcopy(h.source("eurostat"))
    item["demographics"]["documents"][0]["dataset"] = "nama_10_gdp"
    with pytest.raises(SourcePackError):
        demographic_declaration(item)
    item = copy.deepcopy(h.source("eurostat"))
    item["demographics"]["documents"][0]["definition"]["measure"] = "flow"
    with pytest.raises(SourcePackError):
        demographic_declaration(item)


def test_two_documents_naming_the_same_publication_are_refused():
    item = copy.deepcopy(h.source("berlin"))
    item["demographics"]["documents"][1]["url"] = item["demographics"]["documents"][0][
        "url"
    ]
    with pytest.raises(SourcePackError) as refused:
        demographic_declaration(item)
    assert "distinct publication" in str(refused.value)
