"""Citation links to sanctions listings (#2276, MV10) and bounded as-of movement answers (#2279, MV11)."""

from __future__ import annotations

import json

import pytest

from src.osint.movements import (
    NO_COVERAGE,
    MovementError,
    MovementIdentity,
    MovementLinks,
    MovementQueries,
    derive_calls,
    forbidden_keys,
)
from tests.unit.osint.movement_harness import (
    ALL,
    IMO,
    MMSI_NEW,
    NS,
    VESSEL,
    Env,
    facilities,
    load_fisheries_all,
    seed_sanctions,
)

FAC = "movements-facilities"
ACCEPT = [(f"vessel:imo:{IMO}", f"vessel:mmsi:{MMSI_NEW}"), (f"vessel:gfw:{VESSEL}", f"vessel:imo:{IMO}"),
          ("aircraft:icao24:a0f1b2", "aircraft:registration:N901EX")]


def journey_env() -> Env:
    """Acquired sources, facilities, Fisheries records, sanctions snapshots, derived calls and reviewed matches."""
    env = Env().loaded()
    facilities(env.conn)
    load_fisheries_all(env.conn)
    seed_sanctions(env.conn, delisted=True)
    derive_calls(env.conn, NS, facilities_namespace=FAC, principal_id="analyst", scopes=ALL)
    identity = MovementIdentity(env.conn)
    candidates = identity.propose(NS, principal_id="analyst", scopes=ALL, fisheries_namespace="global")["candidates"]
    for a, b in ACCEPT:
        candidate = next(c for c in candidates
                         if {c["left_key"], c["right_key"]} == {f"movements:{a}", f"movements:{b}"})
        identity.review(NS, candidate["candidate_id"], "accept", "identifiers stated together by the cited source",
                        principal_id="reviewer", scopes=ALL)
    env.links = MovementLinks(env.conn).link(NS, principal_id="analyst", scopes=ALL, sanctions_namespace="legal",
                                             fisheries_namespace="global")
    return env


@pytest.fixture(scope="module")
def env():
    return journey_env()


def ask(env, identifier, start, end, **kwargs):
    return MovementQueries(env.conn).window(NS, identifier, start, end, scopes=ALL, facilities_namespace=FAC,
                                            fisheries_namespace="global", sanctions_namespace="legal", **kwargs)


# ------------------------------------------------------------------ MV10


def test_links_need_an_explicit_identifier_in_the_listing(env):
    sanctions = {(link["subject_key"], link["evidence"]["list_entry_id"], link["scheme"])
                 for link in env.links["links"] if link["target"] == "sanctions"}
    assert ("vessel:mmsi:671000002", "SYN-MV-1", "imo") in sanctions
    assert ("aircraft:registration:N901EX", "SYN-MV-2", "registration") in sanctions
    assert not any(entry == "SYN-MV-3" for _, entry, _ in sanctions)  # name-only: never a link
    link = next(link for link in env.links["links"] if link["evidence"].get("list_entry_id") == "SYN-MV-1")
    assert link["evidence"]["stated_identifier"]["kind"] == "imo" and link["evidence"]["source_revision"]


def test_name_only_similarity_is_a_candidate_that_cannot_be_accepted(env):
    (candidate,) = [c for c in env.links["name_candidates"] if c["designation_id"]]
    assert candidate["name"] == "SAMPLE STAR" and candidate["basis"] == "similar-name"
    from src.kb.ownership_store import OwnershipError

    with pytest.raises(OwnershipError, match="never produces an accepted match"):
        MovementIdentity(env.conn).review(NS, candidate["candidate_id"], "accept", "names look alike",
                                          principal_id="reviewer", scopes=ALL)


def test_other_pack_records_are_linked_only_where_they_state_the_identifier(env):
    fisheries = [link for link in env.links["links"] if link["target"] == "fisheries"]
    assert fisheries and all(link["identifier"] == IMO for link in fisheries)
    assert {link["evidence"]["record_type"] for link in fisheries} == {"authorisation", "listing"}


def test_linking_is_idempotent(env):
    again = MovementLinks(env.conn).link(NS, principal_id="analyst", scopes=ALL, sanctions_namespace="legal",
                                         fisheries_namespace="global")
    assert again["created"] == [] and len(again["links"]) == len(env.links["links"])


def test_listing_statements_follow_the_list_revisions_including_delisting(env):
    links = MovementLinks(env.conn)
    listed = links.statements(NS, ["vessel:mmsi:671000002"], "2025-03-01", scopes=ALL)
    vessel = next(s for s in listed if s["link"]["target"] == "sanctions")
    assert vessel["status"] == "listed" and not forbidden_keys(vessel)  # a listing as published, no verdict
    between = links.statements(NS, ["vessel:mmsi:671000002"], "2025-04-01", scopes=ALL)
    assert next(s for s in between if s["link"]["target"] == "sanctions")["status"] == "unknown"  # nothing inferred
    later = links.statements(NS, ["vessel:mmsi:671000002"], "2025-10-01", scopes=ALL)
    delisted = next(s for s in later if s["link"]["target"] == "sanctions")
    assert delisted["status"] == "not_listed_in_snapshot" and delisted["delisting"]["change"] == "delisted"


# ------------------------------------------------------------------ MV11


