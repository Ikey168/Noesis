"""Offline identifier-to-ownership-dossier acceptance for Corporate Ownership and Registries (#1861).

The real source-pack runtime, GLEIF/LEI projector, ownership projector,
document store, entity-history decisions, market instrument master and
authored reports run against pinned *authored* fixtures
(tests/fixtures/ownership/README.md). Network access is refused. This is
offline evidence only; it is not live provider coverage.
"""

import socket
import urllib.request

import duckdb
import pytest

from src.ingestion.ownership_providers import LIVE_VERIFICATION
from src.kb.entity_history import EntityHistoryStore
from src.kb.ownership_dossier import build_dossier, export_dossier
from src.kb.ownership_graph import OwnershipGraph, replay
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_records import digest
from src.kb.ownership_store import OwnershipError, OwnershipStore
from src.kb.ownership_timeline import state_as_of, timeline
from tests.unit.ownership import harness
from tests.unit.ownership.harness import HOLD_KEYS, NS, SCOPES, UK_KEYS

ROWS = {
    "journey": "identifier -> acquisition -> review -> graph -> timeline -> dossier export, offline",
    "conflicts": "conflicting assertions coexist and are returned together with reasons",
    "exceptions": "reporting exceptions are visible and never read as 'no owner'",
    "successors": "successor chains link entities without collapsing them",
    "rejected": "rejected identity matches keep both records apart",
    "reversible": "accepted matches are reversible and auditable",
    "isolation": "namespaces isolate records, candidates and dossiers",
    "unknowns": "unknowns stay unknown and are listed",
    "evidence": "offline evidence is labelled and never reported as live",
    "restart": "re-runs are idempotent and survive a restart",
    "name": "a name lookup returns candidates and never picks",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)


def review_all(env, *, reject_decoy=True):
    service = OwnershipIdentityService(env.conn, now=env.now)
    market = harness.seed_market(env.conn)
    proposed = service.propose(NS, principal_id=harness.PRINCIPAL, scopes=SCOPES, market=market, lei_namespace=NS)
    for item in proposed["candidates"]:
        decoy = harness.DECOY in (item["left_key"], item["right_key"])
        service.review(NS, item["candidate_id"], "reject" if decoy and reject_decoy else "accept",
                       "decoy: no register number" if decoy else "identifiers agree", principal_id=harness.REVIEWER,
                       scopes=harness.REVIEW_SCOPES)
    return service, market


def test_row_journey_identifier_to_explained_dossier(tmp_path):
    env = harness.Env(str(tmp_path / "journey.duckdb"))
    env.install()
    acquired = env.acquire("journey")
    assert acquired["statuses"] == {"entities": "complete", "ownership": "complete"}
    assert OwnershipStore(env.conn).lookup(NS, "lei", harness.UK, principal_id="p", scopes=SCOPES)["status"] == "found"
    _, market = review_all(env)
    dossier = build_dossier(env.conn, NS, "lei", harness.UK, principal_id=harness.PRINCIPAL, scopes=SCOPES,
                            as_of="2025-06-01", market=market, evidence_kind="offline-fixture")
    assert dossier["status"] == "assembled"
    providers = {n["provider"] for e in dossier["entities"] for n in e["names"]}
    assert providers >= {"gleif", "companies-house", "open-ownership"}
    edges = [a for g in dossier["relationships"]["direct_parents"]["groups"] for a in g["assertions"]]
    assert edges and all(a["source"]["provider"] and a["validity"] and a["as_of_status"] for a in edges)
    assert {o["officer"]["name"] for o in dossier["officers"]} == {"DOE, Alex", "ROE, Sam", "POE, Kim"}
    assert {f["accession_number"] for f in dossier["filings"]} >= {"FIXTX001", "FIXTX005"}
    assert dossier["timeline"]["entries"] and dossier["timeline"]["undated"]
    assert any(e["kind"] == "market_corporate_action" for e in dossier["timeline"]["entries"])
    assert dossier["conflicts"] and dossier["unknowns"]
    assert "not a beneficial-ownership, sanctions or AML determination" in dossier["notice"]
    exported = export_dossier(env.conn, dossier, "journey", principal_id=harness.PRINCIPAL, scopes=SCOPES)
    from src.kb.authored_reports import AuthoredReportStore

    report = AuthoredReportStore(env.conn).inspect(NS, exported["report_id"], principal_id=harness.PRINCIPAL, scopes=SCOPES)
    sourced = [a for s in report["content"]["sections"] for a in s["assertions"] if a["kind"] == "sourced"]
    assert sourced and all(a["dependencies"] and a["citations"] for a in sourced)
    graph = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    result = graph.direct_parents(UK_KEYS["gleif"], "2025-06-01")
    assert replay(env.conn, NS, result, principal_id=harness.PRINCIPAL, scopes=SCOPES)["result_hash"] == result["result_hash"]


