"""Devices and manufacturers matched across registries through reviewable identity (#2690, MD07)."""

from __future__ import annotations

import pytest

from src.kb.medical_devices_identity import MedicalDevicesIdentity
from src.kb.ownership_identity import OwnershipIdentityService
from tests.unit import medical_devices_harness as h

GUDID_B = f"medical-devices:gudid:di:{h.FIXTURE_DI}"
EUDAMED = "medical-devices:eudamed:basic-udi-di:4099999FIXTUREFLOWA1"


def world():
    conn = h.connection()
    h.load_all(conn)
    h.load_ownership(conn)
    h.load_product_identity(conn)
    return conn, MedicalDevicesIdentity(conn)


def by_method(candidates):
    out = {}
    for c in candidates:
        out.setdefault(c["method"], []).append(c)
    return out


def test_identifiers_come_before_names_and_nothing_is_merged_or_accepted():
    conn, identity = world()
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS,
                                products_namespace=h.PRODUCTS_NS)
    assert proposed["unavailable"] == []
    methods = by_method(proposed["candidates"])
    assert set(methods) == {"udi-di", "gtin", "premarket-number", "name+country"}
    assert all(c["state"] == "proposed" and c["evidence"] for c in proposed["candidates"])
    (udi,) = methods["udi-di"]
    assert set(udi["records"]) == {GUDID_B, EUDAMED} and udi["basis"] == "exact-identifier"
    assert udi["evidence"][0]["value"] == h.FIXTURE_DI and udi["confidence"] == 0.95
    assert all(c["confidence"] < 0.5 for c in methods["name+country"])
    # devices are never proposed by name: every device candidate rests on an identifier
    for candidate in proposed["candidates"]:
        if any(":gudid:di:" in r or ":basic-udi-di:" in r for r in candidate["records"]):
            assert candidate["method"] in {"udi-di", "gtin"}
    # a re-proposal is idempotent
    again = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS,
                             products_namespace=h.PRODUCTS_NS)
    assert again["proposed"] == []
    # accepted matches never regroup ownership entities (foreign keys stay outside the ownership clusters)
    for candidate in proposed["candidates"]:
        identity.review(h.NS, candidate["candidate_id"], "accept", "fixture review", principal_id="rev",
                        scopes=h.REVIEW_SCOPES)
    clusters = OwnershipIdentityService(conn, initialize=False).clusters(h.NS)
    assert not [k for k in clusters if k.startswith("medical-devices:")]


def test_review_revert_and_the_manufacturer_match_through_an_accepted_device_match():
    _, identity = world()
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert {u["provider"] for u in proposed["unavailable"]} == {"ownership.core", "products.core"}
    (udi,) = by_method(proposed["candidates"])["udi-di"]
    assert "accepted-device-match" not in by_method(proposed["candidates"])
    accepted = identity.review(h.NS, udi["candidate_id"], "accept", "same GS1 DI", principal_id="bob",
                               scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "bob" and accepted["decision_id"]
    followed = by_method(identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)["candidates"])
    (manufacturer,) = followed["accepted-device-match"]
    assert set(manufacturer["records"]) == {"medical-devices:gudid:labeler:999999902",
                                            "medical-devices:eudamed:actor:DE-MF-000099901"}
    assert identity.identity(h.NS, GUDID_B, scopes=h.SCOPES)["state"] == "matched"
    reverted = identity.revert(h.NS, udi["candidate_id"], "reviewer error", principal_id="bob",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and [e["state"] for e in reverted["history"]][-1] == "reverted"
    assert identity.identity(h.NS, GUDID_B, scopes=h.SCOPES)["state"] == "unmatched"
    unmatched = {u["record_key"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)["unmatched"]}
    assert {GUDID_B, EUDAMED} <= unmatched


def test_review_needs_the_review_scope_and_a_candidate_of_this_feature():
    _, identity = world()
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    candidate = proposed["candidates"][0]["candidate_id"]
    with pytest.raises(Exception) as refused:
        identity.review(h.NS, candidate, "accept", "x", principal_id="alice", scopes=h.SCOPES)
    assert getattr(refused.value, "code", None) == "unauthorized"
    with pytest.raises(Exception) as missing:
        identity.review(h.NS, "own-idc:unknown", "accept", "x", principal_id="bob", scopes=h.REVIEW_SCOPES)
    assert getattr(missing.value, "code", None) == "not_found"
