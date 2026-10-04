"""Reviewable ASN, prefix, domain and organisation identity across sources (II07, #2782 under #2743)."""

from __future__ import annotations

import copy

import pytest

from src.kb.internet_infrastructure_identity import InfrastructureIdentity
from src.kb.internet_infrastructure_records import InfrastructureRecordError
from src.kb.internet_infrastructure_store import InternetInfrastructureStore
from tests.unit import internet_infrastructure_harness as h


@pytest.fixture()
def conn():
    value = h.connection()
    h.load_all(value)
    yield value
    value.close()


def _by(proposed, kind):
    return [a for a in proposed if a["kind"] == kind]


def test_stated_identifiers_are_proposed_never_accepted_and_carry_method_evidence_and_confidence(conn):
    identity = InfrastructureIdentity(conn)
    result = identity.propose(h.NS, principal_id="op", scopes=h.SCOPES)
    proposed = result["proposed"]
    assert {a["state"] for a in proposed} == {"proposed"}
    same_asn = _by(proposed, "same-asn")
    providers = {frozenset(e["provider"] for e in a["evidence"]["records"]) for a in same_asn}
    assert providers == {frozenset({"ripestat", "peeringdb"}), frozenset({"ripestat", "rdap"}),
                         frozenset({"peeringdb", "rdap"})}
    assert all(a["method"] == "stated-identifier:asn" and a["confidence"] == 1.0 for a in same_asn)
    assert all(a["evidence"]["identifier"] == {"kind": "asn", "value": "AS64500"} for a in same_asn)
    assert {frozenset(e["provider"] for e in a["evidence"]["records"])
            for a in _by(proposed, "same-prefix")} == {frozenset({"ripestat", "rdap"})}
    # Three certificates of the exact domain against the RDAP domain registration.
    assert len(_by(proposed, "same-domain")) == 3
    (organisation,) = _by(proposed, "organisation")
    assert organisation["method"] == "stated-identifier:asn-holder" and organisation["confidence"] == 0.6
    assert organisation["evidence"]["names_as_stated"] == {"peeringdb": "Example Networks Ltd (fictional)",
                                                          "rdap": "Example Networks Ltd (fictional)"}
    # Proposing again records nothing new; nothing is merged and every record keeps its own source.
    assert identity.propose(h.NS, principal_id="op", scopes=h.SCOPES)["proposed"] == []
    assert len({o["object_id"] for o in InternetInfrastructureStore(conn).objects(h.NS)}) == \
        conn.execute("SELECT count(*) FROM ii_objects").fetchone()[0]


def test_review_accept_reject_and_revert_with_reasons(conn):
    identity = InfrastructureIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="op", scopes=h.SCOPES)["proposed"]
    first, second = _by(proposed, "same-asn")[:2]
    with pytest.raises(InfrastructureRecordError) as caught:
        identity.review(h.NS, first["assertion_id"], "accepted", "same ASN", principal_id="rev",
                        scopes={h.WRITE, "namespace:global:write"})
    assert caught.value.code == "unauthorized"
    with pytest.raises(InfrastructureRecordError):
        identity.review(h.NS, first["assertion_id"], "accepted", "", principal_id="rev", scopes=h.SCOPES)
    accepted = identity.review(h.NS, first["assertion_id"], "accepted", "both state AS64500", principal_id="rev",
                               scopes=h.SCOPES)
    assert [s["state"] for s in accepted["history"]] == ["proposed", "accepted"]
    rejected = identity.review(h.NS, second["assertion_id"], "rejected", "not needed", principal_id="rev",
                               scopes=h.SCOPES)
    assert rejected["state"] == "rejected"
    with pytest.raises(InfrastructureRecordError):
        identity.revert(h.NS, second["assertion_id"], "x", principal_id="rev", scopes=h.SCOPES)
    reverted = identity.revert(h.NS, first["assertion_id"], "reviewer error", principal_id="rev", scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and reverted["history"][-1]["reason"] == "reviewer error"
    assert identity.accepted_for(h.NS, [first["left_object_id"]]) == []


def test_unmatched_records_stay_visible(conn):
    identity = InfrastructureIdentity(conn)
    identity.propose(h.NS, principal_id="op", scopes=h.SCOPES)
    unmatched = identity.unmatched(h.NS, scopes=h.READ_ONLY)
    assert {u["state"] for u in unmatched} == {"unmatched"}
    assert {(u["provider"], u["object_kind"]) for u in unmatched} >= {("peeringdb", "net"), ("rdap", "autnum"),
                                                                      ("peeringdb", "org")}
    org = next(a for a in identity.assertions(h.NS, scopes=h.READ_ONLY) if a["kind"] == "organisation")
    identity.review(h.NS, org["assertion_id"], "accepted", "the ASN ties both", principal_id="rev", scopes=h.SCOPES)
    assert ("peeringdb", "org") not in {(u["provider"], u["object_kind"])
                                        for u in identity.unmatched(h.NS, scopes=h.READ_ONLY)}


def test_a_shared_organisation_name_is_never_a_match_and_no_other_networks_are_listed(conn):
    store = InternetInfrastructureStore(conn)
    # A second RDAP autnum for an undeclared ASN whose holder has the same name: names alone never match.
    page = h.fetch("rdap")[0]
    header = copy.deepcopy(page[0]["ii_unit"])
    item = copy.deepcopy(page[0]["ii_item"])
    item["native_id"] = "AS64501"
    item["resource"] = {"kind": "asn", "value": "AS64501"}
    item["stated_identifiers"] = {"asn": ["AS64501"]}
    header.update(unit_key="rdap:asn:AS64501", unit_sha256="f" * 64, resource=item["resource"])
    store.apply_unit(h.NS, header, [item], source_id="rdap-registrations", run_id="r", principal_id="svc",
                     scopes=h.SCOPES, retrieved_at_ms=h.FIRST_RETRIEVAL)
    identity = InfrastructureIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="op", scopes=h.SCOPES)["proposed"]
    organisations = _by(proposed, "organisation")
    assert len(organisations) == 1
    other = h.one(conn, "rdap", "autnum", "AS64501")["object_id"]
    assert all(other not in (a["left_object_id"], a["right_object_id"]) for a in proposed)
    assert not hasattr(identity, "networks_of_organisation")
