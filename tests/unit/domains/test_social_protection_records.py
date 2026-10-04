"""SS02 (#2747): social protection series records with release vintages, definitions, flags and write-time refusals."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion.social_protection_sources import series_key
from src.kb.social_protection_records import (
    CONTRACT,
    SocialProtectionError,
    check_item,
    cutoff_ms,
    schema_definitions,
)
from src.kb.social_protection_store import SocialProtectionStore
from tests.unit import social_protection_harness as h


def _item(name="esspros", index=0):
    return copy.deepcopy(h.fetch(name)[0][index]["social_item"])


def test_series_are_keyed_by_the_publishers_own_classification_and_published_fields():
    conn = h.connection()
    h.load_all(conn)
    store = SocialProtectionStore(conn)
    esspros = store.find_series(h.NS, provider="eurostat-esspros")
    assert len(esspros) == 10
    total = h.series(conn, "eurostat-esspros", "A.TOTALNOREROUTE.PC_GDP.DE")
    assert set(total["key"]) == {"provider", "dataset", "measure", "function", "scheme_type", "financing",
                                 "cash_or_kind", "basis", "sex", "unit", "area", "frequency"}
    assert total["key"]["function"] == {"scheme": "esspros-spfunc", "code": "TOTAL"}
    assert total["unit"] == {"code": "PC_GDP", "label": "Percentage of gross domestic product (GDP)"}
    socx = h.series(conn, "oecd-socx", "DEU.A.SOCX.PT_B1GQ.ES10._T.TP11")
    assert socx["function"]["scheme"] == "socx-branch" and socx["financing"]["code"] == "ES10"
    ilo = h.series(conn, "ilo-social-protection-coverage", "DEU.A.SDG_0131_RT.SEX_T.SOC_CONTIG_OLD")
    assert ilo["function"] == {"scheme": "ilo-contingency", "code": "SOC_CONTIG_OLD",
                               "label": "Persons above retirement age receiving a pension"}
    assert ilo["population"]["label"] == "persons above statutory pensionable age" and ilo["sex"]["code"] == "SEX_T"
    # Values also live in the Economics series storage (domain society); no new series store.
    assert total["economic_series"]["domain"] == "society"
    row = conn.execute("SELECT count(*) FROM economic_vintages WHERE domain='society' AND series_id=?",
                       [total["series_id"]]).fetchone()
    assert row[0] == 1


def test_definitions_record_the_manual_edition_methodology_and_ilo_notes_verbatim():
    conn = h.connection()
    h.load_all(conn)
    store = SocialProtectionStore(conn)
    answer = store.values(h.NS, h.series(conn, "eurostat-esspros", "A.OLD.TOTAL.MIO_EUR.DE")["series_id"])
    assert answer["definition"]["content"]["manual_edition"].startswith("ESSPROS manual and user guidelines")
    socx = store.values(h.NS, h.series(conn, "oecd-socx", "FRA.A.SOCX.PT_B1GQ.ES10._T.TP_ALL")["series_id"])
    assert socx["definition"]["content"]["methodology"].startswith("SOCX manual")
    ilo = store.values(h.NS, h.series(conn, "ilo-social-protection-coverage",
                                      "FRA.A.SDG_0131_RT.SEX_T.SOC_CONTIG_TOTAL")["series_id"])
    notes = ilo["definition"]["content"]["notes"]
    assert notes == {"NOTE_CLASSIF": ["C7:3020:Contingency as stated by ILO (fixture note)"],
                     "NOTE_INDICATOR": ["T3:9001:Fixture indicator note"],
                     "NOTE_SOURCE": ["R1:2383:Social Security Inquiry (fixture note)"]}
    assert ilo["observations"][0]["attributes"]["NOTE_SOURCE"] == notes["NOTE_SOURCE"][0]


def test_values_are_stored_as_published_with_flags_and_c_is_a_status_never_a_value():
    conn = h.connection()
    h.load_all(conn)
    store = SocialProtectionStore(conn)
    pc = store.values(h.NS, h.series(conn, "eurostat-esspros", "A.TOTALNOREROUTE.PC_GDP.FR")["series_id"])
    cells = {o["period"]: o for o in pc["observations"]}
    assert cells["2096"]["value_text"] == cells["2096"]["value"] == "44.3"
    assert cells["2096"]["flags"] == {"OBS_FLAG": "p"} and cells["2096"]["publication_status"] == "provisional"
    sick = store.values(h.NS, h.series(conn, "eurostat-esspros", "A.SICK.TOTAL.MIO_EUR.FR")["series_id"])
    confidential = {o["period"]: o for o in sick["observations"]}["2096"]
    assert confidential["status"] == "confidential" and confidential["value"] is None
    assert confidential["numeric_value"] is None and confidential["flags"] == {"OBS_FLAG": "c"}
    socx = store.values(h.NS, h.series(conn, "oecd-socx", "DEU.A.SOCX.PT_B1GQ.ES10._T.TP_ALL")["series_id"])
    estimate = {o["period"]: o for o in socx["observations"]}["2097"]
    assert estimate["publication_status"] == "estimated" and estimate["flags"] == {"OBS_STATUS": "E"}
    # A cell flagged confidential that carries a value is refused: 'c' never becomes a value.
    item = _item(index=1)
    cell = item["observations"][0]
    cell.update(status="confidential", flags={"OBS_FLAG": "c"})
    with pytest.raises(SocialProtectionError) as caught:
        check_item(item)
    assert caught.value.code == "invalid_record"


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda i: i.update(beneficiary_id="B-1"), "personal_data"),
    (lambda i: i["observations"][0]["attributes"].update(household_id="H-7"), "personal_data"),
    (lambda i: i.update(per_capita_value="123"), "derived_value"),
    (lambda i: i.update(cofog_equivalent="GF10"), "derived_value"),
    (lambda i: i["observations"][0].update(origin="filled"), "derived_value"),
    (lambda i: i["observations"][0].update(origin="forecast"), "derived_value"),
    (lambda i: i["observations"][0].update(origin="blended"), "derived_value"),
    (lambda i: i["observations"][0].update(value="999"), "recomputed_value"),
    (lambda i: i["function"].update(scheme="socx-branch"), "reclassified"),
    (lambda i: i["function"].update(scheme="cofog", code="GF10"), "reclassified"),
])
def test_person_household_derived_filled_blended_forecast_and_reclassified_values_are_refused(mutate, code):
    item = _item()
    check_item(item)
    mutate(item)
    with pytest.raises(SocialProtectionError) as caught:
        check_item(item)
    assert caught.value.code == code
    # The store refuses the whole release before writing anything.
    conn = h.connection()
    header = h.fetch("esspros")[0][0]["social_release"]
    items = [r["social_item"] for r in h.fetch("esspros")[0]]
    items[0] = item
    with pytest.raises(SocialProtectionError):
        SocialProtectionStore(conn).apply_release(h.NS, header, items, source_id="eurostat-esspros", run_id="r",
                                                  principal_id="svc", scopes=h.SCOPES,
                                                  retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert conn.execute("SELECT count(*) FROM social_protection_vintages").fetchone()[0] == 0


def test_revision_chain_appends_vintages_and_as_of_lookup_selects_the_release_in_force():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    store = SocialProtectionStore(conn)
    de = h.series(conn, "eurostat-esspros", "A.TOTALNOREROUTE.MIO_EUR.DE")
    vintages = store.vintage_rows(h.NS, de["series_id"])
    assert [v["sequence"] for v in vintages] == [1, 2] and vintages[1]["revision_of"] == vintages[0]["vintage_id"]
    assert vintages[0]["release_at"] == "2098-09-15T23:00:00Z" and vintages[1]["release_at"] == "2099-03-20T23:00:00Z"
    revised = vintages[1]["changes"]
    assert [r["period"] for r in revised["revised"]] == ["2096"] and revised["new_periods"] == ["2097"]
    assert revised["definition_change"]["manual_edition_after"].endswith("fixture edition B (synthetic label; verify "
                                                                         "the real edition)")
    before = store.values(h.NS, de["series_id"], as_of_ms=cutoff_ms("2099-01-01"))
    after = store.values(h.NS, de["series_id"], as_of_ms=cutoff_ms("2099-03-21"))
    early = store.values(h.NS, de["series_id"], as_of_ms=cutoff_ms("2098-09-01"))
    assert before["vintage"]["vintage_id"] == vintages[0]["vintage_id"]
    assert after["vintage"]["vintage_id"] == vintages[1]["vintage_id"]
    assert early["status"] == "unavailable" and early["reason"] == "no_release_by_as_of"
    assert {o["period"]: o["value_text"] for o in before["observations"]}["2096"] == "9120.5"
    assert {o["period"]: o["value_text"] for o in after["observations"]}["2096"] == "9125.0"
    # The earlier vintage is never overwritten.
    assert {o["period"]: o["value_text"] for o in store.observations(h.NS, vintages[0]["vintage_id"])}[
        "2096"] == "9120.5"
    # A series a complete later release no longer states is a removed_by_source vintage, never a deletion.
    fr = h.series(conn, "eurostat-esspros", "A.TOTAL.NR.FR")
    statuses = [v["status"] for v in store.vintage_rows(h.NS, fr["series_id"])]
    assert statuses == ["published", "removed"]
    assert store.values(h.NS, fr["series_id"])["status"] == "removed_by_source"
    assert store.values(h.NS, fr["series_id"], as_of_ms=cutoff_ms("2099-01-01"))["status"] == "available"


def test_estimates_keep_their_status_and_a_restating_report_edition_is_a_new_vintage():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    store = SocialProtectionStore(conn)
    socx = h.series(conn, "oecd-socx", "DEU.A.SOCX.PT_B1GQ.ES10._T.TP_ALL")
    first, second = store.vintage_rows(h.NS, socx["series_id"])
    assert first["release_basis"] == "declared_release" and second["release_label"].startswith("SOCX update 2099-04")
    change = next(r for r in second["changes"]["revised"] if r["period"] == "2097")
    assert change["before"]["publication_status"] == "estimated" and change["after"]["publication_status"] == "normal"
    assert {o["period"]: o["publication_status"] for o in store.observations(h.NS, second["vintage_id"])}[
        "2098"] == "estimated"
    ilo = h.series(conn, "ilo-social-protection-coverage", "DEU.A.SDG_0131_RT.SEX_T.SOC_CONTIG_TOTAL")
    first, second = store.vintage_rows(h.NS, ilo["series_id"])
    assert second["edition"] != first["edition"]
    assert second["changes"]["edition_restatement"]["restated_periods"] == ["2094"]


def test_replays_and_unchanged_republications_add_nothing():
    conn = h.connection()
    h.load_all(conn)
    count = conn.execute("SELECT count(*) FROM social_protection_vintages").fetchone()[0]
    assert {r["status"] for r in h.apply(conn, "esspros")} == {"unchanged"}
    assert conn.execute("SELECT count(*) FROM social_protection_vintages").fetchone()[0] == count


def test_record_schema_and_series_key_contract():
    schema = schema_definitions()[CONTRACT]
    assert schema["$id"] == CONTRACT
    item = _item("ilo")
    assert set(schema["required"]) <= set(item)
    key = series_key(item)
    assert "population" not in key and "observations" not in json.dumps(key)
