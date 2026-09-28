"""Engineering Safety connectors on authored fixtures (ES03-ES09, #2064-#2070)."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion.engineering_safety_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    EngineeringSafetyAdapter,
    ad_paragraphs,
    declaration,
    fixture_transport,
    html_blocks,
    parse_applicability,
    parse_complaints,
    parse_date,
    sections,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError, replay_native_fixture
from tests.unit.engineering_safety import harness as h


def statements(source_id, native=None):
    source = h.source(source_id)
    records = replay_native_fixture(source, {"native_pages": native if native is not None else h.pages(source_id)})
    return [r["engineering_safety_record"] for r in records]


def by_id(items, native_id):
    return [s for s in items if s["native_id"] == native_id]


def test_every_source_replays_its_pinned_fixture_and_every_decision_is_recorded():
    result = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK.read_text()))
    assert result["valid"] and result["coverage"]["configured"] == result["coverage"]["verified"] == 9
    assert {s["source_id"] for s in result["sources"]} == set(h.SOURCES)
    for name in ("faa-ad", "faa-drs", "easa-ad", "ntsb", "ntsb-monthly", "phmsa", "csb", "nhtsa-odi",
                 "nhtsa-complaints", "nhtsa-recalls", "bfu", "bea"):
        assert PROVIDER_CONTRACTS[name]["decision"] in {"implement", "link-only", "not implemented"}
    assert LIVE_VERIFICATION["faa-ad"]["status"] == "unverified-live"
    assert LIVE_VERIFICATION["nhtsa-recalls"]["status"] == "not-implemented"
    audit = (h.ROOT / "docs/development/engineering-safety-evidence/source-audit.md").read_text()
    assert "(verify)" in audit and "#1916" in audit
    for source in h.manifest()["sources"]:
        anchor = source["engineering_safety"]["audit"].split("#")[1]
        assert anchor.replace("-", " ") in audit.lower().replace("-", " "), anchor


# ------------------------------------------------------------------ FAA (ES03)


def test_faa_ads_keep_supersession_applicability_effective_date_and_required_actions():
    ads = statements("faa-airworthiness-directives")
    assert [s["native_id"] for s in ads] == ["2025-12-05", "2026-04-12", "2026-04-12", "2026-08-02"]
    superseding, correction = by_id(ads, "2026-04-12")
    assert superseding["identifiers"]["fr_document_number"] == "2026-04512"
    assert correction["identifiers"]["fr_document_number"] == "2026-05120"
    assert correction["revision_label"] == "correction" and "revision_label" not in superseding
    assert superseding["effective_date"] == "2026-04-08" and superseding["revision_date"] == "2026-03-04"
    assert [(r["relation"], r["target_native_id"]) for r in superseding["relations"]] == [("supersedes", "2025-12-05")]
    (clause,) = superseding["applicability"]
    assert clause["parse_state"] == "parsed" and clause["locator"]["paragraph"] == "(c)"
    assert clause["parsed"]["models"] == ["EX-100", "EX-100A"]
    assert clause["parsed"]["serials"] == {"ranges": [["1001", "1400"]]}
    labels = [(s["kind"], s["label"]) for s in superseding["statements"]]
    assert ("required_action", "(g)") in labels and ("compliance_time", "(f)") in labels
    assert ("other", "(h)") in labels and ("incorporated_material", "(j)") in labels
    required = next(s for s in superseding["statements"] if s["kind"] == "required_action")
    assert "(i) If any crack is found" in required["text"] and "(ii) Repeat" in required["text"]
    assert "Alternative Methods" not in required["text"]  # scoped to its own paragraph
    # The (h)-(j) paragraphs are found even though a roman "(i)" sub-item precedes them.
    original = by_id(ads, "2025-12-05")[0]
    assert [s["label"] for s in original["statements"]][-3:] == ["(h)", "(i)", "(j)"]
    assert "relations" not in original  # "None." states no affected AD


def test_ambiguous_applicability_falls_back_to_the_verbatim_clause():
    (unparsed,) = by_id(statements("faa-airworthiness-directives"), "2026-08-02")
    (clause,) = unparsed["applicability"]
    assert clause["parse_state"] == "unparsed" and "parsed" not in clause
    assert "(2) Model EX-200 airplanes" in clause["text"]
    assert parse_applicability("This AD applies to Model EX-1 and EX-2 airplanes, serial numbers 1 through 5 and "
                               "serial numbers 9 through 12.")[0] == "unparsed"
    assert parse_applicability("This AD applies to all airplanes.")[0] == "unparsed"


def test_a_non_ad_federal_register_document_is_out_of_scope_not_a_record():
    source = h.source("faa-airworthiness-directives")
    source["engineering_safety"]["selection"] = [{"document_number": "2026-06001"}]
    adapter = EngineeringSafetyAdapter(source, transport=fixture_transport(h.pages("faa-airworthiness-directives")))
    page = adapter.fetch_page({"operation": "records"}, cursor=None)
    assert page.records == () and page.receipt["selector_outcome"] == "out_of_scope"
    assert page.receipt["out_of_scope"] == 1


def test_paragraph_headings_open_only_in_sequence():
    text = "(a) Effective Date\nX.\n(c) Stray\n(b) Affected ADs\nNone.\n(i) sub item\n(c) Applicability. Inline body\n"
    found = ad_paragraphs(text, 0, len(text))
    assert [(p["label"], p["heading"]) for p in found] == [("(a)", "Effective Date"), ("(b)", "Affected ADs"),
                                                          ("(c)", "Applicability")]
    assert found[1]["text"] == "None.\n(i) sub item" and found[2]["text"] == "Inline body"


# ------------------------------------------------------------------ EASA (ES04)


def test_easa_revisions_share_one_key_and_cross_references_are_only_those_stated():
    base, revised = statements("easa-airworthiness-directives")
    assert base["native_id"] == revised["native_id"] == "2026-0123"
    assert "revision_label" not in base and revised["revision_label"] == "R1"
    assert base["revision_date"] == "2026-03-10" and revised["effective_date"] == "2026-05-16"
    assert "relations" not in base
    assert revised["relations"] == [{"relation": "cross_reference", "target_provider": "faa-ad",
                                     "target_native_id": "2026-04-12", "text": "FAA AD 2026-04-12",
                                     "locator": {"url": "https://ad.easa.europa.eu/ad/2026-0123R1",
                                                 "row": "related ad(s)"}}]
    assert revised["applicability"][0]["parsed"]["serials"] == {"all": True}


def test_easa_answering_another_number_is_schema_drift():
    native = h.pages("easa-airworthiness-directives")
    native[1]["body"] = native[0]["body"]
    with pytest.raises(SourcePackError) as drift:
        statements("easa-airworthiness-directives", native)
    assert drift.value.code == "schema_drift"


# ------------------------------------------------------------------ NTSB (ES05)


def test_ntsb_preliminary_and_final_reports_and_recommendation_statuses():
    case, recommendation = statements("ntsb-investigations")
    assert case["report_status"] == "preliminary" and "statements" not in case
    assert case["occurrence"]["latitude"] == 33.1234
    aircraft = next(s for s in case["subjects"] if s["kind"] == "aircraft")
    assert aircraft["fields"]["registration"] == "N417EX" and aircraft["fields"]["model"] == "EX-100"
    assert recommendation["responses"][0]["status"] == "Open - Await Response"
    assert recommendation["relations"][0] == {"relation": "recommendation_of", "target_provider": "ntsb",
                                              "target_native_id": "ERA26FA101", "locator": {
                                                  "url": "https://data.ntsb.gov/carol-main-public/api/"
                                                         "recommendations/A-26-015",
                                                  "json_pointer": "/NtsbNumbers/0"}}
    final, _ = statements("ntsb-investigations", h.ntsb_pages(case=h.variants()["ntsb_case_final"]))
    assert final["report_status"] == "final" and final["revision_date"] == "2026-08-14"
    cause = next(s for s in final["statements"] if s["kind"] == "probable_cause")
    assert cause["text"].startswith("The fatigue fracture") and cause["locator"]["report_status"] == "final"


def test_coordinates_are_stored_only_when_published():
    case = h.variants()["ntsb_case_final"]
    del case["Latitude"], case["Longitude"]
    final, _ = statements("ntsb-investigations", h.ntsb_pages(case=case))
    assert "latitude" not in final["occurrence"] and final["occurrence"]["place"].startswith("Millbrook")


# ------------------------------------------------------------------ PHMSA (ES06)


def test_phmsa_rows_are_windowed_with_native_units_and_operator_as_published():
    source = h.source("phmsa-hazardous-liquid-incidents")
    adapter = EngineeringSafetyAdapter(source, transport=fixture_transport(h.pages(source["source_id"])))
    page = adapter.fetch_page({"operation": "records"}, cursor=None)
    rows = [r["engineering_safety_record"] for r in page.records]
    assert [r["native_id"] for r in rows] == ["20260045", "20260051"] and page.receipt["out_of_scope"] == 1
    first = rows[0]
    assert first["revision_date"] == "2026-06-01" and first["revision_date_basis"] == "file-vintage"
    assert first["quantities"] == [{"name": "unintentional_release", "value": "125.5", "unit": "bbl",
                                    "locator": {"document": source["engineering_safety"]["documents"][0]["url"],
                                                "vintage": "2026-06-01", "row": 2,
                                                "column": "UNINTENTIONAL_RELEASE_BBLS"}}]
    assert first["subjects"][0]["fields"] == {"operator_id": "39999", "name": "Examplar Pipeline Company LP"}
    assert "latitude" not in rows[1]["occurrence"]  # empty coordinates stay absent


# ------------------------------------------------------------------ CSB (ES07)


def test_csb_sections_are_scoped_including_bold_paragraph_headings():
    investigation, first, second = statements("csb-investigations")
    kinds = [(s["kind"], s["locator"]["section"]) for s in investigation["statements"]]
    assert kinds == [("finding", "Key Findings"), ("finding", "Key Findings"), ("root_cause", "Causal Factors")]
    assert all("safety video" not in s["text"] for s in investigation["statements"])  # no carry-over
    assert investigation["report_status"] == "final"
    assert {s["kind"] for s in investigation["subjects"]} == {"organisation", "facility"}
    assert first["native_id"] == "2026-02-I-TX-R1" and first["responses"][0]["addressee"] == \
        "Examplar Chemical Company"
    assert second["responses"][0]["status_date"] == "2026-07-01"
    blocks = html_blocks("<h2>A</h2><p>one</p><p><strong>B</strong></p><p>two <b>bold</b></p>")
    assert [(heading, [i["text"] for i in items]) for heading, _, items in sections(blocks)] == [
        ("A", ["one"]), ("B", ["two bold"])]


# ------------------------------------------------------------------ NHTSA ODI (ES08)


def test_odi_actions_group_rows_state_upgrades_and_cite_recalls_without_acquiring_them():
    ea, pe = statements("nhtsa-odi-investigations")
    assert (ea["native_id"], pe["native_id"]) == ("EA26002", "PE26003")
    assert {(r["relation"], r["target_native_id"]) for r in ea["relations"]} == {
        ("cites_recall", "26V104000"), ("upgraded_from", "PE26003")}
    assert pe["relations"] == [{"relation": "upgraded_to", "target_provider": "nhtsa-odi",
                                "target_native_id": "EA26002", "text": "upgraded to Engineering Analysis (EA26002",
                                "locator": pe["relations"][0]["locator"]}]
    assert pe["revision_date"] == "2026-05-20" and pe["identifiers"]["closed"] == "2026-05-20"
    years = sorted(s["fields"]["model_year"] for s in pe["subjects"] if s["kind"] == "vehicle")
    assert years == ["2025", "2026"]
    opened, = statements("nhtsa-odi-investigations", h.odi_pages(h.variants()["odi_open_file"]))
    assert opened["native_id"] == "PE26003" and "relations" not in opened and opened["revision_date"] == "2026-01-12"


def test_complaints_are_unverified_records_bounded_never_truncated():
    complaints = statements("nhtsa-complaints")
    assert [c["record_kind"] for c in complaints] == ["complaint", "complaint"]
    assert "unverified" in complaints[0]["title"] and "vin" not in json.dumps(complaints).lower()
    payload = json.loads(json.dumps(h.pages("nhtsa-complaints")[0]["body"]))
    with pytest.raises(SourcePackError) as refused:
        parse_complaints(payload, {"make": "VELOMARK", "model": "CITYRUNNER", "model_year": "2025"},
                         url="https://api.nhtsa.gov/x", bound=1)
    assert refused.value.code == "response_too_large"


# ------------------------------------------------------------------ BFU and BEA (ES09)


def test_bfu_findings_stay_verbatim_in_german_and_bea_is_link_only():
    report, recommendation = statements("bfu-reports")
    assert report["language"] == "de" and report["report_status"] == "final"
    assert {s["kind"] for s in report["statements"]} == {"finding", "probable_cause"}
    assert all(s["language"] == "de" for s in report["statements"])
    assert recommendation["native_id"] == "3X123-26/07/2026" and recommendation["responses"][0]["status"] == "offen"
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        raise AssertionError("link-only sources never fetch")

    source = h.source("bea-reports")
    page = EngineeringSafetyAdapter(source, transport=transport).fetch_page({"operation": "records"}, cursor=None)
    (bea,) = [r["engineering_safety_record"] for r in page.records]
    assert not calls and bea["access"] == "link-only" and bea["language"] == "fr"
    assert "statements" not in bea and bea["url"].startswith("https://bea.aero/")


# ------------------------------------------------------------------ declarations and adapter


@pytest.mark.parametrize(("change", "code"), [
    (lambda d: d.update(selection=[]), "unbounded_source"),
    (lambda d: d.update(selection=[{"document_number": str(i).zfill(4) + "-00001"} for i in range(51)]),
     "unbounded_source"),
    (lambda d: d.update(selection=[{"document_number": "2026-4512"}]), "invalid_mapping"),
    (lambda d: d.update(selection=[{"ad_number": "2026-0123"}]), "invalid_mapping"),
    (lambda d: d.update(provider="faa-drs"), "invalid_mapping"),
])
def test_declarations_are_bounded_and_validated(change, code):
    source = h.source("faa-airworthiness-directives")
    change(source["engineering_safety"])
    with pytest.raises(SourcePackError) as refused:
        declaration(source)
    assert refused.value.code == code


def test_link_only_entries_must_link_to_the_provider_and_files_to_the_endpoint_host():
    bea = h.source("bea-reports")
    bea["engineering_safety"]["entries"][0]["url"] = "https://elsewhere.example/report"
    with pytest.raises(SourcePackError):
        declaration(bea)
    phmsa = h.source("phmsa-hazardous-liquid-incidents")
    phmsa["engineering_safety"]["documents"][0]["url"] = "https://elsewhere.example/file.txt"
    with pytest.raises(SourcePackError):
        declaration(phmsa)
    phmsa = h.source("phmsa-hazardous-liquid-incidents")
    phmsa["engineering_safety"]["window"] = {"year_from": 2000, "year_to": 2026}
    with pytest.raises(SourcePackError):
        declaration(phmsa)


def test_adapter_refuses_ad_hoc_parameters_foreign_cursors_and_other_hosts():
    source = h.source("csb-investigations")
    adapter = EngineeringSafetyAdapter(source, transport=fixture_transport(h.pages(source["source_id"])))
    with pytest.raises(SourcePackError):
        adapter.fetch_page({"operation": "records", "parameters": {"q": "x"}}, cursor=None)
    with pytest.raises(SourcePackError) as drift:
        adapter.fetch_page({"operation": "records"}, cursor=json.dumps({"index": 0, "scope": "other"}))
    assert drift.value.code == "cursor_drift"

    def redirected(**kwargs):
        return {"status": 200, "content": b"<h1>x</h1>", "final_url": "https://evil.example/"}

    with pytest.raises(SourcePackError) as moved:
        EngineeringSafetyAdapter(source, transport=redirected).fetch_page({"operation": "records"}, cursor=None)
    assert moved.value.code == "network_policy"
    native = copy.deepcopy(h.pages(source["source_id"]))
    native[0]["status"] = 503
    with pytest.raises(SourcePackError) as unavailable:
        EngineeringSafetyAdapter(source, transport=fixture_transport(native)).fetch_page(
            {"operation": "records"}, cursor=None)
    assert unavailable.value.code == "source_unavailable"


@pytest.mark.parametrize(("text", "expected"), [
    ("April 8, 2026", "2026-04-08"), ("08/04/2026", "2026-04-08"), ("2026-04-08T00:00:00Z", "2026-04-08"),
    ("20260408", "2026-04-08"), ("12.06.2026", "2026-06-12"), ("8 April 2026", "2026-04-08"), ("", None),
    ("31/02/2026", None), ("soon", None),
])
def test_dates_become_iso_or_absent(text, expected):
    assert parse_date(text) == expected
