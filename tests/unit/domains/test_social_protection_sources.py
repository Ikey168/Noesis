"""SS03-SS05 (#2755, #2762, #2769): ESSPROS, SOCX and ILOSTAT source contracts and bounded acquisition."""

from __future__ import annotations

import json

import pytest

from src.ingestion.social_protection_sources import (
    BOUNDED_COVERAGE,
    CAPS,
    EXCLUSIONS,
    LIVE_VERIFICATION,
    PACING,
    PROVIDER_CONTRACTS,
    PROVIDERS,
    SocialProtectionAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.social_protection_records import readiness
from src.kb.social_protection_store import (
    SocialProtectionProjector,
    SocialProtectionStore,
)
from tests.unit import social_protection_harness as h

AUDIT = h.ROOT / "docs/development/social-protection-evidence/source-audit.md"


def test_contracts_coverage_caps_exclusions_and_live_verification_match_the_ss01_audit():
    audit = AUDIT.read_text()
    assert set(PROVIDERS) == set(h.SOURCES.values()) == set(CAPS) == set(BOUNDED_COVERAGE)
    assert set(PROVIDER_CONTRACTS) == set(LIVE_VERIFICATION) == set(PROVIDERS) | {
        "ilo-world-social-protection-dashboards"}
    for provider in PROVIDERS:
        contract = PROVIDER_CONTRACTS[provider]
        for key in ("publisher", "access", "authentication", "licence", "redistribution", "rate_limits",
                    "revision_model", "definitions", "personal_data", "unavailable_fallback"):
            assert contract[key], (provider, key)
        assert contract["status"] == LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert f"`{provider}`" in audit
    dashboards = PROVIDER_CONTRACTS["ilo-world-social-protection-dashboards"]
    assert dashboards["status"] == LIVE_VERIFICATION["ilo-world-social-protection-dashboards"]["status"] == \
        "not-implemented"
    assert "never scraped" in dashboards["unavailable_fallback"]
    # Caps as the audit's bounded first coverage states them.
    assert CAPS["eurostat-esspros"] == {"documents": 3, "series_per_response": 60}
    assert CAPS["oecd-socx"] == CAPS["ilo-social-protection-coverage"] == {"documents": 1, "series_per_response": 20}
    assert "3 documents, 60 series per response" in audit and "1 document, 20 series" in audit
    assert BOUNDED_COVERAGE["eurostat-esspros"]["places"] == {"Germany": "geo DE", "France": "geo FR"}
    assert BOUNDED_COVERAGE["oecd-socx"]["places"] == {"Germany": "REF_AREA DEU", "France": "REF_AREA FRA"}
    for dataset in ("spr_exp_sum", "spr_exp_func", "spr_pns_ben"):
        assert dataset in BOUNDED_COVERAGE["eurostat-esspros"]["series"] and dataset in audit
    assert "ILO,DF_SDG_0131_SEX_SOC_RT,1.0" in PROVIDER_CONTRACTS["ilo-social-protection-coverage"]["access"]
    assert "DSD_SOCX_AGG@DF_SOCX_AGG" in PROVIDER_CONTRACTS["oecd-socx"]["access"]
    assert "verify" in PROVIDER_CONTRACTS["oecd-socx"]["access"]
    assert "net social expenditure is a separate OECD series" in BOUNDED_COVERAGE["oecd-socx"]["series"]
    assert {"blending ESSPROS, SOCX and ILO figures into one series",
            "combining ESSPROS or SOCX expenditure with COFOG social-protection expenditure",
            "re-classifying one publisher's functions, branches or contingencies into another's"} <= set(EXCLUSIONS)
    assert PACING["oecd-socx"]["min_interval_s"] == 60  # one a minute: the stricter of ~20/minute and ~60/hour


def test_source_pack_entries_are_unverified_live_and_replay_offline_through_the_real_adapter():
    manifest = h.manifest()
    assert manifest["pack_id"] == "society-social-protection" and manifest["version"] == "1.0.0"
    assert {s["source_id"] for s in manifest["sources"]} == {"eurostat-esspros", "oecd-socx",
                                                             "ilo-social-protection-coverage"}
    assert {s["social_protection"]["live_verification"] for s in manifest["sources"]} == {"unverified-live"}
    assert {s["mapping"]["target_schema"] for s in manifest["sources"]} == {"noesis-social-protection-record-v2"}
    assert {s["auth"]["kind"] for s in manifest["sources"]} == {"none"}
    replay = SourcePackConformance(h.ROOT).offline(manifest)
    assert replay["valid"] and replay["coverage"]["verified"] == 3


def test_fixtures_are_authored_synthetic_reference_years_and_releases():
    for name in h.SOURCES:
        for revised in (False, True):
            for page in h.fetch(name, revised=revised):
                header = page[0]["social_release"]
                assert header["published_on"][:4] in {"2098", "2099"}
                assert header["evidence_origin"] == "fixture"
                for record in page:
                    periods = {o["period"] for o in record["social_item"]["observations"]}
                    assert periods and all("2094" <= p <= "2098" for p in periods)


def test_esspros_one_request_per_document_with_flags_and_the_manual_edition():
    sent = []

    def spy(**kwargs):
        sent.append(kwargs["url"])
        return fixture_transport(h.pages("esspros"))(**kwargs)

    pages = h.fetch("esspros", transport=spy)
    assert len(pages) == len(sent) == 3 and all(u.startswith("https://ec.europa.eu/eurostat/") for u in sent)
    assert all("format=SDMX-CSV" in page[0]["url"] for page in pages)
    header = pages[0][0]["social_release"]
    assert header["release_basis"] == "provider_last_update" and header["published_on"] == "2098-09-15"
    assert header["document_key"] == "eurostat-esspros:spr_exp_sum:A.TOTALNOREROUTE.MIO_EUR+PC_GDP.DE+FR"
    items = {r["social_item"]["native_key"]: r["social_item"] for page in pages for r in page}
    assert {i["measure"]["concept"] for i in items.values()} == {"expenditure", "beneficiaries"}
    old = items["A.OLD.TOTAL.MIO_EUR.DE"]
    assert old["function"] == {"scheme": "esspros-spfunc", "code": "OLD", "label": "Old age"}
    assert old["definition"]["manual_edition"].startswith("ESSPROS manual")
    assert any(n["kind"] == "definition_differs" and n["periods"] == ["2094"] for n in old["source_notes"])
    assert any(n["kind"] == "scope_difference" and n["value"] == "oecd-socx" for n in old["source_notes"])
    breaks = items["A.TOTALNOREROUTE.MIO_EUR.DE"]["source_notes"]
    assert any(n["kind"] == "break" and n["periods"] == ["2095"] for n in breaks)
    ben = {o["period"]: o for o in items["A.TOTAL.NR.FR"]["observations"]}
    assert ben["2096"]["publication_status"] == "estimated" and ben["2096"]["flags"] == {"OBS_FLAG": "e"}


def test_socx_dates_releases_without_an_update_stamp_and_keeps_breaks_and_estimates():
    pages = h.fetch("socx")
    header = pages[0][0]["social_release"]
    assert header["release_basis"] == "declared_release" and header["published_on"] == "2098-10-01"
    assert "format=csvfile" in header["url"] and header["dataflow_version"] == "1.0"
    items = {r["social_item"]["native_key"]: r["social_item"] for r in pages[0]}
    fra = items["FRA.A.SOCX.PT_B1GQ.ES10._T.TP11"]
    assert any(n["kind"] == "break" and n["attribute"] == "OBS_STATUS" and n["periods"] == ["2095"]
               for n in fra["source_notes"])
    assert {o["period"]: o["publication_status"] for o in fra["observations"]}["2097"] == "estimated"
    # Without a declared release date the retrieval time dates the release, labelled retrieval_time.
    item = h.source("socx")
    item["social_protection"]["documents"][0].pop("release")
    undeclared = h.fetch("socx", item=item, transport=fixture_transport(h.pages("socx")))
    assert undeclared[0][0]["social_release"]["release_basis"] == "retrieval_time"
    conn = h.connection()
    projector = SocialProtectionProjector(conn)
    projector.project_page(run_id="r", manifest=None, source=item, records=undeclared[0],
                           documents=[{"ingested_at": h.FIRST_RETRIEVAL}], page_receipt=None, principal_id="svc")
    (release,) = SocialProtectionStore(conn).releases(h.NS)
    assert release["release_basis"] == "retrieval_time" and release["release_at_ms"] == h.FIRST_RETRIEVAL


def test_socx_requests_are_paced_at_the_stricter_oecd_limit():
    waits, clock = [], [1000.0]

    def transport(**kwargs):
        clock[0] += 1
        return fixture_transport(h.pages("socx"))(**kwargs)

    from src.ingestion import social_protection_sources as module

    module._LAST_REQUEST.clear()
    adapter = SocialProtectionAdapter(h.source("socx"), transport=transport, sleep=waits.append,
                                      clock=lambda: clock[0])
    adapter.paced = True  # as with the real HTTPS transport
    for _ in range(2):
        adapter.fetch_page({"operation": "release", "parameters": {}, "limit": 20}, cursor=None)
    assert len(waits) == 1 and 58 <= waits[0] <= 60
    module._LAST_REQUEST.clear()


def test_ilo_coverage_keeps_population_contingency_sex_and_notes_and_never_scrapes_dashboards():
    pages = h.fetch("ilo")
    header = pages[0][0]["social_release"]
    assert header["edition"].startswith("World Social Protection Report") and "format=csv" in header["url"]
    assert header["url"].startswith("https://sdmx.ilo.org/rest/data/ILO,DF_SDG_0131_SEX_SOC_RT,1.0/")
    items = {r["social_item"]["native_key"]: r["social_item"] for r in pages[0]}
    old = items["DEU.A.SDG_0131_RT.SEX_T.SOC_CONTIG_OLD"]
    assert old["measure"]["concept"] == "coverage" and old["sex"] == {"code": "SEX_T", "label": "Total (both sexes)"}
    assert old["population"]["label"] == "persons above statutory pensionable age"
    assert set(old["definition"]["notes"]) == {"NOTE_SOURCE", "NOTE_INDICATOR", "NOTE_CLASSIF"}
    # Regional or global modelled estimates are not in first coverage.
    item = h.source("ilo")
    item["social_protection"]["documents"][0]["scope"] = {"modelled_estimates": True}
    with pytest.raises(SourcePackError):
        SocialProtectionAdapter(item)
    assert "ilo-world-social-protection-dashboards" not in PROVIDERS


def test_undeclared_controls_hosts_and_another_publishers_classification_are_refused():
    adapter = SocialProtectionAdapter(h.source("esspros"), transport=fixture_transport(h.pages("esspros")))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "release", "parameters": {"key": "X"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    moved = h.source("socx")
    moved["endpoint"] = "https://example.org/data"
    with pytest.raises(SourcePackError):
        SocialProtectionAdapter(moved)
    borrowed = h.source("socx")
    borrowed["social_protection"]["documents"][0]["function_scheme"] = "esspros-spfunc"
    with pytest.raises(SourcePackError):
        SocialProtectionAdapter(borrowed)
    too_many = h.source("socx")
    too_many["social_protection"]["documents"] *= 2
    with pytest.raises(SourcePackError) as unbounded:
        SocialProtectionAdapter(too_many)
    assert unbounded.value.code == "unbounded_source"
    open_ended = h.source("ilo")
    open_ended["social_protection"]["documents"][0]["params"] = {}
    with pytest.raises(SourcePackError):
        SocialProtectionAdapter(open_ended)


@pytest.mark.parametrize("name", list(h.SOURCES))
def test_a_failed_document_fails_the_run_with_its_code_and_earlier_vintages_stay_current(name):
    conn = h.connection()
    h.apply(conn, name)
    store = SocialProtectionStore(conn)
    provider = h.SOURCES[name]
    before = {s["series_id"]: s["current_vintage_id"] for s in store.find_series(h.NS, provider=provider)}
    failures = {
        "source_unavailable": lambda **kw: {**fixture_transport(h.pages(name, revised=True))(**kw), "status": 503},
        "network_policy": lambda **kw: {**fixture_transport(h.pages(name, revised=True))(**kw),
                                        "final_url": "https://example.org/moved"},
        "schema_drift": lambda **kw: {**fixture_transport(h.pages(name, revised=True))(**kw),
                                      "content": b"DATAFLOW,geo\nX,DE\n"},
        "response_too_large": lambda **kw: {**fixture_transport(h.pages(name, revised=True))(**kw),
                                            "content": b"x" * 3_000_000},
    }
    projector = SocialProtectionProjector(conn)
    for code, transport in failures.items():
        with pytest.raises(SourcePackError) as caught:
            h.fetch(name, revised=True, transport=transport)
        assert caught.value.code == code
        projector.finish_source(run_id=f"run:{code}", manifest=None, source=h.source(name), status="failed",
                                principal_id="svc")
    budget = h.source(name, revised=True)
    budget["budgets"]["max_results"] = 1
    with pytest.raises(SourcePackError) as caught:
        h.fetch(name, revised=True, item=budget)
    assert caught.value.code == "budget_exhausted"
    after = {s["series_id"]: s["current_vintage_id"] for s in store.find_series(h.NS, provider=provider)}
    assert after == before  # nothing revised or removed because of a failure
    assert all(v["status"] == "published" for s in before for v in store.vintage_rows(h.NS, s))
    state = store.provider_state(h.NS, provider)
    assert state["stale"] is True and state["last_success_ms"] is not None
    assert readiness(conn)["providers"][provider]["status"] == "stale"
    failed = [r for r in store.receipts(h.NS, scopes=h.READ_ONLY) if r["outcome"] == "failed"]
    assert len(failed) == 4 and {r["detail"]["failure_code"] for r in failed} == {"source_run_failed"}
    assert json.dumps(failed)  # receipts are serialisable evidence
