"""Payments by a company group per report version; production and reserves side by side (EX08 #2692, EX09 #2697)."""

from __future__ import annotations

from src.kb.extractives_identity import ExtractivesIdentity
from src.kb.extractives_queries import ExtractivesQueries
from src.kb.extractives_records import forbidden_keys
from tests.unit import extractives_harness as h


def _payments(report):
    return {p["record_key"]: p for p in report["payments"]}


def test_group_payments_per_report_version_side_by_side_with_discrepancies_and_revision_citations():
    conn = h.connection()
    h.reviewed(conn, revisions=True)
    queries = ExtractivesQueries(conn)
    answer = queries.payments_for_company(h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                          group=True, all_versions=True, include_unknowns=True)
    assert answer["status"] == "answered" and forbidden_keys(answer) == []
    reports = {r["report_key"]: r for r in answer["reports"]}
    assert set(reports) == {h.NL_REPORT, h.DE_REPORT}  # the Intermediate B.V. payments come in through the group
    nl = reports[h.NL_REPORT]
    assert [v["report_version"] for v in nl["versions"]] == ["1", "2"] and nl["version_used"]["report_version"] == "2"
    payment = _payments(nl)[h.CIT_PAYMENT]
    assert payment["government_reported"]["value"] == "1200000" and payment["company_reported"]["value"] == "1200000"
    assert payment["discrepancy_as_published"]["explanation"] == "resolved in the revised report"
    assert payment["citation"]["report_version"] == "2" and payment["citation"]["revision_id"]
    assert payment["citation"]["source"]["provider"] == "eiti" and payment["citation"]["as_of"]
    assert payment["matched_through"]["method"] == "exact-identifier"
    assert payment["matched_through"]["group_relation"] in {"group-member", "parent-chain"}
    # Per version: version 1 keeps the company-reported figure and the discrepancy it published.
    first = next(v for v in nl["per_version"] if v["report_version"] == "1")
    assert _payments(first)[h.CIT_PAYMENT]["company_reported"]["value"] == "1250000"
    assert _payments(first)[h.CIT_PAYMENT]["discrepancy_as_published"]["value"] == "-50000"
    # Currencies as reported, never converted or summed across reports.
    assert nl["currencies"] == ["EUR", "USD"] and answer["totals"].startswith("not computed")
    assert "total" not in nl
    # Exampla UK Limited (name only, not reviewed) is an unknown, never counted.
    assert [u["key"] for u in answer["unknowns"]] == [h.UK_COMPANY]
    assert all(p["company"]["record_key"] != h.UK_COMPANY for r in answer["reports"] for p in r["payments"])
    bundle = queries.evidence_bundle(answer)
    assertions = bundle["sections"][0]["assertions"]
    assert assertions and all(a["dependencies"][0]["revision"] and a["dependencies"][0]["as_of"] and a["citations"]
                              for a in assertions)
    assert bundle["exclusions"] and len(bundle["bibliography"]) >= 2


def test_as_of_answers_use_the_version_in_force_and_a_company_without_payments_is_not_a_clean_bill():
    conn = h.connection()
    h.reviewed(conn, revisions=True)
    queries = ExtractivesQueries(conn)
    earlier = queries.payments_for_company(h.NS, h.INT_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                           as_of="2023-06-30")
    (report,) = earlier["reports"]
    assert report["version_used"]["report_version"] == "1" and report["later_versions"] == ["2"]
    assert _payments(report)[h.CIT_PAYMENT]["company_reported"]["value"] == "1250000"
    before = queries.payments_for_company(h.NS, h.INT_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                                          as_of="2022-12-31")
    assert before["status"] == "no_payment_on_record" and "not a clean bill" in before["message"]
    trading = queries.payments_for_company(h.NS, "gleif:lei:213800EXAMPLATRADE88", ownership_namespace=h.OWN_NS,
                                           scopes=h.SCOPES)
    assert trading["status"] == "no_payment_on_record"


