"""Flows as of a release with reporter-versus-mirror asymmetries visible (#2550)."""

from __future__ import annotations

import json

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.sanctions_trade import SanctionsTrade
from src.kb.trade_flows import TradeError, forbidden_keys
from src.kb.trade_links import TradeLinks
from src.kb.trade_queries import TradeQueries
from tests.unit import trade_harness as h

HS2022 = {"scheme": "HS", "vintage": "HS2022"}
LEGAL = h.READ_ONLY | {"knowledge:legal:read"}


def result(answer, provider, direction):
    return next(r for r in answer["results"] if r["provider"] == provider and r["direction"] == direction)


def rows(answer, provider, direction):
    return {row["period"]: row for group in result(answer, provider, direction)["groups"] for row in group["rows"]}


def test_reporter_and_mirror_figures_stand_side_by_side_with_a_displayed_asymmetry():
    conn = h.connection()
    h.load_all(conn)
    answer = TradeQueries(conn).flows(h.NS, reporter="276", partner="156", product={"code": "293090", **HS2022},
                                      scopes=h.READ_ONLY)
    exports = rows(answer, "un-comtrade", "export")
    row = exports["2098"]
    (reporter,) = row["reporter_figures"]
    (mirror,) = row["mirror_figures"]
    assert reporter["value"] == "1350000" and reporter["valuation"]["basis"] == "FOB"
    assert mirror["value"] == "1400000" and mirror["valuation"]["basis"] == "CIF" and mirror["role"] == "mirror"
    assert mirror["flow"]["direction"] == "import" and mirror["reporter"]["code"] == "156"
    assert row["asymmetry"] == {
        "status": "displayed",
        "reporter_minus_mirror": "-50000",
        "unit": {"currency": "USD", "scale": "1"},
        "valuation": {"reporter": reporter["valuation"], "mirror": mirror["valuation"]},
        "note": row["asymmetry"]["note"],
    }
    assert reporter["release"]["release_at"].startswith("2099-05-15")
    assert mirror["release"]["release_at"].startswith("2099-06-20")
    group = result(answer, "un-comtrade", "export")["groups"][0]
    assert group["comparability"]["basis"][0]["kind"] == "different_valuation_basis"
    assert result(answer, "eurostat-comext", "export")["status"] == "none_reported"
    assert forbidden_keys(answer) == [] and "never reconciled" in answer["never"]


def test_as_of_selects_the_vintage_released_by_then_and_states_it_for_each_figure():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    queries = TradeQueries(conn)
    product = {"code": "854143", **HS2022}
    before = rows(queries.flows(h.NS, reporter="276", partner="156", product=product, scopes=h.READ_ONLY,
                                as_of_ms=h.day_ms("2099-07-01")), "un-comtrade", "export")["2098"]
    after = rows(queries.flows(h.NS, reporter="276", partner="156", product=product, scopes=h.READ_ONLY,
                               as_of_ms=h.day_ms("2099-10-01")), "un-comtrade", "export")["2098"]
    assert before["reporter_figures"][0]["value"] == "5200000"
    assert after["reporter_figures"][0]["value"] == "5250000"
    assert after["reporter_figures"][0]["release"]["revision_of"] == before["reporter_figures"][0]["release"][
        "vintage_id"]
    assert before["reporter_figures"][0]["release"]["selected_as_of"].startswith("2099-07-01")
    # The mirror's release is chosen by the same cutoff and cited separately.
    assert before["mirror_figures"][0]["release"]["release_at"].startswith("2099-06-20")
    early = queries.flows(h.NS, reporter="276", partner="156", product=product, scopes=h.READ_ONLY,
                          as_of_ms=h.day_ms("2099-06-01"))
    early_rows = rows(early, "un-comtrade", "export")
    assert early_rows["2098"]["mirror_figures"] == [] and early_rows["2098"]["mirror_figure_status"] == "none_reported"
    assert early_rows["2098"]["asymmetry"]["status"] == "not_shown"
    group = result(early, "un-comtrade", "export")["groups"][0]
    assert any(u["reason"] == "no_release_by_as_of" for u in group["unavailable"])


def test_a_product_across_classification_vintages_cites_the_concordance_and_flags_non_exact_mappings():
    conn = h.connection()
    h.load_all(conn)
    answer = TradeQueries(conn).flows(h.NS, reporter="276", partner="156", product={"code": "854143", **HS2022},
                                      scopes=h.READ_ONLY)
    exports = rows(answer, "un-comtrade", "export")
    old = exports["2097"]
    (mirror,) = old["mirror_figures"]
    assert mirror["product"]["code"] == "854140" and mirror["classification"]["vintage"] == "HS2017"
    match = mirror["product_match"]
    assert match["exact"] is False and match["mapping_type"] == "n:1" and "non-exact" in match["flag"]
    assert match["concordance"]["label"] == "WITS concordance HS 2022 (H6) to HS 2017 (H5)"
    assert old["non_exact_mapping"] is True
    assert exports["2098"]["mirror_figures"][0]["product_match"]["exact"] is True
    assert answer["product_resolution"][0]["method"] == "concordance"