def test_row_conflicting_assertions_coexist_with_reasons():
    env = harness.Env().ready()
    review_all(env)
    graph = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    result = graph.direct_parents(UK_KEYS["ch"], "2025-06-01")
    [conflict] = result["conflicts"]
    assert conflict["holders"] == sorted({graph.cluster(HOLD_KEYS["gleif"]), graph.cluster(harness.INT_KEYS["gleif"])})
    assert set(conflict["reasons"]) == {"different_source", "different_date", "different_kind"}
    same_edge = next(g for g in result["groups"] if g["holder"] == graph.cluster(harness.INT_KEYS["gleif"]))
    assert {a["source"]["provider"] for a in same_edge["assertions"]} == {"companies-house", "open-ownership"}
    ids = {a["record_id"] for g in result["groups"] for a in g["assertions"]}
    assert set(conflict["assertion_ids"]) == ids  # every assertion kept, none ranked away


def test_row_reporting_exceptions_are_visible_and_not_absence():
    env = harness.Env().ready()
    review_all(env)
    dossier = build_dossier(env.conn, NS, "lei", harness.HOLD, principal_id=harness.PRINCIPAL, scopes=SCOPES,
                            as_of="2025-06-01")
    exceptions = {(e["source"]["provider"], e["reporting_exception"]["category"]) for e in dossier["reporting_exceptions"]}
    assert ("gleif", "NO_KNOWN_PERSON") in exceptions
    assert ("companies-house", "psc-exempt-as-trading-on-regulated-market") in exceptions
    assert ("open-ownership", "interested-party-exempt-from-disclosure") in exceptions
    assert all(g["holder"] for g in dossier["relationships"]["direct_parents"]["groups"])  # no invented holder
    ended = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES).direct_parents(HOLD_KEYS["ch"], "2016-05-01")
    assert "no-individual-or-entity-with-signficant-control" in {
        e["reporting_exception"]["category"] for e in ended["reporting_exceptions"]}


