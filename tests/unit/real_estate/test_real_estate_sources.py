"""RE01-RE06 (#2460, #2466, #2471, #2477, #2484, #2489): audit, records and bounded acquisition, offline."""

from __future__ import annotations

import json

import jsonschema
import pytest

from src.ingestion.real_estate_sources import (
    BOUNDED_COVERAGE,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    REUSE_CONDITIONS,
    RealEstateFormatError,
    parse_dvf,
    parse_eurostat_hpi,
    published_money,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.real_estate import RealEstateError, RealEstateStore, validate_statement
from tests.unit.real_estate import fixture_builder as fb
from tests.unit.real_estate.harness import NS, ROOT, Env, manifest, owner_markers, source

SCHEMA = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-real-estate-record-v1.json").read_text())


def test_fixtures_are_pinned_and_in_sync():
    for path, text in fb.build(write=False).items():
        assert path.read_text() == text, f"{path} drifted; run python -m tests.unit.real_estate.fixture_builder"
    result = SourcePackConformance(ROOT).offline(manifest())
    assert {s["source_id"] for s in result["sources"]} == {s["source_id"] for s in manifest()["sources"]}


def test_audit_records_every_contract_reuse_condition_and_bounded_coverage():
    for provider in ("hmlr-ppd", "hmlr-ukhpi", "dvf", "eurostat-hpi", "inspire-cp-fr", "inspire-cp-de-nw"):
        contract = PROVIDER_CONTRACTS[provider]
        assert {"publisher", "access", "identifiers", "licence", "rate_limits", "access_decision"} <= set(contract)
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert provider in BOUNDED_COVERAGE
    assert PROVIDER_CONTRACTS["inspire-cp-fr"]["crs"] and PROVIDER_CONTRACTS["inspire-cp-de-nw"]["crs"]
    assert PROVIDER_CONTRACTS["hmlr-inspire-index-polygons"]["access_decision"] == "not-implemented"
    assert any("re-identification" in c for c in REUSE_CONDITIONS["dvf"]["conditions"])
    doc = (ROOT / "docs/roadmaps/geospatial-real-estate-source-audit.md").read_text()
    for needle in ("id_mutation", "localId", "Price Paid", "prc_hpi_q", "re-identification", "EPSG:25831"):
        assert needle in doc


def test_statements_validate_against_the_contract_and_refuse_party_or_valuation_fields():
    env = Env().loaded()
    store = env.store()
    for record in store.records(NS):
        for revision in store.revisions(NS, record["record_id"]):
            jsonschema.validate(revision["statement"], SCHEMA)
    statement = store.current(NS, store.records(NS, record_type="transaction")[0]["record_id"])["statement"]
    for key in ("owner_name", "market_value", "price_per_m2"):
        bad = json.loads(json.dumps(statement))
        bad["as_published"][key] = "x"
        with pytest.raises(RealEstateError) as caught:
            validate_statement(bad)
        assert caught.value.code == "forbidden_field"


def test_ppd_status_rows_are_revisions_and_a_deletion_withdraws_without_erasing():
    env = Env().loaded()
    store = env.store()
    assert {r["record_key"] for r in store.records(NS, provider="hmlr-ppd")} == {
        fb.TX1.strip("{}"), fb.TX2.strip("{}")}  # XX9 is outside the declared districts
    assert env.ppd_release_2()["status"] == "complete"
    tx1 = store.find(NS, "transaction", "hmlr-ppd", fb.TX1.strip("{}"))
    tx2 = store.find(NS, "transaction", "hmlr-ppd", fb.TX2.strip("{}"))
    first, second = store.revisions(NS, tx1["record_id"])
    assert (first["event"], second["event"]) == ("added", "changed") and second["supersedes"] == first["revision_id"]
    assert first["statement"]["as_published"]["price"] == {"value_text": "450000", "value": "450000",
                                                           "currency": "GBP"}
    assert second["statement"]["as_published"]["price"]["value_text"] == "455000"
    events = [r["event"] for r in store.revisions(NS, tx2["record_id"])]
    assert events == ["added", "withdrawn"]
    published = store.revisions(NS, tx2["record_id"])[0]["statement"]["as_published"]
    assert published["category"] == {"code": "B", "label": "Additional Price Paid"}
    assert published["tenure"]["label"] == "Freehold" and published["new_build"] is True
    assert [v["release"] for v in store.vintages(NS, source_id="hmlr-price-paid-data")] == ["2099-02", "2099-03"]
    # Re-reading the same monthly file adds nothing.
    before = store.generation(NS)
    env.ppd_release_2("ppd-2-again")
    assert store.generation(NS) == before


def test_ppd_without_last_modified_is_refused_rather_than_dated_by_retrieval():
    env = Env()
    pages = fb.ppd_pages(1)
    pages[0]["headers"].pop("Last-Modified")
    adapter = env.native(source("hmlr-price-paid-data"), pages)
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "publications", "parameters": {}}, cursor=None)
    assert caught.value.code == "schema_drift" and "Last-Modified" in str(caught.value)


