"""Devices and manufacturers matched across FDA, GUDID and EUDAMED through reviewable identity (MD07)."""

from __future__ import annotations

import pytest

from src.kb.medical_devices_identity import MedicalDeviceIdentity
from src.kb.medical_devices_records import MedicalDeviceError
from tests.unit import medical_devices_harness as h


@pytest.fixture()
def proposed():
    conn = h.connection()
    h.load_all(conn)
    entities = h.seed_ownership(conn)
    identity = MedicalDeviceIdentity(conn)
    result = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.OWN_NS,
                              products_namespace=h.PRODUCTS_NS)
    return conn, identity, result, entities


def by_pair(result):
    return {(c["subject_key"], c["target_key"]): c for c in result["candidates"]}


def test_published_identifiers_come_first_and_names_are_never_used_for_devices(proposed):
    _, _, result, entities = proposed
    pairs = by_pair(result)
    assert pairs[(h.EU_DEVICE, h.GUDID_PUMP)]["method"] == "udi-di"
    assert pairs[(h.PUMP_CLEARANCE, h.GUDID_PUMP)]["method"] == "premarket-number"
    assert pairs[(h.PMA, f"medical-devices:gudid:di:{h.LEAD_DI}")]["method"] == "premarket-number"
    low = pairs[("medical-devices:fda:510k:K999002", h.GUDID_PUMP)]
    assert low["method"] == "product-code" and low["low_evidence"] and low["confidence"] < 0.5
    # Devices are only ever matched on identifiers: every device candidate names shared identifiers or codes.
    for item in result["candidates"]:
        if item["subject_kind"] == "device" and item["target_kind"] == "device":
            assert item["evidence"].get("shared_identifiers") or item["evidence"].get("shared_product_codes")
    duns = pairs[("medical-devices:manufacturer:duns:999000111", entities["exampla"])]
    assert duns["method"] == "exact-identifier" and not duns["low_evidence"]
    names = [c for c in result["candidates"] if c["method"] == "name-jurisdiction"]
    assert names and all(c["low_evidence"] and c["target_key"] == entities["exampla"] for c in names)
    assert entities["decoy"] not in {c["target_key"] for c in result["candidates"]}  # contradicting country
    assert result["coverage"]["products"].startswith("provider missing")


def test_nothing_is_accepted_automatically_and_unmatched_subjects_stay_visible(proposed):
    _, identity, result, _ = proposed
    assert {c["state"] for c in result["candidates"]} == {"proposed"}
    unmatched = {u["key"] for u in result["unmatched"]}
    assert "medical-devices:eudamed:actor:DE-MF-000099901" in unmatched  # Northwind has no candidate at all
    assert h.GUDID_PUMP in unmatched  # proposed but not reviewed is still unmatched
    again = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert again["proposed"] == [] and len(again["candidates"]) == len(result["candidates"])


def test_review_accept_reject_and_revert_are_recorded_decisions(proposed):
    conn, identity, result, _ = proposed
    udi = by_pair(result)[(h.EU_DEVICE, h.GUDID_PUMP)]
    accepted = identity.review(h.NS, udi["candidate_id"], "accept", "UDI-DI 00899999000011 on both",
                               principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "reviewer" and accepted["decision_id"]
    assert conn.execute("SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
                        [accepted["decision_id"]]).fetchone()[0] == "match"
    assert [m["key"] for m in identity.accepted(h.NS, h.GUDID_PUMP, scopes=h.SCOPES)] == [h.EU_DEVICE]
    reverted = identity.revert(h.NS, udi["candidate_id"], "wrong DI transcribed", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]] == [
        "proposed", "accepted", "reverted"]
    assert identity.accepted(h.NS, h.GUDID_PUMP, scopes=h.SCOPES) == []
    low = by_pair(result)[("medical-devices:fda:510k:K999002", h.GUDID_PUMP)]
    rejected = identity.review(h.NS, low["candidate_id"], "reject", "a product code is a device type",
                               principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert rejected["state"] == "rejected"
    with pytest.raises(MedicalDeviceError):
        identity.review(h.NS, low["candidate_id"], "accept", "again", principal_id="reviewer",
                        scopes=h.REVIEW_SCOPES)
    with pytest.raises(MedicalDeviceError):
        identity.review(h.NS, udi["candidate_id"], "accept", "no review scope", principal_id="analyst",
                        scopes=h.SCOPES)
    # Records are never merged or rewritten by a decision.
    assert conn.execute("SELECT count(*) FROM medical_device_records WHERE record_key=?",
                        [h.GUDID_PUMP]).fetchone()[0] == 1


def test_accepted_device_matches_form_a_cluster(proposed):
    _, identity, result, _ = proposed
    pairs = by_pair(result)
    for pair in ((h.EU_DEVICE, h.GUDID_PUMP), (h.PUMP_CLEARANCE, h.GUDID_PUMP)):
        identity.review(h.NS, pairs[pair]["candidate_id"], "accept", "identifiers agree", principal_id="reviewer",
                        scopes=h.REVIEW_SCOPES)
    assert set(identity.device_cluster(h.NS, h.PUMP_CLEARANCE, scopes=h.SCOPES)) == {
        h.PUMP_CLEARANCE, h.GUDID_PUMP, h.EU_DEVICE}


def test_devices_match_products_identities_by_gtin():
    conn = h.connection()
    h.apply(conn, "clinical-devices-accessgudid")
    conn.execute("CREATE TABLE product_identities (identity_id TEXT, namespace TEXT, identifiers_json TEXT)")
    conn.execute("INSERT INTO product_identities VALUES ('product-identity:fixture-pump', ?, ?)",
                 [h.PRODUCTS_NS, '{"gtin": [{"value": "0899999000011", "state": "valid"}]}'])
    result = MedicalDeviceIdentity(conn).propose(h.NS, principal_id="analyst", scopes=h.SCOPES,
                                                 products_namespace=h.PRODUCTS_NS)
    (match,) = [c for c in result["candidates"] if c["target_kind"] == "products-identity"]
    assert match["method"] == "gtin-udi-di" and match["subject_key"] == h.GUDID_PUMP and match["state"] == "proposed"
