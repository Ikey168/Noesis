"""Plan against outturn and vintage against vintage with citations and accounting-basis notes (#1977)."""

from __future__ import annotations

import pytest

from src.kb.public_finance import PublicFinanceError, forbidden_keys
from src.kb.public_finance_queries import PublicFinanceQueries
from tests.unit import public_finance_harness as h

GFS = "estat:gov_10a_main:DE:na_item=TE:sector=S13:unit=MIO_EUR"


@pytest.fixture()
def conn():
    conn = h.connection()
    h.load_budgets(conn)
    yield conn
    conn.close()


def test_plan_supplementary_and_outturn_vintages_side_by_side_with_cited_differences(
    conn,
):
    queries = PublicFinanceQueries(conn)
    grant = h.line_id(conn, "de-bund-haushalt", titel="68101")
    answer = queries.compare_budget_line(h.NS, grant, "2099", scopes=h.SCOPES)
    assert [(c["figure_kind"], c["plan_key"]) for c in answer["columns"]] == [
        ("plan", "haushaltsplan"),
        ("supplementary_plan", "nachtrag-1"),
        ("outturn", "outturn"),
    ]
    for column in answer["columns"]:
        cited = column["current"]["citation"]
        assert (
            cited["provider"] == "bundeshaushalt"
            and cited["provider_vintage"]
            and cited["file_sha256"]
        )
        assert column["current"]["accounting_basis"] == "cash"
    differences = {d["comparison"]: d for d in answer["differences"]}
    assert differences["haushaltsplan -> outturn"]["difference"] == "6350400.00"
    assert differences["nachtrag-1 -> outturn"]["difference"] == "-8649600.00"
    assert differences["nachtrag-1 -> outturn"]["status"] == "computed"
    (vintages,) = answer["vintage_comparison"]
    assert (
        vintages["initial_value"],
        vintages["latest_value"],
        vintages["revision"],
    ) == ("131200400.00", "131350400.00", "150000.00")
    assert [v["source_revision"]["document"] for v in vintages["vintages"]] == [
        "Ist (vorläufig)",
        "Haushaltsrechnung (Ist)",
    ]
    assert (
        answer["conflicts"] == []
        and answer["unknowns"] == []
        and forbidden_keys(answer) == []
    )


def test_unknown_amounts_stay_unknown_and_produce_no_difference(conn):
    revenue = h.line_id(conn, "de-bund-haushalt", titel="11901")
    answer = PublicFinanceQueries(conn).compare_budget_line(
        h.NS, revenue, "2099", scopes=h.SCOPES
    )
    assert {d["status"] for d in answer["differences"]} == {"not-computed"}
    assert "haushaltsplan: amount not published" in answer["unknowns"]


def test_commitments_and_payments_are_different_bases_until_a_cited_method_is_recorded(
    conn,
):
    queries = PublicFinanceQueries(conn)
    line = h.line_id(conn, "eu-budget-line", budget_line="99010201")
    answer = queries.compare_budget_line(h.NS, line, "2099", scopes=h.SCOPES)
    assert (
        answer["columns"] == []
        and "no plan figure acquired for this line and year" in answer["unknowns"]
    )
    (pair,) = answer["payments"]["commitment_payment_pairs"]
    assert pair["status"] == "different-bases" and pair["difference"] is None
    assert (
        pair["commitment"]["accounting_basis"] == "commitment"
        and pair["payment"]["accounting_basis"] == "payment"
    )
    with pytest.raises(PublicFinanceError):
        queries.record_reconciliation(
            h.NS,
            provider="eu-fts",
            from_basis="commitment",
            to_basis="payment",
            citation={"url": "http://x"},
            description="x",
            principal_id="a",
            scopes=h.SCOPES,
        )
    queries.record_reconciliation(
        h.NS,
        provider="eu-fts",
        from_basis="commitment",
        to_basis="payment",
        citation={
            "url": "https://ec.europa.eu/budget/fts-methodology",
            "locator": "section 3 (fictional)",
        },
        description="payments are made against the commitment position they reference",
        principal_id="a",
        scopes=h.SCOPES,
    )
    (pair,) = queries.compare_budget_line(h.NS, line, "2099", scopes=h.SCOPES)[
        "payments"
    ]["commitment_payment_pairs"]
    assert (
        pair["status"] == "computed-under-cited-method"
        and pair["difference"] == "-850000.00"
    )
    assert pair["method"]["citation"]["locator"] == "section 3 (fictional)"


def test_an_esa2010_series_is_shown_beside_cash_figures_and_never_netted(conn):
    h.apply(conn, "gfs", 0, h.GFS_APRIL)
    queries = PublicFinanceQueries(conn)
    grant = h.line_id(conn, "de-bund-haushalt", titel="68101")
    with pytest.raises(PublicFinanceError):
        queries.compare_budget_line(
            h.NS, grant, "2098", scopes=h.READ_ONLY, gfs_series_id=GFS
        )
    answer = queries.compare_budget_line(
        h.NS, grant, "2098", scopes=h.SCOPES, gfs_series_id=GFS
    )
    context = answer["esa2010_context"]
    assert (
        context["value"] == 1085.0
        and context["difference"] is None
        and "never netted" in context["note"]
    )


def test_conflicting_sources_for_the_same_line_and_date_are_flagged_not_merged(conn):
    item = h.source("bund")
    item["source_id"] = "bundeshaushalt-mirror"
    other = h.body("de_bund_2099_haushaltsrechnung.csv").replace(
        "131.350,4", "131.999,9"
    )
    h.apply(conn, "bund", 3, other, item=item)
    grant = h.line_id(conn, "de-bund-haushalt", titel="68101")
    answer = PublicFinanceQueries(conn).compare_budget_line(
        h.NS, grant, "2099", scopes=h.SCOPES
    )
    (conflict,) = [c for c in answer["conflicts"] if c["published_on"] == "2100-04-30"]
    assert {f["amount_text"] for f in conflict["figures"]} == {"131.350,4", "131.999,9"}
    assert (
        len([c for c in answer["columns"] if c["figure_kind"] == "outturn"]) == 2
    )  # both kept


def test_inspection_and_the_budget_dossier_cite_everything_and_list_unknowns(conn):
    h.load_findings(conn)
    queries = PublicFinanceQueries(conn)
    grant = h.line_id(conn, "de-bund-haushalt", titel="68101")
    inspected = queries.inspect_budget_line(h.NS, grant, scopes=h.SCOPES)
    assert inspected["fiscal_years"] == ["2099"] and len(inspected["findings"]) == 2
    dossier = queries.budget_dossier(h.NS, grant, scopes=h.SCOPES)
    assert (
        "no acquired source publishes beneficiary payments against this line"
        in dossier["unknowns"]
    )
    assert forbidden_keys(dossier) == []
    with pytest.raises(PublicFinanceError):
        queries.compare_budget_line(h.NS, grant, "2099", scopes={"knowledge:read"})


def test_a_changed_gfs_cube_without_a_new_update_time_is_refused_not_dropped(conn):
    h.apply(conn, "gfs", 0, h.GFS_APRIL)
    silently = h.body(h.GFS_APRIL).replace("1040.2", "1041.0")
    with pytest.raises(PublicFinanceError) as refused:
        h.apply(conn, "gfs", 0, silently)
    assert refused.value.code == "vintage_conflict"
    assert (
        conn.execute("SELECT count(*) FROM public_finance_gfs_vintages").fetchone()[0]
        == 1
    )
    assert h.apply(conn, "gfs", 0, h.GFS_APRIL)["status"] == "unchanged"
