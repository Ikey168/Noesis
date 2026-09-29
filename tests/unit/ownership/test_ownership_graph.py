"""Ownership-graph queries as of a date: sources, validity, conflicts, bounds and replay (#1858)."""

import pytest

from src.kb.ownership_graph import MAX_DEPTH, OwnershipGraph, as_of_status, control_basis, query, replay
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_records import record
from src.kb.ownership_store import OwnershipError, OwnershipStore
from tests.unit.ownership import harness
from tests.unit.ownership.harness import HOLD_KEYS, NS, SCOPES, UK_KEYS


@pytest.fixture()
def reviewed():
    env = harness.Env().ready()
    service = OwnershipIdentityService(env.conn, now=env.now)
    result = service.propose(NS, principal_id=harness.PRINCIPAL, scopes=SCOPES, market=harness.seed_market(env.conn),
                             lei_namespace=NS)
    for item in result["candidates"]:
        decision = "reject" if harness.DECOY in (item["left_key"], item["right_key"]) else "accept"
        service.review(NS, item["candidate_id"], decision, "fixture review", principal_id=harness.REVIEWER,
                       scopes=harness.REVIEW_SCOPES)
    return env


def graph(env, **kwargs):
    return OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES, **kwargs)


def test_as_of_status_is_half_open_and_unknowns_stay_undetermined():
    validity = {"from": "2016-04-06", "to": "2024-12-31", "from_status": "stated", "to_status": "stated"}
    assert as_of_status(validity, "2016-04-05") == "not_started"
    assert as_of_status(validity, "2016-04-06") == "valid"
    assert as_of_status(validity, "2024-12-31") == "ended"
    assert as_of_status({"from": None, "to": None, "from_status": "unknown", "to_status": "open"}, "2020-01-01") == "undetermined"
    assert as_of_status({"from": "2025-03-31", "to": None, "from_status": "stated", "to_status": "unknown"}, "2026-01-01") == "undetermined"
    assert control_basis({"assertion_kind": "shareholding", "share": {"exact": "8.2"}}) == "minority-as-stated"
    assert control_basis({"assertion_kind": "shareholding", "share": {"band": {"min": "50", "min_inclusive": False}}}) == "majority-as-stated"
    assert control_basis({"assertion_kind": "shareholding", "share": None}) == "share-not-stated"


def test_direct_parents_carry_source_kind_validity_and_agreeing_sources_are_grouped(reviewed):
    result = graph(reviewed).direct_parents(UK_KEYS["gleif"], "2020-01-01")
    [group] = result["groups"]
    assert group["holder"] == graph(reviewed).cluster(HOLD_KEYS["gleif"])
    kinds = {(a["assertion_kind"], a["source"]["provider"]) for a in group["assertions"]}
    assert kinds == {("direct_parent", "gleif"), ("shareholding", "companies-house"), ("voting_rights", "companies-house"),
                     ("appoint_directors", "companies-house")}
    assert set(group["differences"]) == {"different_source", "different_date", "different_kind"}
    for edge in group["assertions"]:
        assert {"source", "validity", "assertion_kind", "as_of_status", "revision"} <= set(edge)
    assert result["conflicts"] == []
    assert {e["as_of_status"] for e in result["excluded"]} == {"not_started"}  # the 2025 statements


def test_conflicting_holders_are_returned_together_with_reasons_never_ranked(reviewed):
    result = graph(reviewed).direct_parents(UK_KEYS["ch"], "2025-06-01")
    [conflict] = result["conflicts"]
    assert len(conflict["holders"]) == 2
    assert set(conflict["reasons"]) == {"different_source", "different_date", "different_kind"}
    assert "not resolved" in conflict["resolution"]
    assert "ended" in {e["as_of_status"] for e in result["excluded"]}  # the ceased Holdings PSC entries
    persons = result["other_holdings"]["person_or_unidentified_holders"]
    assert [p["holder"]["kind"] for p in persons] == ["person"]


def test_reporting_exceptions_and_minority_holdings_are_visible(reviewed):
    g = graph(reviewed)
    holdings = g.direct_parents(HOLD_KEYS["ch"], "2025-06-01")
    categories = {e["reporting_exception"]["category"] for e in holdings["reporting_exceptions"]}
    assert categories == {"NO_KNOWN_PERSON", "psc-exempt-as-trading-on-regulated-market",
                          "interested-party-exempt-from-disclosure"}
    assert holdings["groups"] == []
    minority = holdings["other_holdings"]["minority_as_stated"]
    assert {m["share"]["exact"] for m in minority} == {"8.2"} and len(minority) == 2
    ultimate = g.ultimate_parents(UK_KEYS["gleif"], "2025-06-01")
    assert [grp["holder"] for grp in ultimate["groups"]] == [g.cluster(HOLD_KEYS["gleif"])]
    assert ultimate["derived_chain_tops"]["tops"] == [g.cluster(HOLD_KEYS["gleif"])]
    assert "not a stated ultimate parent" in ultimate["derived_chain_tops"]["note"]


