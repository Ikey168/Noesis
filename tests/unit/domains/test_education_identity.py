"""IPEDS and ETER institutions matched to ROR through reviewable identity (#2414)."""

from __future__ import annotations

import pytest

from src.kb.education_identity import EducationIdentity, ror_url
from src.kb.education_statistics import EducationError
from src.kb.review_targets import ReviewTargets
from tests.unit import education_harness as h

R1, R2, R3, R4 = (f"https://ror.org/{i}" for i in ("0zfs01a23", "0zmc02b34", "0zth03c45", "0zfs04d56"))


@pytest.fixture()
def identity():
    conn = h.connection()
    h.load_all(conn)
    identity = EducationIdentity(conn)
    identity.record_ror(h.NS, h.ror_records(), principal_id="svc", scopes=h.SCOPES)
    yield identity
    conn.close()


def by_subject(result, code):
    return {m["ror_id"]: m for m in result["matches"] if m["subject"]["code"] == code}


def test_published_identifiers_are_exact_and_names_are_candidates(identity):
    result = identity.propose(h.NS, principal_id="svc", scopes=h.SCOPES)
    stated = by_subject(result, "DE0002")[R3]
    assert (stated["basis"], stated["state"], stated["exact"]) == ("source-stated-ror", "exact", True)
    external = by_subject(result, "DE0001")[R2]
    assert (external["basis"], external["state"]) == ("ror-external-id", "exact")
    assert external["evidence"]["shared_identifiers"] == [{"type": "wikidata", "value": "q999000001"}]
    candidates = by_subject(result, "100001")
    assert set(candidates) == {R1, R4} and {m["state"] for m in candidates.values()} == {"candidate"}
    assert candidates[R1]["evidence"]["same_city"] is True and candidates[R4]["evidence"]["same_city"] is False
    assert {m["state"] for m in by_subject(result, "100002").values()} == {"unmatched"}
    assert {m["state"] for m in by_subject(result, "FR0001").values()} == {"unmatched"}
    again = identity.propose(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert again["created"] == []


def test_candidates_are_used_only_once_accepted_and_rejected_ones_never(identity):
    result = identity.propose(h.NS, principal_id="svc", scopes=h.SCOPES)
    candidates = by_subject(result, "100001")
    assert identity.institutions_for_ror(h.NS, R1)["subjects"] == []
    assert identity.ror_for(h.NS, "ipeds-unitid", "100001")["status"] == "candidates_pending"
    accepted = identity.review(h.NS, candidates[R1]["match_id"], "accept", "same campus city and name",
                               principal_id="reviewer", scopes=h.SCOPES)
    rejected = identity.review(h.NS, candidates[R4]["match_id"], "reject", "different city",
                               principal_id="reviewer", scopes=h.SCOPES)
    assert accepted["state"] == "accepted" and rejected["state"] == "rejected"
    assert identity.institutions_for_ror(h.NS, R1)["subjects"] == [{"scheme": "ipeds-unitid", "code": "100001"}]
    assert identity.institutions_for_ror(h.NS, R4)["subjects"] == []
    assert identity.institutions_for_ror(h.NS, R4)["not_used"][0]["state"] == "rejected"
    assert identity.ror_for(h.NS, "ipeds-unitid", "100001")["ror_id"] == R1
    # The decision is an entity identity decision the review inbox can inspect.
    target = {"kind": "entity", "namespace": h.NS, "id": accepted["decision_id"]}
    inspected = ReviewTargets(identity.conn).inspect(target, scopes={"operator"})
    assert inspected["record"]["decision_type"] == "match"
    reverted = identity.revert(h.NS, accepted["match_id"], "wrong campus", principal_id="reviewer", scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and identity.institutions_for_ror(h.NS, R1)["subjects"] == []
    with pytest.raises(EducationError) as caught:
        identity.review(h.NS, rejected["match_id"], "accept", "again", principal_id="reviewer", scopes=h.SCOPES)
    assert caught.value.code == "invalid_state"


def test_ror_status_changes_and_successors_are_reported_not_repointed(identity):
    identity.propose(h.NS, principal_id="svc", scopes=h.SCOPES)
    before = identity.ror_changes(h.NS, R3)
    assert [c["kind"] for c in before] == ["successor"] and before[0]["related_ror_id"] == R2
    changed = identity.record_ror(h.NS, h.ror_records(later=True), principal_id="svc", scopes=h.SCOPES)
    assert changed["changes"] == [{"ror_id": R3, "status": {"before": "active", "after": "inactive"}}]
    after = identity.ror_changes(h.NS, R3)
    assert {c["kind"] for c in after} == {"status", "successor", "status_changed"}
    lookup = identity.institutions_for_ror(h.NS, R3)
    assert lookup["subjects"] == [{"scheme": "eter-id", "code": "DE0002"}]
    # The successor gains nothing through the change.
    assert identity.institutions_for_ror(h.NS, R2)["subjects"] == [{"scheme": "eter-id", "code": "DE0001"}]


def test_a_ror_client_search_supplies_name_candidates_and_scopes_are_enforced(identity):
    class Client:
        calls = []

        def search(self, query):
            self.calls.append(query)
            return {"status": "candidates", "candidates": []}

    client = Client()
    identity.propose(h.NS, principal_id="svc", scopes=h.SCOPES, ror_client=client)
    assert "Example Technical College" in client.calls
    with pytest.raises(EducationError):
        identity.propose(h.NS, principal_id="svc", scopes=h.READ_ONLY)
    with pytest.raises(EducationError):
        ror_url("not-a-ror")
