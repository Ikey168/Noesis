"""Housing record contract (#1937, U02): revisions, corrections, side-by-side sources and unknown dates."""

from __future__ import annotations

import json
import sys

import duckdb
import pytest

from src.ingestion.housing_sources import parse_publication, unit_label
from src.kb.housing import (
    CONTRACT,
    HousingError,
    HousingStore,
    forbidden_keys,
    published_value,
)
from tests.unit import housing_fixture_builder as fb

NS = "global"


def _statbb(published_on: str, *, revised: bool = False) -> dict:
    document = fb.statbb_document("permits")
    publication = parse_publication(
        "statbb-building-csv",
        fb.statbb_csv("permits", revised=revised).encode(),
        document=document,
        headers={"Last-Modified": fb.last_modified(published_on)},
    )
    header = {k: publication[k] for k in publication if k != "items"} | {
        "document": document,
        "evidence_origin": "fixture",
    }
    return header, publication["items"]


def _mietspiegel(edition: str = "2099", csv: str | None = None) -> tuple[dict, list]:
    document = fb.mietspiegel_document(edition)
    publication = parse_publication(
        "rent-index-table-csv",
        (csv or fb.mietspiegel_csv(edition)).encode(),
        document=document,
    )
    header = {k: publication[k] for k in publication if k != "items"} | {
        "document": document,
        "evidence_origin": "fixture",
    }
    return header, publication["items"]


@pytest.fixture()
def store():
    clock = iter(range(1_000, 10_000_000))
    return HousingStore(duckdb.connect(":memory:"), now=lambda: next(clock))


def test_unchanged_reingestion_adds_nothing_whatever_the_run(store):
    header, items = _mietspiegel()
    first = store.apply_publication(
        NS, header, items, source_id="berlin-mietspiegel", run_id="run-1"
    )
    again = store.apply_publication(
        NS, header, items, source_id="berlin-mietspiegel", run_id="run-2"
    )
    assert first["states"] == {"recorded": 6} and again["states"] == {"unchanged": 6}
    assert first["source_revision_id"] == again["source_revision_id"]
    assert (
        store.conn.execute("SELECT count(*) FROM housing_source_revisions").fetchone()[
            0
        ]
        == 1
    )
    cells = store.records("rent_index_cell", NS)
    assert {c["contract"] for c in cells} == {CONTRACT}
    unpublished = next(c for c in cells if c["cell_key"] == "B3")
    assert unpublished["ranges"]["middle"] == {
        "value_text": "-",
        "value": None,
        "published": False,
        "sign": "-",
        "sign_meaning": "no value published for this cell",
    }
    assert (
        next(c for c in cells if c["cell_key"] == "B1")["ranges"]["middle"]["value"]
        == "8.20"
    )


def test_a_changed_reading_is_a_correction_and_a_reversion_is_recorded_again(store):
    header, items = _mietspiegel()
    store.apply_publication(NS, header, items, source_id="berlin-mietspiegel")
    changed_csv = fb.mietspiegel_csv().replace("6,90;8,20;10,50", "6,90;8,25;10,50")
    header2, items2 = _mietspiegel(csv=changed_csv)
    assert store.apply_publication(NS, header2, items2, source_id="berlin-mietspiegel")[
        "states"
    ] == {"unchanged": 5, "corrected": 1}
    store.apply_publication(NS, header, items, source_id="berlin-mietspiegel")
    history = store.records("rent_index_cell", NS, current_only=False, cell_key="B1")
    assert [h["revision_no"] for h in history] == [1, 2, 3]
    assert [h["change"] for h in history] == ["initial", "correction", "correction"]
    assert (
        history[1]["supersedes"] == history[0]["record_id"]
        and history[2]["supersedes"] == history[1]["record_id"]
    )
    assert history[2]["record_id"] != history[0]["record_id"]
    (current,) = store.records("rent_index_cell", NS, cell_key="B1")
    assert (
        current["record_id"] == history[2]["record_id"]
        and current["ranges"]["middle"]["value"] == "8.20"
    )
    assert store.record(NS, history[0]["record_id"])["current"] is False


def test_editions_and_vintages_stay_side_by_side_in_any_arrival_order(store):
    store.apply_publication(NS, *_mietspiegel("2101"), source_id="berlin-mietspiegel")
    store.apply_publication(NS, *_mietspiegel("2099"), source_id="berlin-mietspiegel")
    assert [e["edition_id"] for e in store.editions(NS)] == [
        "berliner-mietspiegel-2099",
        "berliner-mietspiegel-2101",
    ]
    assert len(store.edition(NS, "berliner-mietspiegel-2099")["cells"]) == 5
    late, early = _statbb("2099-09-30", revised=True), _statbb("2099-03-20")
    store.apply_publication(NS, *late, source_id="statistik-bb-bautaetigkeit")
    store.apply_publication(NS, *early, source_id="statistik-bb-bautaetigkeit")
    mitte = store.statistics(
        NS, scheme="berlin-bezirk", code="1", measure="dwellings_permitted"
    )
    assert [(r["vintage"], r["value"]) for r in mitte] == [
        ("2099-03-20", "310"),
        ("2099-09-30", "322"),
    ]
    assert all(r["change"] == "initial" for r in mitte)
    land = store.statistics(NS, scheme="ags", code="11", measure="dwellings_permitted")
    assert land[0]["reporting_area"] == {
        "scheme": "ags",
        "code": "11",
        "label": "Berlin",
        "published_code": "00",
    }


