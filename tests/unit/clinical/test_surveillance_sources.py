"""Surveillance source contracts (I01) and RKI, WHO GHO, Eurostat, Destatis and ECDC acquisition (I03-I05), offline."""

from __future__ import annotations

import inspect
import json

import pytest

from src.ingestion import clinical_providers as cp
from src.ingestion import surveillance_sources as ss
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    validate_source_pack,
)
from src.kb.surveillance import SurveillanceError
from tests.unit import surveillance_fixture_builder as fb
from tests.unit.clinical import surveillance_harness as h


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("offline tests must not open network connections")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)


@pytest.fixture(scope="module")
def env():
    loaded = h.Env()
    receipt = loaded.load_all()
    assert receipt["status"] == "complete", receipt.get("failures")
    return loaded


# ---------------------------------------------------------------------- I01 contracts


def test_every_surveillance_source_has_a_documented_contract_and_access_decision():
    required = {
        "documentation",
        "access",
        "authentication",
        "rate_limits",
        "pagination",
        "cadence",
        "terms",
        "retained_evidence",
        "identifiers",
        "cross_references",
        "status",
        "unavailable_fallback",
        "delivers",
        "dates",
        "case_definitions",
        "geography_codes",
        "condition_identifiers",
        "units",
        "access_decision",
        "reason",
    }
    for provider in ss.PROVIDER_CONTRACTS:
        contract = cp.PROVIDER_CONTRACTS[provider]
        assert required <= set(contract), provider
        assert contract["record_owner"] == "src.kb.surveillance"
        assert set(contract["delivers"]) <= set(ss.KINDS)
    decisions = {p: c["access_decision"] for p, c in ss.PROVIDER_CONTRACTS.items()}
    assert decisions == {
        "rki-open-data": "unverified-live",
        "rki-survstat": "not-implemented",
        "ecdc-atlas": "not-implemented",
        "who-gho": "unverified-live",
        "eurostat-health": "unverified-live",
        "destatis-health": "unverified-live",
    }
    # Nothing is live until a dated run; portals without documented machine access are not scraped.
    assert all(
        cp.LIVE_VERIFICATION[p]["status"] in {"unverified-live", "not-implemented"}
        for p in decisions
    )
    for portal in ("rki-survstat", "ecdc-atlas"):
        assert (
            "not scraped" in ss.PROVIDER_CONTRACTS[portal]["reason"]
            or "never fetched" in ss.PROVIDER_CONTRACTS[portal]["reason"]
        )
    # The clinical providers' own contracts are untouched.
    assert cp.PROVIDER_CONTRACTS["ctgov"]["status"] == "implemented"
    assert "record_owner" not in cp.PROVIDER_CONTRACTS["ctgov"]


def test_the_pack_declares_the_surveillance_sources_with_pinned_fixtures_that_replay():
    manifest = validate_source_pack(json.loads(h.PACK.read_text()))
    assert manifest["version"] == "0.1.1"
    surveillance = [s for s in manifest["sources"] if s["connector"] == "surveillance"]
    assert {s["source_id"] for s in surveillance} == set(h.SOURCES.values())
    assert all(
        s["mapping"]["target_schema"] == "noesis-surveillance-record-v1"
        for s in surveillance
    )
    destatis = next(
        s for s in surveillance if s["surveillance"]["provider"] == "destatis-health"
    )
    assert destatis["auth"] == {
        "kind": "required-secret",
        "secret_ref": "NOESIS_DESTATIS_GENESIS_TOKEN",
    }
    result = SourcePackConformance(h.ROOT).offline(manifest)
    assert result["valid"], result["sources"]
    for source in surveillance:
        fixture = json.loads((h.ROOT / source["fixture"]["path"]).read_text())
        assert fixture["authored"] is True and "Not live evidence" in fixture["note"]


def test_fixtures_and_manifest_are_pinned_and_in_sync():
    built = fb.build(write=False)
    assert built == json.loads(h.PACK.read_text())


# ---------------------------------------------------------------------- I03 RKI


