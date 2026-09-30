"""EN13 (#2267): offline journey from a zone or country to cited generation, load, price and capacity series.

Every acquired source (ENTSO-E through the Climate and Environment adapter, EIA, Ember, Eurostat through the
SDMX connector, Energy-Charts) replays its pinned authored fixture through the real adapters with sockets
blocked. The journey asserts per-source citations, vintage/as-of selection, the provisional/revised
distinction, reviewable identity matches and that a subject without data is reported as having none.
"""

from __future__ import annotations

import socket

import duckdb
import pytest

from src.kb.energy_identity import EnergyIdentity
from src.kb.energy_links import EnergyLinks
from src.kb.energy_market import publish_prices
from src.kb.energy_monitoring import EnergyMonitor
from src.kb.energy_queries import EnergyQueries
from src.kb.energy_store import EnergyStore, ms
from src.kb.subscriptions import SubscriptionStore
from tests.unit.energy.harness import NS, SCOPES, acquire, acquire_all, register_market_listing

ZONE = "10Y1001A1001A82H"


@pytest.fixture()
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted in the offline acceptance journey")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_zone_and_country_to_cited_energy_series_with_vintages_identity_and_monitoring(offline):
    conn = duckdb.connect()
    results = acquire_all(conn)
    assert all(r["ok"] for r in results.values())
    assert {r["provider"] for r in results.values()} == {"entsoe", "eia", "ember", "eurostat", "energy-charts"}
    assert all(r["live_verification"] == "unverified-live" for r in results.values())

    # Reviewable identity: candidates first, then a reviewer accepts them.
    identity = EnergyIdentity(conn)
    proposed = identity.propose(NS, principal_id="analyst", scopes=SCOPES)
    assert proposed["unmatched"] == [] and proposed["ambiguous"] == []
    queries = EnergyQueries(conn)
    unreviewed = queries.generation_mix(NS, "DE", scopes=SCOPES)
    assert [s["source"] for s in unreviewed["sources"]] == []  # Eurostat DE publishes balances, not generation
    for match in proposed["proposed"]:
        identity.review(NS, match["match_id"], "accept", "code table checked", principal_id="reviewer", scopes=SCOPES)

    # Country: Ember and Energy-Charts generation side by side, reached through accepted matches.
    country = queries.generation_mix(NS, "DE", scopes=SCOPES)
    assert [s["source"] for s in country["sources"]] == ["ember:ember:electricity-generation/monthly",
                                                         "energy-charts:energy-charts:public_power"]
    for source in country["sources"]:
        for series in source["series"]:
            citation = series["citation"]
            assert citation["attribution"] and citation["licence"]["terms_url"].startswith("https://")
            assert citation["receipt"]["response_sha256"] and citation["release"]["key"]
            assert series["reached_through"]["match"]["state"] == "accepted"
    assert country["related_subjects"][0]["subject"]["code"] == ZONE
    balances = queries.observations(NS, "DE", {"energy_balance"}, scopes=SCOPES)
    assert {s["facets"]["nrg_bal"] for s in balances["sources"][0]["series"]} == {"GIC", "NRGSUP"}

    # Zone: generation, load, price and capacity from ENTSO-E, each cited to its document.
    for record_type in ("generation", "load", "price", "capacity"):
        answer = queries.observations(NS, ZONE, {record_type}, scopes=SCOPES)
        assert answer["status"] == "answered", record_type
        for series in answer["sources"][0]["series"]:
            assert series["citation"]["locator"]["document_mrid"].startswith("fixture-a")
            assert series["citation"]["receipt"]["execution"] == "injected"

    # Provisional and revised figures stay distinct; as-of picks what was published.
    acquire(conn, "entsoe_revision_2")
    before = queries.generation_mix(NS, ZONE, scopes=SCOPES, as_of_ms=ms("2026-09-25T00:00:00Z"))
    after = queries.generation_mix(NS, ZONE, scopes=SCOPES)
    wind_before = next(s for s in before["sources"][0]["series"] if s["facets"]["fuel"]["code"] == "B19")
    wind_after = next(s for s in after["sources"][0]["series"] if s["facets"]["fuel"]["code"] == "B19")
    assert (wind_before["publication_status"], wind_before["values"][1]["value"]) == ("provisional", "9204")
    assert (wind_after["publication_status"], wind_after["values"][1]["value"]) == ("revised", "9240")
    history = queries.revision_history(NS, wind_after["series_id"], scopes=SCOPES, period_start="2026-09-24T01:00:00Z")
    assert [v["figure"]["value"] for v in history["vintages"]] == ["9204", "9240"]
    acquire(conn, "eurostat_balances_later")
    gic = next(s for s in queries.observations(NS, "DE", {"energy_balance"}, scopes=SCOPES,
                                               as_of_ms=ms("2026-07-01"))["sources"][0]["series"]
               if s["facets"]["nrg_bal"] == "GIC")
    assert gic["publication_status"] == "provisional" and gic["values"][1]["flags"]["OBS_FLAG"] == "p"

    # Capacity by plant/unit as of a date, with unknowns.
    plant = queries.capacity_as_of(NS, "ent-eia-plant-99901", "2026-06-15", scopes=SCOPES)
    assert {c["subject"]["code"]: c["capacity_on_date"] for c in plant["capacity"]} == {"99901:G1": "248",
                                                                                       "99901:G2": "50"}
    assert {c["state"] for c in queries.capacity_as_of(NS, "ent-eia-plant-99901", "2027-01-01",
                                                       scopes=SCOPES)["capacity"]} == {"unknown"}

    # Prices through market storage and citation links.
    listing = register_market_listing(conn)
    price = queries.observations(NS, ZONE, {"price"}, scopes=SCOPES)["sources"][0]["series"][0]
    written = publish_prices(conn, NS, price["vintage_id"], listing_id=listing, entitlement_id="entitlement:entsoe",
                             principal_id="analyst", scopes={"operator"})
    assert len(written["written"]) == 3 and written["refused"][0]["value"] == "-3.50"
    links = EnergyLinks(conn)
    assert len(links.link_market(NS, principal_id="analyst", scopes=SCOPES)["linked"]) == 3

    # Monitoring through subscriptions, at committed watermarks.
    monitor = EnergyMonitor(conn, now=lambda: ms("2026-09-27T06:00:00Z"))
    created = monitor.create(NS, "acceptance", subject=ZONE, principal_id="analyst", scopes=SCOPES,
                             record_types=["generation"])
    SubscriptionStore(conn).commit_watermark(NS, 1, kind="ingestion", detail={"generation": 1})
    first = monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    assert {n["kind"] for n in first["notifications"]} == {"new_release"}

    # A subject with no data is reported as having none on record; re-running the journey adds nothing.
    empty = queries.generation_mix(NS, "10YPL-AREA-----S", scopes=SCOPES)
    assert empty["status"] == "none_on_record" and "no energy records on record" in empty["message"]
    vintages = conn.execute("SELECT count(*) FROM energy_vintages").fetchone()[0]
    again = acquire_all(conn)
    assert all(r["applied"]["vintages"] == 0 for r in again.values())
    assert conn.execute("SELECT count(*) FROM energy_vintages").fetchone()[0] == vintages
    assert EnergyStore(conn).provider_state(NS, "eurostat")["last_execution"] == "injected"
