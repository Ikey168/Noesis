"""Medical device records linked to Product safety, Medicines, trials and ownership by citation or match (MD08)."""

from __future__ import annotations

from src.kb.medical_devices_identity import MedicalDeviceIdentity
from src.kb.medical_devices_links import MedicalDeviceLinks
from src.kb.medical_devices_records import MedicalDeviceStore
from tests.unit import medical_devices_harness as h


def _by(links, record_key, target_pack):
    return [link for link in links if link["record_key"] == record_key and link["target_pack"] == target_pack]


def test_missing_providers_and_targets_are_reported_not_dropped():
    conn = h.connection()
    h.load_all(conn)
    result = MedicalDeviceLinks(conn).link(h.NS, scopes=h.SCOPES)
    (safety,) = _by(result["links"], h.RECALL, "products")
    assert safety["status"] == "provider-missing" and safety["target_key"] == "Z-9901-2025"
    (medicine,) = [link for link in result["links"] if link["target_kind"] == "clinical.medicines"]
    assert medicine["status"] == "provider-missing" and medicine["target_key"] == "NDA999001"
    # The recall cites K999001, which is on record: a citation link to that exact clearance revision.
    (cited,) = [link for link in result["links"] if link["record_key"] == h.RECALL
                and link["target_key"] == h.PUMP_CLEARANCE]
    clearance = MedicalDeviceStore(conn).records(h.NS, scopes=h.SCOPES, record_keys=[h.PUMP_CLEARANCE])[0]
    assert cited["basis"] == "citation" and cited["target_revision"] == clearance["revision_id"]
    recall = MedicalDeviceStore(conn).records(h.NS, scopes=h.SCOPES, record_keys=[h.RECALL])[0]
    assert cited["record_revision_id"] == recall["revision_id"]
    assert result["summary"]["provider-missing"] >= 2


def test_links_to_product_safety_medicines_trials_and_ownership_by_identifier_only():
    conn = h.connection()
    h.load_all(conn)
    seeded = h.seed_clinical(conn)
    notice = h.seed_safety(conn)
    entities = h.seed_ownership(conn)
    identity = MedicalDeviceIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    (duns,) = [c for c in proposed["candidates"] if c["method"] == "exact-identifier"]
    identity.review(h.NS, duns["candidate_id"], "accept", "DUNS 999000111 on both", principal_id="reviewer",
                    scopes=h.REVIEW_SCOPES)
    links = MedicalDeviceLinks(conn).link(h.NS, scopes=h.SCOPES, ownership_namespace=h.OWN_NS,
                                          safety_namespace=h.PRODUCTS_NS)["links"]
    (safety,) = _by(links, h.RECALL, "products")
    assert (safety["status"], safety["target_key"], safety["basis"]) == ("linked", notice, "shared-identifier")
    assert safety["target_revision"] == "product-safety-revision:fixture-1"
    lead = f"medical-devices:gudid:di:{h.LEAD_DI}"
    (medicine,) = [link for link in links if link["record_key"] == lead and link["target_kind"] ==
                   "clinical.medicines"]
    assert (medicine["status"], medicine["target_key"]) == ("linked", seeded["product"])
    assert medicine["evidence"]["application_number"] == "NDA999001"
    trials = [link for link in links if link["target_kind"] == "clinical.core"]
    assert [(t["record_key"], t["target_key"], t["basis"]) for t in trials] == [
        (h.PUMP_CLEARANCE, seeded["trial"], "citation")]
    ownership = [link for link in links if link["target_pack"] == "corporate-ownership"]
    assert ownership and {o["target_key"] for o in ownership} == {entities["exampla"]}
    assert {o["basis"] for o in ownership} == {"accepted-match"} and all(o["target_revision"] for o in ownership)
    # Name-only candidates were not reviewed, so they produce no link.
    assert {o["evidence"]["method"] for o in ownership} == {"exact-identifier"}
    for link in links:
        assert "verdict" not in link and "risk" not in link


def test_linking_is_idempotent_and_links_stay_on_their_revision():
    conn = h.connection()
    clock = h.Clock()
    h.load_all(conn, now=clock)
    links = MedicalDeviceLinks(conn)
    first = links.link(h.NS, scopes=h.SCOPES)
    again = links.link(h.NS, scopes=h.SCOPES)
    assert len(first["links"]) == len(again["links"])
    h.load_all(conn, v2=True, now=clock)
    links.link(h.NS, scopes=h.SCOPES)
    recall_links = [link for link in links.links(h.NS, scopes=h.SCOPES, record_key=h.RECALL)
                    if link["target_key"] == h.PUMP_CLEARANCE]
    assert len({link["record_revision_id"] for link in recall_links}) == 2  # one per recall revision
