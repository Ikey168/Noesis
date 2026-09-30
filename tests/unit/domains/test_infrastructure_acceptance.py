"""CI13 (#2398): offline journey from a place and an operator to cited infrastructure assets.

Every source (WRI GPPD, Global Energy Monitor tracker releases, an Overpass extract, EIA layers and ENTSOG)
replays its pinned authored fixture through the real adapters with sockets blocked. The journey asserts as-of
status with history, cross-source conflicts side by side, reconciliation candidates, operator matches through
Corporate Ownership, citation links to Energy Systems, and the negative cases:

* an area outside the coverage;
* an unmatched operator;
* conflicting status;
* an optional pack that is absent.

Offline evidence only; live evidence is recorded separately (docs/development/infrastructure-evidence/README.md).
"""

from __future__ import annotations

import socket

import duckdb
import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb import infrastructure_assets as ia
from src.kb.geospatial import GeospatialStore
from src.kb.infrastructure_identity import InfrastructureIdentity, party_key
from src.kb.infrastructure_monitoring import InfrastructureMonitor
from src.kb.infrastructure_queries import NONE_ON_RECORD, InfrastructureQueries
from src.kb.subscriptions import SubscriptionStore
from tests.unit.infrastructure.harness import NS, OWNERSHIP_NS, SCOPES, acquire, acquire_all, load_ownership

DISTRICT = [[14.3, 51.5], [14.8, 51.5], [14.8, 51.8], [14.3, 51.8], [14.3, 51.5]]


@pytest.fixture()
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted in the offline acceptance journey")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _group(answer, native_id):
    return next(g for g in answer["assets"]
                if any(v["native_id"] == native_id for v in g["sources"] + g["components"]))