def test_ukhpi_releases_are_vintages_and_revised_months_are_revisions():
    env = Env().loaded()
    store = env.store()
    env.ukhpi_release()
    westminster = store.find(NS, "price_index_observation", "hmlr-ukhpi", "ukhpi:index:E09000033:2099-01")
    revisions = store.revisions(NS, westminster["record_id"])
    assert [(r["release"], r["statement"]["as_published"]["value"]["value_text"]) for r in revisions] == [
        ("2099-03", "151.9"), ("2099-04", "152.2")]
    unchanged = store.find(NS, "price_index_observation", "hmlr-ukhpi", "ukhpi:index:E09000033:2098-12")
    assert len(store.revisions(NS, unchanged["record_id"])) == 1
    assert [v["release"] for v in store.vintages(NS, source_id="hmlr-uk-hpi")] == ["2099-03", "2099-04"]
    average = store.current(NS, store.find(NS, "price_index_observation", "hmlr-ukhpi",
                                           "ukhpi:average_price:E92000001:2099-02")["record_id"])
    assert average["statement"]["as_published"]["value"]["currency"] == "GBP"
    assert not store.records(NS, provider="hmlr-ukhpi") or all(
        "W92000004" not in r["record_key"] for r in store.records(NS, provider="hmlr-ukhpi"))


def test_dvf_keeps_one_value_per_mutation_and_releases_are_vintages_with_removals():
    env = Env().loaded()
    store = env.store()
    multi = store.current(NS, store.find(NS, "transaction", "dvf", "2098-1001")["record_id"])
    published = multi["statement"]["as_published"]
    assert published["price"] == {"value_text": "1250000,00", "value": "1250000.00", "currency": "EUR"}
    assert published["parcel_ids"] == ["75104000AB0012", "75104000AB0013"]
    assert len(published["locals"]) == 2 and "apportioned" not in json.dumps(published).replace(
        "never apportioned", "")
    assert env.dvf_release()["status"] == "complete"
    changed = store.revisions(NS, store.find(NS, "transaction", "dvf", "2098-1002")["record_id"])
    assert [r["statement"]["as_published"]["price"]["value_text"] for r in changed] == ["640000,00", "645000,00"]
    removed = store.revisions(NS, store.find(NS, "transaction", "dvf", "2098-1003")["record_id"])
    assert [r["event"] for r in removed] == ["observed", "removed"] and removed[1]["release"] == "2099-10"
    assert len(store.revisions(NS, multi["record_id"])) == 1
    assert [v["release"] for v in store.vintages(NS, source_id="dvf-geolocalisees")] == ["2099-04", "2099-10"]


def test_dvf_refuses_a_mutation_with_two_values_or_a_party_column():
    document = fb.dvf_document("2099-04")
    rows = fb.dvf_csv("2099-04").replace('"2098-1001","2098-03-14","000001","Vente","1250000,00","10","",'
                                         '"RUE FICTIVE","0001","75004","75104","Paris 4e Arrondissement","75","","",'
                                         '"75104000AB0013"',
                                         '"2098-1001","2098-03-14","000001","Vente","99,00","10","","RUE FICTIVE",'
                                         '"0001","75004","75104","Paris 4e Arrondissement","75","","",'
                                         '"75104000AB0013"')
    with pytest.raises(RealEstateFormatError) as caught:
        parse_dvf(rows.encode(), document=document, release={}, url="u", origin="fixture")
    assert caught.value.code == "schema_drift" and "not apportioned" in str(caught.value)
    party = fb.dvf_csv("2099-04").replace('"latitude"', '"latitude","proprietaire"', 1)
    with pytest.raises(RealEstateFormatError) as caught:
        parse_dvf(party.encode(), document=document, release={}, url="u", origin="fixture")
    assert caught.value.code == "party_column"