def test_land_values_keep_valuation_date_unit_and_currency_and_unknown_dates_stay_unknown(
    store,
):
    declaration = {**fb.BORIS_DECLARATION}
    feature = {
        "provider": "Geoportal Berlin",
        "collection": "brw:bodenrichtwertzonen",
        "native_id": "z.1",
        "feature_id": "geofeature:x",
        "revision_id": "geofeature-rev:x",
        "geometry_id": "geom:x",
        "properties": {
            "wnum": "1099001",
            "brw": 5400,
            "stag": "2099-01-01",
            "nuta": "W",
            "gfz": "2.5",
        },
    }
    recorded = store.apply_feature(
        NS, declaration, feature, source_id="boris", run_id="r1"
    )
    undated = store.apply_feature(
        NS,
        declaration,
        {**feature, "properties": {"wnum": "1099001", "brw": "5400.50", "nuta": "W"}},
        source_id="boris",
        run_id="r2",
    )
    other_source = store.apply_feature(
        NS, declaration, feature, source_id="boris-mirror", run_id="r3"
    )
    assert {recorded["state"], undated["state"], other_source["state"]} == {"recorded"}
    rows = store.land_value_history(NS, zone_id="1099001")
    assert [(r["valuation_date"], r["source_id"]) for r in rows] == [
        (None, "boris"),
        ("2099-01-01", "boris"),
        ("2099-01-01", "boris-mirror"),
    ]
    dated = rows[1]
    assert (dated["value_text"], dated["value"], dated["unit"], dated["currency"]) == (
        "5400",
        "5400",
        "EUR/m²",
        "EUR",
    )
    assert (
        dated["qualifiers"] == {"floor_area_ratio": "2.5"} and dated["use_type"] == "W"
    )
    assert (
        rows[0]["valuation_date_basis"].startswith("unknown")
        and rows[0]["value_text"] == "5400.50"
        and rows[0]["value"] == "5400.50"
    )
    assert forbidden_keys(rows) == []
    with pytest.raises(HousingError) as missing:
        store.apply_feature(
            NS,
            declaration,
            {**feature, "properties": {"brw": 1}},
            source_id="boris",
            run_id="r4",
        )
    assert missing.value.code == "attribute_missing"


def test_published_values_are_kept_exactly_and_units_are_labelled_without_pint(
    monkeypatch,
):
    assert published_value(5400) == {"value_text": "5400", "value": "5400"}
    assert published_value("1.234,50", "de") == {
        "value_text": "1.234,50",
        "value": "1234.50",
    }
    with pytest.raises(HousingError):
        published_value("fünf")
    monkeypatch.setitem(sys.modules, "pint", None)
    label = unit_label("EUR/m²")
    assert label["currency"] == "EUR" and label["label_method"].startswith(
        "declared unit table"
    )


