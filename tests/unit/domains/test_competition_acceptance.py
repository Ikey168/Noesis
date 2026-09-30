"""Offline company-to-cases acceptance for the Corporate Ownership competition feature (#2217, CS13).

The pinned EC, TAM, CMA, FTC and DOJ fixtures replay through the real
source-pack runtime (fixture adapters, sockets blocked) into the ownership
store beside the Corporate Ownership fixtures; identity review, citation
linking, group expansion as of a date, stage history, state aid, a
subscription event and the MCP tools then run over them. Every case, award and
party is fictional. This is offline evidence only; it is not live coverage
(CS14, #2368).
"""

from __future__ import annotations

import asyncio
import socket
import urllib.request

import duckdb
import pytest

from src.ingestion.competition_sources import LIVE_VERIFICATION
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.kb import ownership_bundle
from src.kb.competition import CompetitionStore, forbidden_keys
from src.kb.competition_citations import CompetitionCitations
from src.kb.competition_identity import CompetitionIdentity
from src.kb.competition_monitoring import CompetitionMonitor
from src.kb.competition_queries import awards_for_beneficiary, case_history, cases_for_company
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.subscriptions import SubscriptionStore
from tests.unit import competition_harness as h
from tests.unit.ownership import harness as own


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)


def acquire(conn, run_key: str, *, source_ids=None):
    adapters = SourcePackRuntime(conn).fixture_adapters(ownership_bundle.SOURCE_PACK_ID, h.ROOT)
    return ownership_bundle.acquire(conn, h.OWN_NS, run_key=run_key, principal_id=own.PRINCIPAL,
                                    scopes=own.SCOPES | h.SCOPES, source_ids=source_ids, adapters=adapters,
                                    secret_resolver=lambda _ref: "fixture-credential-not-a-real-key",
                                    dns_resolver=lambda _host: ["8.8.8.8"])


