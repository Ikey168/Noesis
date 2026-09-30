"""Payments by a company group per report version and commodity figures side by side (#2692, #2697)."""

from __future__ import annotations

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.ingestion.extractives_sources import personal_keys
from src.kb.extractives_queries import ExtractivesQueries
from src.kb.extractives_records import ExtractivesError, forbidden_keys
from tests.unit import extractives_harness as h


@pytest.fixture(scope="module")
def env():
    conn = h.connection()
    h.reviewed(conn)
    return conn, ExtractivesQueries(conn)


def _figures(stream, side):
    return [(f["amount_text"], f["currency"]) for f in stream[side]]


def test_group_payments_per_report_revision_side_by_side_with_eiti_discrepancies(env):
    _, queries = env
    before = queries.payments_for_company(h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, group=True,
                                          scopes=h.SCOPES, as_of_ms=h.day_ms("2100-01-01"))
    assert before["status"] == "reported"
    members = {m["entity"] for m in before["group_members"]}
    assert len(members) > 1
    reports = {r["citation"]["fiscal_period"]["start"][:4]: r for r in before["reports"]}
    assert set(reports) == {"2097", "2098"}
    report = reports["2098"]
    assert report["citation"]["revision"] == 1 and report["citation"]["report"]["version"] == "1"
    streams = {s["company"]["name_as_reported"]: s for s in report["streams"]}
    intermediate = streams["Exampla Intermediate B.V."]
    assert _figures(intermediate, "government_reported") == [("790000.00", "PEN")]
    assert _figures(intermediate, "company_reported") == [("800000.00", "PEN")]
    assert intermediate["discrepancies"][0]["discrepancy_text"] == "-10000.00"
    assert intermediate["matched_through"]["method"] == "exact-identifier"
    uk = streams["EXAMPLA UK LIMITED"]
    assert uk["currencies"] == ["USD"] and uk["group_member"] != intermediate["group_member"]
    # Only the matched group's companies: the unmatched one and the redacted person never appear.
    assert "Andes Cobre S.A. (fixture)" not in streams and "[natural person - redacted]" not in streams
    after = queries.payments_for_company(h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, group=True,
                                         scopes=h.SCOPES, history=True)
    latest = {r["citation"]["fiscal_period"]["start"][:4]: r for r in after["reports"]}["2098"]
    assert latest["citation"]["revision"] == 2 and latest["citation"]["revision_of"] == report["citation"][
        "report_id"]
    assert len(latest["revision_history"]) == 2
    revised = {s["company"]["name_as_reported"]: s for s in latest["streams"]}["Exampla Intermediate B.V."]
    assert _figures(revised, "government_reported") == [("800000.00", "PEN")]
    assert revised["discrepancies"][0]["explanation"].startswith("corrected")


def test_currencies_are_never_converted_or_summed_across_reports(env):
    _, queries = env
    answer = queries.payments_for_company(h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, group=True,
                                          scopes=h.SCOPES)
    assert forbidden_keys(answer) == [] and personal_keys(answer) == []
    assert "total" not in str(sorted(answer)) and all("total" not in str(sorted(r)) for r in answer["reports"])
    assert "converting currencies or summing amounts across reports" in answer["exclusions"]
    for report in answer["reports"]:
        assert report["citation"]["source_revision"]["file_sha256"] and report["citation"]["retrieved_at"]


def test_a_company_without_matches_or_the_ownership_store_answers_explicitly(env):
    _, queries = env
    none = queries.payments_for_company(h.NS, "gleif:lei:213800EXAMPLATRADE88", ownership_namespace=h.OWN_NS,
                                        scopes=h.SCOPES)
    assert none["status"] == "no_payment_on_record" and "not a statement" in none["coverage"]
    other = h.connection()
    h.load_all(other)
    degraded = ExtractivesQueries(other).payments_for_company(h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS,
                                                               scopes=h.SCOPES)
    assert degraded["status"] == "ownership_unavailable" and degraded["message"]
    andes = next(c for c in queries.store.companies(h.NS) if c["name_as_reported"].startswith("Andes"))
    direct = queries.payments_for_company(h.NS, andes["key"], scopes=h.READ_ONLY)
    assert direct["status"] == "reported" and len(direct["reports"]) == 1
    with pytest.raises(ExtractivesError):
        queries.payments_for_company(h.NS, andes["key"], scopes={"namespace:global:read"})