def test_subsidiaries_control_chain_and_successor_chain(reviewed):
    g = graph(reviewed)
    subs = g.subsidiaries(HOLD_KEYS["gleif"], "2025-06-01")
    assert {s["subject"] for s in subs["subsidiaries"]} == {g.cluster(UK_KEYS["gleif"]), g.cluster(harness.INT_KEYS["gleif"])}
    assert subs["minority_holdings_as_stated"] == []
    chain = g.control_chain(UK_KEYS["gleif"], "2025-06-01", max_depth=5)
    assert sorted(len(p["entities"]) for p in chain["paths"]) == [2, 3] and chain["cycles"] == []
    successors = g.successor_chain(f"gleif:lei:{harness.TRADE}")
    assert successors["successors"]["chain"][0]["to"] == g.cluster(UK_KEYS["gleif"])
    assert g.cluster(f"gleif:lei:{harness.TRADE}") != g.cluster(UK_KEYS["gleif"])  # never collapsed
    predecessors = g.successor_chain(UK_KEYS["ch"])["predecessors"]["chain"]
    assert predecessors[0]["from"] == f"gleif:lei:{harness.TRADE}" and predecessors[0]["date_status"] == "unknown"


def test_cycles_and_depth_bounds_are_explicit():
    env = harness.Env()
    store = OwnershipStore(env.conn, now=env.now)
    source = {"provider": "open-ownership", "provider_record_id": "cycle"}
    records = []
    for index in range(4):
        records.append(record("legal_entity", f"test:e{index}", source, name=f"Cycle {index}", jurisdiction="GB"))
    for child, parent in ((0, 1), (1, 2), (2, 3), (3, 1)):
        records.append(record("ownership_assertion", f"test:a{child}{parent}", source, subject_key=f"test:e{child}",
                              assertion_kind="appoint_directors", holder={"key": f"test:e{parent}", "kind": "entity"},
                              validity={"from": "2020-01-01", "to_status": "open"}))
    store.put(NS, records, run_id="cycle", principal_id="p", scopes=SCOPES)
    g = graph(env)
    chain = g.control_chain("test:e0", "2024-01-01")
    assert chain["cycles"] and chain["cycles"][0]["path"] == ["test:e0", "test:e1", "test:e2", "test:e3", "test:e1"]
    bounded = g.control_chain("test:e0", "2024-01-01", max_depth=2)
    assert bounded["truncated"] is True and bounded["max_depth"] == 2
    with pytest.raises(OwnershipError):
        g.control_chain("test:e0", "2024-01-01", max_depth=MAX_DEPTH + 1)
    with pytest.raises(OwnershipError):
        g.direct_parents("test:missing")


def test_results_replay_exactly_from_pinned_revisions(reviewed):
    env = reviewed
    first = query(env.conn, NS, "direct_parents", UK_KEYS["ch"], principal_id=harness.PRINCIPAL, scopes=SCOPES,
                  as_of="2025-06-01")
    # New revisions and a reverted identity decision arrive later ...
    source = {"provider": "gleif", "provider_record_id": "late"}
    OwnershipStore(env.conn, now=env.now).put(NS, [record(
        "ownership_assertion", f"gleif:parent:direct:{harness.UK}:{harness.INT}", source, subject_key=UK_KEYS["gleif"],
        assertion_kind="direct_parent", holder={"key": harness.INT_KEYS["gleif"], "kind": "entity"},
        validity={"from": "2025-01-01", "to_status": "open"})], run_id="late", principal_id="p", scopes=SCOPES)
    service = OwnershipIdentityService(env.conn, now=env.now)
    accepted = next(c for c in service.candidates(NS, scopes=SCOPES, state="accepted"))
    service.revert(NS, accepted["candidate_id"], "later reconsidered", principal_id=harness.REVIEWER,
                   scopes=harness.REVIEW_SCOPES)
    current = query(env.conn, NS, "direct_parents", UK_KEYS["ch"], principal_id=harness.PRINCIPAL, scopes=SCOPES,
                    as_of="2025-06-01")
    assert current["result_hash"] != first["result_hash"]
    again = replay(env.conn, NS, first, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert again["result_hash"] == first["result_hash"] and again["pins"] == first["pins"]
