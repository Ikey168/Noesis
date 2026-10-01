"""IP01 and IP03-IP05 (#2588, #2598, #2603, #2608): source contracts and bounded PIP, EU-SILC and OECD IDD acquisition."""

from __future__ import annotations

import json

import pytest

from src.ingestion.income_distribution_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    IncomeDistributionAdapter,
    fixture_transport,
    parse_pip_release,
)
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    load_source_packs,
)
from tests.unit import income_distribution_harness as h


def items(name, **kwargs):
    return [r["income_item"] for page in h.fetch(name, **kwargs) for r in page]


def headers(name, **kwargs):
    return [page[0]["income_release"] for page in h.fetch(name, **kwargs)]


def test_contracts_record_access_terms_limits_revision_model_and_live_status():
    for provider, contract in PROVIDER_CONTRACTS.items():
        for field in ("access", "authentication", "key_handling", "licence", "rate_limits", "revision_model",
                      "updates_corrections_removals", "personal_data"):
            assert contract[field], (provider, field)
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    pack = h.manifest()
    assert {s["income_distribution"]["live_verification"] for s in pack["sources"]} == {"unverified-live"}


def test_the_pack_validates_and_replays_offline_to_its_pinned_output():
    from tests.unit.income_distribution_harness import ROOT

    packs = {p["pack_id"]: p for p in load_source_packs(ROOT / "config/source_packs")}
    report = SourcePackConformance(ROOT).offline(packs["society-income-distribution"])
    assert report["valid"] and report["coverage"]["verified"] == 4


def test_pip_release_and_ppp_round_and_estimation_labels():
    assert parse_pip_release("20990320_2017_02_02_PROD") == {
        "published_on": "2099-03-20", "ppp_base_year": 2017, "ppp_revision": "02_02"}
    pip_headers = headers("pip")
    assert [x["ppp"] for x in pip_headers] == [{"base_year": 2017, "revision": "01_02"},
                                               {"base_year": 2021, "revision": "01_02"}]
    head = next(i for i in items("pip") if i["native_key"].endswith("headcount") and i["ppp_base_year"] == 2017)
    labels = {o["period"]: (o["estimation_type"], o["survey_year"]) for o in head["observations"]}
    assert labels == {"2094": ("survey", "2094"), "2095": ("interpolation", None), "2096": ("survey", "2096")}
    assert {o["welfare_type"] for o in head["observations"]} == {"income"}
    assert head["source_notes"][0]["kind"] == "break" and head["source_notes"][0]["periods"] == ["2096"]
    gini = next(i for i in items("pip") if i["native_key"].endswith("gini") and i["ppp_base_year"] == 2017)
    assert gini["poverty_line"] is None  # a Gini is not published at a line
    region = items("pip-region")
    assert {o["estimation_type"] for i in region for o in i["observations"]} == {"regional-line-up"}
    assert all("pop_in_poverty" not in i["indicator"]["measure"] for i in region)  # only declared measures


def test_eurostat_silc_flags_dataflow_version_threshold_and_income_reference_year():
    pages = headers("silc")
    assert {x["dataflow_version"] for x in pages} == {"1.0"}
    assert {x["release_basis"] for x in pages} == {"provider_last_update"}
    arop = next(i for i in items("silc") if "LI_R_MD60.PC" in i["native_key"])
    by_period = {o["period"]: o for o in arop["observations"]}
    assert by_period["2095"]["flags"] == {"OBS_FLAG": "b"} and by_period["2096"]["flags"] == {"OBS_FLAG": "p"}
    assert by_period["2096"]["survey_year"] == "2096" and by_period["2096"]["income_reference_year"] == "2095"
    assert arop["definition"]["threshold"].startswith("60 %") and arop["definition"]["equivalence_scale"] == \
        "modified-oecd"
    threshold = next(i for i in items("silc") if "LI_C_MD60" in i["native_key"])
    confidential = {o["period"]: o for o in threshold["observations"]}["2096"]
    assert confidential["status"] == "confidential" and confidential["value"] is None
    assert arop["source_notes"] == [{"kind": "break", "attribute": "OBS_FLAG", "value": "break in series",
                                     "periods": ["2095"], "statement": arop["source_notes"][0]["statement"]}]


def test_oecd_idd_definition_methodology_breaks_and_never_mixed_with_eu_silc():
    oecd = items("oecd")
    assert {i["indicator"]["concept"] for i in oecd} == {"gini", "poverty_headcount"}
    for item in oecd:
        assert item["methodology"] == "METH2012" and "D_CUR" in item["income_definition"]
        assert item["survey"] == "OECD IDD" and item["equivalence_scale"] == "square-root"
        assert item["source_notes"][0]["periods"] == ["2095"]
    rate = next(i for i in oecd if i["indicator"]["concept"] == "poverty_headcount")
    assert rate["poverty_line"]["amount"] == "50"
    assert headers("oecd")[0]["release_basis"] == "declared_release"
    conn = h.connection()
    h.load_all(conn)
    oecd_gini = h.series(conn, "oecd-idd", "INC_DISP_GINI")
    silc_gini = h.series(conn, "eurostat-silc", "GINI_HND")
    assert oecd_gini["series_id"] != silc_gini["series_id"]
    assert "EU-SILC" in oecd_gini["key"]["survey"] or oecd_gini["key"]["survey"] == "OECD IDD"


def test_bounds_hosts_and_failures_are_refused_explicitly():
    item = json.loads(json.dumps(h.source("pip")))
    item["income_distribution"]["documents"][0]["params"]["country"] = "all"
    with pytest.raises(SourcePackError) as caught:
        IncomeDistributionAdapter(item, transport=fixture_transport([]))
    assert caught.value.code == "invalid_source"
    item = json.loads(json.dumps(h.source("pip")))
    item["income_distribution"]["documents"][0]["params"]["ppp_version"] = "2021"
    with pytest.raises(SourcePackError):
        IncomeDistributionAdapter(item, transport=fixture_transport([]))
    item = json.loads(json.dumps(h.source("silc")))
    item["endpoint"] = "https://example.org/eurostat"
    with pytest.raises(SourcePackError) as caught:
        IncomeDistributionAdapter(item, transport=fixture_transport([]))
    assert caught.value.code == "unsafe_endpoint"

    def failing(*, url, params, headers, timeout):
        return {"status": 503, "content": b""}

    with pytest.raises(SourcePackError) as caught:
        h.fetch("oecd", transport=failing)
    assert caught.value.code == "source_unavailable"

    def limited(*, url, params, headers, timeout):
        return {"status": 429, "headers": {"Retry-After": "60"}, "content": b""}

    with pytest.raises(SourcePackError) as caught:
        h.fetch("silc", transport=limited)
    assert caught.value.code == "rate_limited"


def test_a_failed_run_leaves_vintages_current_and_reads_stale():
    conn = h.connection()
    h.apply(conn, "oecd", retrieved_at_ms=h.FIRST_RETRIEVAL)
    from src.kb.income_distribution_store import IncomeDistributionProjector

    projector = IncomeDistributionProjector(conn)
    result = projector.finish_source(run_id="r2", manifest=None, source=h.source("oecd"), status="failed",
                                     principal_id="svc")
    assert result["provider_state"]["stale"] is True
    gini = h.series(conn, "oecd-idd", "INC_DISP_GINI")
    assert h.store(conn).values(h.NS, gini["series_id"])["status"] == "available"
