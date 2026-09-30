"""An object's registration, operator, catalogue status and re-entry record as of a date (#2224, SO10)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.astronomy_registration import NO_UN_REGISTRATION
from src.kb.astronomy_registration_identity import RegistrationIdentity
from src.kb.astronomy_registration_queries import RegistrationQueries
from tests.unit.astronomy import harness as ah
from tests.unit.astronomy import registration_harness as h

NS = ah.NS
SCOPES = ah.SCOPES


@pytest.fixture(scope="module")
def conn():
    value = duckdb.connect(":memory:")
    ah.acquire(value, "satcat", "2099-06-01")
    ah.acquire(value, "satcat", "2099-07-01")
    h.acquire_all(value)
    RegistrationIdentity(value).match_objects(NS, principal_id="analyst", scopes=SCOPES)
    yield value
    value.close()


def ask(conn, identifier, as_of, **kw):
    return RegistrationQueries(conn).object_registration_as_of(NS, identifier, as_of, scopes=ah.READ_ONLY, **kw)


def test_registration_transfers_and_status_are_resolved_as_of_the_date(conn):
    april = ask(conn, "2099-001A", "2099-04-15")
    assert april["status"] == "answered"
    current = april["registration"]["current"]
    assert current["registering_state"]["value"] == "Fictland"
    assert current["supervising_state"]["value"] == "Fictland"
    assert current["index_status"]["value"] == "in orbit"
    doc = april["registration"]["documents"][0]
    assert doc["un_document"] == "ST/SG/SER.E/9901" and doc["document_locator"] == {"paragraph": "2"}
    may = ask(conn, "99901", "2099-05-20")
    assert may["registration"]["current"]["supervising_state"]["value"] == "Republic of Examplia"
    assert may["registration"]["current"]["registering_state"]["value"] == "Fictland"  # the original is kept
    assert [d["entry_kind"] for d in may["registration"]["documents"]] == ["registration", "transfer_of_supervision"]
    july = ask(conn, "FICTSAT 1", "2099-07-10")
    assert july["registration"]["current"]["index_status"]["value"] == "decayed"
    assert july["registration"]["current"]["reentry_notice"]["date"] == "2099-06-21"
    assert july["registration"]["un_registration"]["state"] == "on record"


def test_status_change_notice_and_intergovernmental_registrant(conn):
    answer = ask(conn, "2099-003B", "2099-06-10")
    current = answer["registration"]["current"]
    assert current["registering_state"]["registrant_kind"] == "intergovernmental_organisation"
    assert current["status"] == {**current["status"], "value": "non-functional", "effective_date": "2099-05-30"}


def test_reentry_shows_predictions_and_confirmed_report_distinctly(conn):
    answer = ask(conn, "2099-001A", "2099-07-10")
    aerospace = next(r for r in answer["reentry"] if r["provider"] == "aerospace-reentry")
    assert len(aerospace["predictions"]) == 3 and len(aerospace["confirmed"]) == 1
    assert aerospace["confirmed"][0]["reported_time"] == "2099-06-21T10:07:00Z"
    assert aerospace["predictions"][0]["uncertainty"] == {"text": "± 24 hours"}
    discos = next(r for r in answer["reentry"] if r["provider"] == "esa-discos")
    assert discos["confirmed"][0]["citation"]["restricted"] is True
    before = ask(conn, "2099-001A", "2099-06-20")
    aerospace = next(r for r in before["reentry"] if r["provider"] == "aerospace-reentry")
    assert aerospace["confirmed"] == [] and len(aerospace["predictions"]) == 3  # 06-20 18:00 is by the cutoff
    only = RegistrationQueries(conn).reentry_record(NS, "99901", scopes=ah.READ_ONLY)
    assert only["status"] == "answered" and only["un_notices"][0]["un_document"] == "ST/SG/SER.E/9970"


def test_catalogue_status_operators_and_identity_matches_are_cited(conn):
    answer = ask(conn, "2099-001A", "2099-07-10")
    assert {c["provider"] for c in answer["catalogue"]} == {"celestrak-satcat"}
    assert answer["catalogue"][0]["decay_date"] == "2099-06-21"
    names = {(o["operator_name"], o["role"]) for o in answer["operators"]}
    assert ("Examplia Orbital Services", "operator") in names and ("Fictland Space Agency", "owner") in names
    assert answer["identity_matches"]["object_links"]
    assert answer["pins"] and answer["answer_hash"]


def test_missing_registration_is_none_on_record_with_the_sources_consulted(conn):
    rocket = ask(conn, "2099-001B", "2099-07-10")
    assert rocket["registration"]["un_registration"]["state"] == NO_UN_REGISTRATION
    assert "not a statement" in rocket["registration"]["un_registration"]["note"]
    consulted = {s["provider"] for s in rocket["registration"]["un_registration"]["sources_consulted"]}
    assert consulted == {"unoosa-index", "unoosa-registration-documents", "esa-discos", "aerospace-reentry"}
    nothing = ask(conn, "2099-999Z", "2099-07-10")
    assert nothing["status"] == "none_on_record" and "none on record" in nothing["statement"]
    early = ask(conn, "2099-001A", "2099-01-01")
    assert early["status"] == "not_yet_published"


def test_answers_export_as_evidence_bundles_without_restricted_values(conn):
    answer = ask(conn, "2099-001A", "2099-07-10")
    exported = RegistrationQueries(conn).export_bundle(NS, answer, scopes=ah.READ_ONLY)
    bundle = exported["bundle"]
    text = json.dumps(bundle)
    assert "Fictional Satellite Operations\"" not in text  # the DISCOS operator attribution is not exported
    assert "Fictsat-1" not in text and "2099-06-21T10:07:00Z" in text  # Aerospace's own confirmed time is
    kinds = {o["payload"].get("kind") for o in bundle["objects"]}
    assert {"identity-match", "astronomy-registration-record"} <= kinds
    assert any("restricted" in o["reason"] for o in bundle["completeness"]["omissions"])