def test_country_reports_list_government_revenues_and_company_lines_per_revision(env):
    _, queries = env
    answer = queries.payments_for_country(h.NS, "PER", scopes=h.READ_ONLY, as_of_ms=h.day_ms("2100-01-01"))
    assert answer["status"] == "reported" and len(answer["reports"]) == 2
    latest = max(answer["reports"], key=lambda r: r["citation"]["fiscal_period"]["start"])
    assert {s["revenue_stream"]["gfs_code"] for s in latest["government_revenues"]} == {"1141E1", "1112E1"}
    assert "[natural person - redacted]" in {s["company"]["name_as_reported"] for s in latest["companies"]}
    assert queries.payments_for_country(h.NS, "CHL", scopes=h.READ_ONLY)["status"] == "no_report_on_record"


def test_production_side_by_side_by_vintage_never_blended_with_marked_values(env):
    _, queries = env
    answer = queries.production(h.NS, commodity={"hs_code": "2603"}, country="PER", statistic="production",
                                scopes=h.READ_ONLY, history=True)
    assert answer["side_by_side"] and answer["never_blended"] is True
    assert set(answer["by_source"]) == {"usgs-mcs", "bgs-wms"}
    usgs = next(r for r in answer["results"] if r["provider"] == "usgs-mcs")
    bgs = next(r for r in answer["results"] if r["provider"] == "bgs-wms")
    assert usgs["unit"] != bgs["unit"] and usgs["series_id"] != bgs["series_id"]
    assert usgs["marked"]["estimated"] == ["2099"] and len(usgs["vintage_history"]) == 2
    assert usgs["vintage"]["publication"]["label"].startswith("Mineral Commodity Summaries 2100")
    assert all(v["citation"]["vintage_id"] == usgs["vintage"]["vintage_id"] for v in usgs["values"])
    early = queries.production(h.NS, commodity={"hs_code": "2603"}, country="PER", statistic="production",
                               scopes=h.READ_ONLY, as_of_ms=h.day_ms("2099-12-31"))
    early_usgs = next(r for r in early["results"] if r["provider"] == "usgs-mcs")
    assert {v["period"]: v["value"] for v in early_usgs["values"]} == {"2097": "2600", "2098": "2700"}
    withheld = queries.production(h.NS, commodity="Lithium", country="United States", statistic="production",
                                  scopes=h.READ_ONLY)
    assert withheld["results"][0]["marked"]["withheld"] == ["2098", "2099"]
    assert all(v["value"] is None for v in withheld["results"][0]["values"])
    exports = queries.production(h.NS, commodity={"hs_code": "2603"}, country="PER", statistic="exports",
                                 scopes=h.READ_ONLY)
    assert exports["results"][0]["marked"]["not_available"] == ["2098"]
    none = queries.production(h.NS, commodity={"hs_code": "7108"}, country="PER", scopes=h.READ_ONLY)
    assert none["status"] == "none_published"


def test_evidence_bundles_cite_every_item_with_source_revision_and_as_of(env):
    _, queries = env
    payments = queries.payments_for_company(h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, group=True,
                                            scopes=h.SCOPES, as_of_ms=h.day_ms("2100-11-01"))
    production = queries.production(h.NS, commodity={"hs_code": "2603"}, country="PER", scopes=h.READ_ONLY,
                                    as_of_ms=h.day_ms("2100-11-01"))
    for answer in (payments, production):
        bundle = queries.export_bundle(answer, created_at_ms=1)
        result = verify_bundle(bundle)
        # A bundle with omissions (a not-available BGS year) verifies as incomplete, never as complete.
        assert result.errors == [] and result.status in {"valid_with_external_references", "incomplete"}
        evidence = [o for o in bundle["objects"] if o["type"] == "evidence"]
        assert evidence
        for item in evidence:
            payload = item["payload"]
            assert payload["source"]["provider"] and payload["as_of"] and payload["retrieved_at"]
            assert payload.get("report_revision") or payload.get("vintage")
    assert verify_bundle(queries.export_bundle(production, created_at_ms=1)).status == "incomplete"