def test_confidential_cells_and_intra_eu_mirrors_through_comext():
    conn = h.connection()
    h.load_all(conn)
    queries = TradeQueries(conn)
    answer = queries.flows(h.NS, reporter="DE", partner="FR", product={"code": "29309098", "scheme": "CN",
                                                                       "vintage": "CN2099"}, scopes=h.READ_ONLY)
    imports = rows(answer, "eurostat-comext", "import")
    feb = imports["2099-02"]
    assert feb["reporter_figures"][0]["status"] == "confidential" and feb["reporter_figures"][0]["value"] is None
    assert feb["mirror_figures"][0]["value"] == "138000"
    assert feb["asymmetry"]["status"] == "not_shown" and "confidential" in feb["asymmetry"]["reason"]
    exports = rows(answer, "eurostat-comext", "export")
    assert exports["2099-01"]["asymmetry"]["reporter_minus_mirror"] == "-20000"
    none = queries.flows(h.NS, reporter="DE", partner="US", scopes=h.READ_ONLY)
    assert none["status"] == "none_reported"
    assert all(r["status"] == "none_reported" and r["groups"] == [] for r in none["results"])
    with pytest.raises(TradeError):
        queries.flows(h.NS, reporter="DE", partner="FR", directions=["smuggled"], scopes=h.READ_ONLY)
    with pytest.raises(TradeError) as caught:
        queries.flows(h.NS, reporter="DE", partner="FR", scopes={"namespace:global:read"})
    assert caught.value.code == "unauthorized"


def test_flows_in_products_a_cited_measure_covers_and_none_reported_for_the_rest():
    conn = h.connection()
    h.load_all(conn)
    queries = TradeQueries(conn)
    absent = queries.sanctioned_flows(h.NS, reporter="276", partner="156", scopes=LEGAL)
    assert absent["status"] == "provider_absent"
    table = json.loads((h.ROOT / "tests/fixtures/sanctions/dual_use_cn_correlation.json").read_text())
    table["rows"].append({"control_code": "1C350", "product_code": "300215", "product_scheme": "HS6"})
    SanctionsTrade(conn).record_correlations(h.NS, table, principal_id="op", scopes=h.SCOPES)
    TradeLinks(conn).link_sanctions(h.NS, principal_id="op", scopes=h.SCOPES)
    answer = queries.sanctioned_flows(h.NS, reporter="276", partner="156", control_code="1C350", scopes=LEGAL)
    exports = rows(answer, "un-comtrade", "export")
    assert set(exports) == {"2097", "2098"}
    group = result(answer, "un-comtrade", "export")["groups"][0]
    assert {m["target"]["control_code"] for m in group["measures"]} == {"1C350"}
    assert all(f["product"]["code"] == "293090" for row in exports.values() for f in row["reporter_figures"])
    assert [n["product_code"] for n in answer["none_reported"]] == ["300215"]
    assert "lookup aid" in answer["notice"]
    with pytest.raises(TradeError):
        queries.sanctioned_flows(h.NS, reporter="276", partner="156", scopes=h.READ_ONLY)


def test_the_evidence_bundle_cites_every_figure_with_source_vintages_and_as_of():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    queries = TradeQueries(conn)
    answer = queries.flows(h.NS, reporter="DE", partner="FR", scopes=h.READ_ONLY, as_of_ms=h.day_ms("2099-12-31"))
    bundle = queries.export_bundle(answer, created_at_ms=1)
    verified = verify_bundle(bundle)
    # Offline verification: no errors; the bundle is partial (a confidential cell is an omission).
    assert verified.errors == [] and verified.status == "incomplete"
    figures = [o for o in bundle["objects"] if o["type"] == "evidence"]
    count = sum(len(row["reporter_figures"]) + len(row["mirror_figures"])
                for r in answer["results"] for g in r["groups"] for row in g["rows"])
    assert len(figures) == count and count > 0
    for figure in figures:
        payload = figure["payload"]
        assert payload["source"]["provider"] and payload["source"]["file_sha256"]
        assert payload["classification_vintage"]["vintage"] and payload["release_vintage"]["vintage_id"]
        assert payload["as_of"].startswith("2099-12-31")
    reasons = [o["reason"] for o in bundle["completeness"]["omissions"]]
    assert any("confidential" in r for r in reasons) and bundle["completeness"]["status"] == "partial"