def test_country_payments_list_versions_streams_and_match_status_with_the_individual_withheld():
    conn = h.connection()
    h.reviewed(conn, revisions=True)
    answer = ExtractivesQueries(conn).payments_for_country(h.NS, "nl", scopes=h.SCOPES, as_of="2023-06-30")
    (report,) = answer["reports"]
    assert report["version_used"]["report_version"] == "1"
    payments = _payments(report)
    assert payments[h.NW_PAYMENT]["company"]["match_status"] == "unmatched"  # visible, as reported
    assert payments[h.CIT_PAYMENT]["company"]["match_status"] == "matched"
    individual = [p for p in payments.values() if p["company"]["natural_person"]]
    assert individual and individual[0]["company"]["name_as_published"].startswith("[natural person")
    streams = {s["gfs_code"]: s for s in report["revenue_streams"]}
    assert streams["1112E1"]["government_reported_total"]["value"] == "1500000"
    later = ExtractivesQueries(conn).payments_for_country(h.NS, "NL", scopes=h.SCOPES)
    assert h.NW_PAYMENT not in _payments(later["reports"][0])  # removed in version 2
    assert ExtractivesQueries(conn).payments_for_country(h.NS, "NO", scopes=h.SCOPES)["status"] == \
        "no_report_on_record"


def test_production_and_reserves_side_by_side_per_source_and_vintage_never_blended():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    queries = ExtractivesQueries(conn)
    answer = queries.production_and_reserves(h.NS, "copper", "CL", scopes=h.SCOPES, all_vintages=True)
    assert answer["status"] == "answered" and answer["blended"] is False
    by_source = {s["provider"]: s["series"] for s in answer["sources"]}
    assert set(by_source) == {"bgs-wms", "usgs-mcs"}
    usgs = {e["series"]["statistic"]: e for e in by_source["usgs-mcs"]}
    production = usgs["production"]
    assert [(v["period"], v["value_text"], v["revised"], v["estimated"]) for v in production["values"]] == [
        ("2023", "5250000r", True, False), ("2024", "5300000", False, True)]
    # 2022 is not restated by MCS2025: shown from the same source's earlier vintage, cited, never from BGS.
    (older,) = production["periods_not_restated"]
    assert older["period"] == "2022" and older["citation"]["release_version"] == "MCS2024"
    assert len(production["vintages"]) == 2
    assert production["citation"]["release_version"] == "MCS2025"
    # As of mid-2024 the MCS2024 vintage (2023 estimated) and the first BGS publication are used.
    mid = queries.production_and_reserves(h.NS, "copper", "CL", scopes=h.SCOPES, as_of="2024-06-30",
                                          statistic="production")
    mid_usgs = next(s for s in mid["sources"] if s["provider"] == "usgs-mcs")["series"][0]
    assert [(v["period"], v["estimated"]) for v in mid_usgs["values"]] == [("2022", False), ("2023", True)]
    mid_bgs = next(s for s in mid["sources"] if s["provider"] == "bgs-wms")["series"][0]
    assert mid_bgs["citation"]["release_version"] == "2018-2022"
    withheld = queries.production_and_reserves(h.NS, "lithium", "US", scopes=h.SCOPES, statistic="production")
    values = withheld["sources"][0]["series"][0]["values"]
    assert {(v["status"], v["value"]) for v in values} == {("withheld", None)}
    assert queries.production_and_reserves(h.NS, "cobalt", "CD", scopes=h.SCOPES)["status"] == "no_series_on_record"
    bundle = queries.evidence_bundle(answer)
    assert {a["dependencies"][0]["revision"] for a in bundle["sections"][0]["assertions"]} and bundle["bibliography"]


def test_an_hs_heading_reaches_commodities_only_through_accepted_concordance_matches():
    conn = h.connection()
    h.load_all(conn)
    identity = ExtractivesIdentity(conn)
    identity.import_concordance(h.NS, h.CONCORDANCE, principal_id="op", scopes=h.SCOPES)
    matches = identity.propose_commodities(h.NS, principal_id="analyst", scopes=h.SCOPES)["matches"]
    queries = ExtractivesQueries(conn)
    assert queries.production_and_reserves(h.NS, "hs:2709", "DE", scopes=h.SCOPES)["status"] == "no_series_on_record"
    oil = next(m for m in matches if m["subject_key"] == "commodity:crude-petroleum")
    identity.review(h.NS, oil["match_id"], "accept", "checked", principal_id="reviewer", scopes=h.SCOPES)
    answer = queries.production_and_reserves(h.NS, "hs:2709", "DE", scopes=h.SCOPES)
    assert answer["status"] == "answered" and answer["commodity"]["resolved_through"][0]["match_id"] == oil["match_id"]
