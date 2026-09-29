"""Housing acquisition (#1921, #1945, #1955, #1966, #1976): source audit, WFS layers, tables and GENESIS series.

Every run goes through :class:`SourcePackRuntime` with the real WFS or housing
adapter and :class:`HousingProjector`; fixtures are authored and fictional.
"""

from __future__ import annotations

import copy
import json
from functools import partial

import pytest
from jsonschema import Draft7Validator

from src.ingestion.housing_sources import (
    FIXTURE_SECRET,
    NO_SURFACE,
    PROVIDER_CONTRACTS,
    HousingAdapter,
    fixture_request,
    fixture_transport,
    layer_declaration,
    parse_publication,
    HousingFormatError,
)
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    _digest,
    validate_source_pack,
)
from src.kb.geospatial_features import GeospatialFeatureStore
from tests.unit import housing_fixture_builder as fb
from tests.unit import housing_harness as h

NS = h.NS


@pytest.fixture(scope="module")
def world():
    env = h.Env().world()
    yield env
    env.conn.close()


# ---------------------------------------------------------------------- U01 audit


def test_every_audited_source_has_a_decision_a_shape_and_the_no_surface_statement():
    decisions = {k: v["access_decision"] for k, v in PROVIDER_CONTRACTS.items()}
    assert decisions == {
        "fis-broker-boris": "unverified-live",
        "boris-berlin": "not-implemented",
        "fis-broker-bplan": "unverified-live",
        "fis-broker-wohnlagen": "unverified-live",
        "berlin-mietspiegel": "unverified-live",
        "statistik-bb": "unverified-live",
        "destatis": "unverified-live",
        "ibb-wohnungsmarktbericht": "not-implemented",
        "daten-berlin": "not-implemented",
    }
    for contract in PROVIDER_CONTRACTS.values():
        for key in (
            "delivers",
            "delivery_shape",
            "primary_publisher",
            "access",
            "authentication",
            "identifiers",
            "crs",
            "date_semantics",
            "currency_unit",
            "cadence",
            "terms",
            "reason",
        ):
            assert key in contract, key
    assert "credential" in PROVIDER_CONTRACTS["destatis"]["reason"]
    assert PROVIDER_CONTRACTS["ibb-wohnungsmarktbericht"]["primary_publisher"] is False
    doc = (h.ROOT / "docs/roadmaps/geospatial-housing-source-audit.md").read_text()
    assert "No source offers an interpolable value surface" in doc and "_verify_" in doc
    assert "interpolable value surface" in NO_SURFACE


# ---------------------------------------------------------------------- manifest


def test_the_upgrade_keeps_earlier_sources_verbatim_and_pins_valid_fixtures():
    manifest = json.loads(h.UPGRADE.read_text())
    base = json.loads(fb.BASE.read_text())
    assert manifest["pack_id"] == "geospatial-berlin" and manifest["version"] == "1.3.0"
    assert manifest["sources"][: len(base["sources"])] == base["sources"]
    added = manifest["sources"][len(base["sources"]) :]
    assert [s["source_id"] for s in added] == list(h.WFS_SOURCES) + list(
        h.TABULAR_SOURCES
    )
    assert {s.get("connector", "wfs") for s in added} == {"wfs", "housing"}
    schema = json.loads(
        (h.ROOT / "contracts/schemas/jsonschema/noesis-source-pack-v1.json").read_text()
    )
    validated = validate_source_pack(manifest)
    assert not list(Draft7Validator(schema).iter_errors(validated))
    result = SourcePackConformance(h.ROOT).offline(manifest)
    assert result["valid"], [s for s in result["sources"] if not s["valid"]]
    for item in added:
        fixture = json.loads((h.ROOT / item["fixture"]["path"]).read_text())
        assert (
            fixture["scenarios"]
            and fixture["license"]
            and fixture["source_url"].startswith("https://")
        )
        assert "fictional" in fixture["note"]
    for item in validated["sources"]:
        if item["source_id"] in h.WFS_SOURCES:
            assert layer_declaration(item)["namespace"] == NS


