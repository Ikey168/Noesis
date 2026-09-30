"""Offline port-to-logistics-series acceptance for the Economics logistics feature (SL12, #2551).

The pinned UN/LOCODE, UNCTADstat, Eurostat maritime and BLS freight-index fixtures (authored in the documented
shapes; every value is fictional) replay through the real ``logistics`` adapter and the runtime's projector, with
the ``logistics`` feature selected in the composition plan and sockets blocked. The journey takes a port and a
country to cited logistics series with vintages, the UN/LOCODE match basis and trade-flow joins. Offline evidence
only, never live coverage (``docs/roadmaps/economics-logistics-source-audit.md``).
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance
from src.kb.logistics_monitoring import LogisticsMonitor
from src.kb.logistics_ports import LogisticsPorts
from src.kb.logistics_queries import LogisticsQueries
from src.kb.logistics_records import feature_enabled, forbidden_keys, readiness
from tests.unit import logistics_harness as h
from tests.unit import trade_harness as th
from tests.unit.composition.test_migration import _migrated


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def test_port_and_country_to_cited_logistics_series_with_vintages_identity_and_trade_joins():
    conn, coordinator, bundles, _ = _migrated()
    coordinator.select("economics", bundles["economics"]["version"], features=["logistics", "trade-comext",
                                                                            "trade-comtrade"])
    assert coordinator.activate("economics-logistics-acceptance")["status"] == "published"
    assert feature_enabled(conn) is True
    assert "noesis-logistics-record-v1" in PROJECTORS
    assert SourcePackConformance(h.ROOT).offline(json.loads(h.PACK_PATH.read_text()))["valid"]

    # Acquire: trade flows, then every logistics source and the later releases, as the runtime would.
    th.load_all(conn)
    h.load_all(conn, revisions=True)
    assert readiness(conn)["stores_ready"] is True
    ports = LogisticsPorts(conn, now=lambda: h.SECOND_RETRIEVAL)
    ports.import_crosswalk(h.NS, h.CROSSWALK, principal_id="operator-1", scopes=h.SCOPES)
    ports.project_places(h.NS, principal_id="svc", scopes=h.SCOPES)
    proposed = ports.propose_matches(h.NS, principal_id="svc", scopes=h.SCOPES)
    # DE999 has no port counterpart; Wilhelmshaven (1103) was removed from UN/LOCODE before matching.
    assert sorted(u["code"] for u in proposed["unmatched"]) == ["1103", "DE999"]
    candidate = next(m for m in ports.matches(h.NS) if m["source"] == {"scheme": "eurostat-port", "code": "DE003"})
    ports.review(h.NS, candidate["match_id"], "accept", "Eurostat labels DE003 Bremerhaven", principal_id="reviewer",
                 scopes=h.SCOPES)
    ports.link_trade_flows(h.NS, principal_id="svc", scopes=h.SCOPES)
    queries = LogisticsQueries(conn)

    # A port: UN/LOCODE match basis per source, sources side by side, the vintage known at each date, citations.
    early = queries.port(h.NS, "DEHAM", scopes=h.READ_ONLY, as_of_ms=h.day_ms("2099-08-01"), all_vintages=True)
    late = queries.port(h.NS, "DEHAM", scopes=h.READ_ONLY, as_of_ms=h.day_ms("2100-01-15"))
    assert forbidden_keys(early) == [] and forbidden_keys(late) == []
    bases = {(e["provider"], e["concept"]): e["identity_basis"] for e in early["series"]}
    assert bases[("unctadstat", "port_calls")] == "embedded-unlocode"
    assert bases[("eurostat-maritime", "goods_handled")] == "published-crosswalk"
    assert {"port_calls", "goods_handled"} <= set(early["side_by_side"]["by_concept"])
    calls_early = next(e for e in early["series"] if e["concept"] == "port_calls")
    calls_late = next(e for e in late["series"] if e["concept"] == "port_calls")
    assert calls_early["values"][1]["value"] == "7900" and calls_late["values"][1]["value"] == "7950"
    assert len(calls_early["vintages"]) == 2 and calls_late["vintage"]["revision_of"]
    for entry in late["series"]:
        assert entry["citation"]["file_sha256"] and entry["citation"]["evidence_origin"] == "fixture"
        assert entry["licence"]["id"]
    # A candidate reviewed into use: Bremerhaven reaches the Eurostat series through the accepted match.
    bremerhaven = queries.port(h.NS, "DEBRV", scopes=h.READ_ONLY)
    assert [e["identity_basis"] for e in bremerhaven["series"]] == ["name"]
    # A port with no series is none on record.
    assert queries.port(h.NS, "DECUX", scopes=h.READ_ONLY)["status"] == "none_on_record"

    # A country: country-level series only, with the trade-flow joins where trade records share the code.
    germany = queries.country(h.NS, "m49:276", scopes=h.READ_ONLY)
    concepts = {e["concept"] for e in germany["series"]}
    assert concepts == {"container_port_throughput", "merchant_fleet_by_flag"}
    teu = next(e for e in germany["series"] if e["concept"] == "container_port_throughput")
    join = ports.trade_join(h.NS, teu["series_id"])
    assert join["status"] == "linked" and all(link["side_by_side"]["combined"] is False for link in join["links"])
    netherlands = queries.country(h.NS, "m49:528", scopes=h.READ_ONLY)
    nl_teu = next(e for e in netherlands["series"] if e["concept"] == "container_port_throughput")
    assert ports.trade_join(h.NS, nl_teu["series_id"])["status"] == "none_on_record"

    # Freight indices: the revised in-scope index as of each release and the excluded ones by licence decision.
    indices = queries.freight_indices(h.NS, scopes=h.READ_ONLY, as_of_ms=h.day_ms("2099-09-30"))
    assert indices["series"][0]["values"][-1]["value"] == "112.5"
    assert {"baltic-dry-index", "drewry-wci"} <= {i["index_id"] for i in indices["excluded"]}

    # Re-ingestion adds nothing and a restarted monitor replays without duplicates.
    before = conn.execute("SELECT count(*) FROM logistics_vintages").fetchone()[0]
    h.load_all(conn, revisions=True)
    assert conn.execute("SELECT count(*) FROM logistics_vintages").fetchone()[0] == before
    monitor = LogisticsMonitor(conn, now=lambda: h.SECOND_RETRIEVAL)
    sub = monitor.create(h.NS, "acceptance", watch={"ports": ["DEHAM"]}, principal_id="alice", scopes=h.SCOPES)
    first = monitor.run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert {"new_vintage", "revised_value", "port_code_change"} <= {n["kind"] for n in first["notifications"]}
    restarted = LogisticsMonitor(conn, now=lambda: h.SECOND_RETRIEVAL)
    assert restarted.run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
