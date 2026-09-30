"""Treaty, participant and treaty-action records with immutable revisions and as-of lookup (#2590)."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from src.kb.treaties_records import TreatiesError, check_minimisation, feature_enabled
from src.kb.treaties_store import TreatyStore, content_hash
from tests.unit import treaties_harness as h

GERMANY_RATIFICATION = "treaties:action:untc:XXIX-99:germany:ratification:table"
EXAMPLONIA_SIGNATURE = "treaties:action:untc:XXIX-99:examplonia:signature:table"


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    yield connection
    connection.close()


def all_records(v2=False):
    return [r for source_id in (h.UNTC, *h.SOURCES) for r in h.records(source_id, v2=v2)]


def test_records_validate_against_the_contract_and_round_trip():
    validator = Draft202012Validator(json.loads(
        (h.ROOT / "contracts/schemas/jsonschema/noesis-treaty-record-v1.json").read_text()))
    records = all_records() + all_records(v2=True)
    assert {r["record_kind"] for r in records} == {"treaty", "treaty-action"}
    for record in records:
        validator.validate(record)
        assert json.loads(json.dumps(record)) == record
    person = copy.deepcopy(next(r for r in records if r["record_kind"] == "treaty-action"))
    person["fields"]["signatory_name"] = "A. Person"
    assert list(validator.iter_errors(person))
    effect = copy.deepcopy(records[0])
    effect["fields"]["legal_effect"] = "binding"
    assert list(validator.iter_errors(effect))
    pack = json.loads((h.ROOT / "packs/legal/pack.json").read_text())
    assert pack["schema_versions"]["treaty-record"] == "1.0.0"


def test_minimisation_is_enforced_at_write_time(conn):
    record = copy.deepcopy(next(r for r in h.records(h.COE) if r["record_kind"] == "treaty-action"))
    record["fields"]["representative"] = "Permanent Representative (name)"
    with pytest.raises(TreatiesError) as error:
        TreatyStore(conn).project(h.NS, [record], run_id="bad", source_id=h.COE)
    assert error.value.code == "minimisation_violation"
    person = copy.deepcopy(next(r for r in h.records(h.COE) if r["record_kind"] == "treaty-action"))
    person["fields"]["participant"]["kind"] = "natural-person"
    with pytest.raises(TreatiesError):
        check_minimisation(person)
    stored = json.dumps(conn.execute("SELECT record_json FROM treaty_action_revisions").fetchall())
    assert "signatory" not in stored and "representative_name" not in stored


def test_replays_add_nothing_and_a_new_stamp_alone_is_no_change(conn):
    before = conn.execute("SELECT count(*) FROM treaty_action_revisions").fetchone()[0]
    h.load_all(conn)
    assert conn.execute("SELECT count(*) FROM treaty_action_revisions").fetchone()[0] == before
    record = next(r for r in h.records(h.COE) if r["record_kind"] == "treaty-action")
    restamped = {**record, "depositary_revision": "Status as of 01/01/2100", "depositary_date": "2100-01-01"}
    assert content_hash(record) == content_hash(restamped)
    counts = TreatyStore(conn).project(h.NS, [restamped], run_id="restamp", source_id=h.COE)
    assert counts["unchanged"] == 1


def test_corrections_and_removals_are_revisions_never_overwrites(conn):
    store = TreatyStore(conn)
    first = store.revisions(h.NS, GERMANY_RATIFICATION)
    h.load_all(conn, v2=True)
    chain = store.revisions(h.NS, GERMANY_RATIFICATION)
    assert [r["change"] for r in chain] == ["new", "revised"] and chain[0] == first[0]
    assert [r["action_date"] for r in chain] == ["2098-09-03", "2098-09-04"]
    assert [r["depositary_date"] for r in chain] == ["2099-09-30", "2100-04-15"]
    removed = store.revisions(h.NS, EXAMPLONIA_SIGNATURE)
    assert [r["change"] for r in removed] == ["new", "removed-by-source"]
    assert removed[1]["action_date"] == "2098-03-01"  # the removal keeps what was published
    h.load_all(conn)  # the earlier page again: the signature is listed again, the date is re-published
    assert [r["change"] for r in store.revisions(h.NS, EXAMPLONIA_SIGNATURE)] == ["new", "removed-by-source",
                                                                                   "relisted"]


def test_as_of_lookup_selects_the_revision_published_by_a_date(conn):
    store = TreatyStore(conn)
    h.load_all(conn, v2=True)
    assert store.as_of(h.NS, GERMANY_RATIFICATION, "2100-01-01")["action_date"] == "2098-09-03"
    assert store.as_of(h.NS, GERMANY_RATIFICATION, "2100-05-01")["action_date"] == "2098-09-04"
    assert store.as_of(h.NS, GERMANY_RATIFICATION)["revision_no"] == 2
    assert store.as_of(h.NS, GERMANY_RATIFICATION, "2099-01-01") is None
    with pytest.raises(TreatiesError):
        store.as_of(h.NS, GERMANY_RATIFICATION, "not a date")


def test_receipts_and_feature_flags(conn):
    receipts = TreatyStore(conn).receipts(h.NS, f"run:{h.COE}:v1", scopes=h.READ_ONLY)
    assert receipts[0]["contract"] == "noesis-treaty-acquisition-receipt-v1" and receipts[0]["records"] == 12
    assert feature_enabled(conn, "treaties-coe") is False
    with pytest.raises(TreatiesError):
        feature_enabled(conn, "treaties")
    with pytest.raises(TreatiesError):
        TreatyStore(conn).receipts(h.NS, "x", scopes={"knowledge:legal:read"})
