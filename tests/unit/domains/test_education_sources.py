"""Education-statistics source audit and acquisition: IPEDS, ETER, UIS, OECD EAG and Eurostat R&D (#2374, #2383,
#2391, #2397, #2404, #2408)."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion.education_sources import (
    BOUNDED_COVERAGE,
    EXCLUSIONS,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    EducationStatisticsAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import education_harness as h


def items(name, *, revision=False):
    return [r["education_item"] for page in h.fetch(name, revision=revision) for r in page]


def headers(name, *, revision=False):
    return [page[0]["education_release"] for page in h.fetch(name, revision=revision) if page]


def stat(found, code, subject):
    return next(i for i in found if i["record_kind"] != "institution_profile" and i["indicator"]["code"] == code
                and i["subject"]["code"] == subject)


def test_every_source_has_a_recorded_contract_and_bounded_coverage():
    assert set(PROVIDER_CONTRACTS) == {"ipeds", "eter", "unesco-uis", "oecd-eag", "eurostat-rd"}
    for provider, contract in PROVIDER_CONTRACTS.items():
        for key in ("access", "entry_points", "format", "identifiers", "licence", "attribution", "rate_limits",
                    "update_cadence", "revision_model", "temporal_semantics"):
            assert contract[key], (provider, key)
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert provider in BOUNDED_COVERAGE
    assert "provisional" in PROVIDER_CONTRACTS["ipeds"]["revision_model"]
    assert "rank" in BOUNDED_COVERAGE["out_of_scope"] and "university rankings or league tables" in EXCLUSIONS
    audit = (h.ROOT / "docs/development/education-evidence/source-audit.md").read_text()
    for name in ("IPEDS", "ETER", "UIS", "Education at a Glance", "Eurostat", "ranking"):
        assert name in audit


def test_the_scientific_pack_declares_pinned_fixtures_that_replay_offline():
    manifest = h.manifest()
    # 1.2.0 adds the education-statistics sources (#2227); every earlier source is kept verbatim.
    assert manifest["version"] == "1.2.0" and manifest["domains"] == ["scientific"]
    ours = {s["source_id"]: s for s in manifest["sources"] if s["connector"] == "education-statistics"}
    assert set(ours) == set(h.SOURCES.values())
    result = SourcePackConformance(h.ROOT).offline(manifest)
    assert result["valid"]
    records = {s["source_id"]: s["records"] for s in result["sources"] if s["source_id"] in ours}
    assert records == {"ipeds-institution-statistics": 10, "eter-institution-statistics": 12,
                       "unesco-uis-education-indicators": 5, "oecd-eag-education-indicators": 3,
                       "eurostat-rd-statistics": 4}
    assert not (h.ROOT / "packs/education").exists()


def test_ipeds_values_keep_variable_codes_units_survey_year_flags_and_release_stage():
    found = items("ipeds")
    profiles = [i for i in found if i["record_kind"] == "institution_profile"]
    assert {p["subject"]["code"] for p in profiles} == {"100001", "100002"}  # the undeclared UNITID is ignored
    fsu = next(p for p in profiles if p["subject"]["code"] == "100001")["subject"]
    assert fsu["label"] == "Fictional State University" and fsu["city"] == "Springfield"
    assert fsu["stated_ids"] == {"opeid": "09999900"}
    enrolment = stat(found, "EFTOTLT", "100001")
    assert enrolment["indicator"]["label"] == "Grand total" and enrolment["unit"] == {"unit": "persons"}
    first = enrolment["observations"][0]
    assert (first["period"], first["value"], first["status"]) == ("2098", "25000", "reported")
    assert enrolment["dimensions"] == {"EFALEVEL": "1", "survey": "fall-enrollment"}
    imputed = stat(found, "EFTOTLT", "100002")
    assert imputed["observations"][0]["flags"]["imputation_flag"] == "L"
    assert imputed["notes"][0]["kind"] == "imputation" and "Group Median" in imputed["notes"][0]["text"]
    blank = stat(found, "CTOTALT", "100002")["observations"][0]
    assert blank["status"] == "not_applicable" and blank["value"] is None
    suppressed = stat(found, "F1D01", "100002")
    assert suppressed["observations"][0]["status"] == "suppressed" and suppressed["observations"][0]["value"] is None
    assert suppressed["observations"][0]["special_code"] == "-2"
    assert stat(found, "F1D01", "100001")["unit"] == {"unit": "currency", "currency": "USD"}
    assert {hd["release_stage"] for hd in headers("ipeds")[1:]} == {"provisional"}
    final = headers("ipeds", revision=True)
    assert [r["release_stage"] for r in final] == ["final"] and final[0]["published_on"] == "2099-10-01"
    assert stat(items("ipeds", revision=True), "EFTOTLT", "100001")["observations"][0]["value"] == "25140"


def test_eter_special_codes_are_codes_never_zero_and_identifiers_are_kept():
    found = items("eter")
    musterhochschule = next(i for i in found if i["record_kind"] == "institution_profile"
                            and i["subject"]["code"] == "DE0002")["subject"]
    assert musterhochschule["stated_ids"] == {"ror": "https://ror.org/0zth03c45"}
    assert musterhochschule["national_id"] == "DE-HS-0002"
    confidential = stat(found, "STAFF.ACAD.FTE", "DE0002")["observations"][0]
    assert (confidential["status"], confidential["special_code"], confidential["value"]) == ("confidential", "c", None)
    missing = stat(found, "EXP.CURR.TOTAL", "DE0002")["observations"][0]
    assert (missing["status"], missing["special_code"], missing["value"]) == ("missing", "m", None)
    assert stat(found, "STAFF.ACAD.FTE", "DE0001")["observations"][0]["value"] == "2100.5"
    # Only the declared reference year is read.
    assert {o["period"] for i in found if i.get("observations") for o in i["observations"]} == {"2098"}
    assert headers("eter")[0]["release_label"].startswith("ETER data release 2099-06")


def test_uis_qualifiers_and_footnotes_are_notes_and_the_version_is_the_vintage():
    found = items("uis")
    france = stat(found, "GER.5T8", "FRA")
    assert france["notes"] == [{"kind": "qualifier", "code": "UIS_EST", "text": "UIS estimation", "period": "2096"}]
    germany = stat(found, "GER.5T8", "DEU")
    assert germany["notes"][0]["kind"] == "footnote" and "ISCED 2011" in germany["notes"][0]["text"]
    assert germany["isced"] == {"scheme": "ISCED 2011", "level": "5T8", "label": None}
    assert "percentage of the population" in germany["indicator"]["definition"]
    release = headers("uis")[0]
    assert release["release_basis"] == "uis_last_data_update" and release["published_on"] == "2099-02-27"
    assert release["structure"]["version"] == "20990301"
    revised = stat(items("uis", revision=True), "GER.5T8", "DEU")
    assert [o["value"] for o in revised["observations"]] == ["73.1", "74.6"]


def test_oecd_eag_goes_through_the_sdmx_connector_with_status_and_comments_as_notes():
    found = items("oecd")
    release = headers("oecd")[0]
    assert release["url"].startswith("https://sdmx.oecd.org/public/rest/data/OECD.EDU.IMEP,")
    assert "format=csvfile" in release["url"] and release["release_stage"] == "edition"
    france = stat(found, "EXP_INST_GDP", "FRA")
    assert {n["kind"] for n in france["notes"]} == {"status", "footnote"}
    assert any("Break in series" in n["text"] for n in france["notes"])
    usa = stat(found, "EXP_INST_GDP", "USA")["observations"][0]
    assert (usa["status"], usa["special_code"], usa["value"]) == ("missing", "M", None)
    assert stat(found, "EXP_INST_GDP", "DEU")["isced"]["level"] == "ISCED11_5T8"


def test_eurostat_rd_keeps_flags_units_and_last_update_without_conversion():
    found = items("eurostat")
    germany = stat(found, "GERD_HES", "DE")
    assert germany["unit"]["currency"] == "EUR" and germany["unit"]["scale"] == "million"
    assert [o["value"] for o in germany["observations"]] == ["21000.5", "21900.2"]
    assert germany["notes"][0] == {"kind": "flag", "code": "b", "text": "break in time series", "period": "2096"}
    assert germany["dimensions"]["sectperf"] == "HES"
    france = stat(found, "GERD_HES", "FR")
    assert france["notes"] == [{"kind": "flag", "code": "p", "text": "provisional", "period": "2097"}]
    confidential = stat(found, "RD_PERS_HES", "FR")["observations"][0]
    assert confidential["status"] == "confidential" and confidential["value"] is None
    first = headers("eurostat")
    assert first[0]["release_basis"] == "provider_last_update" and first[0]["published_at"].startswith("2099-03-01")
    assert first[0]["url"].startswith("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/rd_e_gerdtot/")


def test_adapter_refuses_foreign_hosts_undeclared_parameters_rate_limits_and_budget_overruns():
    item = h.source("uis")
    bad = copy.deepcopy(item)
    bad["endpoint"] = "https://example.org/api"
    with pytest.raises(SourcePackError) as caught:
        EducationStatisticsAdapter(bad)
    assert caught.value.code == "invalid_manifest"
    adapter = EducationStatisticsAdapter(item, transport=fixture_transport(h.pages("uis")))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "release", "parameters": {"geoUnit": "ALL"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    limited = [dict(p, status=429, headers={"Retry-After": "120"}) for p in h.pages("uis")]
    with pytest.raises(SourcePackError) as caught:
        EducationStatisticsAdapter(item, transport=fixture_transport(limited)).fetch_page(
            {"operation": "release", "parameters": {}}, cursor=None)
    assert caught.value.code == "rate_limited" and caught.value.details["retry_after_ms"] == 120_000
    ipeds = h.source("ipeds")
    with pytest.raises(SourcePackError) as caught:
        EducationStatisticsAdapter(ipeds, transport=fixture_transport(h.pages("ipeds"))).fetch_page(
            {"operation": "release", "parameters": {}, "limit": 1}, cursor="1")
    assert caught.value.code == "response_too_large"
    drift = [dict(p, body=json.dumps({"unexpected": True})) for p in h.pages("uis")]
    with pytest.raises(SourcePackError) as caught:
        EducationStatisticsAdapter(item, transport=fixture_transport(drift)).fetch_page(
            {"operation": "release", "parameters": {}}, cursor=None)
    assert caught.value.code == "schema_drift"


def test_every_page_carries_a_receipt_with_digest_stage_and_fixture_origin():
    item = h.source("eurostat")
    adapter = EducationStatisticsAdapter(item, transport=fixture_transport(h.pages("eurostat")))
    page = adapter.fetch_page({"operation": "release", "parameters": {}}, cursor=None)
    assert page.receipt["evidence_origin"] == "fixture" and len(page.receipt["file_sha256"]) == 64
    assert page.receipt["release_stage"] == "update" and page.next_cursor == "1"
    assert adapter.describe()["education_statistics"]["live_verification"] == "unverified-live"