def test_rki_values_keep_reporting_and_reference_dates_apart_and_the_release_tag(env):
    series = [
        s
        for s in env.series(provider="rki-open-data")
        if s["dimensions"] == {"age_group": "A15-A34"}
        and s["geography"]["code"] == "09162"
    ]
    (young,) = series
    assert young["geography"] == {
        "system": "ags",
        "code": "09162",
        "label": None,
        "code_list_version": "2099-01-01",
    }
    answer = env.store().answer(h.NS, young["series_id"], scopes=h.READ_ONLY)
    values = {(v["reference_period"], v["reporting_date"]): v for v in answer["values"]}
    assert set(values) == {
        ("2098-12-20", "2098-12-28"),
        ("2099-01-02", "2099-01-05"),
        (None, "2099-01-12"),
    }
    substituted = values[(None, "2099-01-12")]
    # The source filled Refdatum with the reporting date (IstErkrankungsbeginn = 0): not a reference date.
    assert (
        substituted["unknown"] == ["reference_period"]
        and substituted["source_reference_text"] == "2099-01-12"
    )
    assert "reference-date-substituted-by-source" in substituted["flags"]
    assert answer["vintage"]["native_revision"] == f"{fb.RKI_REPOSITORY}@2099-01-20"
    assert answer["vintage"]["source_revision"]["release_basis"] == "declared_release"
    assert {
        "kind": "rki-release",
        "identifier": f"{fb.RKI_REPOSITORY}@2099-01-20",
    } in young["citations"]


def test_rki_case_definition_editions_are_revisions_and_a_change_is_a_break_not_a_restatement(
    env,
):
    (young,) = [
        s
        for s in env.series(provider="rki-open-data", geography_code="09162")
        if s["dimensions"] == {"age_group": "A15-A34"}
    ]
    (brk,) = [b for b in young["breaks"] if b["kind"] == "case-definition"]
    assert (brk["period"], brk["from"], brk["to"]) == ("2099-01-02", "2019", "2099")
    assert brk["detail"]["icd_scope"] == {
        "from": ["A15-A19"],
        "to": ["A15-A19", "A31.0"],
    }
    answer = env.store().answer(h.NS, young["series_id"], scopes=h.READ_ONLY)
    editions = {
        v["reference_period"]: (v["case_definition"] or {}).get("version")
        for v in answer["values"]
    }
    assert editions == {"2098-12-20": "2019", "2099-01-02": "2099", None: None}
    history = env.store().definition_history(h.NS, young["definition_key"])
    assert [(r["version"], r["predecessor_version"]) for r in history["revisions"]] == [
        ("2019", None),
        ("2099", "2019"),
    ]
    assert young["delay_note"]["incomplete_recent"] == {"interval": "week", "count": 3}


def test_rki_rerun_is_idempotent_and_a_new_release_tag_is_a_new_vintage():
    env = h.Env()
    env.acquire("r1", ["rki"])
    tables = (
        "surveillance_releases",
        "surveillance_vintages",
        "surveillance_values",
        "surveillance_breaks",
        "surveillance_case_definition_revisions",
    )

    def counts():
        return {
            t: env.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in tables
        }

    before = counts()
    env.acquire("r1-again", ["rki"])
    assert counts() == before
    env.upgrade_rki("2099-02-03")
    receipt = env.acquire("r2", ["rki"])
    assert receipt["status"] == "complete"
    after = counts()
    assert after["surveillance_releases"] == before["surveillance_releases"] + 1
    (district,) = [
        s for s in env.series(provider="rki-open-data", geography_code="09184")
    ]
    vintages = env.store().vintage_rows(h.NS, district["series_id"])
    assert [v["native_revision"].split("@")[1] for v in vintages] == [
        "2099-01-20",
        "2099-02-03",
    ]
    assert vintages[1]["revision_of"] == vintages[0]["vintage_id"]
    earlier = env.store().answer(
        h.NS,
        district["series_id"],
        scopes=h.READ_ONLY,
        vintage_id=vintages[0]["vintage_id"],
    )
    assert {(v["reference_period"], v["value"]) for v in earlier["values"]} == {
        ("2098-12-30", "1"),
        ("2099-01-08", "2"),
    }
    assert vintages[1]["values_changed"] is True
    # Every release stating a series is a vintage; an unchanged age group says its values did not change.
    (older,) = [
        s
        for s in env.series(provider="rki-open-data", geography_code="09162")
        if s["dimensions"] == {"age_group": "A35-A59"}
    ]
    repeated = env.store().vintage_rows(h.NS, older["series_id"])
    assert len(repeated) == 2 and repeated[1]["values_changed"] is False


