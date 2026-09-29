"""Offline address-to-housing-dossier acceptance (#2022, U11).

The journey replays the pinned BORIS, Bebauungsplan, Wohnlagen, Mietspiegel,
permit-statistic and Destatis fixtures through the source-pack runtime,
resolves a fixture address through the reviewable place-resolution flow and
checks the dossier: the containing zone with its valuation date and prior
revision, the plan with its stage history, the rent-index cells and edition,
the district statistics vintage and the citation links, each with its source
and as-of basis. It covers a point on a zone boundary, a point in no published
zone, two sources disagreeing on one period and a plan whose stage changed
between two snapshots - each reported, never resolved - and shows that
re-acquisition is idempotent and that a restart recovers from stored revisions
and subscription watermarks without duplicate events. No network is used; all
fixtures are authored with fictional values.
"""

from __future__ import annotations

import json
import socket

import duckdb
import pytest

from src.kb.geospatial import GeospatialStore
from src.kb.housing import forbidden_keys
from src.kb.housing_links import HousingLinks
from src.kb.housing_monitoring import HousingMonitor
from src.kb.housing_places import HousingPlaces
from src.kb.subscriptions import SubscriptionStore
from tests.unit import housing_harness as h

NS = h.NS
SCOPES = h.REVIEW_SCOPES | h.LEGAL_SCOPES
GEO_SCOPES = {
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:review",
}


