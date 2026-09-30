"""Offline place-to-transactions acceptance for the Geospatial real-estate feature (#2228, RE12 #2516).

The journey replays the pinned HM Land Registry PPD (additions, then a change and
a deletion row), UK HPI (two releases), INSPIRE WFS (France, Nordrhein-Westfalen,
then a changed parcel geometry), DVF (two semi-annual releases) and Eurostat
prc_hpi_q (two vintages) fixtures through the source-pack runtime with sockets
blocked, and drives the MCP tools: identity proposals and review, as-of place
and parcel answers, links and a monitor. All values are fictional, not live
evidence.
"""

from __future__ import annotations

import asyncio
import json
import socket

import duckdb
import pytest

from tests.unit.real_estate import fixture_builder as fb
from tests.unit.real_estate.harness import NS, REVIEW_SCOPES, Env, owner_markers, seed_places
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.real_estate import REAL_ESTATE_TOOLS

FORBIDDEN_OUTPUT = ("market_value", "estimated_value", "price_per_m2", "valuation\"", "owner_name", "buyer_name",
                    "seller_name", "NOM FICTIF")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _tools(monkeypatch, path, principal="alice"):
    state = {"principal": principal, "scopes": set(REVIEW_SCOPES) | {"knowledge:entity-history:review"}}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def _clean(value) -> None:
    text = json.dumps(value, ensure_ascii=False, default=str)
    assert not [m for m in FORBIDDEN_OUTPUT if m in text]
    assert not owner_markers(value)