# ---------------------------------------------------------------------- I04 WHO GHO and ECDC


def test_gho_bounded_values_are_estimates_and_reporting_dates_stay_unknown_when_missing(
    env,
):
    (estimate,) = env.series(provider="who-gho", kind="estimate")
    (observed,) = env.series(provider="who-gho", kind="observation")
    assert (
        estimate["condition"]["code"] == "NOE_TB_INC_EST"
        and observed["condition"]["code"] == "NOE_TB_NOTIF_RATE"
    )
    assert estimate["indicator"]["label"].startswith(
        "Estimated incidence of tuberculosis"
    )
    values = env.store().answer(h.NS, estimate["series_id"], scopes=h.READ_ONLY)[
        "values"
    ]
    assert [
        (v["reference_period"], v["value"], v["lower"], v["upper"]) for v in values
    ] == [("2097", "6.1", "5.2", "7.0"), ("2098", "6.4", "5.4", "7.5")]
    observed_answer = env.store().answer(
        h.NS, observed["series_id"], scopes=h.READ_ONLY
    )
    by_year = {v["reference_period"]: v for v in observed_answer["values"]}
    assert by_year["2097"]["reporting_date"] is None and by_year["2097"]["unknown"] == [
        "reporting_date"
    ]
    assert by_year["2098"]["reporting_date"] == "2099-02-10"
    # Two pages joined by @odata.nextLink; the answer's Last-Modified is the release clock.
    assert (
        observed_answer["vintage"]["source_revision"]["release_basis"]
        == "http_last_modified"
    )
    assert (
        env.store().answer(h.NS, estimate["series_id"], scopes=h.READ_ONLY)["vintage"][
            "source_revision"
        ]["release_basis"]
        == "latest_value_date"
    )
    assert {"kind": "gho-indicator", "identifier": "NOE_TB_NOTIF_RATE"} in observed[
        "citations"
    ]


def test_gho_refuses_unsupported_shapes_and_over_long_answers():
    document = fb.gho_document("NOE_TB_NOTIF_RATE")
    metadata = json.dumps(
        {"value": [{"IndicatorCode": "NOE_TB_NOTIF_RATE", "IndicatorName": "x"}]}
    ).encode()
    row = fb.gho_row(
        "NOE_TB_NOTIF_RATE", fb.GHO_INDICATORS["NOE_TB_NOTIF_RATE"]["rows"][1], 1
    )
    with pytest.raises(ss.SurveillanceFormatError, match="SpatialDimType"):
        ss.parse_gho(
            [
                json.dumps(
                    {"value": [{**row, "SpatialDimType": "WORLDBANKINCOMEGROUP"}]}
                ).encode()
            ],
            metadata,
            document=document,
        )
    with pytest.raises(ss.SurveillanceFormatError, match="another indicator"):
        ss.parse_gho(
            [json.dumps({"value": [{**row, "IndicatorCode": "OTHER"}]}).encode()],
            metadata,
            document=document,
        )
    endless = []
    source = h.source("gho")
    urls = ss.gho_urls(document, source["endpoint"])
    endless.append(
        {"request": ss.fixture_request(urls["metadata"]), "body": metadata.decode()}
    )
    url = urls["data"]
    for n in range(ss.MAX_GHO_PAGES + 1):
        following = urls["data"] + f"&$skip={n + 1}"
        endless.append(
            {
                "request": ss.fixture_request(url),
                "body": json.dumps({"value": [row], "@odata.nextLink": following}),
            }
        )
        url = following
    single = json.loads(json.dumps(source))
    single["surveillance"]["documents"] = [document]
    adapter = ss.SurveillanceAdapter(single, transport=ss.fixture_transport(endless))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert caught.value.code == "budget_exhausted"


