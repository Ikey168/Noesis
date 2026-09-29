"""Offline acceptance: a commodity and place to cited series with vintages and flags (#2213, AF12 #2369).

The journey runs the real source-pack runtime over the pinned FAOSTAT, NASS
Quick Stats, FAS PSD, Eurostat (through the SDMX connector) and Agri-food
portal fixtures, the Geospatial owner (place resolution) and the Products
safety owner (authored RASFF notifications), with sockets blocked. It
reproduces: per-source citations, flags kept verbatim, as-of vintage selection
before and after a revision, reviewable commodity mappings (accepted, rejected
and broader/narrower), a linked food alert, a monitored revision and a
commodity/place with no data reported as none on record. Live coverage (#2370)
is reported separately and stays ``unverified-live``.
"""

from __future__ import annotations

import socket

import pytest

from src.ingestion.agrifood_sources import LIVE_VERIFICATION
from src.kb.agrifood_bundle import readiness
from src.kb.agrifood_identity import AgrifoodIdentity
from src.kb.agrifood_links import AgrifoodLinks
from src.kb.agrifood_monitoring import AgrifoodMonitor
from src.kb.agrifood_queries import AgrifoodQueries
from tests.unit.agrifood import harness as h

NS = h.NS


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_commodity_and_place_to_cited_series_with_vintages_and_flags():
    env = h.Env()
    # 1. Acquire every source of the bounded selection through the runtime, with receipts.
    run = env.run("acceptance-1")
    assert run["status"] == "complete"
    assert {s["source_id"]: s["status"] for s in run["sources"]} == {s: "complete" for s in h.SOURCES}
    assert {r["evidence_origin"] for r in env.store.runs(NS)} == {"fixture"}
    status = readiness(env.conn, NS, scopes=h.READ)
    assert {p["status"] for p in status["providers"].values()} == {"fixture-only"}
    assert {v["status"] for v in LIVE_VERIFICATION.values()} == {"unverified-live"}

    # 2. Reviewable mappings: accepted equivalents, a rejected one, a reviewer's narrower mapping.
    identity = AgrifoodIdentity(env.conn, now=env.tick)
    identity.register_places(principal_id="curator", scopes=h.ALL)
    proposed = identity.propose(NS, principal_id="matcher", scopes=h.WRITE)["crosswalks"]
    assert {c["state"] for c in proposed} == {"proposed"}
    by_pair = {frozenset({(c["left"]["scheme"], c["left"]["code"]), (c["right"]["scheme"], c["right"]["code"])}): c
               for c in proposed}
    for pair in ({("faostat-item", "56"), ("nass-commodity", "CORN")},
                 {("faostat-item", "56"), ("psd-commodity", "0440000")}):
        identity.review(NS, by_pair[frozenset(pair)]["crosswalk_id"], "accept", "same crop", principal_id="rev",
                        scopes=h.REVIEW)
    rejected = by_pair[frozenset({("faostat-item", "56"), ("agrifood-portal-product", "MAI")})]
    identity.review(NS, rejected["crosswalk_id"], "reject", "portal quotes a traded grade", principal_id="rev",
                    scopes=h.REVIEW)
    narrower = identity.propose_manual(NS, "faostat-item:15", "eurostat-crops:C1110", "broader",
                                       "FAOSTAT wheat includes durum; C1110 is common wheat and spelt",
                                       principal_id="rev", scopes=h.REVIEW)
    identity.review(NS, narrower["crosswalk_id"], "accept", "stated scope", principal_id="rev2", scopes=h.REVIEW)

    # 3. Commodity and place to per-source cited series as of a date.
    queries = AgrifoodQueries(env.conn)
    answer = queries.series_as_of(NS, commodity="maize", place="United States", scopes=h.READ, as_of="2025-09-30")
    assert answer["status"] == "found" and answer["place"]["status"] == "resolved"
    assert set(answer["sources"]) == {"faostat", "nass-quickstats", "fas-psd"}
    assert "agri-food-portal" not in answer["sources"]  # the rejected mapping reaches nothing
    for series in answer["series"]:
        for figure in series["observations"]:
            assert figure["citation"]["url"].startswith("https://") and figure["citation"]["attribution"]
            assert figure["vintage"]["release_key"] and figure["flag"]["vocabulary"]
    fao = next(s for s in answer["series"] if s["provider"] == "faostat" and s["measure"]["kind"] == "yield")
    assert {o["flag"]["code"] for o in fao["observations"]} == {"E"}  # flags verbatim
    nass = next(s for s in answer["series"] if s["provider"] == "nass-quickstats")
    assert nass["commodity"]["match"] == "equivalent" and nass["commodity"]["via"][0]["reviewer"] == "rev"
    psd = next(s for s in answer["series"] if s["provider"] == "fas-psd" and s["measure"]["element"] == "Production")
    assert psd["period_type"] == "marketing-year"
    assert {o["estimate_type"] for o in psd["observations"]} == {"publisher-estimate", "publisher-projection"}
    county = queries.series_as_of(NS, commodity="corn", place="us-fips:19169", scopes=h.READ)
    assert county["series"][0]["observations"][0]["value_text"] == "(D)"
    wheat = queries.series_as_of(NS, commodity="faostat-item:15", place="France", scopes=h.READ)
    eurostat = [s for s in wheat["series"] if s["provider"] == "eurostat-agri"]
    assert eurostat and {s["commodity"]["match"] for s in eurostat} == {"narrower"}
    assert {s["provider"] for s in wheat["series"]} == {"faostat", "eurostat-agri"}  # side by side, never summed

    # 4. Links by citation: a RASFF alert names maize; trade flows are unavailable without a provider.
    env.seed_rasff()
    links = AgrifoodLinks(env.conn, now=env.tick)
    assert links.link_rasff(NS, scopes=h.ALL, principal_id="linker")["linked"]
    assert links.link_trade_flows(NS, None, scopes=h.WRITE, principal_id="linker")["status"] == \
        "provider_unavailable"
    monitor = AgrifoodMonitor(env.conn, now=env.tick)
    watch = monitor.create(NS, "maize-us", commodity="maize", place="United States", principal_id="alice",
                           scopes=h.ALL)
    baseline = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.ALL)
    assert {n["kind"] for n in baseline["notifications"]} == {"release", "linked_alert"}

    # 5. A later release revises figures: as-of selection keeps the published history, the monitor hears it.
    assert env.later("acceptance-2")["status"] == "complete"
    before = queries.series_as_of(NS, commodity="maize", place="fao-area:231", scopes=h.READ, as_of="2025-01-31",
                                  measures=["production"])
    after = queries.series_as_of(NS, commodity="maize", place="fao-area:231", scopes=h.READ, as_of="2025-06-30",
                                 measures=["production"])

    def fao_2023(result):
        series = next(s for s in result["series"] if s["provider"] == "faostat")
        return next(o for o in series["observations"] if o["period"] == "2023")

    assert (fao_2023(before)["value"], fao_2023(before)["revised_after_as_of"]) == ("389694460", True)
    assert fao_2023(after)["value"] == "389667000" and fao_2023(after)["vintage"]["released_at"] == "2025-03-20"
    fao_series = next(s for s in after["series"] if s["provider"] == "faostat")["series_id"]
    history = queries.revisions(NS, fao_series, scopes=h.READ, period="2023")
    assert [f["vintage"]["released_at"] for f in history["figures"]["2023"]] == ["2024-12-18", "2025-03-20"]
    heard = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.ALL)
    revisions = [n for n in heard["notifications"] if n["kind"] == "revision"]
    assert revisions and all(n["prior"]["citation"] and n["new"]["citation"] for n in revisions)

    # 6. No data on record, and an idempotent replay.
    none = queries.series_as_of(NS, commodity="soya beans", place="Germany", scopes=h.READ)
    assert none["status"] == "none_on_record" and none["series"] == []
    count = env.conn.execute("SELECT count(*) FROM agrifood_values").fetchone()[0]
    assert env.later("acceptance-3")["status"] == "complete"
    assert env.conn.execute("SELECT count(*) FROM agrifood_values").fetchone()[0] == count
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.ALL)["notifications"] == []
