"""Reviewable composition- and phase-level material identity; polymorphs never match (MT08, #2086)."""

from __future__ import annotations

import pytest

from src.kb.materials_identity import MaterialsIdentity, phase_groups, same_space_group
from src.kb.materials_store import record_key
from tests.unit.materials import harness as h

OWN = {
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:ownership:review",
}
SCOPES = h.SCOPES | OWN
RUTILE = {
    record_key("materials-project", "mp-990001"),
    record_key("jarvis-dft", "JVASP-990101"),
    record_key("oqmd", "99000301"),
    record_key("cod", "9990001"),
}
ANATASE = {
    record_key("materials-project", "mp-990002"),
    record_key("jarvis-dft", "JVASP-990102"),
    record_key("oqmd", "99000302"),
    record_key("cod", "9990002"),
}


@pytest.fixture()
def env():
    return h.Env().load()


def pairs(candidates):
    return {frozenset((c["left_key"], c["right_key"])): c for c in candidates}


def test_candidates_come_from_stated_evidence_and_stay_proposed(env):
    identity = MaterialsIdentity(env.conn)
    result = identity.propose(h.NS, principal_id="alice", scopes=SCOPES)
    found = pairs(result["candidates"])
    assert all(c["state"] == "proposed" for c in found.values())
    mp_jarvis = found[
        frozenset(
            (
                record_key("materials-project", "mp-990001"),
                record_key("jarvis-dft", "JVASP-990101"),
            )
        )
    ]
    assert mp_jarvis["basis"] == "cross-referenced-identifier" and mp_jarvis[
        "evidence"
    ][0]["icsd"] == ["99001"]
    mp_cod = found[
        frozenset(
            (record_key("materials-project", "mp-990001"), record_key("cod", "9990001"))
        )
    ]
    assert mp_cod["basis"] == "structure-similarity"
    assert "not performed" in mp_cod["evidence"][0]["site_comparison"]
    assert result["composition_groups"]["O2Ti"]["level"] == "composition"
    assert phase_groups(env.conn, h.NS) == {}  # nothing is grouped before review


def test_polymorphs_are_never_proposed_at_phase_level(env):
    result = MaterialsIdentity(env.conn).propose(
        h.NS, principal_id="alice", scopes=SCOPES
    )
    for key in pairs(result["candidates"]):
        assert key <= RUTILE or key <= ANATASE, key
    assert result["not_proposed_polymorphs"]
    assert all(
        p["reason"].startswith("different space groups")
        for p in result["not_proposed_polymorphs"]
    )
    assert same_space_group({"symbol": "P 42/m n m"}, {"symbol": "P4_2/mnm"}) is True
    assert same_space_group({"number": 136}, {"number": 141}) is False
    assert same_space_group({"symbol": "P4_2/mnm"}, {}) is None


def test_review_groups_records_and_revert_never_reactivates(env):
    identity = MaterialsIdentity(env.conn)
    found = pairs(
        identity.propose(h.NS, principal_id="alice", scopes=SCOPES)["candidates"]
    )
    candidate = found[
        frozenset(
            (
                record_key("materials-project", "mp-990001"),
                record_key("jarvis-dft", "JVASP-990101"),
            )
        )
    ]
    accepted = identity.review(
        h.NS,
        candidate["candidate_id"],
        "accept",
        "same ICSD code",
        principal_id="bob",
        scopes=SCOPES,
    )
    assert accepted["state"] == "accepted"
    groups = phase_groups(env.conn, h.NS)
    assert groups[candidate["left_key"]] == groups[candidate["right_key"]]
    reverted = identity.revert(
        h.NS,
        candidate["candidate_id"],
        "reviewer changed their mind",
        principal_id="bob",
        scopes=SCOPES,
    )
    assert reverted["state"] == "reverted" and phase_groups(env.conn, h.NS) == {}
    identity.propose(
        h.NS, principal_id="alice", scopes=SCOPES
    )  # same evidence: stays reverted
    assert (
        pairs(identity.candidates(h.NS, scopes=SCOPES))[
            frozenset((candidate["left_key"], candidate["right_key"]))
        ]["state"]
        == "reverted"
    )


def test_records_are_never_merged_and_proposing_is_idempotent(env):
    identity = MaterialsIdentity(env.conn)
    first = identity.propose(h.NS, principal_id="alice", scopes=SCOPES)
    again = identity.propose(h.NS, principal_id="alice", scopes=SCOPES)
    assert first["proposed"] and again["proposed"] == []
    subjects = {s["record_key"]: s for s in identity.subjects(h.NS)}
    assert subjects[record_key("oqmd", "99000301")]["icsd"] == {
        "99001"
    }  # each record keeps its own identifiers
