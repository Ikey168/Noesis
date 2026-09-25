"""Private applicant/project profiles (#1764)."""

import pytest

from src.kb.funding_profiles import FundingProfileError, FundingProfileStore
from tests.unit.funding.harness import NS, SCOPES, Env, founder_profile


def test_facts_are_sectioned_dated_and_never_defaulted():
    env = Env()
    store = FundingProfileStore(env.conn, now=env.now)
    created = store.create(NS, "p", label="Synthetic", principal_id="alice", scopes=SCOPES)
    assert created["sections"] == {"applicant": {}, "project": {}, "preferences": {}}
    view = store.inspect(NS, created["profile_id"], principal_id="alice", scopes=SCOPES)
    assert "applicant.residence_country" in view["unknown_facts"]
    updated = store.update(NS, created["profile_id"], "c1", 1, principal_id="alice", scopes=SCOPES, set_facts={
        "applicant.residence_country": {"value": "DE", "effective_from": "2024-01-01"},
        "project.stage": {"value": "prototype", "evidence": [{"kind": "document", "id": "doc:1", "revision": "r1"}]},
    })
    assert updated["sections"]["applicant"]["applicant.residence_country"]["review"] == "owner-reviewed"
    assert updated["sections"]["project"]["project.stage"]["evidence"][0]["id"] == "doc:1"
    with pytest.raises(FundingProfileError):
        store.update(NS, created["profile_id"], "c2", 2, principal_id="alice", scopes=SCOPES,
                     set_facts={"applicant.residence_country": {"value": None}})
    with pytest.raises(FundingProfileError):
        store.update(NS, created["profile_id"], "c3", 2, principal_id="alice", scopes=SCOPES,
                     set_facts={"applicant.favourite_colour": {"value": "blue"}})


def test_proposed_facts_stay_unknown_until_owner_review_and_history_is_kept():
    env = Env()
    store = FundingProfileStore(env.conn, now=env.now)
    profile = store.create(NS, "p", label="Synthetic", principal_id="alice", scopes=SCOPES)
    proposed = store.update(NS, profile["profile_id"], "import", 1, principal_id="alice", scopes=SCOPES,
                            propose_facts={"applicant.university_affiliation": {"value": True}})
    pinned = store.pinned_facts(NS, profile["profile_id"], 2, principal_id="alice", scopes=SCOPES)
    assert pinned["facts"] == {} and pinned["unreviewed"] == ["applicant.university_affiliation"]
    reviewed = store.update(NS, profile["profile_id"], "review", proposed["revision"], principal_id="alice", scopes=SCOPES,
                            review=["applicant.university_affiliation"])
    assert store.pinned_facts(NS, profile["profile_id"], reviewed["revision"], principal_id="alice", scopes=SCOPES)["facts"] == {
        "applicant.university_affiliation": True}
    assert store.inspect(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES, revision=2)["unreviewed_facts"]
    again = store.update(NS, profile["profile_id"], "review", proposed["revision"], principal_id="alice", scopes=SCOPES,
                         review=["applicant.university_affiliation"])
    assert again["idempotent"]
    with pytest.raises(FundingProfileError) as exc:
        store.update(NS, profile["profile_id"], "stale", 1, principal_id="alice", scopes=SCOPES, clear_facts=["project.stage"])
    assert exc.value.code == "revision_conflict"


def test_isolation_revocation_and_withdrawal():
    env = Env()
    profile = founder_profile(env)
    store = FundingProfileStore(env.conn, now=env.now)
    for principal, scopes in (("mallory", SCOPES), ("root", SCOPES | {"operator"})):
        with pytest.raises(FundingProfileError) as exc:
            store.inspect(NS, profile["profile_id"], principal_id=principal, scopes=scopes)
        assert exc.value.code == "unauthorized"
    assert store.list(NS, principal_id="mallory", scopes=SCOPES) == {"profiles": []}
    revoked = SCOPES - {"namespace:grants:read", "namespace:grants:write"}
    with pytest.raises(FundingProfileError):
        store.inspect(NS, profile["profile_id"], principal_id="alice", scopes=revoked)
    with pytest.raises(FundingProfileError):
        store.inspect(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES - {"knowledge:funding:read"})
    store.withdraw(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    with pytest.raises(FundingProfileError) as exc:
        store.inspect(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES, revision=1)
    assert exc.value.code == "profile_withdrawn"
