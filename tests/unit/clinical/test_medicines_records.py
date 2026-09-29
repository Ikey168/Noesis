"""Medicines-regulation records in the clinical record store (#2214, MR02)."""

from __future__ import annotations

import json

import duckdb
import jsonschema
import pytest

from src.kb.clinical_medicines import CONTRACT, record
from src.kb.clinical_records import ClinicalRecordError, ClinicalRecordStore, record_id, register_schemas
from tests.unit.clinical.harness import ROOT

SCOPES = {"knowledge:clinical:read", "knowledge:clinical:write", "knowledge:ingestion:execute",
          "namespace:ns:read", "namespace:ns:write"}
SCHEMA = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-clinical-medicines-record-v1.json").read_text())
EMA = {"provider": "ema", "native_id": "EMEA/H/C/009001", "jurisdiction": "EU", "authority": "EMA",
       "source_url": "https://www.ema.europa.eu/en/medicines/human/EPAR/noetiglu-fixture"}


def authorisation(kind="grant", status="authorised", date="2020-02-10", **extra):
    return record("marketing-authorisation", **EMA,
                  native_version={"version": None, "date": date, "basis": "epar-revision"},
                  product={"provider": "ema", "native_id": "EMEA/H/C/009001"},
                  procedure={"kind": "product-number", "number": "EMEA/H/C/009001"},
                  event={"kind": kind, "native_status": status, "status": status, "effective_date": date,
                         "locator": {"json_pointer": "/data/0/marketing_authorisation_date"}, **extra})


def label(version="3", text="Fixture warning text.", effective="2025-03-10"):
    return record("label-revision", **EMA, native_version={"version": version, "date": effective,
                                                          "basis": "epar-revision"},
                  product={"provider": "ema", "native_id": "EMEA/H/C/009001"},
                  document={"kind": "smpc", "id": "EMEA/H/C/009001:smpc", "version": version,
                            "effective_date": effective},
                  sections=[{"code": "4.4", "code_system": "smpc", "title": "Special warnings", "text": text,
                             "locator": {"url": EMA["source_url"], "lines": [10, 12]}}],
                  omitted_sections=[{"code": "4.2", "code_system": "smpc", "title": "Posology",
                                     "reason": "dosing text is not retained"}])


def communication(updates=()):
    return record("safety-communication", provider="fda-dsc", native_id="dsc-fixture", jurisdiction="US",
                  authority="FDA", source_url="https://www.fda.gov/drugs/drug-safety-and-availability/dsc-fixture",
                  native_version={"version": None, "date": (list(updates)[-1]["date"] if updates else "2026-04-15"),
                                  "basis": "publication-date"},
                  title="FDA adds warning (fixture)", issued="2026-04-15", updates=list(updates),
                  named_substances=[{"name": "noetiglutide", "role": "generic name", "locator": {"table_row": 1}}])


def test_round_trip_through_the_clinical_store_with_source_revision_and_as_of_time():
    conn = duckdb.connect()
    store = ClinicalRecordStore(conn, now=lambda: 5)
    grant = authorisation()
    summary = store.ingest("ns", "ema-epar", [grant, label()], observation_id="o1", observed_at_ms=100,
                           scopes=SCOPES)
    assert len(summary["created"]) == 2
    got = store.get("ns", record_id("ns", grant), scopes=SCOPES)
    assert got["record"] == grant and got["record"]["contract"] == CONTRACT
    assert got["observed_at_ms"] == 100 and got["record"]["source_url"].startswith("https://")
    assert got["version_key"] == "epar-revision:2020-02-10"
    again = store.ingest("ns", "ema-epar", [grant], observation_id="o2", observed_at_ms=200, scopes=SCOPES)
    assert again["unchanged"] == [record_id("ns", grant)]


