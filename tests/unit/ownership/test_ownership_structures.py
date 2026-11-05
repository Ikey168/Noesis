"""Holding-structure classification only as a source states it (#2890)."""

from __future__ import annotations

import pytest

from src.kb.ownership_store import OwnershipError, OwnershipStore
from src.kb.ownership_structures import (
    CONTRACT,
    STRUCTURE_TYPES,
    StructureClassifications,
)
from tests.unit.ownership import structure_harness as h


@pytest.fixture()
def conn():
    conn = h.connection()
    h.load_ownership(conn)
    return conn


@pytest.fixture()
def default_mode(monkeypatch):
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH", raising=False)
    monkeypatch.delenv("NEURONEWS_PERSON_RESEARCH", raising=False)


def view(conn, key):
    return OwnershipStore(conn, initialize=False).by_key(h.OWN_NS, key, principal_id=h.ANALYST, scopes=h.SCOPES)


def test_unclassified_entities_stay_unclassified_and_names_are_never_read(conn):
    service = StructureClassifications(conn)
    offered = service.propose_from_records(h.OWN_NS, principal_id=h.ANALYST, scopes=h.SCOPES)
    proposals = service.list(h.OWN_NS, scopes=h.SCOPES)
    # Only the trust's stated BODS subtype is offered; "Example Family Office Ltd" states no legal form.
    assert offered["offered"] == 1 and [(p["entity_key"], p["structure_type"]) for p in proposals] == [
        (h.TRUST, "trust")]
    for key in (h.FAMILY_OFFICE, h.STRUCTURE, h.OPERATING):
        found = service.classification(h.OWN_NS, [key], scopes=h.SCOPES)
        assert found["status"] == "unclassified" and found["structure_types"] == []
    # A proposal does not classify until a reviewer accepts it.
    assert service.classification(h.OWN_NS, [h.TRUST], scopes=h.SCOPES)["status"] == "unclassified"
    with pytest.raises(OwnershipError) as refused:
        service.propose(h.OWN_NS, entity_key=h.FAMILY_OFFICE, structure_type="family_office",
                        basis="self-description", stated_value="Family Office",
                        citation={"kind": "ownership-record", "record_id": view(conn, h.FAMILY_OFFICE)["record_id"],
                                  "revision": 1, "field": "name"},
                        principal_id=h.ANALYST, scopes=h.SCOPES)
    assert refused.value.code == "name_inference_refused"
    with pytest.raises(OwnershipError) as refused:
        service.propose(h.OWN_NS, entity_key=h.FAMILY_OFFICE, structure_type="family_office", basis="name",
                        stated_value="Family Office", citation=h.SELF_DESCRIPTION, principal_id=h.ANALYST,
                        scopes=h.SCOPES)
    assert refused.value.code == "invalid_request"


def test_a_classification_must_be_stated_by_its_citation(conn):
    service = StructureClassifications(conn)
    record = view(conn, h.STRUCTURE)
    with pytest.raises(OwnershipError) as refused:  # the record states "ltd", not a holding company
        service.propose(h.OWN_NS, entity_key=h.STRUCTURE, structure_type="holding_company",
                        basis="register-legal-form", stated_value="holding company",
                        citation={"kind": "ownership-record", "record_id": record["record_id"],
                                  "revision": record["revision"], "field": "entity_type"},
                        principal_id=h.ANALYST, scopes=h.SCOPES)
    assert refused.value.code == "not_stated"
    with pytest.raises(OwnershipError) as refused:  # the quote does not contain the stated value
        service.propose(h.OWN_NS, entity_key=h.STRUCTURE, structure_type="family_office", basis="self-description",
                        stated_value="family office", citation=h.SELF_DESCRIPTION, principal_id=h.ANALYST,
                        scopes=h.SCOPES)
    assert refused.value.code == "not_stated"
    other = view(conn, h.TRUST)
    with pytest.raises(OwnershipError) as refused:  # a revision about another entity is not evidence for this one
        service.propose(h.OWN_NS, entity_key=h.STRUCTURE, structure_type="trust", basis="declared-category",
                        stated_value="trust", citation={"kind": "ownership-record", "record_id": other["record_id"],
                                                        "revision": 1, "field": "entity_type.subtype"},
                        principal_id=h.ANALYST, scopes=h.SCOPES)
    assert refused.value.code == "invalid_request"
    with pytest.raises(OwnershipError) as refused:
        service.propose(h.OWN_NS, entity_key=h.STRUCTURE, structure_type="holding_company", basis="self-description",
                        stated_value="holding company", citation={**h.SELF_DESCRIPTION, "sha256": "not-a-digest"},
                        principal_id=h.ANALYST, scopes=h.SCOPES)
    assert refused.value.code == "invalid_request"
    with pytest.raises(OwnershipError):
        service.propose(h.OWN_NS, entity_key=h.STRUCTURE, structure_type="influence_vehicle",
                        basis="self-description", stated_value="holding company", citation=h.SELF_DESCRIPTION,
                        principal_id=h.ANALYST, scopes=h.SCOPES)


