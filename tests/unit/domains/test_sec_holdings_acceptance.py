"""Offline acceptance for SEC 13F, 13D and 13G filings as structured ownership assertions (#2883).

The corporate-ownership source pack runs through the real source-pack runtime,
the ``sec-edgar-holdings`` adapter, the ownership projector, identity review
and the ownership ``graph`` query against the pinned *authored* fixture
``tests/fixtures/ownership/sec-edgar-holdings.json`` (fictional filers and
issuers). Network access is refused. This is offline evidence only; it is not
live provider coverage.
"""

import socket
import urllib.request

import pytest

from src.ingestion.ownership_sec_holdings import LIVE_VERIFICATION
from src.kb.ownership_graph import OwnershipGraph, query, replay
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_records import OwnershipRecordError
from src.kb.ownership_store import OwnershipStore
from tests.unit.ownership import harness
from tests.unit.ownership.harness import NS, SCOPES

SOURCE = "sec-edgar-holdings"
WIDGETS = "sec-edgar:13dg:0009999701-25-000001:issuer"  # Exampla Widgets Inc. as named on the original 13G
RAIL = "sec-edgar:13dg:0009999601-25-000001:issuer"     # Northwind Rail Corp as named on the 13D
MANAGER = "sec-edgar:13f:0009999501-25-000001:manager"  # the 13F filing manager as named on its Q1 report
OTHER = "other-analyst"
OTHER_SCOPES = {"knowledge:ownership:read", f"namespace:{NS}:read"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)


def acquired(**kwargs) -> harness.Env:
    env = harness.Env()
    env.install()
    result = env.acquire("holdings", source_ids=[SOURCE], **kwargs)
    assert result["statuses"] == {"ownership": "complete"}
    return env


def review_sec(env) -> OwnershipIdentityService:
    """Accept the CIK and CUSIP-issuer candidates between the filings' own entity records."""
    service = OwnershipIdentityService(env.conn, now=env.now)
    proposed = service.propose(NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)["candidates"]
    assert proposed and {c["basis"] for c in proposed} == {"exact-identifier", "cross-referenced-identifier"}
    for candidate in proposed:
        assert candidate["left_key"].startswith("sec-edgar:13") and candidate["right_key"].startswith("sec-edgar:13")
        service.review(NS, candidate["candidate_id"], "accept", "CIK or CUSIP issuer number as stated agree",
                       principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)
    return service


def ask(env, name, entity, as_of, *, principal=harness.PRINCIPAL, scopes=SCOPES):
    return query(env.conn, NS, name, entity, principal_id=principal, scopes=scopes, as_of=as_of)


def cited(lines):
    assert lines and all(line["citation"]["accession_number"] and line["citation"]["record_id"]
                         and line["citation"]["document_url"].startswith("https://www.sec.gov/Archives/edgar/data/")
                         for line in lines)
    return {(line["citation"]["accession_number"],
             (line["position"] or {}).get("amount") or (line["share"] or {}).get("exact")) for line in lines}


def test_holders_of_an_issuer_as_of_a_date_cite_each_accession():
    env = acquired()
    review_sec(env)
    spring = ask(env, "holders", WIDGETS, "2025-05-01")
    # Q1 13F as restated by amendment 1 (125,000 shares), not the original 120,000 + put row.
    assert cited(spring["institutional_holdings"]) == {("0009999501-25-000002", "125000")}
    [position] = spring["institutional_holdings"]
    assert position["chain"]["applied_as"] == "restatement" and position["reference_date"] == "2025-03-31"
    assert position["position"]["value_unit"] == "USD" and position["position"]["voting_authority"]["sole"] == "125000"
    assert {(s["accession_number"], s["superseded_by"]) for s in spring["superseded"]} == {
        ("0009999501-25-000001", "0009999501-25-000002")}
    assert cited(spring["beneficial_ownership"]) == {("0009999701-25-000001", "6.1")}
    summer = ask(env, "holders", WIDGETS, "2025-07-15")
    assert cited(summer["institutional_holdings"]) == {("0009999501-25-000003", "90000")}
    [amended] = summer["beneficial_ownership"]
    assert cited([amended]) == {("0009999701-25-000002", "4.2")}
    assert amended["chain"]["is_amendment"] and amended["chain"]["amends_accession"] == "0009999701-25-000001"
    assert amended["powers"]["sole_voting"] == "2100000" and amended["holder"]["kind"] == "entity"
    assert any(s["accession_number"] == "0009999701-25-000001" and s["reason"].startswith("later statement")
               for s in summer["superseded"])
    early = ask(env, "holders", WIDGETS, "2024-12-01")
    assert early["institutional_holdings"] == [] and early["beneficial_ownership"] == []
    assert early["stated_after_as_of"] > 0
    assert "not a beneficial-ownership" in spring["notice"] and "never summed" in spring["filing_notice"]
    assert replay(env.conn, NS, summer, principal_id=harness.PRINCIPAL, scopes=SCOPES)["result_hash"] == summer["result_hash"]


