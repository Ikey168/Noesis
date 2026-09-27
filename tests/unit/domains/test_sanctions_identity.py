"""Reviewable, non-destructive matches between designations and corporate/entity identity (#1954)."""

from __future__ import annotations

import pytest

from src.kb.entity_history import EntityHistoryStore
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_records import record
from src.kb.ownership_store import OwnershipError, OwnershipStore
from src.kb.sanctions import SanctionsError, SanctionsStore
from src.kb.sanctions_identity import SanctionsIdentity
from src.kb.sanctions_queries import SanctionsQueries
from tests.unit import sanctions_harness as h

HISTORY = {"knowledge:entity-history:read"}


@pytest.fixture()
def lists():
    conn = h.connection()
    for list_id, files in h.FILES.items():
        for name in files:
            h.apply(conn, list_id, name)
    yield conn
    conn.close()


def state(conn):
    return {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        for t in (
            "sanctions_designations",
            "sanctions_revisions",
            "sanctions_aliases",
            "sanctions_snapshot_members",
        )
    }


def designation(conn, list_id, entry_id):
    return conn.execute(
        "SELECT designation_id FROM sanctions_designations WHERE list_id=? AND list_entry_id=?",
        [list_id, entry_id],
    ).fetchone()[0]


def test_cross_list_candidates_rest_on_stated_identifiers_with_revisions(lists):
    identity = SanctionsIdentity(lists)
    proposed = identity.propose("global", principal_id="analyst", scopes=h.SCOPES)
    pairs = {tuple(c["records"]): c for c in proposed["candidates"]}
    assert ("sanctions:eu:EU.9003.03", "sanctions:un:QDi.902") in pairs  # same passport
    assert (
        "sanctions:eu:EU.9002.02",
        "sanctions:ofac:99001",
    ) in pairs  # same IMO number
    assert (
        "sanctions:eu:EU.9001.01",
        "sanctions:uk:RUS9001",
    ) in pairs  # same registration number
    for candidate in pairs.values():
        assert (
            candidate["state"] == "proposed"
            and candidate["basis"] == "exact-identifier"
        )
        evidence = candidate["evidence"][0]
        assert evidence["left"]["revision_id"] and evidence["right"]["snapshot_id"]
        assert candidate["source_revisions_compared"]
    # Two entries sharing only a similar name are never proposed automatically.
    assert ("sanctions:un:QDe.901", "sanctions:uk:RUS9001") not in pairs
    again = identity.propose("global", principal_id="analyst", scopes=h.SCOPES)
    assert again["proposed"] == []  # idempotent


def test_accept_and_revert_link_records_without_rewriting_either_side(lists):
    identity = SanctionsIdentity(lists)
    identity.propose("global", principal_id="analyst", scopes=h.SCOPES)
    target = next(
        c
        for c in identity.candidates("global", scopes=h.SCOPES)
        if c["records"] == ["sanctions:eu:EU.9002.02", "sanctions:ofac:99001"]
    )
    before = state(lists)
    with pytest.raises(OwnershipError):  # the review scope is required
        identity.service.review(
            "global",
            target["candidate_id"],
            "accept",
            "same IMO",
            principal_id="analyst",
            scopes=h.SCOPES,
        )
    accepted = identity.view(
        identity.service.review(
            "global",
            target["candidate_id"],
            "accept",
            "same IMO number on both lists",
            principal_id="reviewer",
            scopes=h.REVIEW_SCOPES,
        )
    )
    assert (
        accepted["state"] == "accepted"
        and accepted["decision_id"]
        and accepted["reviewer"] == "reviewer"
    )
    assert (
        accepted["reason"] == "same IMO number on both lists" and state(lists) == before
    )
    decisions = EntityHistoryStore(lists).history(
        "global", accepted["entities"][0], scopes=HISTORY
    )["items"]
    assert decisions and decisions[0]["decision_type"] == "match"
    answer = SanctionsQueries(lists).history_as_of(
        "global", "2026-03-05", scopes=h.SCOPES, identifier="9999991"
    )
    eu = answer["lists"]["eu"][0]
    assert eu["identity"]["links"][0]["candidate_id"] == target["candidate_id"]
    assert sorted(answer["lists"]) == [
        "eu",
        "ofac",
        "uk",
    ]  # linked lists still answer separately
    reverted = identity.view(
        identity.service.revert(
            "global",
            target["candidate_id"],
            "reopen review",
            principal_id="reviewer",
            scopes=h.REVIEW_SCOPES,
        )
    )
    assert reverted["state"] == "reverted" and state(lists) == before
    after = SanctionsQueries(lists).history_as_of(
        "global", "2026-03-05", scopes=h.SCOPES, identifier="9999991"
    )
    assert after["lists"]["eu"][0]["identity"]["links"] == []