def test_a_status_change_is_a_new_record_and_label_revisions_coexist():
    conn = duckdb.connect()
    store = ClinicalRecordStore(conn)
    grant, withdrawal = authorisation(), authorisation("withdrawal", "withdrawn", "2024-11-15",
                                                       reason={"text": "commercial reasons (fixture)",
                                                               "locator": {"paragraph": 3}})
    store.ingest("ns", "ema-epar", [grant, label("3")], observation_id="o1", observed_at_ms=1, scopes=SCOPES)
    store.ingest("ns", "ema-epar", [withdrawal, label("4", "Changed text.")], observation_id="o2", observed_at_ms=2,
                 scopes=SCOPES)
    assert record_id("ns", grant) != record_id("ns", withdrawal)
    rows = store.find("ns", scopes=SCOPES, kinds={"marketing-authorisation", "label-revision"})
    assert len(rows) == 4
    versions = sorted(r["record"]["document"]["version"] for r in rows if r["record_kind"] == "label-revision")
    assert versions == ["3", "4"]


def test_communication_updates_are_revisions_that_keep_the_original():
    conn = duckdb.connect()
    store = ClinicalRecordStore(conn)
    first = communication()
    update = {"date": "2026-06-02", "text": "[6-2-2026] UPDATE: fixture", "locator": {"block": 1}}
    store.ingest("ns", "fda-dsc", [first], observation_id="o1", observed_at_ms=1, scopes=SCOPES)
    summary = store.ingest("ns", "fda-dsc", [communication([update])], observation_id="o2", observed_at_ms=2,
                           scopes=SCOPES)
    rid = record_id("ns", first)
    assert summary["revised"] == [rid]
    history = store.history("ns", rid, scopes=SCOPES)["revisions"]
    assert [h["revision"] for h in history] == [1, 2] and history[0]["record"]["updates"] == []
    assert {"kind": "communication_updated", "path": "updates", "added": [update]} in history[1]["amendments"]


def test_boundary_disclaimer_and_locators_are_enforced():
    with pytest.raises(ClinicalRecordError) as exc:
        authorisation(dose="1 mg")
    assert exc.value.code == "outside_boundary"
    with pytest.raises(ClinicalRecordError) as exc:
        record("medicinal-product", provider="openfda", native_id="NDA299001", jurisdiction="US", authority="FDA",
               source_url="https://api.fda.gov/drug/drugsfda.json",
               native_version={"version": None, "date": None, "basis": "observation"}, name="X")
    assert exc.value.code == "missing_disclaimer"
    with pytest.raises(ClinicalRecordError):
        record("label-revision", **EMA, native_version={"version": "1", "date": None, "basis": "epar-revision"},
               product={"provider": "ema", "native_id": "EMEA/H/C/009001"},
               document={"kind": "smpc", "id": "x", "version": "1"},
               sections=[{"code": "34067-9", "code_system": "loinc", "text": "t", "locator": {"x": 1}}])
    with pytest.raises(ClinicalRecordError):
        record("label-revision", **EMA, native_version={"version": "1", "date": None, "basis": "epar-revision"},
               product={"provider": "ema", "native_id": "EMEA/H/C/009001"},
               document={"kind": "smpc", "id": "x", "version": "1"}, sections=[],
               omitted_sections=[{"code": "4.2", "code_system": "smpc", "text": "dosing"}])
    with pytest.raises(ClinicalRecordError):  # EMA records are EU/EMA
        record("medicinal-product", **{**EMA, "jurisdiction": "US"},
               native_version={"version": None, "date": None, "basis": "observation"}, name="X")
    assert "document.effective_date" in label(effective=None)["unknowns"]


def test_schema_is_registered_with_the_pack_and_records_validate():
    conn = duckdb.connect()
    scopes = {"knowledge:schema:register", "knowledge:schema:read", "knowledge:schema:validate"}
    modules = register_schemas(conn, principal_id="svc", scopes=scopes)
    assert "noesis-clinical-medicines-record" in {m["name"] for m in modules}
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    for item in (authorisation(), label(), communication()):
        result = registry.validate_instance({"kind": "schema", "name": "noesis-clinical-medicines-record",
                                             "version": "1.0.0"}, item, scopes=scopes)
        assert result["valid"], result["errors"]
        jsonschema.validate(item, SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**label(), "dosing": "x"}, SCHEMA)
