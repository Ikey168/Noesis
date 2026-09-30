"""Derived calls with coverage caveats (#2268, MV08) and reviewable movement identity (#2272, MV09)."""

from __future__ import annotations

import pytest

from src.osint.movements import (
    DWELL_SECONDS,
    MovementError,
    MovementIdentity,
    MovementStore,
    derive_calls,
    resolve_facility,
    statement,
)
from tests.unit.osint.movement_harness import (
    ALL,
    IMO,
    MMSI_NEW,
    MMSI_OLD,
    NS,
    Env,
    facilities,
    load_fisheries_gfw,
)

FAC = "movements-facilities"


@pytest.fixture(scope="module")
def env():
    env = Env().loaded()
    facilities(env.conn)
    load_fisheries_gfw(env.conn)
    env.derived = derive_calls(env.conn, NS, facilities_namespace=FAC, principal_id="analyst", scopes=ALL)
    return env


def _derived(env, subject):
    store = MovementStore(env.conn, initialize=False)
    return sorted((store.latest(NS, r["record_id"])["statement"] for r in
                   store.records(NS, record_type="call", provider="derived", subject_keys=[subject])),
                  key=lambda s: s["as_published"]["arrival"])


# ------------------------------------------------------------------ MV08


def test_derived_calls_cite_samples_geometry_and_method(env):
    harbour, bay = _derived(env, "vessel:mmsi:671000002")
    assert harbour["as_published"]["facility"]["name"] == "Example Harbour"
    assert harbour["as_published"]["status"] == "derived" and harbour["provider"] == "derived"
    assert harbour["as_published"]["dwell_seconds"] == 6 * 3600
    assert harbour["as_published"]["method"]["dwell_threshold_seconds"] == DWELL_SECONDS["port"]
    assert len(harbour["as_published"]["sample_record_ids"]) == 19
    assert len(harbour["as_published"]["spatial_receipts"]) == 19  # one geospatial relation receipt per sample
    assert harbour["as_published"]["facility"]["geometry_id"] and harbour["as_published"]["uncertain"] is False
    # The Example Bay stay starts right after an 8.7-hour receiver gap: the gap could hide or fake the arrival.
    assert bay["as_published"]["uncertain"] is True
    assert bay["as_published"]["uncertainty"][0]["relation"] == "adjacent to the call"
    assert bay["as_published"]["uncertainty"][0]["gap"]["kind"] == "no positions received"


def test_airport_calls_touching_window_edges_are_uncertain_and_published_calls_stay_separate(env):
    field_a, field_b = _derived(env, "aircraft:icao24:a0f1b2")
    assert (field_a["as_published"]["facility"]["name"], field_b["as_published"]["facility"]["name"]) == (
        "Example Field A", "Example Field B")
    assert field_a["as_published"]["uncertain"] and field_a["as_published"]["uncertainty"][0]["gap"]["kind"] == \
        "window edge without positions"
    store = MovementStore(env.conn, initialize=False)
    published = [r for r in store.records(NS, record_type="call", subject_keys=["aircraft:icao24:a0f1b2"])
                 if r["provider"] == "opensky"]
    assert len(published) == 2  # OpenSky's estimated airports stay source-published, never replaced


def test_no_coverage_windows_assert_nothing_and_unresolved_facilities_stay_unresolved(env):
    notes = {n["window_id"]: n["note"] for n in env.derived["notes"]}
    assert any(note.startswith("no_coverage_observed") and "no call is asserted or denied" in note
               for note in notes.values())
    assert not _derived(env, "vessel:mmsi:257000009") and not _derived(env, "aircraft:icao24:a0f1e5")
    assert resolve_facility(env.conn, FAC, "airport", "KEXA")["resolution"] == "resolved"
    assert resolve_facility(env.conn, FAC, "port", "EXAMPLE SOUTH ANCHORAGE") == {"resolution": "unresolved"}


