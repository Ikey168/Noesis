"""Tax answers as of an edition, edition history, and public CbCR references through reviewed identity (#2889)."""

from __future__ import annotations

import pytest

from src.kb.ownership_identity import FOREIGN_KEY_PREFIXES, OwnershipIdentityService
from src.kb.tax_identity import TaxIdentity
from src.kb.tax_queries import TaxQueries
from src.kb.tax_records import TaxError, forbidden_paths, personal_data_paths
from tests.unit import tax_harness as h


@pytest.fixture(scope="module")
def loaded():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    return conn


def test_as_of_selects_the_edition_published_by_that_date_and_keeps_estimates_apart(loaded):
    queries = TaxQueries(loaded)
    early = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NLD", as_of="2025-01-01")
    late = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NLD", as_of="2025-12-31")
    for answer in (early, late):
        assert answer["status"] == "reported"
        assert {r["nature"] for r in answer["published_statistics"]} == {"published_statistic"}
        assert {r["nature"] for r in answer["model_estimates"]} == {"model_estimate"}
        assert all(r["observation"] is False and r["method"]["label"] and "not an observation" in r["presented_as"]
                   for r in answer["model_estimates"])
        assert all(v["status"] in {"estimated", "not_published"} for r in answer["model_estimates"]
                   for v in r["values"])
        assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
        assert answer["not_implemented"][0]["status"] == "not-implemented"
    eatr = lambda a: next(r for r in a["model_estimates"] if r["indicator"]["code"] == "EATR")  # noqa: E731
    assert eatr(early)["edition"].startswith("Corporate Tax Statistics 2024")
    assert {v["period"]: v["value"] for v in eatr(early)["values"]} == {"2091": "22.4", "2092": "22.1"}
    assert eatr(late)["edition"].startswith("Corporate Tax Statistics 2025")
    assert {v["period"]: v["value"] for v in eatr(late)["values"]} == {"2091": "22.4", "2092": "21.7", "2093": "21.8"}
    assert "5% real interest rate" in eatr(late)["method"]["description"]
    assert eatr(late)["citation"]["edition"] == eatr(late)["edition"]
    offshore = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="DEU",
                                                  concept="offshore_financial_wealth", as_of="2025-03-01")
    (row,) = offshore["model_estimates"]
    assert offshore["published_statistics"] == [] and row["edition"].startswith("Atlas of the Offshore World, 2024")
    # The removed CbCR series reads as removed by the source after the 2025 edition, with its earlier editions kept.
    removed = next(r for r in late["published_statistics"] if r["indicator"]["code"] == "EMP"
                   and r["counterpart"]["code"] == "FOR")
    assert removed["status"] == "removed_by_source" and removed["values"] == []
    before_any = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NLD", as_of="2024-01-01")
    assert before_any["status"] == "no_records"


def test_tax_justice_network_indices_are_not_implemented_and_disabled_sources_are_reported(loaded):
    queries = TaxQueries(loaded)
    cthi = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NLD",
                                              concept="corporate_tax_haven_index")
    assert cthi["status"] == "not-implemented" and cthi["provider"] == "tjn-cthi" and "non-commercial" in \
        cthi["reason"]
    fsi = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NLD", providers=["tjn-fsi"])
    assert fsi["status"] == "not-implemented"
    only_rates = queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NLD",
                                                    enabled_providers={"oecd-cts-rates"})
    assert {r["provider"] for r in only_rates["published_statistics"] + only_rates["model_estimates"]} == {
        "oecd-cts-rates"}
    assert "oecd-cts-cbcr" in only_rates["not_selected"]
    with pytest.raises(TaxError):
        queries.indicator_for_jurisdiction(h.NS, scopes=set(), jurisdiction="NLD")
    with pytest.raises(TaxError, match="alpha-3"):
        queries.indicator_for_jurisdiction(h.NS, scopes=h.READ_ONLY, jurisdiction="NL")