@pytest.fixture()
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "the acceptance journey must not open a network connection"
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def counts(conn) -> dict[str, int]:
    tables = (
        "housing_land_value_revisions",
        "housing_plan_stages",
        "housing_area_categories",
        "housing_rent_index_editions",
        "housing_rent_index_cells",
        "housing_building_statistics",
        "housing_indicator_vintages",
        "housing_source_revisions",
        "dataset_observations",
        "geospatial_feature_revisions",
    )
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def test_address_to_housing_dossier_with_conflicts_unknowns_and_restart(
    tmp_path, no_network
):
    for source_id in h.HOUSING_SOURCES:
        fixture = json.loads(
            (h.ROOT / h.source(source_id)["fixture"]["path"]).read_text()
        )
        assert (
            fixture["source_url"].startswith("https://")
            and fixture["license"]
            and fixture["retrieved"]
        )
    path = str(tmp_path / "housing.duckdb")
    env = h.Env(conn=duckdb.connect(path)).world()
    h.load_legal_works(env.conn)
    place = env.address()

    # The fixture address resolves through the reviewable place-resolution flow.
    geo = GeospatialStore(env.conn)
    resolution = geo.save_resolution(
        geo.resolve(NS, "Musterstraße 1 (fiktiv)", scopes=GEO_SCOPES),
        principal_id="alice",
        scopes=GEO_SCOPES,
    )
    geo.review(
        NS,
        resolution["resolution_id"],
        "accept",
        selected_place_id=place["place_id"],
        reason="fixture address",
        principal_id="bob",
        scopes=GEO_SCOPES,
    )

    # A monitor on the address, evaluated at the first committed watermark.
    monitor = HousingMonitor(env.conn)
    watch = monitor.create(
        NS,
        "acceptance",
        selector={"place_id": place["place_id"]},
        principal_id="alice",
        scopes=SCOPES,
    )
    SubscriptionStore(env.conn).commit_watermark(
        NS, 1, kind="ingestion", detail={"run": "first"}
    )
    first_events = monitor.run(
        watch["subscription_id"], principal_id="alice", scopes=SCOPES
    )["notifications"]
    assert sorted(n["kind"] for n in first_events) == [
        "new_land_value_publication",
        "new_rent_index_edition",
        "plan_stage_recorded",
    ]

    # A second snapshot: a new Stichtag and a changed plan stage; re-acquisition of everything else adds nothing.
    before = counts(env.conn)
    env.run(list(h.WFS_SOURCES), "wfs-again")
    env.run(list(h.TABULAR_SOURCES), "tables-again")
    assert counts(env.conn) == before
    env.boris_2100()
    env.bplan_stage_change()
    HousingLinks(env.conn).link_citations(
        NS, legal_namespace=h.LEGAL_NS, principal_id="alice", scopes=SCOPES
    )
    SubscriptionStore(env.conn).commit_watermark(
        NS, 2, kind="ingestion", detail={"run": "second"}
    )
    env.conn.close()

    # Restart: a new process opens the stored state and recovers from revisions and the watermark.
    env = h.Env(conn=duckdb.connect(path))
    monitor = HousingMonitor(env.conn)
    recovered = monitor.run(
        watch["subscription_id"], principal_id="alice", scopes=SCOPES
    )
    assert sorted(
        (n["kind"], n["item"].get("zone_id") or n["item"].get("plan_id"))
        for n in recovered["notifications"]
    ) == [("new_land_value_publication", "1099001"), ("plan_stage_recorded", "1-99a")]
    assert (
        monitor.run(watch["subscription_id"], 2, principal_id="alice", scopes=SCOPES)[
            "status"
        ]
        == "replayed"
    )
    events = monitor.poll(
        watch["subscription_id"], principal_id="alice", scopes=SCOPES
    )["events"]
    assert len(events) == len(first_events) + len(
        recovered["notifications"]
    )  # no duplicates

    places = HousingPlaces(env.conn)
    answer = places.dossier(
        NS,
        as_of="2100-06-01",
        principal_id="alice",
        scopes=SCOPES,
        resolution_id=resolution["resolution_id"],
    )
    assert answer["status"] == "answered" and forbidden_keys(answer) == []
    assert answer["input"]["resolution"]["basis"] == "accepted review"
    # Containing zone with valuation date, prior revision, unit, currency and source.
    (zone,) = answer["land_value"]["zones"]
    assert (
        zone["zone_id"],
        zone["selected"]["valuation_date"],
        zone["selected"]["value"],
    ) == ("1099001", "2100-01-01", "5900")
    assert (zone["selected"]["unit"], zone["selected"]["currency"]) == ("EUR/m²", "EUR")
    assert [(r["valuation_date"], r["value"]) for r in zone["prior_revisions"]] == [
        ("2099-01-01", "5400")
    ]
    assert (
        zone["selected"]["source_revision"]["feature_revision_id"]
        and zone["selection_basis"]
    )
    # The plan whose stage changed between the snapshots, with its stage history.
    (plan,) = answer["development_plans"]["plans"]
    assert [(s["stage"], s["stage_date"]) for s in plan["stage_history"]] == [
        ("aufstellungsbeschluss", "2098-03-15"),
        ("festgesetzt", "2099-06-15"),
    ]
    assert plan["stage_on_date"]["stage"] == "festgesetzt"
    # Rent-index cells and edition, matched by the source-labelled Wohnlage.
    (edition,) = answer["rent_index"]["editions"]
    assert edition["edition"]["edition"] == "Berliner Mietspiegel 2099"
    assert (
        edition["edition"]["qualifying_date"] == "2098-09-01"
        and edition["edition"]["page"]
    )
    assert [c["cell_key"] for c in edition["cells_for_place"]] == ["B1", "B2", "B3"]
    assert (
        edition["cells_for_place"][2]["ranges"]["middle"]["published"] is False
    )  # unknown stays unknown
    # District statistics with their vintage.
    (district,) = answer["district"]["districts"]
    permits = next(
        s for s in district["statistics"] if s["measure"] == "dwellings_permitted"
    )
    assert (district["code"], permits["value"], permits["vintage"]) == (
        "001",
        "310",
        "2099-03-20",
    )
    assert permits["source_revision"]["publication_basis"] == "http_last_modified"
    # Two sources disagreeing on the same period stay side by side.
    (disagreement,) = answer["land"]["disagreements"]
    assert {(r["provider"], r["value"]) for r in disagreement["readings"]} == {
        ("statistik-bb", "1520"),
        ("destatis", "1480"),
    }
    # Citation links with their basis.
    cited = {link["reference"]["identifier"]: link for link in answer["links"]}
    assert cited["GVBl. 2099 S. 321"]["state"] == "linked"
    assert cited["GVBl. 2099 S. 321"]["evidence"]["stage"]["stage"] == "festgesetzt"
    assert cited["GVBl. 2098 S. 42"]["state"] == "linked"
    assert cited["BGBl. I 2097 S. 99"]["state"] == "unresolved"
    assert (
        places.replay(answer["receipt"], principal_id="alice", scopes=SCOPES)["status"]
        == "reproduced"
    )

    # A point on a zone boundary and a point in no published zone are reported as such.
    edge = places.dossier(
        NS,
        as_of="2100-06-01",
        principal_id="alice",
        scopes=SCOPES,
        point=h.wgs84(h.EDGE_UTM),
    )
    assert (
        edge["land_value"]["status"] == "several_zones"
        and len(edge["land_value"]["zones"]) == 3
    )
    outside = places.dossier(
        NS,
        as_of="2100-06-01",
        principal_id="alice",
        scopes=SCOPES,
        point=h.wgs84(h.OUTSIDE_UTM),
    )
    assert outside["land_value"]["status"] == "outside_published_zones"
    assert (
        outside["development_plans"]["plans"] == []
        and outside["residential_area"]["status"] == "none"
    )
    env.conn.close()