def test_rederiving_adds_nothing_and_needs_the_geospatial_calculate_scope(env):
    again = derive_calls(env.conn, NS, facilities_namespace=FAC, principal_id="analyst", scopes=ALL)
    assert again["counts"]["created"] == 0 and again["counts"]["unchanged"] == again["derived"]
    with pytest.raises(MovementError, match="knowledge:geospatial:calculate"):
        derive_calls(env.conn, NS, facilities_namespace=FAC, principal_id="analyst", scopes={"knowledge:read"})


def test_a_stay_below_the_dwell_threshold_is_only_an_uncertain_call_when_a_gap_touches_it():
    env = Env()
    assert env.run(source_ids=["movements-opensky"])["status"] == "complete"  # no registry yet: nothing refused
    facilities(env.conn)
    derived = derive_calls(env.conn, NS, facilities_namespace=FAC, principal_id="a", scopes=ALL)
    assert derived["derived"] == 3
    (single,) = _derived(env, "aircraft:icao24:a0f1c3")  # one sample at the field, then the window edge gap
    assert single["as_published"]["dwell_seconds"] == 0 and single["as_published"]["uncertain"] is True
    assert any("below the threshold" in u.get("reason", "") for u in single["as_published"]["uncertainty"])


# ------------------------------------------------------------------ MV09


def _propose(env):
    return MovementIdentity(env.conn).propose(NS, principal_id="analyst", scopes=ALL, fisheries_namespace="global")


def _candidate(candidates, a, b):
    keys = {f"movements:{a}", f"movements:{b}"}
    return next(c for c in candidates if {c["left_key"], c["right_key"]} == keys)


def test_exact_identifier_matches_are_time_bounded_with_their_evidence(env):
    candidates = _propose(env)["candidates"]
    registry = _candidate(candidates, "aircraft:icao24:a0f1b2", "aircraft:registration:N901EX")
    assert registry["basis"] == "exact-identifier" and registry["state"] == "proposed"
    assert registry["evidence"][0]["stated_by"][0]["provider"] == "faa-registry"
    old = _candidate(candidates, f"vessel:imo:{IMO}", f"vessel:mmsi:{MMSI_OLD}")
    new = _candidate(candidates, f"vessel:imo:{IMO}", f"vessel:mmsi:{MMSI_NEW}")
    assert old["evidence"][0]["valid_to"] == "2024-12-31"  # re-flagging: the old MMSI ends
    assert {e["kind"] for e in new["evidence"]} == {"GFW self-reported identity segment", "vessel_identity"}
    fisheries = next(e for e in new["evidence"] if e["kind"].startswith("GFW"))
    assert fisheries["stated_by"][0]["store"] == "fisheries_revisions"  # shared with Fisheries by citation
    assert not any("competing" in e for e in old["evidence"] + new["evidence"])
    dereg = _candidate(candidates, "aircraft:icao24:a0f1d4", "aircraft:registration:N904EX")
    assert dereg["evidence"][0]["valid_to"] == "2099-03-01"


def test_natural_persons_are_never_matched_and_organisations_are_reviewable(env):
    env.conn.execute("CREATE TABLE IF NOT EXISTS canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name "
                     "TEXT NOT NULL, entity_type TEXT, created_at BIGINT NOT NULL)")
    env.conn.execute("INSERT OR IGNORE INTO canonical_entities VALUES ('org:example-air', 'Example Air Charter LLC', "
                     "'organization', 1), ('person:jane', 'Jane Q Example', 'person', 1)")
    identity = MovementIdentity(env.conn)
    organisations = {o["key"] for o in identity.organisations(NS)}
    assert "movements:org:example air charter llc" in organisations
    assert not any("jane" in key or "exmq" in key for key in organisations)
    candidates = _propose(env)["candidates"]
    org = next(c for c in candidates if c["right_key"] == "canonical:org:example-air" or
               c["left_key"] == "canonical:org:example-air")
    assert org["basis"] == "name-jurisdiction" and org["state"] == "proposed"
    assert not any("person:jane" in (c["left_key"], c["right_key"]) for c in candidates)


