"""Offline taxon-and-place-to-biodiversity acceptance for the Climate and Environment pack (#2220, BD12 #2531).

The journey replays the pinned Catalogue of Life (two releases with a status
change), GBIF (occurrences from three datasets including a generalised
sensitive-species record and a withheld one) and IUCN (reference-only
assessment history) fixtures through the source-pack runtime with sockets
blocked, and drives the MCP tools: taxon identity review, place linking, as-of
occurrence queries, status history, a subscription event and the export
bundle. Identifiers and values are illustrative, not live evidence.
"""

from __future__ import annotations

import json
import socket

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.biodiversity import harness
from tests.unit.biodiversity.harness import ALL, NS
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.biodiversity import BIODIVERSITY_TOOLS, BIODIVERSITY_WRITES
from src.mcp_host.introspection import tool_map


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _tools(monkeypatch, path, principal="alice"):
    state = {"principal": principal, "scopes": set(ALL) | {"knowledge:schema:register"}}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_taxon_and_place_to_cited_occurrences_and_conservation_status_history(tmp_path, monkeypatch):
    path = str(tmp_path / "biodiversity.duckdb")
    env = harness.Env(duckdb.connect(path)).loaded()
    assert {r["evidence_origin"] for r in env.store.runs(NS)} == {"fixture"}
    mitte = env.place("Mitte (fixture)", harness.MITTE)["place_id"]
    germany = env.place("Deutschland (fixture)", {"type": "Point", "coordinates": [10.45, 51.16]},
                        source_ids={"iso3166-1": "DE"})["place_id"]
    env.seed_papers()
    env.conn.close()
    tools, state = _tools(monkeypatch, path)

    # Taxon identity review: proposals only, then a reviewed acceptance with both checklist versions.
    proposed = tools["propose_taxon_identity_matches"].fn(namespace=NS)
    assert proposed["proposed"] and not [m for m in proposed["matches"] if m["state"] != "proposed"]
    assert any(c["kind"] == "split-or-lump" for c in proposed["conflicts"])
    otter = next(m for m in proposed["matches"] if {m["left_key"], m["right_key"]} == {"gbif:2433753", "iucn:12419"})
    state["principal"] = "bob"
    accepted = tools["review_taxon_identity_match"].fn(namespace=NS, match_id=otter["match_id"], decision="accept",
                                                       reason="same name and authorship")
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "bob" and accepted["checklists"]

    # Place linking at published precision; the generalised otter record never reaches the small district.
    linked = tools["link_biodiversity_records"].fn(namespace=NS, place_ids=[mitte, germany])
    assert linked["places"]["relations"]["below-generalisation"] == 1
    assert linked["citations"]["resolved"] == 1 and linked["citations"]["unresolved"] >= 1

    # Taxon to occurrences, joined through the accepted match, with licence attribution and explicit unknowns.
    by_taxon = tools["occurrences_for_taxon_or_place"].fn(namespace=NS, taxon="iucn:12419")
    assert {r["gbif_id"] for r in by_taxon["occurrences"]} == {"4022001", "4022002"}
    assert by_taxon["taxon"]["accepted_matches"][0]["match_id"] == otter["match_id"]
    generalised = next(r for r in by_taxon["occurrences"] if r["gbif_id"] == "4022001")
    assert generalised["generalisation"]["generalised"] and generalised["coordinates"] == {"latitude": 52.55,
                                                                                          "longitude": 13.45}
    assert generalised["licence"]["id"] == "CC-BY-NC-4.0" and generalised["dataset"]["citation"]
    assert by_taxon["non_commercial"] == ["4022001", "4022002"] and "abundance" in by_taxon["counts"]["label"]
    withheld = next(r for r in by_taxon["occurrences"] if r["gbif_id"] == "4022002")
    assert withheld["coordinates"] is None and "coordinates" in withheld["unknowns"]

    # Place to occurrences: uncertain records apart, explicit "no occurrence on record" before acquisition.
    by_place = tools["occurrences_for_taxon_or_place"].fn(namespace=NS, place_id=mitte)
    assert [r["gbif_id"] for r in by_place["occurrences"]] == ["4011001"]
    assert {r["gbif_id"] for r in by_place["uncertain"]} == {"4011002", "4011003"}
    assert by_place["excluded"] == {"below-generalisation": 1, "not-linked": 1}
    earlier = tools["occurrences_for_taxon_or_place"].fn(namespace=NS, place_id=mitte, as_of="2020-01-01")
    assert earlier["status"] == "no occurrence on record"

    # Status history: assessor designations per scope, published changes, reference-only licence, explicit unknown.
    history = tools["conservation_status_history"].fn(namespace=NS, taxon="Lutra lutra")
    (scope,) = history["scopes"]
    assert scope["current"]["assessment_id"] == "900003" and scope["changes"][0]["to"]["category"] == "NT"
    assert history["licence"]["tier"] == "reference-only"
    crow = tools["conservation_status_history"].fn(namespace=NS, taxon="Corvus cornix")
    assert crow["status"] == "not assessed on record"
    taxa = tools["lookup_taxa"].fn(namespace=NS, query="Corvus cornix")
    assert taxa["status_changes"][0]["from"]["status"] == "accepted" and taxa["status_changes"][0]["to"]["status"] == \
        "synonym"

    # Export bundle keeps IUCN at citation level.
    exported = tools["export_biodiversity_bundle"].fn(namespace=NS, taxon="Lutra lutra")
    for item in exported["conservation_status"]["scopes"][0]["assessments"]:
        assert "criteria" not in item and "assessment_date" not in item

    # A subscription hears a later acquisition: a new assessment and a removed occurrence, each cited.
    monitor = tools["create_biodiversity_monitor"].fn(namespace=NS, request_key="otter-and-mitte",
                                                      taxa=["Lutra lutra"], places=[mitte])
    baseline = tools["run_biodiversity_monitor"].fn(namespace=NS, subscription_id=monitor["subscription_id"])
    assert baseline["baseline"] and baseline["notifications"]
    later = harness.Env(duckdb.connect(path))
    later.advance(8)
    assert later.run("biodiversity-later", later=True)["status"] == "complete"
    later.conn.close()
    heard = tools["run_biodiversity_monitor"].fn(namespace=NS, subscription_id=monitor["subscription_id"])
    kinds = {n["kind"] for n in heard["notifications"]}
    assert {"assessment_new", "occurrence_removed", "occurrence_new"} <= kinds
    assert all(n["new"]["revision_id"] for n in heard["notifications"])
    assert tools["run_biodiversity_monitor"].fn(namespace=NS,
                                                subscription_id=monitor["subscription_id"])["notifications"] == []

    # Nothing anywhere is an abundance, a modelled output or a derived status.
    for answer in (by_taxon, by_place, history, exported, heard):
        assert not harness.forbidden_keys(json.loads(json.dumps(answer)))