def test_ecdc_exports_enter_only_from_the_operator_as_separate_series(env):
    ecdc = env.series(provider="ecdc-atlas")
    assert {
        (
            s["condition"]["code"],
            s["geography"]["system"],
            s["geography"]["code"],
            s["unit"]["label"],
        )
        for s in ecdc
    } == {
        ("Tuberculosis", "eu-country", "DE", "per 100 000 population"),
        ("Tuberculosis", "eu-country", "DE", "cases"),
        ("Tuberculosis", "ecdc-aggregate", "EU_EEA31", "per 100 000 population"),
        ("Legionnaires' disease", "eu-country", "DE", "cases"),
    }
    (legionella,) = [
        s for s in ecdc if s["condition"]["code"] == "Legionnaires' disease"
    ]
    (value,) = env.store().answer(h.NS, legionella["series_id"], scopes=h.READ_ONLY)[
        "values"
    ]
    assert (
        value["value"] is None
        and value["value_text"] == "-"
        and "missing-as-published" in value["flags"]
    )
    release = env.store().releases(h.NS, provider="ecdc-atlas")[0]
    assert (
        release["evidence_origin"] == "operator"
        and release["release_basis"] == "declared_extraction"
    )
    # The same export again adds nothing; ECDC and WHO values for Germany stay separate series.
    assert env.import_ecdc()["status"] == "unchanged"
    who = env.series(provider="who-gho")
    assert not {s["series_id"] for s in who} & {s["series_id"] for s in ecdc}


def test_ecdc_export_variant_shapes_are_refused_and_writes_need_scopes():
    env = h.Env()
    bad = fb.ecdc_export()
    bad["content"] = bad["content"].replace("TxtValue", "Comment")
    with pytest.raises(SurveillanceError, match="columns"):
        env.import_ecdc(export=bad)
    undeclared = fb.ecdc_export()
    undeclared["document"]["units"] = {"N": "cases"}
    with pytest.raises(SurveillanceError, match="not declared"):
        env.import_ecdc(export=undeclared)
    with pytest.raises(SurveillanceError) as caught:
        env.store().import_export(
            h.NS, fb.ecdc_export(), principal_id="p", scopes=h.READ_ONLY
        )
    assert caught.value.code == "unauthorized"


# ---------------------------------------------------------------------- I05 Eurostat and Destatis


def test_eurostat_series_come_through_the_sdmx_connector_with_flags_breaks_and_vintages():
    source_text = inspect.getsource(ss.parse_eurostat)
    assert "SDMXConnector" in source_text and "sdmx.Client" not in inspect.getsource(ss)
    env = h.Env()
    env.acquire("r1", ["eurostat"])
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    assert germany["geography"]["system"] == "eu-country" and germany["condition"] == {
        "scheme": "eurostat-icd10",
        "code": "A15-A19_B90",
        "label": "Tuberculosis",
    }
    assert germany["unit"] == {"label": "deaths", "published": "NR", "kind": "count"}
    first = env.store().answer(h.NS, germany["series_id"], scopes=h.READ_ONLY)
    by_year = {v["reference_period"]: v for v in first["values"]}
    assert by_year["2098"]["flags"] == ["b: break in time series", "p: provisional"]
    assert all(v["reporting_date"] is None for v in first["values"])
    assert (
        first["vintage"]["source_revision"]["release_basis"] == "eurostat_last_update"
    )
    assert first["vintage"]["source_revision"]["published_at"] == "2099-03-15T11:00:00"
    assert [(b["kind"], b["period"]) for b in germany["breaks"]] == [
        ("publisher-flag", "2098")
    ]
    (bayern,) = env.series(provider="eurostat-health", geography_code="DE2")
    assert bayern["geography"]["system"] == "nuts"
    confidential = {
        v["reference_period"]: v
        for v in env.store().answer(h.NS, bayern["series_id"], scopes=h.READ_ONLY)[
            "values"
        ]
    }["2098"]
    assert confidential["value"] is None and confidential["value_text"] == ":"
    env.eurostat_update()
    env.acquire("r2", ["eurostat"])
    vintages = env.store().vintage_rows(h.NS, germany["series_id"])
    assert (
        len(vintages) == 2 and vintages[1]["revision_of"] == vintages[0]["vintage_id"]
    )
    assert vintages[1]["values_changed"] is True
    # Bayern's values did not change: a vintage of the new update that says so.
    assert [
        v["values_changed"] for v in env.store().vintage_rows(h.NS, bayern["series_id"])
    ] == [True, False]
    env.acquire("r3", ["eurostat"])
    assert (
        len(env.store().vintage_rows(h.NS, germany["series_id"])) == 2
    )  # re-acquiring the update adds nothing