def test_review_is_recorded_reversible_and_only_accepted_matches_connect(env):
    identity = MovementIdentity(env.conn)
    candidates = _propose(env)["candidates"]
    link = _candidate(candidates, f"vessel:imo:{IMO}", f"vessel:mmsi:{MMSI_NEW}")
    before = identity.connected(NS, f"vessel:imo:{IMO}", "2025-03-01", "2025-03-31")
    assert f"vessel:mmsi:{MMSI_NEW}" not in before["subjects"] and before["proposed"]
    accepted = identity.review(NS, link["candidate_id"], "accept", "IMO and MMSI stated together by GFW and AIS",
                               principal_id="reviewer", scopes=ALL)
    assert accepted["state"] == "accepted" and accepted["decision_id"]
    joined = identity.connected(NS, f"vessel:imo:{IMO}", "2025-03-01", "2025-03-31")
    assert f"vessel:mmsi:{MMSI_NEW}" in joined["subjects"]
    # Outside the stated period the match does not join (MMSIs are reassigned).
    assert f"vessel:mmsi:{MMSI_NEW}" not in identity.connected(NS, f"vessel:imo:{IMO}", "2019-01-01",
                                                               "2019-01-31")["subjects"]
    reverted = identity.revert(NS, link["candidate_id"], "re-check", principal_id="reviewer", scopes=ALL)
    assert reverted["state"] == "reverted"
    assert f"vessel:mmsi:{MMSI_NEW}" not in identity.connected(NS, f"vessel:imo:{IMO}", "2025-03-01",
                                                               "2025-03-31")["subjects"]
    store = MovementStore(env.conn, initialize=False)  # the records themselves were never touched
    assert store.records(NS, subject_keys=[f"vessel:mmsi:{MMSI_NEW}"])


def test_mmsi_reuse_is_time_bounded_and_overlapping_claims_compete():
    env = Env()
    store = MovementStore(env.conn)
    src = {"url": None, "locator": "/", "evidence_origin": "fixture"}

    def pair(mmsi, imo, start, end, key):
        return statement("vessel_identity", "kystdatahuset-ais", f"vessel:mmsi:{mmsi}", key,
                         {"mmsi": mmsi, "imo": imo},
                         identifiers=[{"scheme": "mmsi", "value": mmsi},
                                      {"scheme": "imo", "value": imo, "valid_from": start, "valid_to": end}],
                         source=src, event="observed", effective_from=start, effective_to=end)

    store.observe(NS, [pair("627000002", "9000027", "2019-01-01", "2024-12-31", "a"),
                       pair("627000002", "9000039", "2025-06-01", "2025-12-31", "b"),  # reassigned later: no conflict
                       pair("671000002", "9000027", "2025-01-15", "2025-12-31", "c"),
                       pair("671000002", "9000065", "2025-05-01", "2025-05-31", "d")])  # overlapping: competing
    facts = MovementIdentity(env.conn).facts(NS, scopes=ALL)
    reuse = [f for f in facts if "vessel:mmsi:627000002" in (f["left"], f["right"])]
    assert len(reuse) == 2 and not any(f.get("competing") for f in reuse)
    clash = [f for f in facts if "vessel:mmsi:671000002" in (f["left"], f["right"])]
    assert all(f["competing"]["vessel:mmsi:671000002"] for f in clash)
    candidates = MovementIdentity(env.conn).propose(NS, principal_id="a", scopes=ALL)["candidates"]
    assert sum(1 for c in candidates if "movements:vessel:mmsi:671000002" in (c["left_key"], c["right_key"])) == 2
    assert all(c["state"] == "proposed" for c in candidates)  # competing candidates stay for review, never merged