def test_eurostat_vintages_flags_and_rebasing_as_a_new_edition():
    env = Env().loaded()
    store = env.store()
    fr_q2 = store.find(NS, "price_index_observation", "eurostat-hpi",
                       next(r["record_key"] for r in store.records(NS, provider="eurostat-hpi")
                            if ":FR:2098-Q2:" in r["record_key"]))
    first = store.current(NS, fr_q2["record_id"])["statement"]["as_published"]
    assert first["flags"] == [{"code": "p", "label": "provisional"}] and first["base_period"] == "2015=100"
    env.eurostat_vintage()
    revisions = store.revisions(NS, fr_q2["record_id"])
    assert [r["statement"]["as_published"]["value"]["value_text"] for r in revisions] == ["132.0", "132.4"]
    assert revisions[1]["statement"]["as_published"]["flags"] == []
    assert len(store.vintages(NS, source_id="eurostat-house-price-index")) == 2
    # A publisher rebase (new unit code and label) is a new series edition; the old edition is untouched.
    cube = fb.eurostat_cube("FR", 2, unit="I25_Q", unit_label="Index, 2025=100")
    document = {**fb.EUROSTAT_DOCUMENT, "geo": ["FR"], "filters": {**fb.EUROSTAT_FILTERS, "unit": "I25_Q"}}
    statements, _release = parse_eurostat_hpi({"FR": cube.encode()}, document=document, url="u", origin="fixture")
    before = {r["record_id"]: len(store.revisions(NS, r["record_id"])) for r in store.records(NS)}
    store.observe(NS, statements)
    assert all(len(store.revisions(NS, rid)) == n for rid, n in before.items())
    rebased = [r for r in store.records(NS, provider="eurostat-hpi") if r["record_id"] not in before]
    assert len(rebased) == 3 and all(":I25_Q:" in r["record_key"] for r in rebased)
    assert store.current(NS, rebased[0]["record_id"])["statement"]["as_published"]["base_period"] == "2025=100"


def test_inspire_parcels_come_through_the_wfs_path_bounded_without_rights_holders():
    env = Env().loaded()
    store = env.store()
    parcels = {store.current(NS, r["record_id"])["statement"]["as_published"]["national_cadastral_reference"]: r
               for r in store.records(NS, record_type="parcel")}
    assert set(parcels) == {"75104000AB0012", "75104000AB0013", "053001001000120003______"}
    published = store.current(NS, parcels["75104000AB0012"]["record_id"])["statement"]["as_published"]
    assert published["inspire_id"] == "FR.CADASTRALPARCELS.75104000AB0012"
    assert published["geometry"]["source_crs"] == fb.FR_SRS and published["geometry"]["geometry_id"]
    assert published["geometry"]["coordinate_transform"]["producer"]
    row = env.conn.execute("SELECT source_crs, source_geometry_json FROM geospatial_feature_revisions WHERE "
                           "feature_id=?", [published["geometry"]["feature_id"]]).fetchone()
    assert row[0] == fb.FR_SRS and json.loads(row[1])["coordinates"][0][0] == [452600.0, 5410900.0]
    tables = [r[0] for r in env.conn.execute("SELECT table_name FROM information_schema.tables").fetchall()]
    for table in tables:
        assert "NOM FICTIF" not in json.dumps(env.conn.execute(f"SELECT * FROM {table}").fetchall(), default=str)
    runs = store.runs(NS)
    assert any(o.get("properties_dropped") == ["proprietaire"] for r in runs for o in r["outcomes"])
    # A changed geometry adds a parcel revision; an unchanged parcel adds nothing.
    env.parcel_revision()
    assert len(store.revisions(NS, parcels["75104000AB0013"]["record_id"])) == 2
    assert len(store.revisions(NS, parcels["75104000AB0012"]["record_id"])) == 1
    assert not owner_markers([store.revisions(NS, r["record_id"]) for r in store.records(NS)])


def test_parcel_runs_are_held_to_the_pinned_bbox():
    env = Env()
    adapter = env.runtime().fixture_adapters("geospatial-real-estate", ROOT)["inspire-cp-france"]
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "features", "parameters": {"bbox": [0, 0, 1, 1]}, "limit": 50},
                           cursor=None)
    assert caught.value.code == "parameter_forbidden"


def test_money_is_kept_as_published():
    assert published_money("1 250 000,50", "EUR", decimal_comma=True) == {
        "value_text": "1 250 000,50", "value": "1250000.50", "currency": "EUR"}
    with pytest.raises(RealEstateFormatError):
        published_money("n/a", "GBP")


def test_store_is_namespace_scoped_and_readiness_reports_the_feature_off():
    from src.kb.real_estate import readiness

    env = Env().loaded()
    assert RealEstateStore(env.conn, initialize=False).records("other") == []
    report = readiness(env.conn)
    assert report["selected"] is False and report["stores_ready"] is True
    assert report["records"]["transaction"] == 5
