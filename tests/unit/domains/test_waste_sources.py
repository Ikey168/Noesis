"""WC03-WC05 (#2759, #2766, #2773): waste source contracts and acquisition (Eurostat, EEA transfers, OECD)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.ingestion.waste_sources import (
    BOUNDED_COVERAGE,
    CAPS,
    EEA_COLUMNS,
    EXCLUSIONS,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    WasteAdapter,
    eea_query,
    fixture_transport,
    forbidden_columns,
    selected_columns,
)
from src.kb.waste_store import WasteStore
from tests.unit import waste_fixture_builder as builder
from tests.unit import waste_harness as h


def test_every_provider_has_the_audited_contract_caps_coverage_and_an_unverified_live_entry():
    assert set(PROVIDER_CONTRACTS) == set(h.SOURCES) == set(LIVE_VERIFICATION) == set(CAPS)
    for provider, contract in PROVIDER_CONTRACTS.items():
        for key in ("publisher", "delivers", "access", "authentication", "licence", "redistribution", "rate_limits",
                    "revision_model", "definitions", "unavailable_fallback", "verify"):
            assert contract[key], (provider, key)
        assert contract["status"] == LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert LIVE_VERIFICATION[provider]["checked"] is None and BOUNDED_COVERAGE[provider]["caps"]
    assert CAPS["eurostat-waste"]["documents"] == 2 and CAPS["eurostat-waste"]["series_per_response"] == 60
    assert CAPS["eurostat-circular-economy"]["series_per_response"] == 10
    assert CAPS["eurostat-circular-economy"]["documents_per_dataset"] == 1
    assert CAPS["eea-industry-waste-transfers"] == {"documents": 1, "rows_per_response": 500, "reporting_years": 1}
    assert CAPS["oecd-municipal-waste"]["series_per_response"] == 10
    assert CAPS["oecd-municipal-waste"]["min_request_interval_s"] == 60  # the stricter of the recorded OECD limits
    assert BOUNDED_COVERAGE["eurostat-waste"]["places"] == {"Germany": "geo DE", "France": "geo FR"}
    assert BOUNDED_COVERAGE["oecd-municipal-waste"]["places"] == {"Germany": "REF_AREA DEU", "France": "REF_AREA FRA"}
    assert {"filling years a source did not publish (biennial odd years stay absent)",
            "blending Eurostat, OECD and EEA figures", "summing facility transfers into national totals",
            "recycling rates, per-capita or material-flow figures of our own"} <= set(EXCLUSIONS)
    audit = (h.ROOT / "docs/development/waste-evidence/source-audit.md").read_text()
    for name in ("env_wasgen", "env_wastrt", "cei_wm011", "cei_srm030", "eea_truncated", "LIVE_VERIFICATION",
                 "#2740", "unverified-live", "one document per dataset"):
        assert name in audit, name


def test_the_pinned_fixtures_are_current_and_replay_offline_through_the_real_adapter():
    manifest = h.manifest()
    assert manifest["pack_id"] == "climate-environment-waste" and manifest["version"] == "1.0.0"
    assert {s["source_id"] for s in manifest["sources"]} == set(h.SOURCES)
    assert {s["source_id"] for s in manifest["sources"]} >= {"eurostat-waste", "eurostat-circular-economy",
                                                             "eea-industry-waste-transfers", "oecd-municipal-waste"}
    assert {s["waste"]["live_verification"] for s in manifest["sources"]} == {"unverified-live"}
    assert {s["mapping"]["target_schema"] for s in manifest["sources"]} == {"noesis-waste-record-v2"}
    replay = SourcePackConformance(h.ROOT).offline(manifest)
    assert replay["valid"] and replay["coverage"]["verified"] == 4
    # The pinned files are exactly what the authored builder produces.
    for path, text in builder.build(write=False).items():
        assert path.read_text() == text, path


def test_fixtures_are_authored_with_fictional_years_and_release_dates():
    for provider in h.SOURCES:
        for revision in (False, True):
            for record in (r for page in h.fetch(provider, revision=revision) for r in page):
                header, item = record["waste_release"], record["waste_item"]
                assert header["evidence_origin"] == "fixture"
                assert header["published_on"][:4] in {"2098", "2099"}, header["published_on"]
                if item["kind"] == "series":
                    assert {o["period"] for o in item["observations"]} <= {"2094", "2095", "2096", "2097"}
                else:
                    assert item["reporting_year"] in range(2094, 2098)


def test_eurostat_keeps_flags_biennial_gaps_and_the_series_key_as_published():
    records = [r for page in h.fetch("eurostat-waste") for r in page]
    assert len(records) == 10 and records[0]["waste_release"]["release_basis"] == "provider_last_update"
    assert records[0]["waste_release"]["published_on"] == "2098-03-15"
    items = {(i["dataset"], i["area"]["code"], i["hazard"]["code"], i["operation"]["code"]): i
             for i in (r["waste_item"] for r in records)}
    de = items[("env_wasgen", "DE", "HAZ_NHAZ", "not_applicable")]
    assert [o["period"] for o in de["observations"]] == ["2094", "2096"] and de["periodicity"] == "biennial"
    assert de["observations"][1]["flags"] == {"OBS_FLAG": "p"}
    fr = items[("env_wasgen", "FR", "HAZ_NHAZ", "not_applicable")]
    assert any(n["kind"] == "break" and n["periods"] == ["2096"] for n in fr["source_notes"])
    assert items[("env_wastrt", "DE", "HAZ_NHAZ", "RCV_R")]["activity"] == {"code": "not_applicable"}
    cei = {(r["waste_item"]["dataset"], r["waste_item"]["area"]["code"]): r["waste_item"]
           for page in h.fetch("eurostat-circular-economy") for r in page}
    assert cei[("cei_wm011", "DE")]["unit"]["code"] == "RT"
    assert cei[("cei_wm011", "DE")]["observations"][0]["value"] == "66.1"  # stored as published, never recomputed


def test_eea_query_reuses_the_eea_industry_pattern_and_selects_no_operator_or_contact_column():
    query = eea_query("DE", "Berlin", 2096)
    assert selected_columns(query) == list(EEA_COLUMNS) and forbidden_columns(query) == []
    assert "[IED].[latest]" in query and "f.countryCode = 'DE' AND f.city = 'Berlin'" in query
    for word in ("operator", "parent", "address", "contact", "authority", "name"):
        assert not any(word in c.casefold() for c in selected_columns(query)), word
    records = [r for page in h.fetch("eea-industry-waste-transfers") for r in page]
    header = records[0]["waste_release"]
    assert header["url"].startswith("https://discodata.eea.europa.eu/sql?") and "nrOfHits=500" in header["url"]
    assert header["dataset_version"]["stated"] == "v12.0 (authored fixture)"
    assert header["release_basis"] == "provider_dataset_version" and header["complete"] and not header["truncated"]
    item = records[0]["waste_item"]
    assert set(item) >= {"inspire_id", "reporting_year", "hazardous", "treatment", "destination", "quantity",
                         "method"}
    assert not any(k in json.dumps(records) for k in ("parentCompanyName", "facilityName", "permitAuthority"))
    with pytest.raises(SourcePackError):
        unbounded = json.loads(json.dumps(h.source("eea-industry-waste-transfers")))
        unbounded["waste"]["documents"][0]["selection"]["limit"] = 10000
        WasteAdapter(unbounded)


def test_without_a_stated_dataset_version_the_retrieval_time_is_labelled():
    body = json.dumps({"results": json.loads(h.pages("eea-industry-waste-transfers")[0]["body"])["results"]})
    pages = [dict(h.pages("eea-industry-waste-transfers")[0], body=body)]
    header = h.fetch("eea-industry-waste-transfers", transport=fixture_transport(pages))[0][0]["waste_release"]
    assert header["release_basis"] == "retrieval_time" and header["published_on"] is None
    assert header["dataset_version"]["stated"] is None and "retrieval_time" in header["dataset_version"]["basis"]


def test_a_full_eea_page_is_stored_as_truncated_and_never_removes_rows():
    conn = h.connection()
    h.load_facilities(conn)
    h.apply(conn, "eea-industry-waste-transfers", retrieved_at_ms=h.FIRST_RETRIEVAL)
    rows = [builder.transfer(f"DE.UBA.PRTR/FIXTURE{n:04d}.FACILITY", "NONHW", "R", "DOMESTIC", n + 1, "E")
            for n in range(500)]
    body = json.dumps({"datasetVersion": "v12.1 (authored fixture)", "datasetPublished": "2098-09-01",
                       "results": rows})
    pages = [dict(h.pages("eea-industry-waste-transfers")[0], body=body)]
    applied = h.apply(conn, "eea-industry-waste-transfers", retrieved_at_ms=h.SECOND_RETRIEVAL,
                      transport=fixture_transport(pages))
    assert applied[0]["truncated"] is True and applied[0]["complete"] is False and applied[0]["removed"] == 0
    store = WasteStore(conn)
    release = store.release(h.NS, applied[0]["release_id"])
    assert release["truncated"] is True and release["complete"] is False
    # The five rows of the earlier complete page are still current; nothing was marked removed by a truncated page.
    for row in store.transfer_rows(h.NS, inspire_id=h.FACILITY_1):
        assert store.transfer_vintages(h.NS, row["row_id"])[-1]["status"] == "published"


def test_oecd_marks_the_dataflow_verify_dates_by_declared_release_and_keeps_breaks():
    records = [r for page in h.fetch("oecd-municipal-waste") for r in page]
    header = records[0]["waste_release"]
    assert header["release_basis"] == "declared_release" and header["published_on"] == "2098-11-20"
    assert "format=csvfile" in header["url"] and header["url"].startswith("https://sdmx.oecd.org/")
    fra = next(r["waste_item"] for r in records if r["waste_item"]["area"]["code"] == "FRA")
    assert "verify" in fra["dataflow"]["verify"]
    assert {o["period"]: o["flags"] for o in fra["observations"]}["2096"] == {"OBS_STATUS": "B"}
    assert any(n["kind"] == "break" and n["value"] == "B" for n in fra["source_notes"])
    undeclared = json.loads(json.dumps(h.source("oecd-municipal-waste")))
    del undeclared["waste"]["documents"][0]["release"]
    header = h.fetch("oecd-municipal-waste", item=undeclared)[0][0]["waste_release"]
    assert header["release_basis"] == "retrieval_time" and header["published_on"] is None
    unflagged = json.loads(json.dumps(h.source("oecd-municipal-waste")))
    del unflagged["waste"]["documents"][0]["dataflow"]
    with pytest.raises(SourcePackError):
        WasteAdapter(unflagged)


@pytest.mark.parametrize("provider", h.SOURCES)
def test_a_failed_document_fails_the_run_with_its_code_and_earlier_vintages_stay_current(provider):
    conn = h.connection()
    h.load_all(conn, facilities=provider == "eea-industry-waste-transfers")
    store = WasteStore(conn)
    before = conn.execute("SELECT count(*) FROM waste_vintages").fetchone()[0]
    env_before = conn.execute("SELECT count(*) FROM environment_vintages").fetchone()[0] \
        if provider == "eea-industry-waste-transfers" else None
    cases = {
        "source_unavailable": [dict(p, status=503) for p in h.pages(provider)],
        "schema_drift": [dict(p, body="DATAFLOW,TIME_PERIOD\nx,2096\n" if "csv" in str(p["request"]) or
                              "sdmx" in str(p["request"]) else '{"rows": []}') for p in h.pages(provider)],
    }
    for code, pages in cases.items():
        with pytest.raises(SourcePackError) as caught:
            h.fetch(provider, transport=fixture_transport(pages))
        assert caught.value.code == code, (provider, caught.value.code)
        store.record_failure(h.NS, provider, code=caught.value.code, run_id=f"run:{code}", source_id=provider,
                             scopes=h.SCOPES)

    def moved(**kwargs):
        return {**fixture_transport(h.pages(provider))(**kwargs), "final_url": "https://example.org/x"}

    with pytest.raises(SourcePackError) as redirected:
        h.fetch(provider, transport=moved)
    assert redirected.value.code == "network_policy"
    small = json.loads(json.dumps(h.source(provider)))
    small["budgets"]["max_bytes"] = 50
    with pytest.raises(SourcePackError) as large:
        h.fetch(provider, item=small)
    assert large.value.code == "response_too_large"
    assert conn.execute("SELECT count(*) FROM waste_vintages").fetchone()[0] == before
    if env_before is not None:
        assert conn.execute("SELECT count(*) FROM environment_vintages").fetchone()[0] == env_before
    state = store.provider_state(h.NS, provider)
    assert state["stale"] is True and state["reason"] == "schema_drift"
    assert not conn.execute("SELECT count(*) FROM waste_release_members WHERE status='removed'").fetchone()[0]


def test_statistical_releases_over_budget_are_refused_never_truncated():
    item = json.loads(json.dumps(h.source("eurostat-waste")))
    item["budgets"]["max_results"] = 2
    with pytest.raises(SourcePackError) as caught:
        h.fetch("eurostat-waste", item=item)
    assert caught.value.code == "budget_exhausted"
    adapter = WasteAdapter(h.source("oecd-municipal-waste"), transport=fixture_transport(h.pages("oecd-municipal-waste")))
    with pytest.raises(SourcePackError) as forbidden:
        adapter.fetch_page({"operation": "release", "parameters": {"key": "x"}}, cursor=None)
    assert forbidden.value.code == "parameter_forbidden"


def test_the_runtime_drives_the_pack_and_a_failure_marks_the_source_stale():
    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore

    conn = h.connection()
    h.load_facilities(conn)
    clock = {"now": h.FIRST_RETRIEVAL}

    def tick():
        clock["now"] += 1_000
        return clock["now"]

    SourcePackStore(conn).install(h.manifest(), principal_id="operator", enable=True, now_ms=10)
    runtime = SourcePackRuntime(conn, now=tick, sleep=lambda _d: None)
    for source in h.SOURCES:
        runtime.accept_license("climate-environment-waste", source, principal_id="operator")
    request = {"pack_id": "climate-environment-waste", "run_key": "waste-1", "operation": "release",
               "max_results": 1000, "max_bytes": 20_000_000, "timeout_ms": 60_000, "source_ids": list(h.SOURCES)}
    receipt = runtime.run(request, principal_id="operator",
                          adapters=runtime.fixture_adapters("climate-environment-waste", h.ROOT),
                          dns_resolver=lambda _host: ["8.8.8.8"], secret_resolver=lambda _ref: None)
    assert receipt["status"] == "complete", receipt
    store = WasteStore(conn)
    assert {r["provider"] for r in store.releases(h.NS)} == set(h.SOURCES)
    assert all(not store.provider_state(h.NS, p)["stale"] for p in h.SOURCES)
    assert all(r["retrieved_at_ms"] > h.FIRST_RETRIEVAL for r in store.releases(h.NS))  # the runtime's clock
    # A failed source run leaves every vintage current and reads as stale.
    vintages = conn.execute("SELECT count(*) FROM waste_vintages").fetchone()[0]
    broken = {"oecd-municipal-waste": runtime.factory.compile(
        h.source("oecd-municipal-waste"),
        transport=fixture_transport([dict(p, status=503) for p in h.pages("oecd-municipal-waste")]))}
    failed = runtime.run({**request, "run_key": "waste-2", "source_ids": ["oecd-municipal-waste"]},
                         principal_id="operator", adapters=broken, dns_resolver=lambda _host: ["8.8.8.8"],
                         secret_resolver=lambda _ref: None)
    assert failed["status"] != "complete"
    assert store.provider_state(h.NS, "oecd-municipal-waste")["stale"] is True
    assert conn.execute("SELECT count(*) FROM waste_vintages").fetchone()[0] == vintages
