"""CI02 (#2363): asset, status-revision, capacity and owner-assertion records on the existing spatial storage."""

from __future__ import annotations

import duckdb
import pytest

from src.kb import infrastructure_assets as ia
from src.kb.geospatial import GeospatialStore
from tests.unit.infrastructure.harness import NS, SCOPES, acquire

LICENCE = {"id": "cc-by-4.0", "terms_url": "https://creativecommons.org/licenses/by/4.0/"}


def _record(**overrides):
    base = dict(name="Fixture Plant", source_url="https://example.org/x", attribution="Fixture publisher",
                licence=LICENCE, release={"key": "r1", "released_at": "2026-01-01", "basis": "declared_release",
                                          "label": "Release 1"},
                retrieved_at="2026-09-25T06:00:00Z", geometry={"type": "Point", "coordinates": [14.5, 51.5]},
                geometry_receipt={"crs_published": "EPSG:4326", "precision_m": 5.566,
                                  "precision_basis": "coordinates published to 4 decimal places"},
                status={"published": "Operating", "normalized": "operating", "effective_date": "1985",
                        "effective_basis": "start year"},
                capacities=[{"metric": "electrical_capacity", "value": "750", "unit": "MW"}],
                owners=[{"role": "owner", "name": "Fixture AG", "share": "60", "share_text": "Fixture AG [60%]"}])
    base.update(overrides)
    return ia.record("gem", "gem:coal-plants", "G1", "generating_unit", **base)


def test_records_validate_against_the_json_schema_and_every_asset_class_is_covered():
    value = _record()
    assert ia.validate_against_schema(value) == []
    assert {"power_plant", "pipeline", "lng_terminal", "transmission_line", "mine"} <= set(ia.ASSET_CLASSES)
    for asset_class in ia.ASSET_CLASSES:
        assert ia.validate_against_schema(ia.record(
            "gem", "gem:x", "X1", asset_class, name=None, source_url="https://example.org/x",
            attribution="a", licence=LICENCE, release={"key": "r", "released_at": None, "basis": "retrieval_time"},
            retrieved_at="2026-09-25T06:00:00Z")) == []
    # sub-records carry their own definitions
    assert ia.validate_against_schema(value["status"], definition="status") == []
    assert ia.validate_against_schema(value["owners"][0], definition="owner_assertion") == []
    assert ia.validate_against_schema({"role": "owner"}, definition="owner_assertion")


@pytest.mark.parametrize(("overrides", "code"), [
    ({"status": {"published": "Operating", "normalized": "working"}}, "invalid_infrastructure_record"),
    ({"capacities": [{"metric": "electrical_capacity", "value": 750.0, "unit": "MW"}]}, "invalid_infrastructure_record"),
    ({"geometry_receipt": None}, "geometry_receipt_required"),
    ({"attributes": {"criticality": "high"}}, "sensitive_enrichment_refused"),
    ({"release": {"key": "r", "released_at": None, "basis": "declared_release"}}, "release_required"),
    ({"cited_notes": [{"url": "https://www.gem.wiki/X", "text": "copied wiki paragraph"}]},
     "invalid_infrastructure_record"),
])
def test_invalid_records_fail_closed(overrides, code):
    with pytest.raises(ia.InfrastructureError) as caught:
        _record(**overrides)
    assert caught.value.code == code


def test_publisher_vocabulary_is_kept_beside_the_normalized_status_and_owners_are_not_resolved():
    value = _record()
    assert value["status"] == {"published": "Operating", "normalized": "operating", "effective_date": "1985",
                               "effective_basis": "start year"}
    assert value["owners"][0] == {"role": "owner", "name": "Fixture AG", "share": "60",
                                  "share_text": "Fixture AG [60%]", "identifiers": []}


def test_revisions_status_and_capacity_revisions_keep_history_and_geometry_goes_to_the_spatial_store():
    conn = duckdb.connect()
    first = acquire(conn, "gem-coal-plants-de")
    assert first["ok"] and first["applied"]["revisions"] == 2
    again = acquire(conn, "gem-coal-plants-de")
    assert again["applied"]["revisions"] == 0 and again["applied"]["unchanged"] == 2  # replay is a no-op
    later = acquire(conn, "gem_coal_plants_release_2")
    assert later["applied"]["status_revisions"] == 1 and later["applied"]["capacity_revisions"] == 1
    store = ia.InfrastructureStore(conn)
    unit_a = ia.asset_id(NS, "gem", "gem:coal-plants", "G100001")
    revisions = store.revisions(NS, unit_a, scopes=SCOPES)
    assert [r["sequence"] for r in revisions] == [1, 2] and revisions[1]["revision_of"] == revisions[0]["revision_id"]
    history = store.status_history(NS, unit_a, scopes=SCOPES)
    assert [(s["published"], s["normalized"], s["effective_date"]) for s in history] == [
        ("operating", "operating", "1985"), ("retired", "retired", "2026")]
    assert history[1]["supersedes"] == history[0]["status_revision_id"]
    unit_b = ia.asset_id(NS, "gem", "gem:coal-plants", "G100002")
    assert [c["value"] for c in store.capacity_history(NS, unit_b, scopes=SCOPES)] == ["750", "760"]
    owners = store.owner_assertions(NS, store.revisions(NS, unit_b, scopes=SCOPES)[-1]["revision_id"], scopes=SCOPES)
    assert [(o["role"], o["name"], o["share"]) for o in owners] == [
        ("owner", "Fixture Energie AG", "100"), ("parent", "Fixture Holding SE", "75"), ("parent", "Stadtwerke Fixture", "25")]
    # geometry projected onto the existing geospatial store with its precision receipt
    geometry = GeospatialStore(conn, initialize=False).geometry(NS, revisions[0]["geometry_id"],
                                                                scopes={"knowledge:geospatial:read"})
    assert geometry["geometry"]["type"] == "Point" and geometry["source"]["owner"] == "geospatial.infrastructure"
    assert revisions[0]["record"]["geometry_receipt"]["crs_stored"] == "EPSG:4326"
    assert "4 decimal places" in revisions[0]["record"]["geometry_receipt"]["precision_basis"]
    assert not [t for t in ("infra_geometries", "infra_places") if ia.table_exists(conn, t)]


def test_writes_are_scoped_and_never_global():
    conn = duckdb.connect()
    store = ia.InfrastructureStore(conn)
    with pytest.raises(ia.InfrastructureError) as caught:
        store.apply(NS, [_record()], run_id="r", principal_id="p", scopes={"knowledge:infrastructure:read"})
    assert caught.value.code == "unauthorized"
    with pytest.raises(ia.InfrastructureError) as caught:
        store.apply("global", [_record()], run_id="r", principal_id="p", scopes={"operator"})
    assert caught.value.code == "namespace_forbidden"
