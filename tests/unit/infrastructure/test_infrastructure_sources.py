"""CI01, CI03-CI06 (#2359, #2367, #2371, #2373, #2376): bounded, receipted acquisition of the registries."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.ingestion import infrastructure_sources as src
from src.ingestion.provider_execution import ProviderError
from src.ingestion.source_packs import SourcePackConformance, validate_source_pack
from src.kb import infrastructure_assets as ia
from tests.unit.infrastructure import fixture_builder as fb
from tests.unit.infrastructure.harness import NS, SCOPES, acquire

ROOT = Path(__file__).resolve().parents[3]
PACK = json.loads((ROOT / "config/source_packs/geospatial-infrastructure.json").read_text())


def _latest(conn, provider, dataset, native_id):
    store = ia.InfrastructureStore(conn, initialize=False)
    return store.revisions(NS, ia.asset_id(NS, provider, dataset, native_id), scopes=SCOPES)[-1]["record"]


def test_every_source_has_a_recorded_decision_and_the_pack_replays_offline():
    assert set(src.PROVIDER_CONTRACTS) == {"gppd", "gem", "osm", "eia", "entsog"}
    for contract in src.PROVIDER_CONTRACTS.values():
        assert {"decision", "terms", "attribution", "redistribution", "rate_limits", "revisions"} <= set(contract)
    assert all(v["status"] == "unverified-live" for v in src.LIVE_VERIFICATION.values())
    assert {e["source"] for e in src.EXCLUDED} == {"eia", "entsog", "gem", "osm"}
    assert {a["id"] for a in src.BOUNDED_COVERAGE["areas"]} == {"de-lusatia", "de", "us-gulf-coast",
                                                                "us-southern-california"}
    doc = (ROOT / "docs/development/infrastructure-evidence/source-audit.md").read_text()
    assert "Security-sensitivity rule" in doc and "ODbL" in doc and "non-commercial" in doc
    pack = validate_source_pack(PACK)
    assert pack["domains"] == ["geospatial"] and len(pack["sources"]) == len(fb.SELECTIONS)
    assert all(s["connector"] == "infrastructure" for s in pack["sources"])
    result = SourcePackConformance(ROOT).offline(PACK)
    assert result["valid"] and result["coverage"]["configured"] == result["coverage"]["verified"]
    # the Berlin source pack is untouched: its content is pinned by the upgrade chains
    berlin = json.loads((ROOT / "config/source_packs/geospatial.json").read_text())
    assert not [s for s in berlin["sources"] if s.get("connector") == "infrastructure"]


def test_gppd_rows_are_keyed_by_gppd_idnr_with_release_estimates_and_country_bounds():
    conn = duckdb.connect()
    result = acquire(conn, "gppd-deu")
    assert result["ok"] and result["applied"]["assets"] == 2  # the FRA row is outside the selection
    plant = _latest(conn, "gppd", "gppd:global_power_plant_database", "DEU9990001")
    assert {"scheme": "wepp_id", "value": "99001"} in plant["identifiers"]
    assert plant["release"] == {"key": "gppd:Global Power Plant Database v1.3.0", "released_at": "2021-06-02",
                                "basis": "declared_release", "label": "Global Power Plant Database v1.3.0"}
    assert plant["capacities"][0]["value"] == "1500.0" and plant["capacities"][0]["effective_date"] == "2019"
    assert plant["estimates"] == [{"metric": "annual_generation", "year": 2017, "value": "9050.0", "unit": "GWh",
                                   "basis": "publisher estimate", "note": "CAPACITY-FACTOR-V1"}]
    assert plant["attributes"]["reported_generation_gwh"] == {"2018": "9100.5", "2019": "8700.2"}
    assert plant["owners"] == [{"role": "owner", "name": "Fixture Energie AG", "share": None, "share_text": None,
                                "identifiers": []}]
    assert plant["licence"]["id"] == "cc-by-4.0" and "status" in " ".join(plant["unknowns"])
    receipts = ia.InfrastructureStore(conn).receipts(NS, scopes=SCOPES)
    assert len(receipts) == 1 and receipts[0]["status"] == "ok"


def test_gem_releases_keep_ids_shares_cited_notes_and_the_non_commercial_constraint():
    conn = duckdb.connect()
    acquire(conn, "gem-coal-plants-de")
    unit = _latest(conn, "gem", "gem:coal-plants", "G100001")
    assert {"scheme": "gppd_idnr", "value": "DEU9990001"} in unit["identifiers"]
    assert {"scheme": "gem_location_id", "value": "L100001"} in unit["identifiers"]
    assert unit["cited_notes"] == [{"url": "https://www.gem.wiki/Fixture_Lignite_Plant_Nord",
                                    "label": "GEM wiki page (cited, not copied)"}]
    assert any("non-commercial" in c for c in unit["licence"]["constraints"])
    assert [(o["role"], o["name"], o["share"], o["share_text"]) for o in unit["owners"]][1] == (
        "parent", "Fixture Holding SE", "60", "Fixture Holding SE [60%]")
    acquire(conn, "gem_coal_plants_release_2")
    later = _latest(conn, "gem", "gem:coal-plants", "G100001")
    assert later["status"]["normalized"] == "retired" and later["release"]["label"] == "GCPT July 2026"
    acquire(conn, "gem-gas-pipelines-de")
    pipeline = _latest(conn, "gem", "gem:gas-pipelines", "P0001")
    assert pipeline["geometry"]["type"] == "LineString" and pipeline["capacities"][0]["unit"] == "bcm/y"
    with pytest.raises(ProviderError) as caught:
        src.plan("gem", {**fb.SELECTIONS["gem-coal-plants-de"][1], "file_url": "https://example.org/gcpt.csv"})
    assert caught.value.code == "network_policy"


def test_overpass_extracts_are_bounded_receipted_versioned_and_drop_editor_identity():
    step = src.plan("osm", fb.SELECTIONS["osm-lusatia"][1])[0]
    query = step["params"]["data"]
    assert "[timeout:60][maxsize:16777216][bbox:51.3,14.2,51.8,14.8]" in query and "out meta geom 200;" in query
    with pytest.raises(ProviderError):
        src.plan("osm", {"bbox": [13.0, 51.0, 15.0, 52.0]})  # larger than 1 x 1 degree
    with pytest.raises(ProviderError):
        src.plan("osm", {"bbox": [14.2, 51.3, 14.8, 51.8], "tags": ["amenity=school"]})
    conn = duckdb.connect()
    result = acquire(conn, "osm-lusatia")
    receipt = ia.InfrastructureStore(conn).receipt_row(result["receipts"][0]["receipt_id"])
    assert receipt["request"]["params"]["data"] == query
    assert receipt["coverage"]["notes"]["osm_timestamp_osm_base"] == "2026-09-24T21:00:00Z"
    plant = _latest(conn, "osm", "osm:overpass", "way/9002")
    assert plant["element"] == {"type": "way", "id": "9002", "version": "3", "changeset": "990001",
                                "timestamp": "2026-05-01T10:00:00Z"}
    assert "fixture-mapper" not in json.dumps(plant) and "424242" not in json.dumps(plant)
    assert {"scheme": "gppd_idnr", "value": "DEU9990002"} in plant["identifiers"]
    assert plant["owners"][0]["role"] == "operator" and plant["licence"]["id"] == "odbl-1.0"
    disused = _latest(conn, "osm", "osm:overpass", "node/9301")
    assert disused["status"]["published"] == "disused:power=plant" and disused["status"]["normalized"] == "mothballed"
    relation = _latest(conn, "osm", "osm:overpass", "relation/9401")
    assert relation["geometry"] is None
    later = acquire(conn, "osm_lusatia_later")
    assert later["applied"]["revisions"] == 2 and later["applied"]["unchanged"] == 6  # version bump + new element
    store = ia.InfrastructureStore(conn)
    versions = [r["record"]["element"]["version"] for r in
                store.revisions(NS, ia.asset_id(NS, "osm", "osm:overpass", "way/9002"), scopes=SCOPES)]
    assert versions == ["3", "4"]


def test_eia_and_entsog_layers_keep_feature_ids_units_and_directions():
    conn = duckdb.connect()
    for name in ("eia-power-plants-socal", "eia-lng-terminals-gulf", "eia-gas-pipelines-gulf", "entsog-de-points"):
        assert acquire(conn, name)["ok"]
    plant = _latest(conn, "eia", "eia:power-plants", "plant:99901")
    assert {"scheme": "eia_plant_code", "value": "99901"} in plant["identifiers"]
    assert plant["capacities"][0]["effective_date"] == "2026-07"
    terminal = _latest(conn, "eia", "eia:lng-terminals", "lng-terminals:7")
    assert terminal["release"]["basis"] == "retrieval_time"
    assert terminal["cited_references"][0]["identifier"] == "CP99-001-000"
    point = _latest(conn, "entsog", "entsog:connectionpoints", "ITP-09991")
    assert [(c["direction"], c["value"], c["unit"]) for c in point["capacities"]] == [
        ("entry", "1000000000", "kWh/d"), ("exit", "800000000", "kWh/d")]  # Physical Flow is not a capacity
    assert point["geometry"] is None and point["release"]["basis"] == "provider_last_update"
    assert point["owners"][0]["identifiers"] == [{"scheme": "entsog_operator_key", "value": "DE-TSO-9991"}]
    with pytest.raises(ProviderError):
        src.plan("entsog", {"point_keys": [f"ITP-{i}" for i in range(21)], "from": "2026-09-20", "to": "2026-09-21"})


def test_a_failed_step_is_receipted_and_changes_no_stored_value():
    conn = duckdb.connect()
    acquire(conn, "eia-power-plants-socal")
    provider, selection = fb.selection("eia-power-plants-socal")
    failed = src.acquire(conn, provider, selection, namespace=NS, scopes=SCOPES, principal_id="analyst",
                         fetch=lambda **_: {"status": 503, "content": b""})
    assert not failed["ok"] and failed["failures"][0]["failure_code"] == "source_unavailable"
    store = ia.InfrastructureStore(conn)
    assert len(store.revisions(NS, ia.asset_id(NS, "eia", "eia:power-plants", "plant:99901"), scopes=SCOPES)) == 1
    assert store.provider_state(NS, "eia")["stale"] is True
