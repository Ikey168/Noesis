"""EIOPA statistics, SFCR reports, NAIC under its access decision and catastrophe-loss estimates (#2230, IN03-IN06)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import tests.unit.insurance_harness as h
from src.domains.market import insurance
from src.domains.market.insurance import InsuranceStore, record_id
from src.ingestion.insurance_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    InsuranceAdapter,
    fixture_transport,
    insurance_declaration,
    parse_document,
    read_csv,
    read_xlsx,
    replay_native_fixture,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError, validate_source_pack

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def conn():
    return h.connection()


def test_the_audit_is_restated_in_code_and_every_source_is_unverified_live():
    audit = (ROOT / "docs/development/insurance-evidence/source-audit.md").read_text()
    assert "(verify)" in audit and "Bounded coverage" in audit and "public_and_acquired" in audit
    assert PROVIDER_CONTRACTS["naic-public"]["access_decision"] == "metadata-only"
    assert {p for p, c in PROVIDER_CONTRACTS.items() if c["access_decision"] == "excluded"} == {
        "perils", "verisk-pcs", "naic-licensed-products"}
    assert LIVE_VERIFICATION["eiopa-insurance-statistics"]["status"] == "unverified-live"
    assert LIVE_VERIFICATION["perils"]["status"] == "excluded"


def test_the_production_pack_validates_and_replays_its_pinned_fixtures_offline():
    manifest = validate_source_pack(json.loads(h.PACK.read_text()))
    assert manifest["pack_id"] == "insurance-supervisory-and-catastrophe-losses" and manifest["domains"] == ["market"]
    assert all(s["connector"] == "insurance" for s in manifest["sources"])
    report = SourcePackConformance(ROOT).offline(manifest)
    assert report["valid"], report
    # The production scope excludes every authored row: nothing fictional is stored for EIOPA, Florida or NCEI.
    for key in ("eiopa", "florida", "ncei"):
        source = h.production(key)
        assert replay_native_fixture(source, json.loads((ROOT / source["fixture"]["path"]).read_text())) == []
    naic = h.production("naic")
    assert naic["insurance"]["access_decision"] == "metadata-only"
    [reference] = replay_native_fixture(naic, json.loads((ROOT / naic["fixture"]["path"]).read_text()))
    assert reference["insurance_record"]["kind"] == "publication_reference"


def test_eiopa_releases_are_vintages_with_markers_kept_and_scope_counted(conn):
    [first] = h.acquire(conn, "eiopa", "2025-07-01")
    assert first["counts"]["out_of_scope"] == 1 and first["counts"]["outside_window"] == 1
    assert first["counts"]["rejected"] == 1  # the undeclared marker "??"
    assert first["release"] == {"dataset": "Premiums, claims and expenses by line of business",
                                "release": "2025-06", "release_date": "2025-06-30"}
    h.acquire(conn, "eiopa", "2026-01-05")
    store = InsuranceStore(conn, initialize=False)
    assert [r["release"] for r in store.releases(h.NS)] == ["2025-06", "2025-12"]
    views = {v["record"]["indicator"] + "|" + v["record"]["dimensions"]["line_of_business"]: v
             for v in store.visible(h.NS, kinds=("supervisory_indicator",))
             if v["record"]["dimensions"]["country"] == "DE"}
    fire = views["gross_written_premiums|Fire and other damage to property insurance"]
    assert fire["record"]["value"] is None and fire["record"]["marker"] == "c"
    motor = views["gross_written_premiums|Motor vehicle liability insurance"]
    history = store.history(h.NS, motor["record_id"])
    assert [(r["record"]["value"], r["record"]["release"]) for r in history] == [
        ("1000.5", "2025-06"), ("1010.5", "2025-12")]
    # The unchanged claims figure added no revision in the second release.
    claims = views["gross_claims_incurred|Motor vehicle liability insurance"]
    assert len(store.history(h.NS, claims["record_id"])) == 1
    assert motor["record"]["definition"].startswith("Gross written premiums as published")


def test_an_xlsx_workbook_reads_the_same_cells_as_the_csv_export():
    raw = (h.FIXTURES / "eiopa_release_2025-06.csv").read_bytes()
    declared = h.fictional("eiopa", ["eiopa_release_2025-06.csv"])["insurance"]
    rows_csv, missing = read_csv(raw, declared)
    rows_xlsx, missing_x = read_xlsx(h.make_xlsx(raw.decode()), {**declared, "sheet": "Data"})
    assert rows_csv == rows_xlsx and missing == missing_x == []


def test_sfcr_figures_are_quoted_with_qrt_locators_and_unreadable_cells_stay_unknown(conn):
    receipts = h.acquire(conn, "sfcr", "2025-05-02")
    assert [r["counts"]["records"] for r in receipts] == [1, 1]
    store = InsuranceStore(conn, initialize=False)
    reports = {v["record"]["insurer"]["reporting_level"]: v["record"] for v in store.visible(h.NS)}
    group = reports["group"]
    assert group["insurer"]["lei"] == h.GROUP_LEI and group["reporting_year"] == 2024
    figures = {f["figure_id"]: f for f in group["figures"]}
    ratio = figures["S.23.01.22|R0690|C0010"]
    assert ratio["status"] == "reported" and ratio["value"] == "212" and ratio["unit"] == "%"
    assert "R0690" in ratio["quoted"]
    assert figures["S.23.01.22|R0680|C0010"]["value"] == "5432.1"
    absent = figures["S.25.01.22|R0220|C0100"]
    assert absent["status"] == "unknown" and absent["value"] is None and "not found" in absent["reason"]
    # Solo templates only for the solo report, group templates only for the group report.
    assert not any(f["template"] == "S.25.01.21" for f in group["figures"])
    assert {f["template"] for f in reports["solo"]["figures"]} == {"S.25.01.21", "S.23.01.01"}
    assert reports["solo"]["insurer"]["lei"] == h.SOLO_LEI


def test_a_corrected_sfcr_is_a_revision_and_the_earlier_report_is_kept(conn):
    h.acquire(conn, "sfcr", "2025-05-02")
    h.acquire(conn, "sfcr", "2025-06-12")
    store = InsuranceStore(conn, initialize=False)
    rid = record_id(h.NS, "insurer-sfcr", f"{h.GROUP_LEI}|sfcr|2024|group")
    history = store.history(h.NS, rid)
    ratios = [next(f["value"] for f in r["record"]["figures"] if f["row"] == "R0690") for r in history]
    assert ratios == ["212", "210"]
    assert history[0]["record"]["document"]["sha256"] != history[1]["record"]["document"]["sha256"]


def test_a_report_that_does_not_name_the_declared_insurer_is_never_attributed_to_it():
    source = h.fictional("sfcr", ["sfcr_group_2024.pdf"])
    source["insurance"]["insurers"] = [{"name": "Allianz SE", "reporting_level": "group"}]
    source["insurance"]["documents"][0]["insurer"] = {"name": "Allianz SE", "reporting_level": "group"}
    adapter = InsuranceAdapter(h.rehash(source), transport=fixture_transport(h.pages(["sfcr_group_2024.pdf"])))
    page = adapter.fetch_page({"operation": "documents", "parameters": {}, "limit": 10}, cursor=None)
    assert [r["rejection"]["code"] for r in page.records] == ["out_of_scope"]


def test_without_a_pdf_extractor_every_figure_is_unknown_not_estimated(monkeypatch):
    import src.ingestion.connectors.paper.pdf_parser as pdf

    monkeypatch.setattr(pdf, "extract_pdf_text", lambda raw: None)
    source = h.fictional("sfcr", ["sfcr_solo_2024.pdf"])
    parsed = parse_document("sfcr-pdf", h.body("sfcr_solo_2024.pdf"), source["insurance"],
                            source["insurance"]["documents"][0])
    [report] = parsed["records"]
    assert all(f["status"] == "unknown" and f["value"] is None for f in report["figures"])
    assert "PyMuPDF" in report["figures"][0]["reason"] and report["unknowns"] == ["lei"]


def test_naic_follows_its_recorded_metadata_only_decision(conn):
    [receipt] = h.acquire(conn, "naic", "2025-06-01")
    assert receipt["access_decision"] == "metadata-only"
    [view] = InsuranceStore(conn, initialize=False).visible(h.NS)
    assert view["record"]["kind"] == "publication_reference" and "figures" not in view["record"]
    assert view["record"]["access_decision"] == "metadata-only" and view["record"]["reason"]
    source = h.fictional("naic", ["naic_market_share_2024.pdf"])
    for change, code in (
        ({"format": "naic-market-share-csv"}, "metadata_only"),
        ({"access_decision": "in-scope"}, "invalid_mapping"),
        ({"provider": "naic-licensed-products"}, "licence_excluded"),
    ):
        broken = copy.deepcopy(source)
        broken["insurance"].update(change)
        with pytest.raises(SourcePackError) as refused:
            insurance_declaration(broken)
        assert refused.value.code == code
    keyed = copy.deepcopy(source)
    keyed["auth"] = {"kind": "api-key", "secret_ref": "NOESIS_NAIC_KEY"}
    with pytest.raises(SourcePackError) as subscription:
        insurance_declaration(keyed)
    assert subscription.value.code == "licence_excluded"


def test_naic_rows_are_keyed_by_company_and_group_code_once_a_decision_permits(monkeypatch, conn):
    """Simulates a recorded human decision changing NAIC to in-scope; the shipped decision stays metadata-only."""
    permitted = {**insurance.LICENCE_DECISIONS["naic-public"], "decision": "in-scope",
                 "kinds": ("supervisory_indicator",)}
    monkeypatch.setitem(insurance.LICENCE_DECISIONS, "naic-public", permitted)
    source = h.fictional("naic", ["naic_market_share.csv"])
    source["insurance"].update({
        "access_decision": "in-scope", "format": "naic-market-share-csv", "dataset": "Market share (fixture)",
        "indicator": "direct_written_premiums", "indicator_label": "Direct written premium", "decimal": ".",
        "columns": {"company_code": "Company Code", "group_code": "Group Code", "company_name": "Company Name",
                    "line": "Line", "year": "Year", "amount": "Direct Written Premium"},
    })
    source["insurance"]["documents"][0].update({"release": "2024", "publication_date": "2025-05-20"})
    [receipt] = h.run(conn, h.rehash(source), ["naic_market_share.csv"], "2025-06-01")
    assert receipt["counts"]["records"] == 2 and receipt["counts"]["rejected"] == 1
    views = InsuranceStore(conn, initialize=False).visible(h.NS)
    assert {(v["record"]["insurer"]["naic_company_code"], v["record"]["insurer"]["naic_group_code"]) for v in views} == {
        ("12345", "4321"), ("54321", "4321")}
    assert {v["record"]["period"]["reference_year"] for v in views} == {2024}


def test_estimates_are_publisher_revisions_never_merged_and_excluded_publishers_refused(conn):
    [florida] = h.acquire(conn, "florida", "2025-02-01")
    assert florida["counts"]["outside_window"] == 1  # the 2019 event
    h.acquire(conn, "ncei", "2025-07-15")
    store = InsuranceStore(conn, initialize=False)
    views = store.visible(h.NS, kinds=("catastrophe_loss_estimate",))
    fiktiva = [v for v in views if v["record"]["event"]["name"].startswith("Hurricane Fiktiva")]
    assert {v["record"]["source"]["provider"] for v in fiktiva} == {"florida-oir-claims", "noaa-ncei-billion-dollar"}
    assert len({v["record_id"] for v in fiktiva}) == 2  # one series per publisher
    oir = next(v for v in fiktiva if v["record"]["source"]["provider"] == "florida-oir-claims")
    assert [r["record"]["value"] for r in store.history(h.NS, oir["record_id"])] == [
        "1000000000", "1500000000", "1450000000"]
    assert oir["record"]["event"]["identifiers"] == {"nhc_storm_id": "AL992024"}
    assert oir["record"]["source"]["licence"]["decision"] == "in-scope"
    ncei = next(v for v in fiktiva if v["record"]["source"]["provider"] == "noaa-ncei-billion-dollar")
    assert ncei["record"]["estimate_type"] == "economic" and ncei["revisions_on_record"] == 2
    perils = h.fictional("florida", ["perils_press_release.csv"])
    perils["insurance"]["provider"] = "perils"
    with pytest.raises(SourcePackError) as refused:
        insurance_declaration(perils)
    assert refused.value.code == "licence_excluded"