def test_eurostat_refuses_undocumented_flags_and_undeclared_units():
    document = fb.eurostat_document()
    raw = fb.eurostat_csv().replace(",bp", ",q").encode()
    with pytest.raises(ss.SurveillanceFormatError, match="flag letters"):
        ss.parse_eurostat(raw, document=document)
    with pytest.raises(ss.SurveillanceFormatError, match="not declared"):
        ss.parse_eurostat(
            fb.eurostat_csv().encode(),
            document={**document, "units": {"RT": "per 100 000 population"}},
        )


def test_destatis_goes_through_the_genesis_connector_needs_its_credential_and_keeps_signs_absent(
    env,
):
    (berlin,) = env.series(provider="destatis-health", geography_code="11")
    values = {
        v["reference_period"]: v
        for v in env.store().answer(h.NS, berlin["series_id"], scopes=h.READ_ONLY)[
            "values"
        ]
    }
    assert values["2097"]["value"] is None and values["2097"]["value_text"] == "."
    assert values["2098"]["value"] == "12"
    assert berlin["geography"] == {
        "system": "ags",
        "code": "11",
        "label": "Berlin",
        "code_list_version": "2099-01-01",
    }
    release = env.store().releases(h.NS, provider="destatis-health")[0]
    assert (
        release["release_basis"] == "genesis_table_updated"
        and release["published_on"] == "2099-08-12"
    )
    adapter = ss.SurveillanceAdapter(h.source("destatis"), transport=h.Web().transport)
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert caught.value.code == "authentication_failed"


# ---------------------------------------------------------------------- network policy


def test_the_default_transport_is_the_runtimes_and_cross_host_answers_are_refused():
    from src.ingestion.source_pack_runtime import HTTPSPageAdapter

    adapter = ss.SurveillanceAdapter(h.source("rki"))
    assert adapter.transport.func is HTTPSPageAdapter._request
    assert adapter.transport.keywords == {"max_bytes": 20000000}
    web = h.Web()
    request = fb.rki_request("2099-01-20")
    web.pages[request]["final_url"] = "https://evil.example/Daten.csv"
    redirected = ss.SurveillanceAdapter(h.source("rki"), transport=web.transport)
    with pytest.raises(SourcePackError) as caught:
        redirected.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert caught.value.code == "network_policy"
    moved = json.loads(json.dumps(h.source("rki")))
    moved["endpoint"] = "https://example.org"
    with pytest.raises(SourcePackError, match="documented host"):
        ss.SurveillanceAdapter(moved)
    with pytest.raises(SourcePackError, match="declared documents only"):
        ss.SurveillanceAdapter(h.source("rki"), transport=h.Web().transport).fetch_page(
            {"operation": "records", "parameters": {"tag": "main"}}, cursor=None
        )


def test_rki_variant_shapes_are_refused_never_read_partially():
    document = fb.rki_document()
    with pytest.raises(ss.SurveillanceFormatError, match="not in the release"):
        ss.parse_rki(
            fb.rki_csv("2099-01-20").replace("Refdatum", "Referenzdatum").encode(),
            document=document,
        )
    duplicate = fb.rki_csv("2099-01-20") + fb.RKI_ROWS["2099-01-20"][0] + "\n"
    with pytest.raises(ss.SurveillanceFormatError, match="twice"):
        ss.parse_rki(duplicate.encode(), document=document)
    with pytest.raises(ss.SurveillanceFormatError, match="0 or 1"):
        ss.parse_rki(
            fb.rki_csv("2099-01-20").replace(",1,3", ",?,3").encode(), document=document
        )
    with pytest.raises(ss.SurveillanceFormatError):
        ss.rki_url({**document, "path": "../secrets.csv"})
