"""Registrations matched to SATCAT objects and parties to entities through reviewable identity (#2224, SO07-SO08)."""

from __future__ import annotations

import duckdb
import pytest

from src.kb.astronomy_identity import AstronomyIdentity
from src.kb.astronomy_records import AstronomyError
from src.kb.astronomy_registration import RegistrationStore
from src.kb.astronomy_registration_identity import RegistrationIdentity
from src.kb.entities import ensure_entity_schema, register_canonical_entity
from src.kb.ownership_records import record as ownership_record
from src.kb.ownership_store import OwnershipStore
from tests.unit.astronomy import harness as ah
from tests.unit.astronomy import registration_harness as h

NS = ah.NS
SCOPES = ah.SCOPES | {"knowledge:entity-history:read"}


@pytest.fixture
def conn():
    value = duckdb.connect(":memory:")
    ah.acquire(value, "satcat", "2099-06-01")
    ah.acquire(value, "gcat_satcat", "2099-07-01")
    h.acquire_all(value)
    yield value
    value.close()


def rid_of(conn, key, kind="registration_entry", entry_kind=None):
    views = RegistrationStore(conn, initialize=False).visible(NS, keys=[key], kinds=[kind])["records"]
    return [v for v in views if entry_kind is None or v["record"].get("entry_kind") == entry_kind]


def test_exact_identifiers_link_with_evidence_and_records_stay_separate(conn):
    identity = RegistrationIdentity(conn)
    before = RegistrationStore(conn).visible(NS)["records"]
    result = identity.match_objects(NS, principal_id="analyst", scopes=SCOPES)
    registration = rid_of(conn, "cospar:2099-001A", entry_kind="registration")[0]
    links = identity.object_links(NS, [registration["record_id"]])
    assert links and all(link["basis"] == "exact-identifier" for link in links)
    assert links[0]["matched_on"] == {"cospar": "2099-001A", "norad": "99901"}
    assert links[0]["evidence"]["registration"]["revision_id"] == registration["revision_id"]
    # Re-running adds nothing; records are never edited.
    again = identity.match_objects(NS, principal_id="analyst", scopes=SCOPES)
    assert again["linked"] == [] and result["linked"]
    assert RegistrationStore(conn).visible(NS)["records"] == before


def test_conflicting_identifiers_stay_as_candidates_and_name_only_is_never_auto_linked(conn):
    identity = RegistrationIdentity(conn)
    result = identity.match_objects(NS, principal_id="analyst", scopes=SCOPES)
    by_basis = {}
    for candidate in result["candidates"]:
        by_basis.setdefault(candidate["basis"], []).append(candidate)
    # FICTSAT 3: registered and SATCAT say 2099-003B/99904, GCAT pairs 99904 with 2099-003A.
    conflicts = by_basis["registration-identifier-conflict"]
    assert any(c["evidence"]["differ"] == ["cospar"] and c["evidence"]["catalogue"]["provider"] == "gcat"
               for c in conflicts)
    assert all(c["state"] == "proposed" for c in result["candidates"])
    cube = rid_of(conn, "name:fictcube")[0]
    assert not identity.object_links(NS, [cube["record_id"]])
    assert any(u["object_name"] == "FICTCUBE" for u in result["unmatched"])
    # A reviewer may accept a conflict candidate; it is an entity identity decision, nothing merges.
    reviewed = AstronomyIdentity(conn).review(NS, conflicts[0]["candidate_id"], "accept", "same object per ESA",
                                              principal_id="reviewer", scopes=SCOPES)
    assert reviewed["state"] == "accepted" and reviewed["decision_id"]
    assert identity.accepted_object_keys(NS)