def test_tools_are_declared_in_the_catalog_with_environment_scopes_and_live_state_is_separate(tmp_path,
                                                                                              monkeypatch):
    for name in BIODIVERSITY_TOOLS:
        assert _mutability(name) == ("write" if name in BIODIVERSITY_WRITES else "read"), name
    assert _required_scopes("knowledge_engine_mcp", "read", "biodiversity_source_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "review_taxon_identity_match")[0] == \
        "knowledge:environment:review"
    catalog = json.loads((harness.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert {t["name"] for t in catalog["tools"] if t["name"] in BIODIVERSITY_TOOLS} == BIODIVERSITY_TOOLS
    path = str(tmp_path / "b.duckdb")
    harness.Env(duckdb.connect(path)).loaded().conn.close()
    tools, _ = _tools(monkeypatch, path)
    contracts = tools["biodiversity_source_contracts"].fn()
    assert contracts["iucn_licence_decision"]["decision"] == "reference-only"
    assert {v["status"] for k, v in contracts["live_verification"].items() if k in {"col", "gbif", "iucn"}} == {
        "unverified-live"}
    ready = tools["biodiversity_readiness"].fn(namespace=NS)
    assert ready["selected"] is False and ready["default"] is False
    assert {p["state"]["last_evidence_origin"] for p in ready["providers"].values()} == {"fixture"}
    description = tools["occurrences_for_taxon_or_place"].description
    assert "abundance" in description and "presence/absence" in description
