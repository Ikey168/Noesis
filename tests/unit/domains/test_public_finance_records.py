"""Public-finance records: revisions, vintages, idempotency and distinct record kinds (#1926, #1933, #1941, #1948)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft7Validator

from src.kb.public_finance import PublicFinanceError, PublicFinanceStore, forbidden_keys
from tests.unit import public_finance_harness as h

SCHEMA = Draft7Validator(
    json.loads(
        (
            h.ROOT / "contracts/schemas/jsonschema/noesis-public-finance-record-v1.json"
        ).read_text()
    )
)


@pytest.fixture()
def conn():
    conn = h.connection()
    yield conn
    conn.close()


def _series(store, line_id, kind):
    keys = store.series_for_line(h.NS, line_id)
    return [
        s
        for s in (store.figure_series(h.NS, k) for k in keys)
        if s["revisions"][0]["figure_kind"] == kind
    ]


def test_plan_supplementary_and_outturn_figures_are_distinct_revisioned_records(conn):
    h.load_budgets(conn)
    store = PublicFinanceStore(conn)
    grant = h.line_id(conn, "de-bund-haushalt", titel="68101")
    line = store.line(h.NS, grant)
    assert (
        line["codes"] == {"einzelplan": "98", "kapitel": "9801", "titel": "68101"}
        and line["side"] == "expenditure"
    )
    (plan,) = _series(store, grant, "plan")
    (supplementary,) = _series(store, grant, "supplementary_plan")
    (outturn,) = _series(store, grant, "outturn")
    assert (
        plan["current"]["amount_text"] == "125.000"
        and plan["current"]["normalized"]["value"] == "125000000.00"
    )
    assert (
        supplementary["current"]["plan_key"] == "nachtrag-1"
        and supplementary["current"]["amount"] == "140000"
    )
    assert [r["amount_text"] for r in outturn["revisions"]] == [
        "131.200,4",
        "131.350,4",
    ]
    assert (
        outturn["revisions"][1]["previous_figure_id"]
        == outturn["revisions"][0]["figure_id"]
    )
    assert all(
        r["accounting_basis"] == "cash" and r["currency"] == "EUR"
        for r in outturn["revisions"]
    )
    for record in [line, plan["current"], *outturn["revisions"]]:
        assert list(SCHEMA.iter_errors(record)) == [] and forbidden_keys(record) == []
    plans = store.plans(h.NS, provider="bundeshaushalt")
    assert [(p["figure_kind"], p["plan_key"], p["label"]) for p in plans] == [
        ("supplementary_plan", "nachtrag-1", "1. Nachtragshaushalt (Soll)"),
        ("plan", "haushaltsplan", "Haushaltsplan (Soll)"),
    ]
    assert {r["identifier"] for r in plans[1]["references"]} == {
        "BGBl. 2098 I Nr. 999",
        "99/1001",
    }


def test_every_outturn_vintage_is_kept_even_when_a_later_one_repeats_the_figure(conn):
    h.load_budgets(conn)
    store = PublicFinanceStore(conn)
    study = h.line_id(conn, "de-bund-haushalt", titel="53201")
    (outturn,) = _series(store, study, "outturn")
    assert (
        len(outturn["revisions"]) == 1
    )  # the Haushaltsrechnung repeats 3.990: no new revision
    assert [v["source_revision"]["document"] for v in outturn["vintages"]] == [
        "Ist (vorläufig)",
        "Haushaltsrechnung (Ist)",
    ]
    revenue = h.line_id(conn, "de-bund-haushalt", titel="11901")
    (plan,) = _series(store, revenue, "plan")
    assert (
        plan["current"]["amount"] is None and plan["current"]["normalized"] is None
    )  # unknown stays unknown


def test_reacquisition_is_idempotent_whatever_the_fetch_time(conn):
    first = h.load_budgets(conn)
    again = [
        h.apply(conn, "bund", i, name, run_id=f"again:{i}", now=h.Clock())
        for i, name in enumerate(h.BUND_FILES)
    ]
    assert [r["status"] for r in again] == ["unchanged"] * 4
    assert {r["release_id"] for r in again} <= {r["release_id"] for r in first}
    counts = conn.execute("SELECT count(*) FROM public_finance_figures").fetchone()[0]
    assert h.apply(conn, "fts", 0, h.FTS, run_id="again:fts")["status"] == "unchanged"
    assert (
        conn.execute("SELECT count(*) FROM public_finance_figures").fetchone()[0]
        == counts
    )


def test_the_revision_in_force_follows_the_source_date_not_arrival(conn):
    h.apply(conn, "bund", 3, "de_bund_2099_haushaltsrechnung.csv")
    h.apply(
        conn, "bund", 2, "de_bund_2099_ist_vorlaeufig.csv"
    )  # the older vintage arrives late
    store = PublicFinanceStore(conn)
    grant = h.line_id(conn, "de-bund-haushalt", titel="68101")
    (outturn,) = _series(store, grant, "outturn")
    assert (
        outturn["current"]["amount_text"] == "131.350,4"
        and outturn["current"]["published_on"] == "2100-04-30"
    )
    assert [v["source_revision"]["published_on"] for v in outturn["vintages"]] == [
        "2100-01-20",
        "2100-04-30",
    ]


def test_a_reversion_to_earlier_figures_is_a_new_correction(conn):
    h.load_budgets(conn)
    later = (
        h.body("de_bund_2099_haushaltsrechnung.csv")
        .replace("30.04.2100", "15.09.2100")
        .replace("131.350,4", "131.200,4")
    )
    item = h.source("bund")
    item["public_finance"]["documents"][3]["label"] = "Haushaltsrechnung (berichtigt)"
    h.apply(conn, "bund", 3, later, item=item)
    store = PublicFinanceStore(conn)
    (outturn,) = _series(
        store, h.line_id(conn, "de-bund-haushalt", titel="68101"), "outturn"
    )
    assert [r["amount_text"] for r in outturn["revisions"]] == [
        "131.200,4",
        "131.350,4",
        "131.200,4",
    ]
    assert outturn["current"]["published_on"] == "2100-09-15"


def test_figures_published_on_the_same_date_that_differ_are_kept_and_flagged(conn):
    h.apply(conn, "bund", 3, "de_bund_2099_haushaltsrechnung.csv")
    other = h.body("de_bund_2099_haushaltsrechnung.csv").replace(
        "131.350,4", "131.999,9"
    )
    h.apply(conn, "bund", 3, other)
    store = PublicFinanceStore(conn)
    (outturn,) = _series(
        store, h.line_id(conn, "de-bund-haushalt", titel="68101"), "outturn"
    )
    (conflict,) = outturn["conflicts"]
    assert {f["amount_text"] for f in conflict["figures"]} == {"131.350,4", "131.999,9"}


def test_a_release_stating_one_line_twice_is_refused(conn):
    text = h.body("de_bund_2099_soll.csv")
    doubled = text + text.strip().splitlines()[-1] + "\n"
    with pytest.raises(PublicFinanceError, match="twice"):
        h.apply(conn, "bund", 0, doubled)
    assert (
        conn.execute("SELECT count(*) FROM public_finance_releases").fetchone()[0] == 0
    )


def test_berlin_lines_that_share_codes_in_two_bereiche_stay_two_lines(conn):
    h.apply(conn, "berlin", 0, h.BERLIN)
    store = PublicFinanceStore(conn)
    schools = store.lines(h.NS, scheme="de-be-haushalt", codes={"titel": "51901"})
    assert {
        (line["codes"]["bereich"], (line["district"] or {}).get("code"))
        for line in schools
    } == {("31", "001"), ("32", "002")}
    main = h.line_id(conn, "de-be-haushalt", bereich="30")
    assert store.line(h.NS, main)["district"] is None
    years = {
        store.figure(h.NS, r[0])["fiscal_year"]
        for r in conn.execute(
            "SELECT figure_id FROM public_finance_figures WHERE line_id=?", [main]
        ).fetchall()
    }
    assert years == {"2099", "2100"}


def test_payments_are_their_own_records_and_a_changed_row_is_a_revision(conn):
    h.apply(conn, "fts", 0, h.FTS)
    store = PublicFinanceStore(conn)
    payments = store.payments(h.NS)
    assert len(payments) == 4 and {p["record_type"] for p in payments} == {
        "beneficiary_payment"
    }
    assert {p["payment_kind"] for p in payments} == {"commitment", "payment"}
    assert all(list(SCHEMA.iter_errors(p)) == [] for p in payments)
    republished = h.body(h.FTS).replace('"480,500.00"', '"481,000.00"')
    h.apply(
        conn,
        "fts",
        0,
        republished,
        headers={"Last-Modified": "Fri, 30 Jul 2100 10:00:00 GMT"},
    )
    (current,) = [
        p for p in store.payments(h.NS) if p["payment_key"] == "FTS-2099-000002"
    ]
    history = store.payment_history(h.NS, current["series_key"])
    assert [p["amount"] for p in history] == ["480500.00", "481000.00"] and current[
        "revision_no"
    ] == 2
    assert history[1]["previous_payment_id"] == history[0]["payment_id"]
    beneficiaries = {b["beneficiary_key"]: b for b in store.beneficiaries(h.NS)}
    assert set(beneficiaries) == {
        "public-finance:beneficiary:eu-fts:vat:DE:DE999999999",
        "public-finance:beneficiary:eu-fts:vat:PT:PT999999990",
    }


def test_audit_findings_quote_passages_cite_lines_and_refuse_verdicts(conn):
    h.load_budgets(conn)
    applied = h.load_findings(conn)
    assert applied["revisions"] == 2 and h.load_findings(conn)["status"] == "unchanged"
    store = PublicFinanceStore(conn)
    grant = h.line_id(conn, "de-bund-haushalt", titel="68101")
    study = h.line_id(conn, "de-bund-haushalt", titel="53201")
    assert (
        len(store.findings(h.NS, line_id=grant)) == 2
    )  # the Titel cite and the Kapitel-level cite
    (kapitel_only,) = store.findings(h.NS, line_id=study)
    assert kapitel_only["cites_line"]["levels"] == ["einzelplan", "kapitel"]
    assert (
        list(SCHEMA.iter_errors(kapitel_only)) == []
        and forbidden_keys(kapitel_only) == []
    )
    sheet = json.loads(h.body("brh_bemerkungen_2100.json"))
    sheet["findings"][0]["verdict"] = "Verschwendung"
    with pytest.raises(PublicFinanceError) as refused:
        store.import_findings(h.NS, sheet, principal_id="auditor", scopes=h.SCOPES)
    assert refused.value.code == "verdict_refused"
    corrected = json.loads(h.body("brh_bemerkungen_2100.json"))
    corrected["findings"][0]["quote"] += " (berichtigt)"
    store.import_findings(h.NS, corrected, principal_id="auditor", scopes=h.SCOPES)
    revised = [
        f
        for f in store.findings(h.NS, line_id=grant)
        if f["passage"]["locator"] == "Nr. 42, Tz. 3"
    ]
    assert revised[0]["revision_no"] == 2 and revised[0]["previous_finding_id"]
    with pytest.raises(PublicFinanceError) as denied:
        store.import_findings(h.NS, corrected, principal_id="x", scopes=h.READ_ONLY)
    assert denied.value.code == "unauthorized"


def test_a_file_without_the_revenue_expenditure_marker_does_not_split_a_line(conn):
    h.apply(conn, "bund", 0, "de_bund_2099_soll.csv")
    bare = "\n".join(
        line.rsplit(";", 2)[0] + ";" + line.rsplit(";", 1)[1]
        if not line.startswith("#")
        else line
        for line in h.body("de_bund_2099_nachtrag1.csv").splitlines()
    )
    assert "Einnahme/Ausgabe" not in bare
    h.apply(conn, "bund", 1, bare + "\n")
    assert len(PublicFinanceStore(conn).lines(h.NS, scheme="de-bund-haushalt")) == 3