def test_review_accept_reject_revert_and_side_by_side(conn):
    service = StructureClassifications(conn)
    proposed = service.propose(h.OWN_NS, entity_key=h.STRUCTURE, structure_type="holding_company",
                               basis="self-description", stated_value="holding company",
                               citation=h.SELF_DESCRIPTION, principal_id=h.ANALYST, scopes=h.SCOPES)
    assert proposed["contract"] == CONTRACT and proposed["state"] == "proposed" and proposed["created"]
    again = service.propose(h.OWN_NS, entity_key=h.STRUCTURE, structure_type="holding_company",
                            basis="self-description", stated_value="holding company", citation=h.SELF_DESCRIPTION,
                            principal_id=h.ANALYST, scopes=h.SCOPES)
    assert again["classification_id"] == proposed["classification_id"] and not again["created"]
    with pytest.raises(OwnershipError) as refused:  # the analyst cannot review without the review scope
        service.review(h.OWN_NS, proposed["classification_id"], "accept", "ok", principal_id=h.ANALYST,
                       scopes=h.SCOPES - {"knowledge:ownership:review"})
    assert refused.value.code == "unauthorized"
    with pytest.raises(OwnershipError):
        service.review(h.OWN_NS, proposed["classification_id"], "accept", " ", principal_id=h.REVIEWER,
                       scopes=h.SCOPES)
    accepted = service.review(h.OWN_NS, proposed["classification_id"], "accept", "annual report states it",
                              principal_id=h.REVIEWER, scopes=h.SCOPES)
    assert accepted["classifies"] and accepted["decision"]["by"] == h.REVIEWER
    assert accepted["citation"]["sha256"] == "a" * 64 and accepted["citation"]["locator"]["page"] == 2
    spv = service.propose(h.OWN_NS, entity_key=h.STRUCTURE, structure_type="special_purpose_vehicle",
                          basis="declared-category", stated_value="special purpose vehicle",
                          citation={**h.SELF_DESCRIPTION, "reference": "fixture:prospectus:2099",
                                    "quote": "The issuer is a special purpose vehicle."},
                          principal_id=h.ANALYST, scopes=h.SCOPES)
    service.review(h.OWN_NS, spv["classification_id"], "accept", "prospectus states it", principal_id=h.REVIEWER,
                   scopes=h.SCOPES)
    both = service.classification(h.OWN_NS, [h.STRUCTURE], scopes=h.SCOPES)
    assert both["structure_types"] == ["holding_company", "special_purpose_vehicle"] and both["differ"]
    service.revert(h.OWN_NS, spv["classification_id"], "prospectus concerns another issuer",
                   principal_id=h.REVIEWER, scopes=h.SCOPES)
    service.revert(h.OWN_NS, proposed["classification_id"], "withdrawn", principal_id=h.REVIEWER, scopes=h.SCOPES)
    after = service.classification(h.OWN_NS, [h.STRUCTURE], scopes=h.SCOPES)
    assert after["status"] == "unclassified" and {i["state"] for i in after["not_in_force"]} == {"reverted"}
    history = service.get(h.OWN_NS, proposed["classification_id"], scopes=h.SCOPES)["history"]
    assert [step["state"] for step in history] == ["proposed", "accepted", "reverted"]
    trust = service.propose_from_records(h.OWN_NS, principal_id=h.ANALYST, scopes=h.SCOPES)["proposed"][0]
    rejected = service.review(h.OWN_NS, trust, "reject", "not reviewed yet", principal_id=h.REVIEWER, scopes=h.SCOPES)
    assert rejected["state"] == "rejected" and not rejected["classifies"]
    assert set(STRUCTURE_TYPES) >= {"family_office", "private_foundation", "trust", "trustee", "holding_company"}


def test_stated_trust_subtype_cites_its_record_revision(conn):
    service = StructureClassifications(conn)
    [cid] = service.propose_from_records(h.OWN_NS, principal_id=h.ANALYST, scopes=h.SCOPES)["proposed"]
    item = service.get(h.OWN_NS, cid, scopes=h.SCOPES)
    record = view(conn, h.TRUST)
    assert item["basis"] == "declared-category" and item["stated_value"] == "trust"
    expected = {"kind": "ownership-record", "record_id": record["record_id"], "revision": record["revision"],
                "field": "entity_type.subtype", "provider": "open-ownership", "record_hash": record["record_hash"]}
    assert {k: item["citation"][k] for k in expected} == expected
    assert service.propose_from_records(h.OWN_NS, principal_id=h.ANALYST, scopes=h.SCOPES)["proposed"] == []


def test_natural_persons_follow_person_research_mode(conn, default_mode, monkeypatch):
    from src.kb.ownership_records import record

    person = "fixture:person:trustee-1"
    OwnershipStore(conn).apply(h.OWN_NS, [record("person", person, {"provider": "open-ownership",
                                                                     "provider_record_id": "trustee-1"},
                                                 name="A Fictional Trustee",
                                                 native={"role_as_stated": "trustee"})],
                               run_id="person", observed_at_ms=2, principal_id="ownership-loader")
    service = StructureClassifications(conn)
    citation = {"kind": "ownership-record", "record_id": view(conn, person)["record_id"], "revision": 1,
                "field": "native.role_as_stated"}
    with pytest.raises(OwnershipError) as refused:
        service.propose(h.OWN_NS, entity_key=person, structure_type="trustee", basis="self-description",
                        stated_value="trustee", citation=citation, principal_id=h.ANALYST, scopes=h.SCOPES)
    assert refused.value.code == "person_research_required"
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    assert service.propose(h.OWN_NS, entity_key=person, structure_type="trustee", basis="self-description",
                           stated_value="trustee", citation=citation, principal_id=h.ANALYST,
                           scopes=h.SCOPES)["state"] == "proposed"


def test_reads_need_ownership_scopes(conn):
    with pytest.raises(OwnershipError) as refused:
        StructureClassifications(conn).list(h.OWN_NS, scopes={"knowledge:ownership:read"})
    assert refused.value.code == "unauthorized"
    with pytest.raises(OwnershipError):
        StructureClassifications(conn).propose_from_records(h.OWN_NS, principal_id=h.ANALYST,
                                                            scopes={"knowledge:ownership:read",
                                                                    f"namespace:{h.OWN_NS}:read"})
