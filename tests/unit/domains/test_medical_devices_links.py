"""Medical-device records linked to other packs by citation, shared identifier or accepted match (#2694, MD08)."""

from __future__ import annotations

from src.kb.medical_devices_identity import MedicalDevicesIdentity
from src.kb.medical_devices_links import MedicalDevicesLinks
from src.kb.medical_devices_records import MedicalDevicesStore
from tests.unit import medical_devices_harness as h


def test_links_record_their_basis_and_point_at_revisions_on_both_sides():
    conn = h.connection()
    h.load_all(conn)
    h.load_ownership(conn)
    h.load_product_identity(conn)
    notice = h.load_product_safety(conn)
    h.load_clinical(conn)
    identity = MedicalDevicesIdentity(conn)
    for candidate in identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS,
                                      products_namespace=h.PRODUCTS_NS)["candidates"]:
        if candidate["method"] in {"gtin", "name+country"}:
            identity.review(h.NS, candidate["candidate_id"], "accept", "fixture", principal_id="rev",
                            scopes=h.REVIEW_SCOPES)
    links = MedicalDevicesLinks(conn)
    result = links.link(h.NS, principal_id="alice", scopes=h.SCOPES, products_namespace=h.PRODUCTS_NS,
                        clinical_namespace=h.NS, ownership_namespace=h.OWN_NS)
    by_kind = {r["kind"]: r for r in result["results"]}
    assert {k: r["status"] for k, r in by_kind.items()} == dict.fromkeys(
        ("product-safety", "medicines", "trial", "ownership", "products"), "linked")
    safety = by_kind["product-safety"]["links"]
    assert {link["target_key"] for link in safety} == {notice}
    assert {link["basis"]["basis"] for link in safety} == {"citation"} and safety[0]["basis"]["quoted_at"]
    assert all(link["target_revision"] and link["record_revision_id"] for link in safety)
    (medicine,) = by_kind["medicines"]["links"]
    assert medicine["record_key"] == "medical-devices:fda:pma:P999901:S002"
    assert medicine["basis"]["application_number"] == "NDA099001"
    assert medicine["basis"]["quoted_at"] == ["/fields/ao_statement"]
    (trial,) = by_kind["trial"]["links"]
    assert trial["record_key"] == "medical-devices:fda:510k:K999901" and trial["basis"]["trial"] == "NCT09999901"
    ownership = by_kind["ownership"]["links"]
    assert ownership and {link["basis"]["basis"] for link in ownership} == {"accepted-match"}
    assert all(link["target_revision"] for link in ownership)
    (product,) = by_kind["products"]["links"]
    assert product["target_key"] == "product-identity:fixture-examplepump"
    # every link points at the current revision of its record
    current = {r["record_key"]: r["revision_id"] for r in MedicalDevicesStore(conn).records(h.NS, scopes=h.SCOPES)}
    for link in links.links(h.NS, scopes=h.SCOPES):
        assert link["record_revision_id"] in {current.get(link["record_key"])} | {
            r["revision_id"] for r in MedicalDevicesStore(conn).history(h.NS, link["record_key"], scopes=h.SCOPES)}
    again = links.link(h.NS, principal_id="alice", scopes=h.SCOPES, products_namespace=h.PRODUCTS_NS,
                       clinical_namespace=h.NS, ownership_namespace=h.OWN_NS)
    assert sum(r["created"] for r in again["results"]) == 0


def test_missing_providers_and_targets_are_reported_not_dropped():
    conn = h.connection()
    h.load_all(conn)
    result = MedicalDevicesLinks(conn).link(h.NS, principal_id="alice", scopes=h.SCOPES,
                                            products_namespace=h.PRODUCTS_NS)
    statuses = {r["kind"]: r["status"] for r in result["results"]}
    assert statuses["product-safety"] == "provider_unavailable" and statuses["medicines"] == "provider_unavailable"
    assert statuses["trial"] == "provider_unavailable"
    assert statuses["ownership"] == statuses["products"] == "none_linked"
    # with the clinical store present but without the cited application, the citation is a missing target
    h.load_clinical(conn, namespace="elsewhere")
    medicines = MedicalDevicesLinks(conn).link(h.NS, principal_id="alice", scopes=h.SCOPES, kinds=["medicines"])
    assert medicines["missing_targets"] == [{"record_key": "medical-devices:fda:pma:P999901:S002",
                                             "application_number": "NDA099001",
                                             "reason": "the cited application is not on record in the Medicines "
                                                       "records"}]
    h.load_product_safety(conn, namespace="other-products")
    safety = MedicalDevicesLinks(conn).link(h.NS, principal_id="alice", scopes=h.SCOPES | {
        "namespace:other-products:read"}, kinds=["product-safety"], products_namespace=h.PRODUCTS_NS)
    assert [m["recall_number"] for m in safety["missing_targets"]] == ["Z-9901-2099", "Z-9901-2099"]