def test_place_and_operator_to_cited_assets_with_status_history_identity_and_monitoring(offline):
    conn = duckdb.connect()
    results = acquire_all(conn)
    assert all(r["ok"] for r in results.values())
    assert {r["provider"] for r in results.values()} == {"gppd", "gem", "osm", "eia", "entsog"}
    assert all(r["live_verification"] == "unverified-live" for r in results.values())
    monitor = InfrastructureMonitor(conn, now=lambda: 1_790_488_800_000)
    watched = monitor.create(NS, "district", subject={"kind": "place", "bbox": [14.3, 51.5, 14.8, 51.8]},
                             principal_id="analyst", scopes=SCOPES)
    SubscriptionStore(conn).commit_watermark(NS, 1, kind="ingestion", detail={"run": "initial"})
    assert monitor.run(watched["subscription_id"], principal_id="analyst", scopes=SCOPES)["notifications"]

    # Reconciliation: identifier matches are active, proximity pairs wait for review.
    identity = InfrastructureIdentity(conn)
    matches = identity.propose_asset_matches(NS, principal_id="analyst", scopes=SCOPES)["matches"]
    assert {m["basis"] for m in matches} == {"identifier", "proximity-name-class"}
    candidates = [m for m in matches if m["state"] == "candidate"]
    assert candidates and all(m["distance_m"] is not None for m in candidates)

    # Place: a district polygon as of a date, answered per source with citations.
    place = GeospatialStore(conn).register_place(
        NS, "Fixture Landkreis", "district", names=[{"value": "Fixture Landkreis", "language": "de",
                                                     "kind": "canonical"}],
        source_ids={"fixture": "landkreis"}, parent_ids=[], principal_id="analyst", scopes=SCOPES,
        geometry={"type": "Polygon", "coordinates": [DISTRICT]})
    queries = InfrastructureQueries(conn)
    answer = queries.assets_in_place(NS, place_id=place["place_id"], as_of="2026-09-01", scopes=SCOPES,
                                     principal_id="analyst")
    assert answer["coverage"]["status"] == "covered"
    nord = _group(answer, "DEU9990001")
    for view in nord["sources"] + nord["components"]:
        citation = view["citation"]
        assert citation["release"]["key"] and citation["receipt"]["response_sha256"] and citation["licence"]["id"]
    assert nord["identity"]["pending_candidates"]
    for candidate in nord["identity"]["pending_candidates"]:
        identity.review_asset_match(NS, candidate["match_id"], "accept", "outline and name agree",
                                    principal_id="reviewer", scopes=SCOPES)

    # Later GEM release: Unit A retires; history keeps the earlier status and the monitor hears it.
    acquire(conn, "gem_coal_plants_release_2")
    SubscriptionStore(conn).commit_watermark(NS, 2, kind="ingestion", detail={"run": "gem-2"})
    changes = monitor.run(watched["subscription_id"], principal_id="analyst", scopes=SCOPES)["notifications"]
    assert "status_change" in {n["kind"] for n in changes}
    after = queries.assets_in_place(NS, place_id=place["place_id"], as_of="2026-09-01", scopes=SCOPES,
                                    principal_id="analyst")
    nord = _group(after, "DEU9990001")
    assert {v["provider"] for v in nord["sources"]} == {"gppd", "osm"}
    assert nord["disagreements"]["capacity"]  # 1500.0 MW (GPPD) beside 1450 MW (OSM)
    unit_a = next(c for c in nord["components"] if c["native_id"] == "G100001")
    assert [s["normalized"] for s in unit_a["status_history"]] == ["operating", "retired"]
    assert unit_a["status"]["normalized"] == "retired"
    before = _group(queries.assets_in_place(NS, place_id=place["place_id"], as_of="2025-01-01", scopes=SCOPES,
                                            principal_id="analyst"), "DEU9990001")
    assert next(c for c in before["components"] if c["native_id"] == "G100001")["status"]["normalized"] == "operating"
    bundle = queries.export_bundle(after)
    assert verify_bundle(bundle).errors == []

    # Conflicting status: EIA says Operating, GEM says construction; both shown once accepted.
    lng = next(m for m in matches if m["relation"] == "same_asset" and m["state"] == "candidate"
               and "lng" in (m["evidence"].get("names") or [""])[0].casefold())
    identity.review_asset_match(NS, lng["match_id"], "accept", "same terminal", principal_id="reviewer", scopes=SCOPES)
    gulf = queries.assets_in_place(NS, bbox=[-94.0, 29.5, -93.5, 30.0], scopes=SCOPES, principal_id="analyst")
    assert {s["normalized"] for s in _group(gulf, "T0001")["disagreements"]["status"]} == {"operating", "construction"}

    # Optional pack absent: operator matching and energy links are skipped, answers still work.
    assert "not installed" in identity.propose_operator_matches(NS, ownership_namespace=OWNERSHIP_NS,
                                                                principal_id="analyst", scopes=SCOPES)["skipped"]
    assert "not installed" in identity.link_energy(NS, energy_namespace="energy", principal_id="analyst",
                                                   scopes=SCOPES)["skipped"]

    # Operator: Corporate Ownership installed, the identifier candidate reviewed, then an operator answer.
    load_ownership(conn)
    identity.propose_operator_matches(NS, ownership_namespace=OWNERSHIP_NS, principal_id="analyst", scopes=SCOPES)
    candidate = next(c for c in identity.operator_candidates(NS, scopes=SCOPES)
                     if party_key("Fixture Energie AG") in (c["left_key"], c["right_key"]))
    assert candidate["basis"] == "cross-referenced-identifier"
    identity.review_operator_match(NS, candidate["candidate_id"], "accept", "QID on both", principal_id="reviewer",
                                   scopes=SCOPES)
    operator = queries.assets_of_operator(NS, "lei:529900FIXTURE0000001", as_of="2026-09-01", scopes=SCOPES)
    natives = {v["native_id"] for g in operator["assets"] for v in g["sources"] + g["components"]}
    assert {"DEU9990001", "way/9001", "G100001", "G100002"} <= natives
    assert operator["operator_matches"][0]["candidate"]["state"] == "accepted"

    # Energy Systems installed: the EIA plant links by its published plant code.
    from tests.unit.energy.harness import acquire as energy_acquire

    energy_acquire(conn, "energy-eia-capacity")
    assert identity.link_energy(NS, energy_namespace="energy", principal_id="analyst", scopes=SCOPES)["linked"]

    # Negative cases: outside coverage, unmatched operator, an empty covered area.
    assert queries.assets_in_place(NS, bbox=[100.0, 10.0, 101.0, 11.0], scopes=SCOPES,
                                   principal_id="analyst")["status"] == "not_covered"
    unmatched = queries.assets_of_operator(NS, "lei:529900FIXTURE0000002", scopes=SCOPES)
    assert unmatched["assets"] == [] and unmatched["status"].startswith("unmatched operator")
    assert queries.assets_in_place(NS, bbox=[14.21, 51.31, 14.22, 51.32], scopes=SCOPES,
                                   principal_id="analyst")["status"] == NONE_ON_RECORD

    # Re-running the acquisition adds nothing.
    again = acquire_all(conn)
    assert all(r["applied"]["revisions"] == 0 for r in again.values())
    store = ia.InfrastructureStore(conn)
    assert store.receipts(NS, scopes=SCOPES)