def test_row_successor_chain_links_without_collapsing():
    env = harness.Env().ready()
    review_all(env)
    graph = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    chain = graph.successor_chain(f"gleif:lei:{harness.TRADE}")["successors"]["chain"]
    assert chain[0]["to"] == graph.cluster(UK_KEYS["gleif"]) and chain[0]["event_type"] == "succession"
    assert graph.cluster(f"gleif:lei:{harness.TRADE}") == f"gleif:lei:{harness.TRADE}"
    trading = build_dossier(env.conn, NS, "lei", harness.TRADE, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert trading["identity"]["members"] == [f"gleif:lei:{harness.TRADE}"]


def test_row_rejected_identity_match_keeps_records_apart():
    env = harness.Env().ready()
    service, _ = review_all(env)
    rejected = service.candidates(NS, scopes=SCOPES, state="rejected")
    assert rejected and all(harness.DECOY in (c["left_key"], c["right_key"]) for c in rejected)
    graph = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert graph.cluster(harness.DECOY) == harness.DECOY
    decisions = EntityHistoryStore(env.conn).history(NS, rejected[0]["left_entity"] if harness.DECOY == rejected[0]["left_key"]
                                                     else rejected[0]["right_entity"], scopes={"knowledge:entity-history:read"})
    assert {d["decision_type"] for d in decisions["items"]} == {"non-match"}


def test_row_accepted_match_is_reversible_and_auditable():
    env = harness.Env().ready()
    service, _ = review_all(env)
    match = harness.candidate(service, UK_KEYS["gleif"], UK_KEYS["ch"])
    before = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert before.cluster(UK_KEYS["gleif"]) == before.cluster(UK_KEYS["ch"])
    service.revert(NS, match["candidate_id"], "second reviewer disagrees", principal_id=harness.REVIEWER,
                   scopes=harness.REVIEW_SCOPES)
    after = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    # Still linked through the BODS statement that carries both identifiers: only the reverted edge is gone.
    assert {c["state"] for c in service.candidates(NS, scopes=SCOPES, record_key=UK_KEYS["ch"])} >= {"reverted", "accepted"}
    assert after.cluster(UK_KEYS["gleif"]) == after.cluster(UK_KEYS["bods"])
    audit = env.conn.execute("SELECT count(*) FROM entity_history_audit WHERE namespace=?", [NS]).fetchone()[0]
    assert audit >= len(service.candidates(NS, scopes=SCOPES)) + 1


def test_row_namespace_isolation():
    env = harness.Env().ready()
    review_all(env)
    other = {"knowledge:ownership:read", "knowledge:ownership:write", "namespace:tenant-b:read", "namespace:tenant-b:write"}
    with pytest.raises(OwnershipError):
        build_dossier(env.conn, "tenant-b", "lei", harness.UK, principal_id="b", scopes=other)
    with pytest.raises(OwnershipError) as caught:
        build_dossier(env.conn, NS, "lei", harness.UK, principal_id="b", scopes=other)
    assert caught.value.code == "unauthorized"
    assert OwnershipIdentityService(env.conn).candidates("tenant-b", scopes=other) == []
    with pytest.raises(OwnershipError):
        env.acquire("wrong-namespace", scopes=other)


def test_row_unknowns_stay_unknown():
    env = harness.Env().ready()
    review_all(env)
    dossier = build_dossier(env.conn, NS, "lei", harness.UK, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    unknown = {u for item in dossier["unknowns"] for u in item["unknown"]}
    assert {"appointed_on", "validity.to"} <= unknown
    line = timeline(env.conn, NS, UK_KEYS["gleif"], principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert all(e["event_time"] is None and e["date_status"] == "unknown" for e in line["undated"])
    state = state_as_of(env.conn, NS, UK_KEYS["gleif"], "2019-01-01", principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert [o["officer"] for o in state["officers_with_unknown_tenure"]] == ["POE, Kim"]


def test_row_offline_evidence_is_labelled_and_never_live():
    env = harness.Env().ready()
    review_all(env)
    runs = env.conn.execute("SELECT request_json FROM source_pack_runs WHERE pack_id='corporate-ownership'").fetchall()
    assert runs and all('"network":"disabled"' in r[0] for r in runs)  # fixture adapters; network never enabled
    dossier = build_dossier(env.conn, NS, "lei", harness.UK, principal_id=harness.PRINCIPAL, scopes=SCOPES,
                            evidence_kind="offline-fixture")
    assert dossier["evidence"]["kind"] == "offline-fixture" and dossier["evidence"]["runs"]
    assert all(v["status"] != "verified-live" for v in LIVE_VERIFICATION.values())
    for name in ("gleif-level2.json", "companies-house.json", "sec-edgar.json", "open-ownership-bods.json"):
        assert harness.load(name)["authored"] is True


def test_row_rerun_is_idempotent_and_restart_safe(tmp_path):
    path = str(tmp_path / "restart.duckdb")
    env = harness.Env(path)
    env.install()
    env.acquire("first")
    review_all(env)
    revisions = env.conn.execute("SELECT count(*) FROM ownership_record_revisions").fetchone()[0]
    first = build_dossier(env.conn, NS, "lei", harness.UK, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    again = env.acquire("first")
    assert all(r.get("idempotent") for r in again["receipts"].values())
    env.conn.close()
    env.conn = duckdb.connect(path)
    env.acquire("second")
    assert env.conn.execute("SELECT count(*) FROM ownership_record_revisions").fetchone()[0] == revisions
    after = build_dossier(env.conn, NS, "lei", harness.UK, principal_id=harness.PRINCIPAL, scopes=SCOPES,
                          pins=first["pins"]["records"], identity=first["pins"]["identity"])
    keep = lambda d: digest({k: v for k, v in d.items() if k not in {"pins", "dossier_hash", "evidence"}})  # noqa: E731
    assert first["status"] == "assembled" and keep(after) == keep(first)


def test_row_name_lookup_never_picks():
    env = harness.Env().ready()
    result = build_dossier(env.conn, NS, "name", "Exampla Holdings|GB", principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert result["status"] == "needs_selection" and len(result["candidates"]) >= 2
    assert "never picks" in result["note"]


def test_every_acceptance_row_has_a_test():
    names = {name for name in globals() if name.startswith("test_row_")}
    assert {f"test_row_{row}" for row in ("conflicting_assertions_coexist_with_reasons",)} <= names
    assert len(names) == len(ROWS)
