"""Substance, identifier, classification, restriction, authorisation and data-point records (CH02, #2284)."""

from __future__ import annotations

import json

import duckdb
import jsonschema
import pytest

from src.kb.substances_records import (
    CONTRACT,
    DATA_POINT_LABEL,
    SCHEMA_VERSIONS,
    SubstanceError,
    cas_valid,
    classify_query,
    ec_valid,
    statement,
    validate_statement,
)
from src.kb.substances_store import SubstanceStore
from tests.unit.chemicals.harness import NS, ROOT

SCHEMA = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-substance-record-v1.json").read_text())
SUBJECT = {"key": "echa:substance:100.001.133", "kind": "single-component", "name": "4,4'-isopropylidenediphenol"}
SOURCE = {"url": "https://chem.echa.europa.eu/api-cnl-inventory/v1/harmonised/100.001.133",
          "locator": "/entries/0/history/0", "attribution": "Source: European Chemicals Agency"}
ACT_2008 = {"title": "Regulation (EC) No 1272/2008", "celex": "32008R1272", "eli": None, "atp": None,
            "entry": "index 604-030-00-0", "locator": "Annex VI"}
ACT_ATP9 = {**ACT_2008, "title": "Commission Regulation (EU) 2016/1179", "celex": "32016R1179", "atp": "ATP 9"}


def classification(hazards, act, applies_from, event="inclusion"):
    return statement("classification", "echa-clp", SUBJECT, "harmonised:604-030-00-0",
                     {"kind": "harmonised", "index_number": "604-030-00-0",
                      "hazard_classes": [{"hazard_class_category": c, "hazard_statement": h} for c, h in hazards]},
                     source=SOURCE, event=event, effective_from=applies_from, legal_act=act)


def test_identifier_check_digits_and_query_classification():
    assert cas_valid("80-05-7") and not cas_valid("80-05-8")
    assert ec_valid("201-245-8") and not ec_valid("201-245-9")
    assert classify_query("80-05-7") == ("cas", "80-05-7")
    assert classify_query("201-245-8") == ("ec", "201-245-8")
    assert classify_query("iisbaclafkspit-uhfffaoysa-n") == ("inchikey", "IISBACLAFKSPIT-UHFFFAOYSA-N")
    assert classify_query("DTXSID7020182") == ("dtxsid", "DTXSID7020182")
    assert classify_query("  Bisphenol   A ") == ("name", "bisphenol a")
    assert SCHEMA_VERSIONS["substance-record"] == "1.0.0"


def test_statements_validate_against_the_contract_and_reject_verdicts_and_synthesis():
    value = classification([("Repr. 2", "H361f")], ACT_2008, "2009-01-20")
    jsonschema.validate(value, SCHEMA)
    assert value["contract"] == CONTRACT
    for bad in ({"hazard_score": 3}, {"safety_advice": "wear gloves"}, {"synthesis_route": "x"}):
        with pytest.raises(SubstanceError) as caught:
            validate_statement({**value, "as_published": {**value["as_published"], **bad}})
        assert caught.value.code == "invalid_record"
    with pytest.raises(SubstanceError):  # a classification names its legal act
        validate_statement({**value, "legal_act": None})
    with pytest.raises(SubstanceError):  # a restriction holds the entry number and conditions text
        statement("restriction", "echa-reach", SUBJECT, "annex-xvii:66", {"entry_number": "66"}, source=SOURCE,
                  legal_act=ACT_2008)
    with pytest.raises(SubstanceError):  # a malformed CAS must be flagged, never used silently
        statement("identifier", "pubchem", {"key": "pubchem:cid:1", "kind": "unknown"}, "cas:80-05-8",
                  {"scheme": "cas", "value": "80-05-8"}, source=SOURCE)


def test_data_points_are_labelled_as_the_sources_data():
    point = statement("data_point", "comptox", {"key": "comptox:dtxsid:DTXSID7020182", "kind": "unknown"},
                      "toxval:1", {"endpoint": "NOAEL", "value": "5", "unit": "mg/kg-day",
                                   "study_reference": "ref", "data_source": "src"}, source=SOURCE)
    assert point["label"] == DATA_POINT_LABEL
    with pytest.raises(SubstanceError):
        validate_statement({**point, "label": "a hazard conclusion"})


def test_round_trip_and_revisions_coexist_without_overwrite():
    store = SubstanceStore(duckdb.connect(":memory:"))
    first = store.apply(NS, classification([("Repr. 2", "H361f")], ACT_2008, "2009-01-20"))
    again = store.apply(NS, classification([("Repr. 2", "H361f")], ACT_2008, "2009-01-20"))
    assert first["status"] == "created" and again["status"] == "unchanged"
    second = store.apply(NS, classification([("Repr. 1B", "H360F")], ACT_ATP9, "2018-03-01", event="amendment"))
    assert second["status"] == "revised" and second["record_id"] == first["record_id"]
    revisions = store.revisions(NS, first["record_id"])
    assert [r["revision_no"] for r in revisions] == [1, 2]
    assert revisions[0]["statement"]["as_published"]["hazard_classes"][0]["hazard_class_category"] == "Repr. 2"
    assert revisions[1]["legal_act"]["celex"] == "32016R1179" and revisions[1]["effective_from"] == "2018-03-01"
    assert revisions[0]["statement"] == classification([("Repr. 2", "H361f")], ACT_2008, "2009-01-20")
    assert store.record(NS, first["record_id"])["subject_key"] == SUBJECT["key"]


def test_identifier_revisions_index_every_published_value_including_conflicts():
    store = SubstanceStore(duckdb.connect(":memory:"))
    subject = {"key": "pubchem:cid:6623", "kind": "single-component", "name": "Bisphenol A"}
    for value in ("80-05-7", "27100-33-0"):
        store.apply(NS, statement("identifier", "pubchem", subject, f"cas:{value}",
                                  {"scheme": "cas", "value": value, "conflict": True}, source=SOURCE))
    rows = store.identifiers(NS, subject["key"])
    assert {r["value"] for r in rows} == {"80-05-7", "27100-33-0"} and all(r["conflict"] for r in rows)
    assert store.find_subjects(NS, "cas", "80-05-7") == [subject["key"]]