def test_fixtures_and_manifest_are_pinned_and_in_sync(tmp_path, monkeypatch):
    before = h.UPGRADE.read_text()
    fixtures = {
        p: p.read_text()
        for p in (h.ROOT / "tests/fixtures/source_packs").glob(
            "geospatial-housing-*.json"
        )
    }
    monkeypatch.setattr(fb, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(fb, "ROOT", tmp_path)
    (tmp_path / "tests/fixtures/source_packs").mkdir(parents=True)
    monkeypatch.setattr(
        fb,
        "BASE",
        h.ROOT / "packs/climate-environment/source_packs/geospatial-berlin-1.2.0.json",
    )
    rebuilt = fb.build()
    for path, text in fixtures.items():
        assert (
            tmp_path / "tests/fixtures/source_packs" / path.name
        ).read_text() == text, path.name
    rebuilt_sources = {s["source_id"]: s for s in rebuilt["sources"]}
    for item in json.loads(before)["sources"]:
        assert rebuilt_sources[item["source_id"]]["fixture"] == item["fixture"], item[
            "source_id"
        ]


# ---------------------------------------------------------------------- U03 BORIS


def test_boris_zones_land_in_the_feature_store_and_project_land_value_revisions(world):
    store = GeospatialFeatureStore(world.conn)
    coverage = store.collection_coverage(
        NS, h.BORIS_COLLECTION, scopes={"knowledge:geospatial:read"}
    )
    assert coverage["active"] == 3 and coverage["completeness"] == "complete"
    rows = world.store().land_value_history(NS)
    assert [
        (r["zone_id"], r["valuation_date"], r["value_text"], r["unit"], r["currency"])
        for r in rows
    ] == [
        ("1099001", "2099-01-01", "5400", "EUR/m²", "EUR"),
        ("1099002", "2099-01-01", "6100", "EUR/m²", "EUR"),
        ("1099003", "2099-01-01", "4800", "EUR/m²", "EUR"),
    ]
    zone = rows[0]
    feature = store.feature(
        NS, zone["source_revision"]["feature_id"], scopes={"knowledge:geospatial:read"}
    )
    assert (
        zone["source_revision"]["feature_revision_id"]
        == feature["current"]["revision_id"]
    )
    assert (
        zone["geometry_id"] == feature["current"]["geometry_id"]
    )  # referenced, never copied
    assert zone["use_type"] == "W" and zone["qualifiers"]["floor_area_ratio"] == "2.5"
    assert zone["valuation_date_basis"] == "published Stichtag"


def test_replaying_the_same_publication_is_idempotent_and_a_new_stichtag_is_a_new_revision():
    env = h.Env().world()
    again = env.run(["berlin-boris-bodenrichtwerte"], "boris-again")
    (projection,) = [s["projection"] for s in again["sources"]]
    assert projection["housing_outcomes"] == []
    assert len(env.store().land_value_history(NS)) == 3
    receipt = env.boris_2100()
    assert receipt["status"] == "complete"
    history = env.store().land_value_history(NS, zone_id="1099001")
    assert [(r["valuation_date"], r["value"]) for r in history] == [
        ("2099-01-01", "5400"),
        ("2100-01-01", "5900"),
    ]
    # One zone feature, two feature revisions: each publication references the geometry it was read with.
    assert (
        history[0]["source_revision"]["feature_id"]
        == history[1]["source_revision"]["feature_id"]
    )
    assert (
        history[0]["source_revision"]["feature_revision_id"]
        != history[1]["source_revision"]["feature_revision_id"]
    )
    assert all(r["change"] == "initial" for r in history)
    unchanged = env.store().land_value_history(NS, zone_id="1099003")
    assert [r["value"] for r in unchanged] == [
        "4800",
        "4800",
    ]  # a repeated value on a new Stichtag is kept
    env.boris_2100("boris-2100-again")
    assert len(env.store().land_value_history(NS)) == 6
    env.conn.close()


def test_an_unmapped_attribute_change_is_a_new_feature_revision_but_no_housing_correction(
    world,
):
    features = fb.boris_features("2099-01-01")
    features[2] = copy.deepcopy(features[2])
    features[2]["properties"]["bemerkung"] = "redaktionelle Änderung (fiktiv)"
    adapter = world.wfs_adapter(
        "berlin-boris-bodenrichtwerte", features, "2099-03-03T08:00:00Z"
    )
    receipt = world.run(
        ["berlin-boris-bodenrichtwerte"],
        "boris-unmapped",
        adapters={"berlin-boris-bodenrichtwerte": adapter},
    )
    assert receipt["sources"][0]["projection"]["states"] == {
        "projected": 1,
        "unchanged": 2,
    }
    history = world.store().records(
        "land_value_revision", NS, current_only=False, zone_id="1099003"
    )
    assert [r["change"] for r in history] == ["initial"]


def test_a_feature_without_its_housing_attributes_keeps_its_geometry_and_is_reported(
    world,
):
    features = fb.boris_features("2099-01-01")
    features[1] = copy.deepcopy(features[1])
    features[1]["properties"].pop("brw")
    adapter = world.wfs_adapter(
        "berlin-boris-bodenrichtwerte", features, "2099-03-02T08:00:00Z"
    )
    receipt = world.run(
        ["berlin-boris-bodenrichtwerte"],
        "boris-missing-value",
        adapters={"berlin-boris-bodenrichtwerte": adapter},
    )
    outcomes = receipt["sources"][0]["projection"]["housing_outcomes"]
    assert [(o["outcome"], o["reason"]) for o in outcomes] == [
        ("not_projected", "attribute_missing")
    ]
    assert (
        len(world.store().land_value_history(NS, zone_id="1099002")) == 1
    )  # the earlier reading stays


# ---------------------------------------------------------------------- U04 plans and Wohnlagen


def test_a_changed_plan_stage_creates_a_new_stage_record_and_keeps_the_previous_one():
    env = h.Env().world()
    first = env.store().plan_history(NS, "1-99a")
    assert [(r["stage"], r["stage_label"], r["stage_date"]) for r in first] == [
        ("aufstellungsbeschluss", "Aufstellungsbeschluss", "2098-03-15")
    ]
    assert first[0]["references"] == [
        {
            "scheme": "berlin-amtsblatt",
            "identifier": "ABl. 2098 S. 777",
            "relation": "stage_decided_by",
            "attribute": "afs_abl",
            "stage": "aufstellungsbeschluss",
        }
    ]
    env.bplan_stage_change()
    history = env.store().plan_history(NS, "1-99a")
    assert [(r["stage"], r["stage_date"]) for r in history] == [
        ("aufstellungsbeschluss", "2098-03-15"),
        ("festgesetzt", "2099-06-15"),
    ]
    assert history[0]["record_id"] == first[0]["record_id"]
    env.bplan_stage_change("bplan-2-again")
    assert len(env.store().plan_history(NS, "1-99a")) == 2
    assert env.store().plan_history(NS, " 1-99A ") == env.store().plan_history(
        NS, "1-99a"
    )
    env.conn.close()


def test_an_unknown_stage_label_is_kept_as_published_and_marked_unrecognized(world):
    features = fb.bplan_features(1)
    features[0] = copy.deepcopy(features[0])
    features[0]["properties"]["status"] = "ruhend"
    adapter = world.wfs_adapter(
        "berlin-bebauungsplaene", features, "2099-03-05T08:00:00Z"
    )
    world.run(
        ["berlin-bebauungsplaene"],
        "bplan-ruhend",
        adapters={"berlin-bebauungsplaene": adapter},
    )
    stages = {r["stage"]: r for r in world.store().plan_history(NS, "1-98")}
    assert (
        stages["unrecognized"]["stage_label"] == "ruhend"
        and stages["unrecognized"]["stage_date"] is None
    )
    assert (
        stages["unrecognized"]["stage_recognized"] is False and "festgesetzt" in stages
    )


def test_wohnlagen_are_source_labelled_categories_of_their_edition(world):
    rows = sorted(
        world.store().records("area_category", NS), key=lambda r: r["area_id"]
    )
    assert [
        (r["category"], r["edition"]["edition_id"], r["edition"]["valid_from"])
        for r in rows
    ] == [
        ("mittel", "berliner-mietspiegel-2099", "2099-05-15"),
        ("gut", "berliner-mietspiegel-2099", "2099-05-15"),
    ]
    assert all("value" not in r and "score" not in r for r in rows)
    assert rows[0]["category_basis"].startswith("source-labelled")


# ---------------------------------------------------------------------- U05 Mietspiegel and Statistik BB


def test_a_mietspiegel_edition_keeps_its_cells_dates_and_publication_page(world):
    (edition,) = world.store().editions(NS)
    assert (
        edition["edition"],
        edition["qualifying_date"],
        edition["valid_from"],
        edition["published_on"],
    ) == ("Berliner Mietspiegel 2099", "2098-09-01", "2099-05-15", "2099-05-15")
    assert edition["page"] == "S. 14, Mietspiegeltabelle" and edition[
        "publication_url"
    ].endswith(".pdf")
    assert edition["source_revision"]["evidence_origin"] == "fixture"
    cells = world.store().edition(NS, "berliner-mietspiegel-2099")["cells"]
    b1 = next(c for c in cells if c["cell_key"] == "B1")
    assert b1["dimensions"] == {
        "baualter": "bis 1918",
        "wohnflaeche": "40 bis unter 60 m²",
        "wohnlage": "mittel",
    }
    assert {k: v["value"] for k, v in b1["ranges"].items()} == {
        "lower": "6.90",
        "middle": "8.20",
        "upper": "10.50",
    }
    assert b1["unit"] == "EUR/m² monthly" and b1["currency"] == "EUR"


def test_a_new_edition_never_overwrites_the_previous_one():
    env = h.Env().world()
    env.mietspiegel_2101()
    editions = env.store().editions(NS)
    assert [e["edition_id"] for e in editions] == [
        "berliner-mietspiegel-2099",
        "berliner-mietspiegel-2101",
    ]
    old = env.store().edition(NS, "berliner-mietspiegel-2099")["cells"]
    assert (
        next(c for c in old if c["cell_key"] == "B1")["ranges"]["middle"]["value"]
        == "8.20"
    )
    env.conn.close()


def test_permit_and_completion_statistics_are_vintaged_series_per_reporting_area(world):
    mitte = world.store().statistics(NS, scheme="berlin-bezirk", code="001")
    assert {
        (r["statistic"], r["measure"], r["value"], r["unit"], r["vintage"])
        for r in mitte
    } == {
        ("permits", "dwellings_permitted", "310", "dwellings", "2099-03-20"),
        ("permits", "floor_area_permitted", "20.1", "1000 m²", "2099-03-20"),
        ("completions", "dwellings_completed", "260", "dwellings", "2099-05-10"),
        ("completions", "floor_area_completed", "16.9", "1000 m²", "2099-05-10"),
    }
    land = world.store().statistics(
        NS, scheme="ags", code="11", measure="dwellings_permitted"
    )
    assert [(r["value"], r["reporting_area"]["published_code"]) for r in land] == [
        ("1520", "00")
    ]
    env = h.Env().world()
    env.statbb_revised()
    revised = env.store().statistics(
        NS, scheme="berlin-bezirk", code="001", measure="dwellings_permitted"
    )
    assert [(r["vintage"], r["value"]) for r in revised] == [
        ("2099-03-20", "310"),
        ("2099-09-30", "322"),
    ]
    env.conn.close()


def test_undeclared_columns_undated_publications_and_foreign_hosts_are_refused():
    document = fb.statbb_document("permits")
    with pytest.raises(HousingFormatError) as drift:
        parse_publication(
            "statbb-building-csv",
            fb.statbb_csv("permits")
            .replace("Wohnflaeche", "Wohnflaeche;Extra")
            .encode(),
            document=document,
        )
    assert drift.value.code == "schema_drift"
    undated = document
    with pytest.raises(HousingFormatError, match="no publication date"):
        parse_publication(
            "statbb-building-csv", fb.statbb_csv("permits").encode(), document=undated
        )
    dated = parse_publication(
        "statbb-building-csv",
        fb.statbb_csv("permits").encode(),
        document=undated,
        headers={"Last-Modified": "Wed, 01 Apr 2099 10:00:00 GMT"},
    )
    assert (dated["published_on"], dated["publication_basis"]) == (
        "2099-04-01",
        "http_last_modified",
    )
    item = copy.deepcopy(h.source("statistik-bb-bautaetigkeit"))
    item["housing"]["documents"][0]["url"] = "https://example.org/other.csv"
    item.pop("source_hash")
    item["source_hash"] = _digest(item)
    with pytest.raises(SourcePackError) as foreign:
        HousingAdapter(item)
    assert foreign.value.code == "invalid_manifest"


def test_the_adapter_uses_the_runtime_default_transport_and_refuses_cross_host_responses():
    item = h.source("berlin-mietspiegel")
    adapter = HousingAdapter(item)
    assert (
        isinstance(adapter.transport, partial)
        and adapter.transport.func is HTTPSPageAdapter._request
    )
    assert adapter.transport.keywords == {"max_bytes": item["budgets"]["max_bytes"]}
    document = item["housing"]["documents"][0]
    page = {
        **fb.page(fixture_request(document), fb.mietspiegel_csv(), "text/csv"),
        "final_url": "https://evil.example/mietspiegeltabelle.csv",
    }
    redirected = HousingAdapter(item, transport=fixture_transport([page]))
    with pytest.raises(SourcePackError) as refused:
        redirected.fetch_page(
            {"operation": "publications", "parameters": {}}, cursor=None
        )
    assert refused.value.code == "network_policy"
    with pytest.raises(SourcePackError) as parameters:
        redirected.fetch_page(
            {"operation": "publications", "parameters": {"bbox": [1, 2, 3, 4]}},
            cursor=None,
        )
    assert parameters.value.code == "parameter_forbidden"


# ---------------------------------------------------------------------- U06 Destatis


def test_genesis_tables_land_in_the_observation_store_as_vintages_referenced_by_housing(
    world,
):
    from src.ingestion.connectors.dataset.store import ObservationStore

    observations = ObservationStore(world.conn)
    berlin = "destatis:31111-0004:BAUGEN:11"
    header = observations.get_series(berlin)
    assert (header["provider"], header["unit"], header["license"]) == (
        "destatis",
        "count",
        "dl-de-by-2.0",
    )
    assert [(o.period, o.value) for o in observations.get_observations(berlin)] == [
        ("2097", 1390.0),
        ("2098", 1480.0),
    ]
    (vintage,) = world.store().indicator_vintages(NS, series_id=berlin)
    assert (
        vintage["published_on"] == "2099-04-10"
        and vintage["vintage_basis"] == "genesis_table_updated"
    )
    assert (
        "values_in" in vintage and "observations" not in vintage
    )  # numbers are not duplicated
    bayern = world.store().indicator_vintages(
        NS, series_id="destatis:31111-0004:BAUGEN:09"
    )[0]
    assert (
        bayern["signs"]["2098"]["sign"] == "."
        and observations.get_observations("destatis:31111-0004:BAUGEN:09")[1].value
        is None
    )
    before = world.conn.execute("SELECT count(*) FROM dataset_observations").fetchone()[
        0
    ]
    world.run(["destatis-genesis-bautaetigkeit"], "genesis-again")
    assert (
        world.conn.execute("SELECT count(*) FROM dataset_observations").fetchone()[0]
        == before
    )


def test_genesis_needs_its_credential_and_is_a_dataset_connector():
    from src.ingestion.connectors.dataset.base import DatasetConnector
    from src.ingestion.connectors.dataset.genesis import GenesisConnector, table_urls

    item = h.source("destatis-genesis-bautaetigkeit")
    with pytest.raises(SourcePackError) as missing:
        HousingAdapter(item, transport=lambda **_: {}).fetch_page(
            {"operation": "publications", "parameters": {}}, cursor=None
        )
    assert missing.value.code == "authentication_failed"
    documents = item["housing"]["documents"]
    responses = {}
    for document in documents:
        urls = table_urls(item["endpoint"], document["table"])
        responses[urls["metadata"]] = fb.genesis_metadata(document["table"]).encode()
        responses[urls["data"]] = fb.genesis_csv(document["table"]).encode()
    connector = GenesisConnector(
        documents, endpoint=item["endpoint"], get=responses.__getitem__
    )
    assert isinstance(connector, DatasetConnector)
    records = list(connector.harvest())
    assert {r.series_id for r in records} == {
        "destatis:31111-0004:BAUGEN:11",
        "destatis:31111-0004:BAUGEN:09",
        "destatis:31231-0003:BAUFER:11",
        "destatis:31231-0003:BAUFER:09",
    }
    assert (
        len({r.as_of for r in records if r.series_id.startswith("destatis:31111")}) == 1
    )
    adapter = HousingAdapter(
        item,
        transport=fixture_transport(
            json.loads((h.ROOT / item["fixture"]["path"]).read_text())["native_pages"]
        ),
        secret=FIXTURE_SECRET,
    )
    page = adapter.fetch_page(
        {"operation": "publications", "parameters": {}}, cursor=None
    )
    assert (
        page.receipt["publication_basis"] == "genesis_table_updated"
        and page.next_cursor == "1"
    )
