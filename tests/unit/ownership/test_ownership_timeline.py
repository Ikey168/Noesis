"""Corporate-event and filing timeline with as-of selection (#1859)."""

import pytest

from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_records import record
from src.kb.ownership_store import OwnershipStore, canonical_entity_id
from src.kb.ownership_timeline import state_as_of, timeline
from src.kb.temporal import record_temporal_assertion
from tests.unit.ownership import harness
from tests.unit.ownership.harness import NS, SCOPES, UK_KEYS


@pytest.fixture()
def linked():
    env = harness.Env().ready()
    market = harness.seed_market(env.conn)
    service = OwnershipIdentityService(env.conn, now=env.now)
    for item in service.propose(NS, principal_id=harness.PRINCIPAL, scopes=SCOPES, market=market, lei_namespace=NS)["candidates"]:
        if harness.DECOY not in (item["left_key"], item["right_key"]):
            service.review(NS, item["candidate_id"], "accept", "fixture", principal_id=harness.REVIEWER,
                           scopes=harness.REVIEW_SCOPES)
    return env, market


def test_entries_cite_source_event_time_and_record_time(linked):
    env, market = linked
    record_temporal_assertion(env.conn, domain="ownership-notes", backing="namespace", assertion_kind="entity",
                              assertion_id="note:rename-announced", observed_at_ms=harness.ms("2025-02-01T00:00:00Z"),
                              valid_from_ms=harness.ms("2025-02-01T00:00:00Z"), valid_time_precision="day",
                              payload={"canonical_entity_id": canonical_entity_id(UK_KEYS["ch"]),
                                       "summary": "Rebrand announced (fixture temporal assertion)"})
    line = timeline(env.conn, NS, UK_KEYS["gleif"], principal_id=harness.PRINCIPAL, scopes=SCOPES, market=market)
    kinds = {e["kind"] for e in line["entries"]}
    assert kinds == {"registration", "event", "filing", "officer", "ownership", "market_corporate_action", "temporal_assertion"}
    for entry in line["entries"]:
        assert entry["event_time"] and entry["record_time_ms"] and entry["source"]["provider"]
    times = [e["event_time"] for e in line["entries"]]
    assert times == sorted(times)
    action = next(e for e in line["entries"] if e["kind"] == "market_corporate_action")
    assert action["event_time"] == "2025-05-20" and action["source"]["store"] == "src.domains.market.actions"
    note = next(e for e in line["entries"] if e["kind"] == "temporal_assertion")
    assert note["event_time"] == "2025-02-01" and note["source"]["store"] == "src.kb.temporal"


def test_unknown_dates_are_undated_never_interpolated(linked):
    env, market = linked
    line = timeline(env.conn, NS, UK_KEYS["ch"], principal_id=harness.PRINCIPAL, scopes=SCOPES)
    undated = {(e["kind"], e["summary"]) for e in line["undated"]}
    assert ("officer", "POE, Kim appointed secretary") in undated
    assert any(kind == "event" and summary.startswith("succession") for kind, summary in undated)
    assert all(e["date_status"] == "unknown" and e["event_time"] is None for e in line["undated"])
    assert "never interpolated" in line["note"]


def test_state_as_of_reconstructs_registration_officers_and_ownership(linked):
    env, _ = linked
    early = state_as_of(env.conn, NS, UK_KEYS["gleif"], "2018-01-01", principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert [o["officer"] for o in early["officers"]] == ["DOE, Alex"]
    assert [o["officer"] for o in early["officers_with_unknown_tenure"]] == ["POE, Kim"]
    assert {r["register"] for r in early["registrations"]} == {"Companies House"}
    assert early["direct_parents"]["conflicts"] == []
    late = state_as_of(env.conn, NS, UK_KEYS["gleif"], "2025-06-01", principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert [o["officer"] for o in late["officers"]] == ["ROE, Sam"]
    assert late["direct_parents"]["conflicts"]


def test_state_as_of_uses_revisions_known_at_the_record_time(linked):
    env, _ = linked
    known = env.now()
    source = {"provider": "companies-house", "provider_record_id": "09990002:officers"}
    OwnershipStore(env.conn, now=env.now).put(NS, [record(
        "officer_role", "companies-house:officer:09990002:FIXOFF002:director", source, entity_key=UK_KEYS["ch"],
        officer={"name": "ROE, Sam", "key": "companies-house:officer:FIXOFF002", "kind": "person"}, role="director",
        appointed_on="2021-06-01", resigned_on="2025-03-01")], run_id="correction", principal_id="p", scopes=SCOPES)
    now = state_as_of(env.conn, NS, UK_KEYS["gleif"], "2025-06-01", principal_id=harness.PRINCIPAL, scopes=SCOPES)
    then = state_as_of(env.conn, NS, UK_KEYS["gleif"], "2025-06-01", principal_id=harness.PRINCIPAL, scopes=SCOPES,
                       known_at_ms=known)
    assert [o["officer"] for o in now["officers"]] == []
    assert [o["officer"] for o in then["officers"]] == ["ROE, Sam"]