def test_place_and_parcel_to_cited_transactions_indices_and_parcels_with_vintages(tmp_path, monkeypatch):
    path = str(tmp_path / "real-estate.duckdb")
    env = Env(duckdb.connect(path)).loaded()
    ids = seed_places(env)
    env.conn.close()
    tools, state = _tools(monkeypatch, path)
    assert REAL_ESTATE_TOOLS <= set(tools)
    assert tools["real_estate_source_contracts"].fn()["live_verification"]["dvf"]["status"] == "unverified-live"

    monitor = tools["create_real_estate_monitor"].fn(namespace=NS, request_key="journey",
                                                     places=[ids["district"], ids["paris"]],
                                                     parcels=["75104000AB0013"])
    baseline = tools["run_real_estate_monitor"].fn(subscription_id=monitor["subscription_id"])
    assert baseline["baseline"] and baseline["notifications"]

    # Identity: exact from published identifiers, candidates reviewed by someone else.
    proposed = tools["propose_real_estate_matches"].fn(namespace=NS)
    matches = proposed["matches"]
    assert {m["basis"] for m in matches if m["state"] == "exact"} == {"parcel-identifier", "place-code"}
    address = next(m for m in matches if m["basis"] == "address")
    state["principal"] = "bob"
    reviewed = tools["review_real_estate_match"].fn(namespace=NS, match_id=address["match_id"], decision="accept",
                                                    reason="same published address")
    assert reviewed["state"] == "accepted" and reviewed["decision_id"]
    state["principal"] = "alice"
    assert {u["source_transaction_id"] for u in proposed["unmatched"]} >= {"2098-1002", fb.TX1.strip("{}")}

    # Later releases: a PPD change file (C and D rows), UK HPI and DVF releases, a Eurostat vintage, a parcel edit.
    later = Env(duckdb.connect(path), start_ms=4_090_000_000_000, install=False)
    for step in (later.ppd_release_2, later.ukhpi_release, later.dvf_release, later.eurostat_vintage,
                 later.parcel_revision):
        assert step()["status"] == "complete"
    later.conn.close()

    # Place as of dates: revision selection, withdrawn rows, side-by-side indices and citations.
    march = tools["real_estate_place_as_of"].fn(namespace=NS, as_of="2099-03-20", place_id=ids["district"])
    april = tools["real_estate_place_as_of"].fn(namespace=NS, as_of="2099-04-01", place_id=ids["district"])
    assert {t["source_transaction_id"]: t["price"]["value_text"] for t in march["transactions"]}[fb.TX1] == "450000"
    by_id = {t["source_transaction_id"]: t for t in april["transactions"]}
    assert by_id[fb.TX1]["price"] == {"value_text": "455000", "value": "455000", "currency": "GBP"}
    assert by_id[fb.TX2]["status"] == "withdrawn"
    assert by_id[fb.TX1]["place_match"]["basis"] == "published-place-code"
    assert all(t["citation"]["url"] and t["citation"]["release"] for t in april["transactions"])
    hpi_march = {g["index_id"]: g for g in march["indices"]}["ukhpi:index"]
    assert {g["index_id"]: g for g in april["indices"]}["ukhpi:index"]["observations"][1]["vintage"] == "2099-03"
    after = tools["real_estate_place_as_of"].fn(namespace=NS, as_of="2099-04-20", place_id=ids["district"])
    hpi_april = {g["index_id"]: g for g in after["indices"]}["ukhpi:index"]
    assert [o["vintage"] for o in hpi_march["observations"]] == ["2099-03", "2099-03"]
    jan = next(o for o in hpi_april["observations"] if o["period"] == "2099-01")
    assert jan["value"]["value_text"] == "152.2" and jan["vintage"] == "2099-04" and jan["revisions_known"] == 2

    paris = tools["real_estate_place_as_of"].fn(namespace=NS, as_of="2099-11-01", place_id=ids["paris"])
    tx = {t["source_transaction_id"]: t for t in paris["transactions"]}
    assert tx["2098-1003"]["status"] == "removed" and tx["2098-1001"]["price"]["currency"] == "EUR"
    assert {m["basis"] for m in tx["2098-1001"]["parcel_matches"]} == {"parcel-identifier"}
    assert {g["provider"] for g in paris["indices"]} == {"eurostat-hpi"}
    assert {p["national_cadastral_reference"] for p in paris["parcels"]} == {"75104000AB0012", "75104000AB0013"}
    assert any("re-identification" in n for n in paris["notices"])

    # Parcel as of a date: revision history, pinned match drift, then a recorded re-match.
    parcel = tools["real_estate_parcel_as_of"].fn(namespace=NS, as_of="2099-10-01", reference="75104000AB0013")
    assert len(parcel["parcel"]["revision_history"]) == 2
    assert parcel["parcel"]["geometry"]["source_crs"] == fb.FR_SRS
    drift = parcel["transactions"][0]["match"]
    assert drift["parcel_revised_since_match"]
    rematched = tools["rematch_real_estate_parcel"].fn(namespace=NS, match_id=drift["match_id"],
                                                       reason="publisher corrected the boundary")
    assert rematched["match"]["state"] == "exact"

    # Links from shared identifiers only; a parcel without linked records has none.
    links = tools["link_real_estate_records"].fn(namespace=NS)
    assert all(link["basis"] in {"parcel-reference", "geography-code", "dataset-code"} for link in links["links"])
    assert tools["list_real_estate_links"].fn(namespace=NS, record_id=parcel["parcel"]["record_id"])["links"] == []

    # None on record, and the monitor hears each later release once.
    empty = tools["real_estate_place_as_of"].fn(namespace=NS, as_of="2099-12-31",
                                                codes=[{"scheme": "insee-commune", "code": "75056"}])
    assert empty["status"] == "none_on_record"
    heard = tools["run_real_estate_monitor"].fn(subscription_id=monitor["subscription_id"])
    kinds = {n["kind"] for n in heard["notifications"]}
    assert {"transaction_revised", "transaction_withdrawn", "index_vintage_new", "parcel_revised"} <= kinds
    again = tools["run_real_estate_monitor"].fn(subscription_id=monitor["subscription_id"])
    assert again["notifications"] == []
    for output in (march, april, paris, parcel, links, heard, proposed):
        _clean(output)