def test_name_only_registration_becomes_a_candidate_when_a_catalogue_object_has_that_name():
    conn = duckdb.connect(":memory:")
    ah.acquire(conn, "gcat_satcat", "2099-07-01")  # GCAT names 'Fictsat 2' with no NORAD number
    store = RegistrationStore(conn)
    store.apply(NS, [{
        "kind": "registration_entry",
        "source": {"provider": "unoosa-registration-documents", "source_record_id": "ST/SG/SER.E/9999#para 1"},
        "entry_kind": "registration", "object_name": "FICTSAT 2", "registering_state": "Fictland",
        "un_document": "ST/SG/SER.E/9999", "document_date": "2099-05-01", "document_locator": {"paragraph": "1"},
        "language": "en", "quotation": "1. Name of space object: FICTSAT 2", "launch_date": "2099-04-10",
    }], run_id="r", observed_at_ms=1)
    result = RegistrationIdentity(conn).match_objects(NS, principal_id="analyst", scopes=SCOPES)
    assert [c["basis"] for c in result["candidates"]] == ["registration-name-only"]
    assert RegistrationIdentity(conn).object_links(NS) == []


def seed_entities(conn):
    ensure_entity_schema(conn)
    ids = {}
    for name, kind in (("Fictland", "country"), ("Republic of Examplia", "country"),
                       ("European Fictional Space Organisation", "organization"),
                       ("Fictional Satellite Operations LLC", "organization"),
                       ("Fictland University Hospital", "organization")):
        ids[name] = register_canonical_entity(conn, "ent-fict-" + name.lower().replace(" ", "-"), name,
                                              entity_type=kind)
    OwnershipStore(conn).apply(ah.OWN_NS, [ownership_record(
        "legal_entity", "gleif:lei:5299FICT0000000000001", {"provider": "gleif", "provider_record_id": "x"},
        name="Fictional Satellite Operations LLC", jurisdiction="US",
        identifiers=[{"scheme": "lei", "value": "5299FICT0000000000001"}])],
        run_id="o", observed_at_ms=1, principal_id="p")
    return ids


def test_states_intergovernmental_registrants_and_operators_become_reviewable_party_candidates(conn):
    seed_entities(conn)
    identity = RegistrationIdentity(conn)
    result = identity.match_parties(NS, principal_id="analyst", scopes=SCOPES, ownership_namespace=ah.OWN_NS)
    pairs = {(c["left_key"], c["basis"]) for c in result["candidates"]} | {
        (c["right_key"], c["basis"]) for c in result["candidates"]}
    assert ("space-registration:state:fictland", "name-jurisdiction") in pairs
    assert ("space-registration:state:republic of examplia", "name-jurisdiction") in pairs
    assert ("space-registration:organisation:european fictional space organisation", "name-jurisdiction") in pairs
    assert ("space-registration:operator:fictional satellite operations llc", "exact-identifier") in pairs
    assert ("space-registration:operator:fictland university", "similar-name") in pairs
    # No party is created beyond what a registration or operator assertion states.
    stated = {p["party_key"] for p in identity.parties(NS)}
    assert all(c["left_key"] in stated or c["right_key"] in stated for c in result["candidates"])
    assert not any("military" in key or "intelligence" in key for key in stated)
    assert all(c["state"] == "proposed" for c in result["candidates"])


def test_party_review_accepts_rejects_and_never_accepts_a_similar_name(conn):
    seed_entities(conn)
    identity = RegistrationIdentity(conn)
    candidates = identity.match_parties(NS, principal_id="analyst", scopes=SCOPES)["candidates"]
    state = next(c for c in candidates if "state:fictland" in c["left_key"] + c["right_key"])
    accepted = identity.review_party(NS, state["candidate_id"], "accept", "same State",
                                     principal_id="reviewer", scopes=SCOPES)
    assert accepted["state"] == "accepted"
    assert identity.party_identity(NS, "state", "Fictland")["state"] == "matched"
    similar = next(c for c in candidates if c["basis"] == "similar-name")
    with pytest.raises(Exception):
        identity.review_party(NS, similar["candidate_id"], "accept", "looks alike", principal_id="reviewer",
                              scopes=SCOPES)
    rejected = identity.review_party(NS, similar["candidate_id"], "reject", "a hospital is not the university",
                                     principal_id="reviewer", scopes=SCOPES)
    assert rejected["state"] == "rejected"
    assert identity.party_identity(NS, "operator", "Fictland University")["state"] == "unmatched"
    with pytest.raises(AstronomyError):
        identity.review_party(NS, "own-idc:not-mine", "accept", "x", principal_id="reviewer", scopes=SCOPES)
