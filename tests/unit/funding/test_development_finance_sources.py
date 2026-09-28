"""Development-finance source contracts and acquisition: IATI, World Bank, OECD CRS (#1952, #1985, #2003, #2013)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.development_finance_sources import (
    BOUNDED_COVERAGE,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    PROVIDER_HOSTS,
    DevelopmentFinanceAdapter,
    DevelopmentFinanceFormatError,
    IATIDatastoreClient,
    classify_recipient,
    decimal_text,
    identifier_key,
    iati_query,
    iati_selection,
    iso_day,
    parse_iati_activities,
    parse_world_bank_projects,
    world_bank_link_evidence,
)
from src.ingestion.provider_execution import DurableHTTP, ProviderError
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit.funding import development_finance_harness as h


def test_every_provider_has_a_decision_and_nothing_is_verified_live_without_a_dated_run():
    assert set(PROVIDER_CONTRACTS) == {
        "iati-datastore",
        "oecd-crs",
        "world-bank-projects",
        "transparenzportal-bund",
        "eu-aid-explorer",
    }
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert contract["access_decision"] in {"unverified-live", "not-implemented"}, (
            provider
        )
        assert contract["reason"] and contract["delivers"] in {
            "publisher-reported activities",
            "statistical aggregates",
            "portal view",
            "publisher-reported project records (World Bank as publisher)",
        }
        assert LIVE_VERIFICATION[provider]["status"] == contract["access_decision"]
    for provider in ("transparenzportal-bund", "eu-aid-explorer"):
        assert PROVIDER_CONTRACTS[provider]["delivers"] == "portal view"
        assert (
            "never scraped" in PROVIDER_CONTRACTS[provider]["reason"]
            or "nothing is scraped" in PROVIDER_CONTRACTS[provider]["reason"]
        )
    for provider in ("iati-datastore", "oecd-crs", "world-bank-projects"):
        contract = PROVIDER_CONTRACTS[provider]
        assert {
            "authentication",
            "rate_limits",
            "pagination",
            "identifiers",
            "terms",
            "retained_evidence",
            "unavailable_fallback",
            "verify",
        } <= set(contract)
        assert BOUNDED_COVERAGE[provider]
    assert "never all of IATI" in PROVIDER_CONTRACTS["iati-datastore"]["coverage"]


def test_iati_activities_are_parsed_as_reported_with_unknowns_left_unknown():
    parsed = parse_iati_activities((h.FIXTURES / "iati_fdpa_2098-03.xml").read_bytes())
    water, education = parsed["activities"]
    assert water["iati_identifier"] == "XM-DAC-99901-FICT-0001"
    assert (
        water["last_updated_at"] == "2098-03-01T10:00:00+00:00"
        and water["reporting_org"]["ref"] == h.FDPA
    )
    commitment, disbursement, undated = water["transactions"]
    assert (commitment["type"], commitment["value"], commitment["currency_source"]) == (
        "2",
        "1000000",
        "stated",
    )
    assert (disbursement["currency"], disbursement["currency_source"]) == (
        "EUR",
        "activity-default",
    )
    assert undated["value_date"] is None and undated["date"] == "2098-02-25"
    (period,) = water["results"][0]["indicators"][0]["periods"]
    assert (period["targets"][0]["value"], period["actuals"][0]["value"]) == (
        "5000",
        "1200",
    )
    # No default currency and no stated currency: unknown, never assumed.
    assert (
        education["default_currency"] is None
        and education["transactions"][1]["currency"] is None
    )
    implementer = education["participating_orgs"][1]
    assert (
        implementer["ref"] is None and implementer["name"] == "Fictional Learning Trust"
    )
    assert "None" not in json.dumps(parsed)


def test_an_activity_without_an_identifier_is_recorded_as_rejected_without_dropping_the_page():
    env = h.Env()
    broken = h.edited(
        "iati_fdpa_2098-03.xml",
        (
            "<iati-identifier>XM-DAC-99901-FICT-0002</iati-identifier>",
            "<iati-identifier> </iati-identifier>",
        ),
    )
    result = env.iati(broken, observation="r1")
    assert result["counts"] == {"first": 1}
    (rejected,) = result["coverage"]["rejected"]
    assert rejected["activity_index"] == 1 and "iati-identifier" in rejected["reason"]
    from src.kb.development_finance import DevelopmentFinanceStore

    (coverage,) = DevelopmentFinanceStore(env.conn).coverage_rows(h.NS)
    assert coverage["rejected"] == result["coverage"]["rejected"]


def test_xml_entities_and_foreign_roots_are_refused():
    with pytest.raises(DevelopmentFinanceFormatError):
        parse_iati_activities(
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><iati-activities/>'
        )
    with pytest.raises(DevelopmentFinanceFormatError):
        parse_iati_activities(b"<iati-organisations/>")
    with pytest.raises(DevelopmentFinanceFormatError):
        parse_iati_activities(b'<iati-activities version="1.05"/>')


def test_shared_parsing_rules():
    assert (
        iso_day("6/30/2102 12:00:00 AM") == "2102-06-30"
        and iso_day("2098-02-01T00:00:00Z") == "2098-02-01"
    )
    assert iso_day("") is None and iso_day("next year") is None
    assert (
        decimal_text("150,000,000") == "150000000" and decimal_text("12.50") == "12.5"
    )
    assert decimal_text("NaN") is None and decimal_text(None) is None
    # One identifier key on both sides of every match; separators are kept.
    assert identifier_key(" xm-dac-99901-fict-0001 ") == "XM-DAC-99901-FICT-0001"
    assert identifier_key("XM-DAC-1-2") != identifier_key("XM-DAC-12")
    assert identifier_key("GB-COH-990/00001") == "GB-COH-990/00001"


def test_selections_are_explicit_and_bounded():
    with pytest.raises(ValueError):
        iati_selection({})
    with pytest.raises(ValueError):
        iati_selection({"recipient_countries": ["Kenya"]})
    clean = iati_selection({"publishers": [h.FDPA], "sectors": ["14030"]})
    assert (
        iati_query(clean)
        == 'reporting_org_ref:("XM-DAC-99901") AND sector_code:("14030")'
    )


def test_iati_acquisition_uses_durable_http_with_a_secret_key_and_retains_documents():
    env = h.Env()
    result = env.iati("iati_fdpa_2098-03.xml", observation="r1")
    assert result["ok"] and result["counts"] == {"first": 2}
    assert (
        result["evidence"]["evidence_origin"] == "fixture"
        and result["evidence"]["execution"] == "injected"
    )
    assert len(result["documents"]) == 2
    call = env.web.calls[0]
    assert (
        call["url"] == IATIDatastoreClient.ENDPOINT
        and "Ocp-Apim-Subscription-Key" in call["headers"]
    )
    stored = env.conn.execute(
        "SELECT receipt_json FROM provider_execution_requests"
    ).fetchall()
    assert "fixture-subscription-key-0001" not in json.dumps([r[0] for r in stored])
    # Replaying the same observation reads the capture again and adds nothing.
    again = env.iati("iati_fdpa_2098-03.xml", observation="r1")
    assert again["counts"] == {"unchanged": 2}


def test_the_clients_run_on_the_real_default_transport_policy():
    import duckdb

    conn = duckdb.connect()
    # The runtime's default transport (no redirects, exact hosts, public addresses only) is what a live run uses.
    http = DurableHTTP(
        conn,
        budget_id="b",
        provider="iati-datastore",
        principal_id="p",
        allowed_hosts=PROVIDER_HOSTS["iati-datastore"],
        reuse_notice="n",
        resolver=lambda host: [],
    )
    assert http.configuration["transport"] == "network"
    client = IATIDatastoreClient(http, principal_id="p")
    with pytest.raises(ProviderError) as blocked:
        client.page({"publishers": [h.FDPA]}, "o")
    assert blocked.value.code == "ssrf_blocked"
    with pytest.raises(ValueError):
        IATIDatastoreClient(
            DurableHTTP(
                conn,
                budget_id="c",
                provider="iati-datastore",
                principal_id="p",
                allowed_hosts=["evil.example"],
                reuse_notice="n",
            ),
            principal_id="p",
        )
    with pytest.raises(ProviderError) as other:
        http.request(
            "k", "https://search.worldbank.org/api/v2/projects", principal_id="p"
        )
    assert other.value.code == "host_forbidden"


def test_a_failed_refresh_is_stale_coverage_that_keeps_the_last_revision():
    env = h.Env()
    env.iati("iati_fdpa_2098-03.xml", observation="r1")
    failed = env.at("2098-04-01T08:00:00").iati(503, observation="r2")
    assert not failed["ok"] and failed["failure"]["state"] == "stale"
    from src.kb.development_finance import DevelopmentFinanceStore

    store = DevelopmentFinanceStore(env.conn)
    assert len(store.current(h.NS)) == 2
    (latest,) = [
        c
        for c in store.latest_coverage(h.NS).values()
        if c["provider"] == "iati-datastore"
    ]
    assert latest["failure_code"] and not latest["complete"]
    assert all(
        store.publication_state(h.NS, k)["state"] == "published"
        for k in store.current(h.NS)
    )


def test_a_page_budget_stop_is_partial_coverage():
    env = h.Env()
    result = env.iati("iati_fdpa_2098-03.xml", observation="r1", rows=2)
    # Two activities fill a page of two: the next page is read and is short, so the selection is complete.
    assert result["coverage"]["complete"] and result["coverage"]["pages_read"] == 2
    client = IATIDatastoreClient(env.http("iati-datastore"), principal_id="ingest")
    collected = client.collect({"publishers": [h.FDPA]}, "r2", rows=2, max_pages=1)
    assert (
        not collected["coverage"]["complete"]
        and collected["coverage"]["stop_reason"] == "page budget reached"
    )


def test_world_bank_projects_are_parsed_as_reported():
    parsed = parse_world_bank_projects(
        json.loads((h.FIXTURES / "wb_projects_2098-04.json").read_text())
    )
    water, education, roads = parsed["projects"]
    assert water["commitments"]["total"] == {
        "text": "150,000,000",
        "value": "150000000",
    }
    assert water["closing_date"] == "2102-06-30" and water["countries"] == ["KE"]
    assert (
        roads["approval_date"] is None
        and roads["commitments"] == {}
        and roads["currency"] is None
    )
    # A project with an unusable id is recorded as rejected; the rest of the page is kept.
    odd = parse_world_bank_projects(
        {"projects": {"X1": {"id": "X1"}, "P999009": {"id": "P999009"}}}
    )
    assert [p["project_id"] for p in odd["projects"]] == ["P999009"]
    assert odd["rejected"][0]["key"] == "X1"
    with pytest.raises(DevelopmentFinanceFormatError):
        parse_world_bank_projects({"rows": 0})


def test_a_link_needs_a_stated_identifier():
    water = parse_iati_activities((h.FIXTURES / "iati_fdpa_2098-03.xml").read_bytes())[
        "activities"
    ][0]
    assert world_bank_link_evidence("P999001", water)[0]["kind"] == "related-activity"
    assert world_bank_link_evidence("P999002", water) == []
    own = {"iati_identifier": "44000-P999003", "reporting_org": {"ref": "44000"}}
    assert (
        world_bank_link_evidence("P999003", own)[0]["kind"]
        == "world-bank-iati-publication"
    )
    # The same identifier from another reporting organisation is not the World Bank's publication.
    assert (
        world_bank_link_evidence(
            "P999003", {**own, "reporting_org": {"ref": "XM-DAC-99901"}}
        )
        == []
    )


def test_crs_recipient_codes_are_classified_by_structure():
    assert classify_recipient("KEN")["kind"] == "country"
    assert (
        classify_recipient("998")["kind"] == "aggregate"
        and classify_recipient("289")["kind"] == "aggregate"
    )
    # A number outside the aggregate structure is unknown, never guessed to be one country.
    assert classify_recipient("248")["kind"] == "unknown"
    assert (
        classify_recipient("DPGC")["kind"] == "aggregate"
        and classify_recipient("AFR_X")["kind"] == "aggregate"
    )
    assert classify_recipient(None)["kind"] == "unknown"


def test_the_crs_source_goes_through_the_sdmx_connector_and_its_pinned_fixture_replays():
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    url, query = SDMXConnector("OECD").csv_url(
        "OECD.DCD.FSD,DSD_CRS@DF_CRS,1.4", "DEU.KEN.140", {}
    )
    assert url.startswith("https://sdmx.oecd.org/public/rest/data/") and query == {
        "format": "csvfile"
    }
    with pytest.raises(ValueError):
        SDMXConnector("OECD").csv_url("DSD_CRS", "DEU")
    pack = h.manifest()
    assert pack["version"] == "1.4.0"
    result = SourcePackConformance(h.ROOT).offline(pack)
    (crs,) = [s for s in result["sources"] if s["source_id"] == h.CRS_SOURCE]
    assert crs["valid"] and crs["records"] == 3
    source = h.crs_source()
    assert (
        source["license"]["terms_url"].startswith("https://")
        and source["license"]["redistribution"]
    )


def test_the_crs_adapter_refuses_other_hosts_and_drifted_shapes():
    source = h.crs_source()
    document = source["development_finance"]["documents"][0]
    from src.ingestion.development_finance_sources import (
        fixture_request,
        fixture_transport,
    )

    redirected = DevelopmentFinanceAdapter(
        source,
        transport=fixture_transport(
            [
                {
                    "request": fixture_request(document),
                    "status": 200,
                    "body": "x",
                    "final_url": "https://evil.example/data.csv",
                }
            ]
        ),
    )
    with pytest.raises(SourcePackError) as refused:
        redirected.fetch_page({"operation": "release", "parameters": {}}, cursor=None)
    assert refused.value.code == "network_policy"
    drifted = DevelopmentFinanceAdapter(
        source,
        transport=fixture_transport(
            [
                {
                    "request": fixture_request(document),
                    "status": 200,
                    "body": "STRUCTURE,STRUCTURE_ID\nx,y\n",
                }
            ]
        ),
    )
    with pytest.raises(SourcePackError) as drift:
        drifted.fetch_page({"operation": "release", "parameters": {}}, cursor=None)
    assert drift.value.code == "schema_drift"
    # The default transport is the runtime's own (same-host redirects only, byte ceiling).
    default = DevelopmentFinanceAdapter(source)
    assert default.transport.func.__qualname__ == "HTTPSPageAdapter._request"


def test_crs_regional_codes_are_aggregates_by_structure_and_by_name():
    """Codes like 1027 "Eastern Africa, regional" are aggregates, never a single country (review)."""
    from src.ingestion.development_finance_sources import split_code_label

    for code in ("1027", "1030", "389", "489", "798", "998"):
        assert classify_recipient(code)["kind"] == "aggregate", code
    assert classify_recipient("KEN", "Africa, regional")["kind"] == "aggregate"
    assert classify_recipient(*split_code_label("KEN: Kenya")) == {
        "code": "KEN",
        "label": "Kenya",
        "kind": "country",
        "scheme": "iso3166-1-alpha3",
    }
    assert (
        classify_recipient(*split_code_label("1027: Eastern Africa, regional"))["kind"]
        == "aggregate"
    )
    assert classify_recipient("X9")["kind"] == "unknown"
