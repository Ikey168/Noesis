"""Trade-flow records: keys, appended vintages, as-of lookup, reporter/mirror separation, concordances (#2537)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft7Validator

from src.kb.trade_flows import (
    CONTRACT,
    TradeComparability,
    TradeError,
    TradeFlowStore,
    comparability_basis,
    forbidden_keys,
)
from tests.unit import trade_harness as h


def comtrade_series(store, **filters):
    return store.find_series(h.NS, provider="un-comtrade", **filters)


def test_the_contract_is_a_valid_schema_that_refuses_estimated_or_reconciled_values():
    schema = json.loads((h.ROOT / f"contracts/schemas/jsonschema/{CONTRACT}.json").read_text())
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    assert not list(validator.iter_errors({"contract": CONTRACT, "record_type": "series", "role": "mirror"}))
    assert list(validator.iter_errors({"contract": CONTRACT, "record_type": "observation", "reconciled_value": "1"}))


def test_observation_keys_keep_reporter_and_mirror_apart_with_classification_and_valuation():
    conn = h.connection()
    h.apply(conn, "comtrade", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = TradeFlowStore(conn)
    reporter = comtrade_series(store, reporter_codes=["276"], partner_codes=["156"], product_codes=["854143"],
                               flow_direction="export")
    mirror = comtrade_series(store, reporter_codes=["156"], partner_codes=["276"], flow_direction="import")
    assert len(reporter) == 1 and reporter[0]["role"] == "reporter"
    assert {s["role"] for s in mirror} == {"mirror"} and len(mirror) == 3
    series = reporter[0]
    assert series["pair"] == {"reporter": "276", "partner": "156"}
    assert series["classification"] == {"scheme": "HS", "vintage": "HS2022", "code": "H6"}
    assert series["valuation"] == {"basis": "FOB", "source": "reported"}
    assert series["reporter"]["iso3"] == "DEU" and series["partner"]["label"] == "China"
    assert series["dimensions"] == {"customsCode": "C00", "motCode": "0", "mosCode": "0", "partner2Code": "0"}
    # The mirror's 2097 figures were reported in HS2017: a separate series with its own classification vintage.
    vintages = {s["classification"]["vintage"] for s in mirror}
    assert vintages == {"HS2017", "HS2022"}
    assert all(s["valuation"]["basis"] == "CIF" for s in mirror)
    values = store.values(h.NS, series["series_id"])
    observation = values["observations"][0]
    assert observation["value"] == "5000000" and observation["fob_value"] == "5000000"
    assert observation["flags"]["isReported"] is True and observation["quantities"][0]["unit"] == "kg"
    assert values["vintage"]["source_revision"]["release_basis"] == "comtrade_last_released"


def test_each_release_is_an_appended_vintage_and_as_of_selects_the_one_released_by_then():
    conn = h.connection()
    h.apply(conn, "comtrade", retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert {r["status"] for r in h.apply(conn, "comtrade", retrieved_at_ms=h.SECOND_RETRIEVAL)} == {"unchanged"}
    h.apply(conn, "comtrade", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    store = TradeFlowStore(conn)
    (series,) = comtrade_series(store, reporter_codes=["276"], product_codes=["854143"], flow_direction="export")
    vintages = store.vintage_rows(h.NS, series["series_id"])
    assert len(vintages) == 2 and vintages[1]["revision_of"] == vintages[0]["vintage_id"]
    assert vintages[1]["values_changed"] is True and vintages[1]["release_at"].startswith("2099-09-01")
    first = {o["period"]: o["value"] for o in store.observations(h.NS, vintages[0]["vintage_id"])}
    second = {o["period"]: o["value"] for o in store.observations(h.NS, vintages[1]["vintage_id"])}
    assert first["2098"] == "5200000" and second["2098"] == "5250000"  # the earlier vintage is never overwritten
    as_of_june = store.values(h.NS, series["series_id"], as_of_ms=h.day_ms("2099-06-01"))
    assert as_of_june["vintage"]["vintage_id"] == vintages[0]["vintage_id"]
    assert store.values(h.NS, series["series_id"], as_of_ms=h.day_ms("2099-10-01"))["vintage"]["vintage_id"] == (
        vintages[1]["vintage_id"]
    )
    early = store.values(h.NS, series["series_id"], as_of_ms=h.day_ms("2099-01-01"))
    assert early["status"] == "unavailable" and early["reason"] == "no_release_by_as_of"
    # An unchanged series in the re-release is a new vintage with values_changed False, never a rewrite.
    (unchanged,) = comtrade_series(store, reporter_codes=["276"], product_codes=["293090"], flow_direction="import")
    rows = store.vintage_rows(h.NS, unchanged["series_id"])
    assert len(rows) == 2 and rows[1]["values_changed"] is False


def test_a_changed_publication_without_a_new_release_clock_is_refused():
    conn = h.connection()
    h.apply(conn, "comext", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = TradeFlowStore(conn)
    (records,) = h.fetch("comext")[:1]
    header = dict(records[0]["trade_release"])
    header["file_sha256"] = "0" * 64  # another file with the same release clock
    items = [dict(r["trade_item"]) for r in records]
    items[0] = {**items[0], "observations": [{**items[0]["observations"][0], "value": "1", "value_text": "1"}]}
    with pytest.raises(TradeError) as caught:
        store.apply_release(h.NS, header, items, run_id="r", source_id="eurostat-comext-trade-flows")
    assert caught.value.code == "vintage_conflict"


def test_confidential_cells_carry_no_value_and_forbidden_keys_are_refused():
    conn = h.connection()
    h.apply(conn, "comext", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = TradeFlowStore(conn)
    (series,) = store.find_series(h.NS, reporter_codes=["DE"], product_codes=["29309098"], flow_direction="import")
    feb = store.values(h.NS, series["series_id"])["observations"][1]
    assert feb["status"] == "confidential" and feb["value"] is None and feb["flags"]["status"] == "c"
    records = h.fetch("comext")[0]
    header = {**records[0]["trade_release"], "file_sha256": "1" * 64}
    bad = [{**dict(records[0]["trade_item"]), "imputed_value": "1"}]
    with pytest.raises(TradeError):
        store.apply_release(h.NS, {**header, "item_count": 1}, bad, run_id="r", source_id="x")
    leaked = dict(records[0]["trade_item"])
    leaked["observations"] = [{**leaked["observations"][0], "status": "confidential"}]
    with pytest.raises(TradeError):
        store.apply_release(h.NS, {**header, "item_count": 1}, [leaked], run_id="r", source_id="x")


def test_concordances_keep_mapping_types_as_published_and_revise_without_overwriting():
    conn = h.connection()
    h.apply(conn, "wits", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = TradeFlowStore(conn)
    tables = {t["label"]: t for t in store.concordances(h.NS)}
    hs = tables["WITS concordance HS 2022 (H6) to HS 2017 (H5)"]
    rows = {r["source_code"]: r for r in store.concordance_rows(h.NS, hs["concordance_id"])}
    assert rows["293090"]["mapping_type"] == "1:1" and rows["854143"]["mapping_type"] == "n:1"
    assert rows["854143"]["mapping_basis"] == "derived-from-published-pairs"
    assert rows["854143"]["weight_state"] == "not-published" and "weight" not in rows["854143"]
    sitc = store.map_code(h.NS, "854143", {"scheme": "HS", "vintage": "HS2022"}, {"scheme": "SITC", "vintage": "SITC4"})
    assert {t["code"] for t in sitc["targets"]} == {"77637", "77639"}
    assert {t["mapping_type"] for t in sitc["targets"]} == {"1:n"} and not any(t["exact"] for t in sitc["targets"])
    back = store.map_code(h.NS, "854140", {"scheme": "HS", "vintage": "HS2017"}, {"scheme": "HS", "vintage": "HS2022"})
    assert {t["code"] for t in back["targets"]} == {"854141", "854142", "854143", "854149"}
    assert {t["mapping_type"] for t in back["targets"]} == {"1:n"} and back["targets"][0]["direction"] == "reverse"
    assert store.labels(h.NS, "HS", "854140")[0]["vintage"] == "HS2017"
    # An operator-imported UNSD table: relationship as published, weights only when published; a change revises.
    table = {
        "label": "UNSD correlation HS 2022 to HS 2017 (operator import)",
        "source": {"scheme": "HS", "vintage": "HS2022"},
        "target": {"scheme": "HS", "vintage": "HS2017"},
        "citation": {"url": "https://unstats.un.org/unsd/classifications/Econ", "published_on": "2099-01-05",
                     "file_sha256": "a" * 64, "sheet": "HS2022-HS2017"},
        "rows": [{"source_code": "854143", "target_code": "854140", "mapping_type": "n:1", "row": 7}],
    }
    first = store.import_concordance(h.NS, table, principal_id="op", scopes=h.SCOPES)
    assert first["status"] == "applied" and first["concordances"] == 1
    assert store.import_concordance(h.NS, table, principal_id="op", scopes=h.SCOPES)["status"] == "unchanged"
    revised = {**table, "citation": {**table["citation"], "file_sha256": "b" * 64},
               "rows": table["rows"] + [{"source_code": "293090", "target_code": "293090", "mapping_type": "1:1",
                                         "weight": "1"}]}
    store.import_concordance(h.NS, revised, principal_id="op", scopes=h.SCOPES)
    unsd = next(t for t in store.concordances(h.NS) if t["provider"] == "unsd-classifications")
    history = store.concordance_history(h.NS, unsd["concordance_key"])
    assert [t["revision"] for t in history] == [1, 2] and history[0]["row_count"] == 1
    assert store.concordance_rows(h.NS, history[1]["concordance_id"], source_code="293090")[0]["weight"] == "1"
    with pytest.raises(TradeError):
        store.import_concordance(h.NS, {**table, "rows": [{"source_code": "1", "target_code": "2"}]},
                                 principal_id="op", scopes=h.SCOPES)
    with pytest.raises(TradeError) as caught:
        store.import_concordance(h.NS, table, principal_id="op", scopes=h.READ_ONLY)
    assert caught.value.code == "unauthorized"


def test_comparability_notes_are_reviewable_and_never_merge_series():
    conn = h.connection()
    h.apply(conn, "comtrade", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = TradeFlowStore(conn)
    (reporter,) = comtrade_series(store, reporter_codes=["276"], product_codes=["293090"], flow_direction="export")
    (mirror,) = comtrade_series(store, reporter_codes=["156"], product_codes=["293090"], flow_direction="import")
    basis = comparability_basis(reporter, mirror)
    assert [b["kind"] for b in basis] == ["different_valuation_basis"]
    notes = TradeComparability(conn)
    note = notes.record(h.NS, reporter["series_id"], mirror["series_id"], "different_valuation_basis",
                        "Germany reports exports FOB, China imports CIF", principal_id="alice", scopes=h.SCOPES)
    assert note["state"] == "proposed" and note["cited"][0]["source_revision"]["provider"] == "un-comtrade"
    accepted = notes.review(h.NS, note["note_id"], "accept", "documented", principal_id="bob", scopes=h.SCOPES)
    assert accepted["state"] == "accepted"
    assert notes.notes_for(h.NS, mirror["series_id"], reporter["series_id"])[0]["relation"] == (
        "different_valuation_basis"
    )
    notes.revert(h.NS, note["note_id"], "withdrawn", principal_id="bob", scopes=h.SCOPES)
    assert notes.notes_for(h.NS, reporter["series_id"], mirror["series_id"]) == []
    assert len(comtrade_series(store)) == 10  # nothing merged
    assert forbidden_keys(store.values(h.NS, reporter["series_id"])) == []