def test_a_similar_name_alone_is_a_candidate_that_can_never_be_accepted(lists):
    identity = SanctionsIdentity(lists)
    un_entity = designation(lists, "un", "QDe.901")
    candidate = identity.propose_link(
        "global",
        un_entity,
        target_key="entity:examplar",
        target_entity="ent-examplar",
        evidence={
            "kind": "name",
            "value": "Examplar Freight LLC",
            "target_source": "news document doc-1",
        },
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    view = next(
        c
        for c in identity.candidates("global", scopes=h.SCOPES)
        if c["candidate_id"] == candidate["candidate_id"]
    )
    assert view["basis"] == "similar-name" and view["state"] == "proposed"
    with pytest.raises(OwnershipError) as caught:
        identity.service.review(
            "global",
            candidate["candidate_id"],
            "accept",
            "looks the same",
            principal_id="reviewer",
            scopes=h.REVIEW_SCOPES,
        )
    assert caught.value.code == "insufficient_evidence"
    rejected = identity.service.review(
        "global",
        candidate["candidate_id"],
        "reject",
        "name only",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    assert rejected["state"] == "rejected"


def test_reviewer_evidence_must_be_stated_by_the_list(lists):
    identity = SanctionsIdentity(lists)
    person = designation(lists, "eu", "EU.9003.03")
    with pytest.raises(SanctionsError) as caught:
        identity.propose_link(
            "global",
            person,
            target_key="k",
            target_entity="ent-k",
            evidence={"kind": "passport", "value": "Z0000000", "target_source": "doc"},
            principal_id="analyst",
            scopes=h.SCOPES,
        )
    assert caught.value.code == "not_stated"
    corroborated = identity.propose_link(
        "global",
        person,
        target_key="entity:ivan",
        target_entity="ent-ivan",
        evidence={
            "kind": "name",
            "value": "Ivan Fictional-Example",
            "target_source": "registry extract",
            "attributes": [{"kind": "date_of_birth", "value": "1970-01-01"}],
        },
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    view = next(
        c
        for c in identity.candidates("global", scopes=h.SCOPES, designation_id=person)
        if c["candidate_id"] == corroborated["candidate_id"]
    )
    assert view["basis"] == "name-jurisdiction"
    assert view["evidence"][0]["corroborating_attributes"] == [
        {"kind": "date_of_birth", "value": "1970-01-01"}
    ]


def test_ownership_records_match_only_by_identifiers_within_their_register_country():
    conn = h.connection()
    store = SanctionsStore(conn)
    entry = {
        "list_id": "uk",
        "entry_id": "GBR9001",
        "party_kind": "entity",
        "names": [
            {
                "name": "Fixture Holdings Ltd",
                "kind": "primary",
                "quality": None,
                "script": None,
                "language": None,
            }
        ],
        "identifiers": [
            {
                "kind": "registration_number",
                "value": "09990002",
                "source_type": "Company number",
                "country": "GB",
                "note": None,
            },
            {
                "kind": "registration_number",
                "value": "09990003",
                "source_type": "Company number",
                "country": "FR",
                "note": None,
            },
        ],
        "addresses": [],
        "programmes": [],
        "legal_basis": [],
        "dates_of_birth": [],
        "nationalities": [],
        "remarks": [],
        "listed_on": None,
        "amended_on": None,
        "cross_references": {},
    }
    store.apply_snapshot(
        "global",
        {
            "list_id": "uk",
            "publication_date": "2026-03-15",
            "file_sha256": "f" * 64,
            "entry_count": 1,
            "format": "uk-sanctions-list-xml",
        },
        [entry],
        run_id="r",
        source_id="uk-sanctions-list",
    )
    source = {"provider": "companies-house", "provider_record_id": "09990002:profile"}
    records = [
        record(
            "legal_entity",
            "companies-house:gb-coh:09990002",
            source,
            name="Fixture Holdings Ltd",
            jurisdiction="GB",
            identifiers=[{"scheme": "gb-coh", "value": "09990002"}],
        ),
        record(
            "legal_entity",
            "companies-house:gb-coh:09990003",
            {**source, "provider_record_id": "3"},
            name="Other Ltd",
            jurisdiction="GB",
            identifiers=[{"scheme": "gb-coh", "value": "09990003"}],
        ),
    ]
    OwnershipStore(conn).apply(
        "global", records, run_id="o", observed_at_ms=1, principal_id="p"
    )
    before = OwnershipStore(conn).records("global", principal_id="p", scopes=h.SCOPES)
    result = SanctionsIdentity(conn).propose(
        "global", principal_id="analyst", scopes=h.SCOPES, ownership_namespace="global"
    )
    records_matched = [c["records"] for c in result["candidates"]]
    assert records_matched == [
        ["companies-house:gb-coh:09990002", "sanctions:uk:GBR9001"]
    ]  # FR number ignored
    assert result["candidates"][0]["evidence"][0]["right"]["revision"] == 1
    assert (
        OwnershipStore(conn).records("global", principal_id="p", scopes=h.SCOPES)
        == before
    )
    # Sanctions links never regroup ownership entities.
    service = OwnershipIdentityService(conn)
    service.review(
        "global",
        result["candidates"][0]["candidate_id"],
        "accept",
        "same company number",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    assert service.clusters("global") == {}