def test_holdings_of_a_filer_across_issuers_follow_13f_amendment_types():
    env = acquired()
    review_sec(env)
    graph = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    spring = ask(env, "holdings", MANAGER, "2025-05-01")
    # One filer, two issuers, each resolved through reviewed CUSIP-issuer / CIK candidates.
    assert {(line["subject"], line["position"]["amount"]) for line in spring["institutional_holdings"]} == {
        (graph.cluster(WIDGETS), "125000"), (graph.cluster(RAIL), "75000")}
    assert cited(spring["institutional_holdings"]) == {("0009999501-25-000002", "125000"),
                                                       ("0009999501-25-000002", "75000")}
    summer = ask(env, "holdings", MANAGER, "2025-07-15")
    assert cited(summer["institutional_holdings"]) == {("0009999501-25-000003", "90000"),
                                                       ("0009999501-25-000004", "10000")}
    added = next(x for x in summer["institutional_holdings"] if x["position"]["amount"] == "10000")
    assert added["chain"]["applied_as"] == "new_holdings" and added["chain"]["amendment_number"] == "2"
    original_q1 = ask(env, "holdings", MANAGER, "2025-04-01")
    rows = {(x["position"]["cusip"], x["position"]["amount_type"], x["position"]["put_call"])
            for x in original_q1["institutional_holdings"]}
    assert ("99990BAA7", "PRN", None) not in rows  # the restatement dropped the notes row; it is superseded, not lost
    assert any(s["accession_number"] == "0009999501-25-000001" for s in original_q1["superseded"])


def test_13d_group_members_and_natural_person_minimisation():
    env = acquired()
    review_sec(env)
    mine = ask(env, "holders", RAIL, "2025-05-01")
    lines = mine["beneficial_ownership"]
    assert cited(lines) == {("0009999601-25-000001", "7.4"), ("0009999601-25-000001", "1.1")}
    assert len(lines) == 4 and {x["group"]["member_of_group"] for x in lines} == {"a"}
    partners = next(x for x in lines if x["holder"]["name"] == "Juniper Activist Partners LP (fictional)")
    members = partners["group"]["members"]
    assert {m["name"] for m in members if not m["withheld"]} == {"Juniper Activist GP LLC (fictional)",
                                                                 "Juniper Co-Investment Trust (fictional)"}
    assert [m for m in members if m["withheld"]] == [{"key": None, "name": None, "kind": "person", "withheld": True}]
    assert partners["powers"] == {"sole_voting": "0", "shared_voting": "3700000", "sole_dispositive": "0",
                                  "shared_dispositive": "3700000", "aggregate_amount": "3700000"}
    assert cited(mine["institutional_holdings"]) == {("0009999501-25-000002", "75000")}
    # The acquiring principal sees its own owner-scoped person line; another analyst sees it withheld but cited.
    assert any(x["holder"]["kind"] == "person" and x["holder"]["name"] for x in lines)
    theirs = ask(env, "holders", RAIL, "2025-05-01", principal=OTHER, scopes=OTHER_SCOPES)
    assert len(theirs["beneficial_ownership"]) == 3
    assert all(x["holder"]["kind"] == "entity" for x in theirs["beneficial_ownership"])
    assert [(w["accession_number"], w["holder_kind"]) for w in theirs["withheld"]] == [
        ("0009999601-25-000001", "person")]
    assert "Dana" not in str(theirs)