def test_a_same_stamp_value_change_is_refused_but_metadata_corrections_are_recorded(
    store,
):
    from src.ingestion.connectors.dataset.store import ObservationStore

    def publication(csv: str, metadata: str) -> tuple[dict, list]:
        document = fb.genesis_document("31111-0004")
        parsed = parse_publication(
            "destatis-genesis-ffcsv",
            csv.encode(),
            document=document,
            metadata=metadata.encode(),
            url="https://www-genesis.destatis.de/x",
        )
        return (
            {k: parsed[k] for k in parsed if k != "items"}
            | {"document": document, "evidence_origin": "fixture"},
            parsed["items"],
        )

    base = publication(fb.genesis_csv("31111-0004"), fb.genesis_metadata("31111-0004"))
    assert store.apply_publication(NS, *base, source_id="genesis")["states"] == {
        "recorded": 2
    }
    assert store.apply_publication(NS, *base, source_id="genesis")["states"] == {
        "unchanged": 2
    }
    series_id = "destatis:31111-0004:BAUGEN:11"
    assert len(ObservationStore(store.conn).vintages(series_id)) == 1
    changed = publication(
        fb.genesis_csv("31111-0004", overrides={("11", "2098"): "1499"}),
        fb.genesis_metadata("31111-0004"),
    )
    with pytest.raises(HousingError) as refused:
        store.apply_publication(NS, *changed, source_id="genesis")
    assert refused.value.code == "vintage_conflict"
    relabelled = publication(
        fb.genesis_csv("31111-0004").replace(
            "Genehmigte Wohnungen", "Wohnungen genehmigt"
        ),
        fb.genesis_metadata("31111-0004"),
    )
    assert store.apply_publication(NS, *relabelled, source_id="genesis")["states"] == {
        "corrected": 2
    }
    older = publication(
        fb.genesis_csv("31111-0004", overrides={("11", "2098"): "1400"}),
        fb.genesis_metadata("31111-0004", updated="01.01.2099 08:00:00h"),
    )
    store.apply_publication(NS, *older, source_id="genesis")
    header = ObservationStore(store.conn).get_series(series_id)
    assert (
        header["metadata"]["published_on"] == "2099-04-10"
    )  # the newest stamp keeps the header
    values = store.indicator_values(NS, series_id, as_of_ms=4_075_000_000_000)
    assert (
        values["status"] == "selected"
        and values["vintage"]["published_on"] == "2099-01-01"
    )
    assert {o["period"]: o["value"] for o in values["observations"]} == {
        "2097": 1390.0,
        "2098": 1400.0,
    }
    assert (
        store.indicator_values(NS, series_id, as_of_ms=1)["status"]
        == "historical_vintage_unavailable"
    )


def test_published_signs_keep_their_meaning_per_table_kind():
    document = fb.statbb_document("completions")
    csv = fb.statbb_csv("completions").replace(
        "02;Friedrichshain-Kreuzberg;2098;150;9,6",
        "02;Friedrichshain-Kreuzberg;2098;-;.",
    )
    parsed = parse_publication(
        "statbb-building-csv",
        csv.encode(),
        document=document,
        headers={"Last-Modified": fb.last_modified("2099-05-10")},
    )
    kreuzberg = {
        i["measure"]: i for i in parsed["items"] if i["reporting_area"]["code"] == "002"
    }
    assert (
        kreuzberg["dwellings_completed"]["value"],
        kreuzberg["dwellings_completed"]["sign_meaning"],
    ) == ("0", "nothing (exactly zero) as published")
    assert kreuzberg["floor_area_completed"]["value"] is None
    assert (
        kreuzberg["floor_area_completed"]["sign_meaning"] == "unknown or confidential"
    )


def test_listing_survives_one_unreadable_row(store):
    store.apply_publication(NS, *_mietspiegel(), source_id="berlin-mietspiegel")
    store.conn.execute(
        "UPDATE housing_rent_index_cells SET content_json='{broken' WHERE cell_key='A1'"
    )
    cells = store.records("rent_index_cell", NS)
    assert len(cells) == 5 and sum(bool(c.get("invalid")) for c in cells) == 1
    with pytest.raises(HousingError):
        store.records("rent_index_cell", NS, no_such_column="x")


def test_every_record_type_validates_against_the_documented_and_registered_contract(
    store,
):
    from pathlib import Path

    from jsonschema import Draft7Validator

    from src.kb.housing import RECORD_TYPES, register_schemas

    root = Path(__file__).resolve().parents[3]
    schema = json.loads(
        (root / f"contracts/schemas/jsonschema/{CONTRACT}.json").read_text()
    )
    assert set(schema["properties"]["record_type"]["enum"]) == set(RECORD_TYPES)
    validator = Draft7Validator(schema)
    store.apply_publication(NS, *_mietspiegel(), source_id="berlin-mietspiegel")
    store.apply_publication(
        NS, *_statbb("2099-03-20"), source_id="statistik-bb-bautaetigkeit"
    )
    store.apply_feature(
        NS,
        fb.BORIS_DECLARATION,
        {
            "provider": "Geoportal Berlin",
            "collection": "brw:bodenrichtwertzonen",
            "native_id": "z.1",
            "feature_id": "geofeature:x",
            "revision_id": "geofeature-rev:x",
            "geometry_id": "geom:x",
            "properties": {"wnum": "1099001", "brw": 5400, "stag": "2099-01-01"},
        },
        source_id="boris",
        run_id="r1",
    )
    checked = 0
    for record_type in (
        "rent_index_edition",
        "rent_index_cell",
        "building_statistic",
        "land_value_revision",
    ):
        for record in store.records(record_type, NS):
            assert not list(validator.iter_errors(record)), record_type
            checked += 1
    assert checked == 1 + 5 + 6 + 1
    doc = (root / "docs/guides/geospatial-housing.md").read_text()
    assert CONTRACT in doc and "valuation date" in doc
    (registered,) = register_schemas(
        store.conn, principal_id="svc", scopes={"knowledge:schema:register"}
    )
    assert registered["name"] == "housing-record"