def test_series_history_lists_every_edition_with_changes_and_citations(loaded):
    queries = TaxQueries(loaded)
    series = h.series_by(loaded, provider="eutax-offshore-wealth", jurisdiction="DEU", indicator="OFW_USD")
    history = queries.series_history(h.NS, series["series_id"], scopes=h.READ_ONLY)
    assert [e["edition"][:33] for e in history["editions"]] == ["Atlas of the Offshore World, 2024",
                                                                 "Atlas of the Offshore World, 2025"]
    assert len(history["editions"]) == 2 and "not an observation" in history["presented_as"]
    second = history["editions"][1]
    assert second["changes"]["new_periods"] == ["2093"]
    assert second["changes"]["revised"][0]["period"] == "2092"
    assert second["changes"]["method_change"]["edition_before"].startswith("Atlas of the Offshore World, 2024")
    assert all(e["citation"]["edition"] == e["edition"] for e in history["editions"])
    bundle = TaxQueries.evidence_bundle(queries.indicator_for_jurisdiction(
        h.NS, scopes=h.READ_ONLY, jurisdiction="DEU", as_of="2025-12-31"))
    estimates = next(s for s in bundle["sections"] if s["id"] == "model_estimates")["assertions"]
    assert estimates and all("[model estimate; method:" in a["text"] for a in estimates)
    assert all(a["dependencies"][0]["revision"] and a["dependencies"][0]["as_of"]
               for s in bundle["sections"] for a in s["assertions"])


def test_public_cbcr_references_reach_ownership_entities_only_through_reviewed_decisions():
    conn = h.connection()
    h.record_references(conn)
    identity = TaxIdentity(conn)
    absent = identity.propose_company_matches(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst",
                                              scopes=h.SCOPES)
    assert absent["status"] == "ownership_absent" and len(absent["unmatched"]) == 3
    h.ownership(conn)
    clusters_before = OwnershipIdentityService(conn).clusters(h.OWN_NS)
    proposed = identity.propose_company_matches(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst",
                                                scopes=h.SCOPES)
    candidates = proposed["candidates"]
    assert candidates and {c["state"] for c in candidates} == {"proposed"}  # nothing auto-accepted
    by_ref: dict[str, set[str]] = {}
    for c in candidates:
        by_ref.setdefault(c["reference_key"], set()).add(c["method"])
    holdings = "tax-cbcr:report:exampla-holdings-plc:2025-01-01_2025-12-31"
    intermediate = "tax-cbcr:report:exampla-intermediate-b-v:2025-01-01_2025-12-31"
    assert "exact-identifier" in by_ref[holdings] and by_ref[intermediate] == {"name-jurisdiction"}
    assert not any("northwind" in key for key in by_ref)
    queries = TaxQueries(conn)
    nothing = queries.company_reports_for_entity(h.NS, scopes=h.SCOPES, ownership_key=h.HOLDINGS_LEI)
    assert nothing["status"] == "no_accepted_match" and nothing["pending_candidates"]
    for view in candidates:
        if view["method"] == "exact-identifier":
            identity.review(h.NS, view["candidate_id"], "accept", "the report states the LEI",
                            principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    answer = queries.company_reports_for_entity(h.NS, scopes=h.SCOPES, ownership_key=h.HOLDINGS_LEI)
    (report,) = answer["reports"]
    assert report["company_name_as_published"] == "Exampla Holdings plc" and report["status"] == "reference-only"
    assert report["identity"]["method"] == "exact-identifier" and report["identity"]["reviewer"] == "reviewer"
    unmatched = {u["reference_key"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    assert holdings not in unmatched and intermediate in unmatched
    accepted = next(c for c in identity.candidates(h.NS, scopes=h.SCOPES) if c["state"] == "accepted")
    reverted = identity.revert(h.NS, accepted["candidate_id"], "LEI re-checked", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted"
    assert queries.company_reports_for_entity(h.NS, scopes=h.SCOPES, ownership_key=h.HOLDINGS_LEI)["reports"] == []
    assert "tax-cbcr:" in FOREIGN_KEY_PREFIXES
    assert OwnershipIdentityService(conn).clusters(h.OWN_NS) == clusters_before