def test_amendments_are_new_records_and_replays_are_idempotent():
    env = acquired()
    store = OwnershipStore(env.conn)
    original = store.by_key(NS, "sec-edgar:13dg:0009999701-25-000001:reporting-person:0:stake",
                            principal_id=harness.PRINCIPAL, scopes=SCOPES)
    amendment = store.by_key(NS, "sec-edgar:13dg:0009999701-25-000002:reporting-person:0:stake",
                             principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert original["record"]["share"]["exact"] == "6.1" and original["revision"] == 1
    assert amendment["record"]["share"]["exact"] == "4.2"
    assert amendment["record"]["filing"]["amendment"]["chain_key"] == original["record"]["filing"]["amendment"]["chain_key"]
    assert amendment["record"]["native"]["five_percent_or_less"] is True
    revisions = env.conn.execute("SELECT count(*) FROM ownership_record_revisions").fetchone()[0]
    again = env.acquire("holdings-again", source_ids=[SOURCE])
    assert again["statuses"] == {"ownership": "complete"}
    assert env.conn.execute("SELECT count(*) FROM ownership_record_revisions").fetchone()[0] == revisions


def test_identity_review_is_what_widens_an_answer():
    env = acquired()
    spring = ask(env, "holders", WIDGETS, "2025-05-01")
    # Unreviewed, only this 13G's own statement answers; the 13F rows name the issuer in other records.
    assert cited(spring["beneficial_ownership"]) == {("0009999701-25-000001", "6.1")}
    assert spring["institutional_holdings"] == []
    summer = ask(env, "holders", WIDGETS, "2025-07-15")
    # The amendment chain is keyed by the CIKs the filings state, so the 13G/A supersedes the original even before
    # review; its issuer record joins this answer only once the CIK candidate is accepted.
    assert summer["beneficial_ownership"] == []
    assert [(s["accession_number"], s["superseded_by"]) for s in summer["superseded"]] == [
        ("0009999701-25-000001", "0009999701-25-000002")]
    service = OwnershipIdentityService(env.conn, now=env.now)
    service.propose(NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    pending = ask(env, "holders", WIDGETS, "2025-07-15")["identity"]["pending"]
    assert {c["basis"] for c in pending} == {"exact-identifier", "cross-referenced-identifier"}


def test_person_research_mode_stores_reporting_persons_unscoped(monkeypatch):
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    env = acquired()
    review_sec(env)
    theirs = ask(env, "holders", RAIL, "2025-05-01", principal=OTHER, scopes=OTHER_SCOPES)
    person = next(x for x in theirs["beneficial_ownership"] if x["holder"]["kind"] == "person")
    assert person["holder"]["name"] == "Dana Q. Fixture (fictional natural person)" and theirs["withheld"] == []
    stored = OwnershipStore(env.conn).by_key(NS, "sec-edgar:13dg:0009999601-25-000001:reporting-person:2",
                                             principal_id=OTHER, scopes=OTHER_SCOPES)
    assert stored["record"]["person_policy"] == "ownership-person-research-v1" and not stored["record"]["owner_scoped"]
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH")
    with pytest.raises(OwnershipRecordError) as refused:
        OwnershipStore(env.conn).apply(NS, [stored["record"]], run_id="replay", observed_at_ms=1, principal_id=OTHER)
    assert refused.value.code == "minimisation_violation"


def test_offline_evidence_is_labelled_and_never_live():
    assert LIVE_VERIFICATION["status"] == "unverified-live"
    fixture = harness.load("sec-edgar-holdings.json")
    assert fixture["authored"] is True and "fictional" in fixture["note"]
    assert {"13f-information-table", "13d-group-members", "13g-amendment-five-percent-or-less"} <= set(
        fixture["scenarios"])
