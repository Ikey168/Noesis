"""Generated dependency/advisory fixtures; no live provider validation."""

import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.domains.technical.impact_reports import ImpactReportError, ImpactReportStore
from src.domains.technical.inventory import InventoryStore
from src.domains.technical.model import package_object_id, record_advisory_range, record_object
from src.ingestion.revisions import DocumentRevisionStore
from src.kb.authored_reports import AuthoredReportStore
from src.kb.research_projects import ResearchProjectStore
from src.kb.investigation_templates import _validate as validate_template


SCHEMA_DIR = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"
AUTH = {"principal_id": "alice", "scopes": {"operator"}}


def _source(revisions, document_id):
    return revisions.observe({"document_id": document_id, "title": document_id,
                              "content": "Captured generated evidence for " + document_id})["revision_id"]


def _package(conn, revisions, name):
    coordinate = "pkg:pypi:" + name
    document_id = "pypi:" + name
    _source(revisions, document_id)
    package_id = package_object_id(coordinate)
    record_object(conn, object_type="package", object_id=package_id, coordinate=coordinate,
                  canonical_name=name, source_document_id=document_id, observed_at=1)
    return package_id


def _advisory(conn, revisions, package_id, advisory_id, events):
    document_id = "osv:" + advisory_id
    _source(revisions, document_id)
    record_object(conn, object_type="advisory", object_id=advisory_id,
                  canonical_name=advisory_id, status="active",
                  source_url="https://osv.dev/vulnerability/" + advisory_id,
                  source_document_id=document_id, observed_at=2)
    record_advisory_range(conn, advisory_id, package_id, ecosystem="pypi",
                          range_type="ECOSYSTEM", events=events,
                          source_document_id=document_id, observed_at=2)


def test_report_comparison_export_and_existing_project_report_contracts():
    template = json.loads((Path(__file__).resolve().parents[3] /
                           "config/investigation_templates/technical-dependency-impact.json").read_text())
    assert validate_template(template)["source_packs"][0]["pack_id"] == "technical-software-knowledge"
    conn = duckdb.connect()
    revisions = DocumentRevisionStore(conn)
    vulnerable = _package(conn, revisions, "vulnerable")
    fixed = _package(conn, revisions, "fixed")
    _advisory(conn, revisions, vulnerable, "A-1", [{"introduced": "1.0"}, {"fixed": "2.0"}])
    _advisory(conn, revisions, fixed, "A-2", [{"introduced": "1.0"}, {"fixed": "2.0"}])
    inventory = InventoryStore(conn).import_inventory(
        "vulnerable==1.5\nfixed==2.0\nmissing==1.0\n", "requirements.txt", owner_id="alice")
    project = ResearchProjectStore(conn).create(
        "research", "technical-project", questions=["Which dependencies are affected?"],
        success_criteria=["Classify captured dependencies"],
        scope={"namespaces": ["research"], "domains": []}, budget={}, **AUTH)
    store = ImpactReportStore(conn)
    first = store.create("research", "snapshot-one", inventory["inventory_id"],
                         project_id=project["project_id"], **AUTH)
    assert [f["classification"] for f in first["findings"]] == [
        "affected", "unaffected_under_assessed_ranges", "unknown"]
    assert len(first["source_snapshots"]) == 4
    assert first["inventory_hash"] == inventory["inventory_hash"]
    jsonschema.validate(first, json.loads((SCHEMA_DIR / "noesis-technical-impact-report-v1.json").read_text()))
    assert store.create("research", "snapshot-one", inventory["inventory_id"],
                        project_id=project["project_id"], **AUTH)["idempotent"]
    _advisory(conn, revisions, fixed, "A-3", [{"introduced": "2.0"}, {"fixed": "3.0"}])
    second = store.create("research", "snapshot-two", inventory["inventory_id"],
                          project_id=project["project_id"], **AUTH)
    compared = store.compare("research", first["report_id"], second["report_id"], **AUTH)
    assert compared["same_inventory"] and not compared["same_sources"]
    assert compared["changed_count"] == 1
    jsonschema.validate(compared, json.loads((SCHEMA_DIR / "noesis-technical-impact-comparison-v1.json").read_text()))
    exported = store.export("research", first["report_id"], **AUTH)
    assert len(exported["affected"]) == len(exported["unaffected_under_assessed_ranges"]) == len(exported["unknown"]) == 1
    assert exported["authored_report_content"]["sections"][0]["assertions"][0]["dependencies"][0]["revision"]
    assert not exported["executed_changes"]
    jsonschema.validate(exported, json.loads((SCHEMA_DIR / "noesis-technical-impact-export-v1.json").read_text()))
    authored = AuthoredReportStore(conn).create("research", "impact", exported["authored_report_content"], **AUTH)
    assert authored["content"]["bibliography"] == exported["bibliography"]
    linked = ResearchProjectStore(conn).revise(
        "research", project["project_id"], project["revision"],
        add_links=[exported["project_link"]], **AUTH)
    assert any(all(link[key] == value for key, value in exported["project_link"].items())
               for link in linked["links"])
    conn.close()


def test_missing_source_downgrades_claim_and_access_is_rechecked():
    conn = duckdb.connect()
    revisions = DocumentRevisionStore(conn)
    package_id = _package(conn, revisions, "library")
    record_object(conn, object_type="advisory", object_id="A-missing",
                  canonical_name="A-missing", status="active", observed_at=1,
                  source_document_id="osv:absent")
    record_advisory_range(conn, "A-missing", package_id, ecosystem="pypi",
                          range_type="ECOSYSTEM", events=[{"introduced": "0"}],
                          source_document_id="osv:absent", observed_at=1)
    inventory = InventoryStore(conn).import_inventory("library==1.0\n", "requirements.txt", owner_id="alice")
    store = ImpactReportStore(conn)
    report = store.create("research", "missing", inventory["inventory_id"], **AUTH)
    assert report["findings"][0]["classification"] == "unknown"
    assert report["findings"][0]["advisories"][0]["reason"] == "source_revision_unavailable"
    assert report["missing_source_documents"] == ["osv:absent"]
    limited = {"knowledge:technical:read", "namespace:research:read", "document:pypi:library:read"}
    assert store.inspect("research", report["report_id"], principal_id="alice", scopes=limited)["report_id"] == report["report_id"]
    with pytest.raises(ImpactReportError) as error:
        store.inspect("research", report["report_id"], principal_id="alice",
                      scopes=limited - {"document:pypi:library:read"})
    assert error.value.code == "unauthorized"
    conn.close()