def build(path: str | None = None):
    conn = duckdb.connect(path) if path else duckdb.connect()
    ownership_bundle.install_source_pack(conn, principal_id="operator", scopes={"operator"}, accept_terms=True)
    registry = acquire(conn, "registries")
    # The competition sources are the optional feature's: a default acquisition leaves them out.
    assert "competition" not in registry["receipts"]
    assert not conn.execute("SELECT count(*) FROM ownership_records WHERE namespace='competition'").fetchone()[0]
    ownership = OwnershipIdentityService(conn)
    for item in ownership.propose(h.OWN_NS, principal_id=own.PRINCIPAL, scopes=own.SCOPES)["candidates"]:
        decoy = own.DECOY in (item["left_key"], item["right_key"])
        ownership.review(h.OWN_NS, item["candidate_id"], "reject" if decoy else "accept", "register identifiers",
                         principal_id=own.REVIEWER, scopes=own.REVIEW_SCOPES)
    competition = acquire(conn, "competition", source_ids=list(h.SOURCES))
    assert competition["statuses"] == {"competition": "complete"}
    identity = CompetitionIdentity(conn)
    proposed = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    clusters = ownership.clusters(h.OWN_NS)
    good = {clusters[h.HOLD_ENTITY], clusters[h.INT_ENTITY]}
    for view in proposed["candidates"]:
        ok = clusters.get(view["ownership_key"], view["ownership_key"]) in good
        identity.review(h.NS, view["candidate_id"], "accept" if ok else "reject",
                        "identifiers and register name checked" if ok else "a different register entity",
                        principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    h.seed_legal(conn)
    CompetitionCitations(conn).link(h.NS, scopes=h.SCOPES)
    return conn, identity, proposed


def test_company_to_cited_cases_and_aid_awards_with_stage_history():
    conn, identity, proposed = build()
    # Identity: identifiers first, names low evidence, nothing auto-accepted; Northwind stays unmatched.
    methods = {(v["subject_key"], v["method"]) for v in proposed["candidates"]}
    assert (h.AWARD_INT, "exact-identifier") in methods
    assert all(v["state"] == "proposed" for v in proposed["candidates"])
    unmatched = {u["name_as_published"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    assert {"Northwind Widgets GmbH", "Northwind Widgets, Inc.", "Northwind Energie B.V."} <= unmatched

    # Company to cases as of a date, with its group through the ownership graph.
    answer = cases_for_company(conn, h.NS, h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                               as_of="2025-06-01", group=True, include_unknowns=True)
    assert not forbidden_keys(answer)
    rows = {r["case_key"]: r for r in answer["cases"]}
    assert set(rows) == {h.MERGER, h.CMA, h.FTC, h.AID_CASE}
    assert set(answer["by_authority"]) == {"ec", "uk-cma", "us-ftc"}
    assert rows[h.AID_CASE]["group_relation"] == "group-member" and rows[h.AID_CASE]["ownership_path"]
    assert all(r["cites"]["case_revision_id"] and r["cites"]["identity_decision"] for r in rows.values())
    assert all(r["case_revision"]["source"]["evidence_origin"] == "fixture" for r in rows.values())
    assert h.DOJ not in rows  # Northwind's DOJ action names no matched party of this group
    assert all(u["status"] == "unmatched" for u in answer["unknowns"])

    # "No case on record" is explicit and never a clean bill.
    none = cases_for_company(conn, h.NS, h.TRADE_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES,
                             as_of="2025-06-01")
    assert none["status"] == "no_case_on_record" and "not a clean bill" in none["message"]

    # Citation links: the EUMR by CELEX, the CMA's explicit reference to the EC case, the unresolved SA.99003.
    links = CompetitionCitations(conn).links(h.NS)
    assert any(link["target_key"] == "celex:32004R0139" and link["status"] == "resolved" for link in links)
    assert any(link["citing_record_key"] == h.CMA and link["target_case_key"] == h.MERGER for link in links)
    assert any(link["raw"] == "SA.99003" and link["status"] == "unresolved" for link in links)

    # State aid for the beneficiary: measure case linked, totals computed with inputs, unknowns explicit.
    awards = awards_for_beneficiary(conn, h.NS, h.INT_ENTITY, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    assert {a["award_key"] for a in awards["awards"]} == {h.AWARD_INT, h.AWARD_OTHER}
    assert awards["computed_totals"]["by_currency"][0]["sum"] == "13250000.00"
    assert any(u["sa_number"] == "SA.99003" for u in awards["unknowns"] if "sa_number" in u)

    # A subscription on the company notices the merger's final decision and the corrected award.
    monitor = CompetitionMonitor(conn, now=h.Clock())
    subs = SubscriptionStore(conn)
    subs.commit_watermark(h.NS, 1)
    sub = monitor.create(h.NS, "exampla", watch="company", key=h.HOLD_ENTITY, ownership_namespace=h.OWN_NS,
                         group=True, principal_id="alice", scopes=h.SCOPES)["subscription_id"]
    monitor.run(sub, 1, principal_id="alice", scopes=h.SCOPES)
    for source_id in ("ec-competition-cases", "eu-state-aid-tam"):
        h.apply(conn, source_id, v2=True)
    subs.commit_watermark(h.NS, 2)
    events = monitor.run(sub, 2, principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {"case_revised", "stage_change", "new_decision_document", "aid_award_changed"} <= {e["kind"] for e in events}

    # Stage history after the final decision, in date order with superseded revisions on request.
    history = case_history(conn, h.NS, h.MERGER, scopes=h.SCOPES, include_superseded=True)
    assert [s["stage_date"] for s in history["stages"]] == sorted(s["stage_date"] for s in history["stages"])
    assert history["stages"][-1]["stage_as_published"].startswith("Art. 8(2)")
    assert len(history["case_revisions"]) == 2 and history["cited_by"]

    # Re-acquiring the pinned (v1) pages revises only what changed back - the merger case and the two awards -
    # while the v2 stages and documents stay (append-only); a second replay adds nothing.
    count = "SELECT count(*) FROM ownership_record_revisions"
    before = conn.execute(count).fetchone()[0]
    acquire(conn, "competition-replay", source_ids=list(h.SOURCES))
    assert conn.execute(count).fetchone()[0] - before == 3
    before = conn.execute(count).fetchone()[0]
    acquire(conn, "competition-replay-2", source_ids=list(h.SOURCES))
    assert conn.execute(count).fetchone()[0] == before
    assert all(p["status"] == "unverified-live" for p in LIVE_VERIFICATION.values())
    assert CompetitionStore(conn).receipts(h.NS, next(iter(conn.execute(
        "SELECT DISTINCT run_id FROM competition_receipts ORDER BY run_id").fetchone())), scopes=h.SCOPES)


def test_the_mcp_tools_answer_the_journey(tmp_path, monkeypatch):
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "competition-mcp.duckdb")
    conn, _, _ = build(path)
    conn.close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", set(h.REVIEW_SCOPES)))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    contracts = tools["competition_source_contracts"].fn()
    assert set(contracts["live_verification"]) == {"ec-competition", "eu-tam", "uk-cma", "us-ftc", "us-doj"}
    cases = tools["lookup_competition_cases"].fn(namespace=h.NS, entity=h.HOLD_ENTITY, ownership_namespace=h.OWN_NS,
                                                 as_of="2025-06-01")
    assert {r["case_key"] for r in cases["cases"]} == {h.MERGER, h.CMA, h.FTC}
    history = tools["competition_case_history"].fn(namespace=h.NS, case_key=h.MERGER)
    assert history["status"] == "answered" and history["stages"]
    awards = tools["state_aid_awards_for_beneficiary"].fn(namespace=h.NS, entity=h.INT_ENTITY,
                                                           ownership_namespace=h.OWN_NS)
    assert awards["status"] == "answered"
    dossier = tools["build_competition_dossier"].fn(ownership_namespace=h.OWN_NS, scheme="lei",
                                                    value="213800EXAMPLAHOLDS95", as_of="2025-06-01")
    assert dossier["status"] == "assembled" and dossier["competition"]["cases"]
    candidates = tools["list_competition_identity_candidates"].fn(namespace=h.NS, ownership_namespace=h.OWN_NS)
    assert candidates["unmatched"] and candidates["conflicts"] == []
    readiness = tools["competition_readiness"].fn(namespace=h.NS)
    assert readiness["enabled"] is False and readiness["providers"]["eu-tam"]["records"] == 3