def test_vessel_answer_joins_registry_identity_windows_calls_and_listings(env):
    answer = ask(env, f"IMO {IMO}", "2025-03-01", "2025-03-31")
    assert answer["identifier"] == {"scheme": "imo", "value": IMO}
    assert {f"vessel:mmsi:{MMSI_NEW}", f"vessel:gfw:{VESSEL}"} <= set(answer["subjects"])
    assert {m["to"] for m in answer["identity_matches"]["accepted"]} >= {f"vessel:mmsi:{MMSI_NEW}"}
    assert answer["vessel_identity"] and all(f["stated_by"][0]["store"] == "fisheries_revisions"
                                             for f in answer["vessel_identity"])
    ais = next(w for w in answer["sample_windows"] if w["provider"] == "kystdatahuset-ais")
    assert ais["coverage"]["status"] == "positions_observed" and ais["coverage"]["gaps"]
    assert ais["samples"] and all(s["revision_id"] for s in ais["samples"])
    published = answer["calls"]["source_published"]
    assert [c["event_id"] for c in published] == ["synthetic-pv-0001"] and published[0]["confidence"] == "4"
    derived = answer["calls"]["derived"]
    assert {c["facility"]["name"] for c in derived} == {"Example Harbour", "Example Bay"}
    assert any(c["uncertain"] for c in derived)
    listing = next(s for s in answer["sanctions"] if s["link"]["target"] == "sanctions")
    assert listing["status"] == "unknown" and answer["as_of"] == "2025-03-31"  # between two list snapshots
    on_snapshot = ask(env, f"IMO {IMO}", "2025-03-01", "2025-03-31", as_of="2025-03-01")
    assert next(s for s in on_snapshot["sanctions"] if s["link"]["target"] == "sanctions")["status"] == "listed"
    assert answer["coverage"]["status"] == "partial" and not forbidden_keys(answer)
    later = ask(env, f"IMO {IMO}", "2025-03-01", "2025-03-31", as_of="2025-10-01")
    assert next(s for s in later["sanctions"] if s["link"]["target"] == "sanctions")["status"] == \
        "not_listed_in_snapshot"


def test_aircraft_answer_has_registry_samples_and_both_kinds_of_calls(env):
    answer = ask(env, "a0f1b2", "2099-05-01T00:00:00Z", "2099-05-02T00:00:00Z")
    (registry,) = answer["registry"]
    assert registry["record_key"] == "faa:N901EX" and registry["citation"]["provider"] == "faa-registry"
    (window,) = answer["sample_windows"]
    assert len(window["samples"]) == 10 and window["coverage"]["gaps"]
    codes = {(c["event"], c["facility"]["code"], c["facility"]["resolution"]) for c in
             answer["calls"]["source_published"]}
    assert codes == {("departure", "KEXA", "resolved"), ("arrival", "KEXB", "resolved")}
    assert len(answer["calls"]["derived"]) == 2 and all(c["status"] == "derived" for c in answer["calls"]["derived"])
    assert any(s["link"]["scheme"] == "registration" for s in answer["sanctions"])


def test_no_coverage_is_reported_as_no_coverage_observed(env):
    empty = ask(env, "257000009", "2025-03-10", "2025-03-11")
    assert empty["coverage"]["statement"] == NO_COVERAGE == "no coverage observed"
    assert empty["sample_windows"][0]["coverage"]["statement"] == NO_COVERAGE
    unknown = ask(env, "a0f1aa", "2099-05-01", "2099-05-02")
    assert unknown["coverage"]["status"] == "no_coverage_observed" and not unknown["sample_windows"]
    text = json.dumps(unknown).casefold()
    assert "did not move" in text and "never evidence" in text  # only ever inside the caveat's negation


@pytest.mark.parametrize(("identifier", "start", "end", "code"), [
    ("N907EX", "2099-05-01", "2099-05-02", "privacy_opt_out"),
    ("a0f1f6", "2099-05-01", "2099-05-02", "privacy_opt_out"),
    ("a0f1c3", "2099-05-01", "2099-05-02", "private_aircraft_refused"),
    ("Jane Q Example", "2099-05-01", "2099-05-02", "person_identifier_refused"),
    ("a0f1b2", "2099-01-01", "2099-05-01", "over_bound"),
])
def test_refusals_state_their_reason(env, identifier, start, end, code):
    with pytest.raises(MovementError) as refused:
        ask(env, identifier, start, end)
    assert refused.value.code == code and str(refused.value)


def test_the_movement_scope_is_required_and_registry_answers_withhold_natural_persons(env):
    with pytest.raises(MovementError, match="knowledge:osint:movements"):
        MovementQueries(env.conn).window(NS, "a0f1b2", "2099-05-01", "2099-05-02", scopes={"knowledge:read"})
    registry = MovementQueries(env.conn).registry(NS, "N902EX", scopes={"knowledge:read"}, as_of="2099-05-01")
    (entry,) = registry["registry"]
    assert entry["as_published"]["registrant"]["name"] == "withheld: a natural person"
    assert "JANE" not in json.dumps(registry)
    dereg = MovementQueries(env.conn).registry(NS, "N904EX", scopes={"knowledge:read"}, as_of="2099-06-01")
    assert dereg["registry"][0]["event"] == "deregistered"


def test_answers_export_as_evidence_bundles_with_source_revision_and_as_of(env):
    queries = MovementQueries(env.conn)
    answer = ask(env, f"IMO {IMO}", "2025-03-01", "2025-03-31")
    exported = queries.export_bundle(answer)
    objects = exported["bundle"]["objects"]
    kinds = {o["payload"].get("kind") for o in objects}
    assert {"movement-sample-window", "movement-position-sample", "movement-call-derived",
            "movement-call-source_published", "movement-identity-match", "movement-listing-statement",
            "movement-answer"} <= kinds
    evidence = [o for o in objects if o["type"] == "evidence"]
    assert all(o["payload"]["as_of"] == "2025-03-31" for o in evidence)
    assert queries.export_bundle(answer) == exported  # deterministic
