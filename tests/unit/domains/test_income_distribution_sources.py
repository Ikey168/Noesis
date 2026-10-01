"""Income-distribution acquisition: PIP, EU-SILC and OECD IDD through the real adapter (#2583, IP01, IP03-IP05)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.income_distribution_sources import (
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    IncomeDistributionAdapter,
    fixture_transport,
    income_declaration,
    pip_version,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import income_distribution_harness as h


def items(name, *, revision=False):
    return [r["income_item"] for page in h.fetch(name, revision=revision) for r in page]


def test_every_source_is_audited_unverified_live_and_replays_to_its_pinned_output():
    assert set(PROVIDER_CONTRACTS) == {"pip", "eu-silc", "oecd-idd"}
    for contract in PROVIDER_CONTRACTS.values():
        assert {"access", "authentication", "rate_limits", "terms", "revision_model", "verify"} <= set(contract)
    assert {v["status"] for v in LIVE_VERIFICATION.values()} == {"unverified-live"}
    assert {"stored", "excluded", "retention", "who_may_query"} <= set(MINIMISATION)
    manifest = h.manifest()
    assert manifest["domains"] == ["society"]
    assert {s["income_distribution"]["live_verification"] for s in manifest["sources"]} == {"unverified-live"}
    replay = SourcePackConformance(h.ROOT).offline(json.loads((h.ROOT / "config/source_packs/society.json").read_text()))
    assert replay["valid"] and replay["coverage"]["verified"] == 3


def test_pip_values_carry_welfare_type_survey_year_and_estimation_labels_per_value():
    pip = items("pip")
    deu = next(i for i in pip if i["native_key"].startswith("pip:DEU") and i["indicator"]["code"] == "headcount")
    assert deu["welfare_concept"] == "income" and deu["ppp_base_year"] == "2017"
    assert deu["poverty_line"] == {"kind": "absolute", "value": "2.15", "unit": "PPP$ per person per day",
                                   "ppp_base_year": "2017", "set_by": "World Bank (declared request parameter)"}
    assert all(o["attributes"]["welfare_type"] == "income" and o["attributes"]["survey_year"] for o in
               deu["observations"])
    assert any(n["kind"] == "break" and n["periods"] == ["2098"] for n in deu["source_notes"])
    idn = next(i for i in pip if i["native_key"].startswith("pip:IDN") and i["indicator"]["code"] == "headcount")
    assert idn["welfare_concept"] == "consumption" and idn["reference_year_basis"] == "lineup-year"
    assert [o["attributes"]["estimation_type"] for o in idn["observations"]] == [
        "interpolation", "survey", "extrapolation"]
    region = next(i for i in pip if i["native_key"].startswith("pip-grp:SSF"))
    assert region["area"] == {"scheme": "wb-region", "code": "SSF", "label": "Sub-Saharan Africa"}
    gini = next(i for i in pip if i["native_key"].startswith("pip:DEU") and i["indicator"]["code"] == "gini")
    assert gini["poverty_line"] is None and gini["ppp_base_year"] is None


def test_pip_release_version_dates_the_release_and_names_the_ppp_round():
    assert pip_version("20991120_2017_02_02_PROD")["ppp_revision"] == "02"
    header = h.fetch("pip")[0][0]["income_release"]
    assert header["release_basis"] == "pip_release_version" and header["published_on"] == "2026-08-01"
    assert header["structure"]["release_version"]["ppp_version"] == "2017"
    source = h.source("pip")
    bad = json.loads(json.dumps(source))
    bad["income_distribution"]["documents"][0]["params"]["version"] = "20260801_2021_01_01_PROD"
    with pytest.raises(SourcePackError):
        income_declaration(bad)


def test_eu_silc_flags_thresholds_and_the_two_reference_years_stay_distinct():
    silc = items("eusilc")
    arop = next(i for i in silc if i["native_key"] == "A.LI_R_MD60.PC.T.TOTAL.DE")
    assert arop["equivalence_scale"]["code"] == "modified-oecd"
    assert arop["poverty_line"]["share"] == "60" and arop["definition"]["poverty_line"]["kind"] == "relative"
    last = arop["observations"][-1]
    assert last["flags"] == {"OBS_FLAG": "p"}
    assert last["attributes"]["survey_year"] == "2098" and last["attributes"]["income_reference_year"] == "2097"
    berlin = next(i for i in silc if i["area"]["code"] == "DE30")
    confidential = next(o for o in berlin["observations"] if o["period"] == "2097")
    assert confidential["status"] == "confidential" and confidential["value"] is None
    threshold = next(i for i in silc if i["indicator"]["concept"] == "poverty_threshold")
    assert threshold["unit"]["code"] == "EUR"
    header = h.fetch("eusilc")[0][0]["income_release"]
    assert header["release_basis"] == "provider_last_update" and header["published_on"] == "2026-06-15"


def test_oecd_methodology_and_income_definition_are_per_series_and_breaks_are_notes():
    oecd = items("oecd")
    methods = {(i["area"]["code"], i["indicator"]["code"], i["methodology_version"]) for i in oecd}
    assert ("DEU", "INC_DISP_GINI", "2011 terms of reference (previous income definition)") in methods
    assert ("DEU", "INC_DISP_GINI", "2012 terms of reference (income definition from 2012 onwards)") in methods
    assert all(i["equivalence_scale"]["code"] == "square-root" and i["definition"]["income_definition"] for i in oecd)
    usa = next(i for i in oecd if i["area"]["code"] == "USA")
    assert any(n["kind"] == "break" and n["periods"] == ["2097"] for n in usa["source_notes"])
    poverty = next(i for i in oecd if i["indicator"]["concept"] == "poverty_headcount_ratio")
    assert poverty["poverty_line"]["share"] == "50"
    assert h.fetch("oecd")[0][0]["income_release"]["structure"]["dataflow_version"] == "1.0"


def test_person_level_fields_are_refused_and_errors_do_not_truncate():
    source = h.source("pip")
    page = dict(h.pages("pip")[0])
    rows = json.loads(page["body"])
    rows[0]["household_id"] = "H-1"
    page["body"] = json.dumps(rows)
    adapter = IncomeDistributionAdapter(source, transport=fixture_transport([page] + h.pages("pip")[1:]))
    with pytest.raises(SourcePackError) as refused:
        adapter.fetch_page({"operation": "release", "parameters": {}}, cursor=None)
    assert refused.value.code == "personal_data_refused"
    limited = IncomeDistributionAdapter(source, transport=fixture_transport(h.pages("pip")))
    with pytest.raises(SourcePackError) as budget:
        limited.fetch_page({"operation": "release", "parameters": {}, "limit": 1}, cursor=None)
    assert budget.value.code == "budget_exhausted"
    busy = IncomeDistributionAdapter(source, transport=fixture_transport([{**h.pages("pip")[0], "status": 429}]))
    with pytest.raises(SourcePackError) as limited_rate:
        busy.fetch_page({"operation": "release", "parameters": {}}, cursor=None)
    assert limited_rate.value.code == "rate_limited"
