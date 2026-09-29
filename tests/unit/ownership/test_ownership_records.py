"""O02 record model: kinds, bands, validity, unknowns, forbidden determinations and schema registration (#1851)."""

import json

import duckdb
import jsonschema
import pytest

from src.kb.ownership_records import (
    CONTROL_KINDS,
    PARENT_KINDS,
    SCHEMAS,
    OwnershipRecordError,
    record,
    register_schemas,
    schema,
    validate_record,
)
from tests.unit.ownership import harness

SOURCE = {"provider": "companies-house", "provider_record_id": "09990002:psc", "url": "https://find-and-update.company-information.service.gov.uk/company/09990002"}


def assertion(**overrides):
    fields = {"subject_key": "companies-house:gb-coh:09990002", "assertion_kind": "shareholding",
              "holder": {"key": "companies-house:gb-coh:09990001", "name": "EXAMPLA HOLDINGS PLC", "kind": "entity"},
              "share": {"band": {"min": "75", "max": "100", "min_inclusive": True, "max_inclusive": True}},
              "validity": {"from": "2016-04-06"}}
    fields.update(overrides)
    return record("ownership_assertion", "fixture:assertion", SOURCE, **fields)


def test_direct_ultimate_and_control_are_distinct_kinds_and_bands_stay_bands():
    assert set(PARENT_KINDS).isdisjoint(CONTROL_KINDS)
    band = assertion()
    assert band["share"]["band"] == {"min": "75", "max": "100", "min_inclusive": True, "max_inclusive": True}
    assert "exact" not in band["share"]
    assert band["validity"] == {"from": "2016-04-06", "to": None, "from_status": "stated", "to_status": "unknown"}
    assert "validity.to" in band["unknowns"] and "validity.from" not in band["unknowns"]
    with pytest.raises(OwnershipRecordError, match="consolidation"):
        assertion(assertion_kind="direct_parent")  # parents state no percentage
    parent = assertion(assertion_kind="direct_parent", share=None)
    assert parent["assertion_kind"] == "direct_parent"
    with pytest.raises(OwnershipRecordError):
        assertion(share={"exact": 8.2})  # floats are rejected; decimals stay exact strings
    with pytest.raises(OwnershipRecordError):
        assertion(share={"exact": "8.2", "band": {"min": "5"}})


def test_reporting_exceptions_name_no_holder_and_unknowns_are_listed():
    exception = record("ownership_assertion", "fixture:exception", SOURCE, subject_key="gleif:lei:x",
                       assertion_kind="reporting_exception", holder=None,
                       reporting_exception={"level": "ultimate", "category": "NATURAL_PERSONS"})
    assert exception["unknowns"] == ["validity.from", "validity.to"]
    with pytest.raises(OwnershipRecordError):
        record("ownership_assertion", "fixture:bad", SOURCE, subject_key="x", assertion_kind="reporting_exception",
               holder={"name": "someone", "kind": "entity"}, reporting_exception={"level": "psc", "category": "c"})
    unknown_holder = assertion(holder={"kind": "unknown"}, share=None)
    assert {"holder.identity", "share"} <= set(unknown_holder["unknowns"])
    officer = record("officer_role", "fixture:officer", SOURCE, entity_key="k", officer={"name": "POE, Kim"},
                     role="secretary")
    assert officer["appointed_status"] == "unknown" and officer["unknowns"] == ["appointed_on"]
    event = record("corporate_event", "fixture:event", SOURCE, entity_key="k", event_type="succession")
    assert event["date_status"] == "unknown" and set(event["unknowns"]) == {"event_date", "related_entity_key"}


def test_no_beneficial_ownership_sanctions_or_aml_determination_is_storable():
    for field in ("determination", "sanctions", "aml", "risk_score"):
        with pytest.raises(OwnershipRecordError) as caught:
            validate_record({**assertion(), field: "anything"})
        assert caught.value.code == "determination_forbidden"


def test_invalid_validity_and_dates_fail_closed():
    with pytest.raises(OwnershipRecordError):
        assertion(validity={"from": "2020-01-01", "to": "2019-01-01"})
    with pytest.raises(OwnershipRecordError):
        assertion(validity={"from": "06/04/2016"})
    with pytest.raises(OwnershipRecordError):
        assertion(validity={"to": "2020-01-01", "to_status": "open"})
    partial = assertion(validity={"from": "2016-04"})
    assert partial["validity"]["from"] == "2016-04"  # partial dates stay partial
    with pytest.raises(OwnershipRecordError):
        validate_record({**assertion(), "contract": "noesis-ownership-record-v0"})


def test_schemas_are_registered_in_the_schema_registry_and_fixture_records_validate():
    conn = duckdb.connect()
    scopes = {"knowledge:schema:register", "knowledge:schema:read", "knowledge:schema:validate"}
    modules = register_schemas(conn, principal_id="ownership-service", scopes=scopes)
    assert {m["name"] for m in modules} == set(SCHEMAS)
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    env = harness.Env().ready()
    from src.kb.ownership_store import OwnershipStore

    views = OwnershipStore(env.conn).records(harness.NS, principal_id=harness.PRINCIPAL, scopes=harness.SCOPES)
    kinds = set()
    for view in views:
        checked = registry.validate_instance({"kind": "schema", "name": "ownership-record", "version": "1.0.0"},
                                             view["record"], scopes=scopes)
        assert checked["valid"], (view["record"]["record_key"], checked["errors"])
        kinds.add(view["record"]["kind"])
    assert kinds == {"legal_entity", "person", "registration", "officer_role", "ownership_assertion",
                     "corporate_event", "filing_reference"}
    bad = registry.validate_instance({"kind": "schema", "name": "ownership-record", "version": "1.0.0"},
                                     {**views[0]["record"], "determination": "owner"}, scopes=scopes)
    assert not bad["valid"]
    assert register_schemas(conn, principal_id="ownership-service", scopes=scopes)[0].get("idempotent_replay")
    for name in SCHEMAS:
        jsonschema.Draft7Validator.check_schema(schema(name))
    assert json.loads(json.dumps(schema("ownership-dossier")))["properties"]["contract"]["const"] == "noesis-ownership-dossier-v1"


def test_entity_records_reference_canonical_entities_not_a_second_store():
    env = harness.Env().ready()
    tables = {r[0] for r in env.conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert {"canonical_entities", "ownership_records", "ownership_record_revisions"} <= tables
    assert not {t for t in tables if t.startswith("ownership_") and "entit" in t}
    rows = env.conn.execute("SELECT r.canonical_entity_id, c.preferred_name FROM ownership_records r JOIN canonical_entities c "
                            "ON c.canonical_id=r.canonical_entity_id WHERE r.kind='legal_entity'").fetchall()
    assert len(rows) == env.conn.execute("SELECT count(*) FROM ownership_records WHERE kind='legal_entity'").fetchone()[0]
    # Same name, different identifiers: two canonical entities, never one.
    names = {name for _, name in rows}
    assert "Exampla Holdings Ltd" in names and "Exampla Holdings plc" in names
    assert env.conn.execute("SELECT count(*) FROM entity_aliases").fetchone()[0] == 0  # no name aliasing
    person = env.conn.execute("SELECT c.preferred_name FROM ownership_records r JOIN canonical_entities c "
                              "ON c.canonical_id=r.canonical_entity_id WHERE r.kind='person'").fetchone()
    assert person == ("owner-scoped person",)  # the shared entity table never learns an owner-scoped name
