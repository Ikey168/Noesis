"""Fixture-only upgrade impact and guarded apply integration tests."""

import copy
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_pack_upgrades import SourcePackUpgradeStore
from src.ingestion.source_packs import SourcePackError, SourcePackStore
from src.kb.authored_reports import AuthoredReportStore
from src.kb.investigation_templates import InvestigationTemplateStore
from src.kb.research_projects import ResearchProjectStore
from src.ingestion.revisions import DocumentRevisionStore

ROOT = Path(__file__).resolve().parents[3]
OPERATOR = {"operator"}


def pack():
    return json.loads((ROOT / "config/source_packs/research.json").read_text())


def candidate(old):
    value = copy.deepcopy(old)
    value["version"] = "1.3.0"
    value["sources"] = [v for v in value["sources"] if v["source_id"] != "openalex-works"]
    value["sources"][0]["auth"] = {"kind": "required-secret", "secret_ref": "NOESIS_TEST_KEY"}
    value["sources"][0]["mapping"] = {"target_schema": "scholarly-work-v1", "version": "2.0.0"}
    return value


@pytest.fixture
def seeded():
    conn = duckdb.connect(":memory:")
    old = pack()
    SourcePackStore(conn).install(old, principal_id="operator", enable=True)
    runtime = SourcePackRuntime(conn)
    for source in old["sources"]:
        runtime.accept_license(old["pack_id"], source["source_id"], principal_id="operator")
    runtime.set_schedule(old["pack_id"], {"kind": "interval", "interval_s": 600}, principal_id="operator")
    yield conn, old, runtime
    conn.close()


def _definition(old):
    return {"name": "Research", "description": "Pinned discovery", "parameters": {},
            "questions": ["What changed?"], "success_criteria": ["Evidence found"],
            "scope": {"domains": ["research"], "namespaces": []},
            "source_packs": [{"pack_id": old["pack_id"], "version": old["version"]}],
            "report_outline": ["Evidence"]}


def _dependents(conn, old):
    templates = InvestigationTemplateStore(conn)
    alice = templates.create("research", "alice-template", _definition(old),
                             principal_id="alice", scopes=OPERATOR)
    templates.create("research", "bob-template", _definition(old),
                     principal_id="bob", scopes=OPERATOR)
    ResearchProjectStore(conn).create(
        "research", "alice-project", questions=["Q"], success_criteria=["A"],
        scope={"domains": ["research"], "namespaces": []}, budget={},
        origin={"source_packs": [{"pack_id": old["pack_id"], "version": old["version"]}]},
        principal_id="alice", scopes=OPERATOR)
    ResearchProjectStore(conn).create(
        "research", "bob-project", questions=["Q"], success_criteria=["A"],
        scope={"domains": ["research"], "namespaces": []}, budget={},
        origin={"source_packs": [{"pack_id": old["pack_id"], "version": old["version"]}]},
        principal_id="bob", scopes=OPERATOR)
    DocumentRevisionStore(conn)
    payload = json.dumps({"metadata": {"source_pack_version": old["version"]}})
    conn.execute("""INSERT INTO document_revision_records
      (document_id,revision,revision_id,source_id,pack_id,payload_json,payload_hash,
       content_hash,metadata_hash,change_kind,change_class,lifecycle,observed_at_ms,
       committed_watermark,created_at_ms) VALUES
      ('doc-1',1,'rev-1','crossref-works',?,?,?,?,?,?,?,'active',1,1,1)""",
      [old["pack_id"], payload, "a"*64, "b"*64, "c"*64, "created", "substantive"])
    report = {"title": "Result", "snapshot": {"id": "snap", "generations": {"research": 1}},
              "bibliography": [{"id": "ref", "text": "Source"}], "limitations": [],
              "sections": [{"id": "sec", "title": "Evidence", "assertions": [{
                  "id": "asrt", "text": "An assertion", "kind": "sourced", "citations": ["ref"],
                  "dependencies": [
                      {"kind": "source", "id": "doc-1", "revision": "rev-1",
                       "namespace": "research", "locator": {"document_id": "doc-1", "revision_id": "rev-1"}},
                      {"kind": "claim", "id": "claim-1", "revision": "1",
                       "namespace": "research", "locator": {}}
                  ]}]}]}
    AuthoredReportStore(conn).create("research", "alice-report", report,
                                    principal_id="alice", scopes=OPERATOR)
    return alice


def test_impact_traces_saved_objects_without_mutation_and_scopes(seeded):
    conn, old, _ = seeded
    _dependents(conn, old)
    upgrade = SourcePackUpgradeStore(conn)
    newer = candidate(old)
    before = SourcePackStore(conn).status(old["pack_id"])
    result = upgrade.preview_impact(newer, principal_id="operator", scopes=OPERATOR, limit=2)
    assert result["total"] == 6
    assert result["next_offset"] == 2
    all_effects = result["effects"] + upgrade.preview_impact(
        newer, principal_id="operator", scopes=OPERATOR, offset=2)["effects"]
    assert {v["kind"] for v in all_effects} == {"template", "project", "schedule", "report"}
    assert all(v["retained_status"] == "retained" for v in all_effects if v["kind"] in {"template", "project"})
    report = next(v for v in all_effects if v["kind"] == "report")
    assert report["unsupported_dependency_types"] == ["claim"]
    assert report["citation_action"].startswith("none")
    assert SourcePackStore(conn).status(old["pack_id"])["manifest_hash"] == before["manifest_hash"]
    restricted = upgrade.preview_impact(newer, principal_id="alice", scopes={
        "knowledge:read", "knowledge:projects:read", "knowledge:reports:read",
        "namespace:research:read", "domain:research:read", "document:doc-1:read"})
    assert restricted["total"] == 3
    assert restricted["inaccessible"] == {"withheld": True}
    for contract, output in (("impact", result),):
        schema = json.loads((ROOT / f"contracts/schemas/jsonschema/noesis-source-pack-upgrade-{contract}-v1.json").read_text())
        Draft7Validator(schema).validate(output)


def test_apply_rolls_back_on_preflight_then_replays_and_keeps_old_schedule(seeded):
    conn, old, runtime = seeded
    newer = candidate(old)
    upgrade = SourcePackUpgradeStore(conn)
    preview = upgrade.preview_impact(newer, principal_id="operator", scopes=OPERATOR)
    args = {"preview_hash": preview["preview"]["preview_hash"], "impact_hash": preview["impact_hash"],
            "apply_key": "review-1", "principal_id": "operator", "scopes": OPERATOR,
            "dns_resolver": lambda _: ["8.8.8.8"]}
    with pytest.raises(SourcePackError) as denied:
        upgrade.apply(newer, **args)
    assert denied.value.code == "preflight_failed"
    assert SourcePackStore(conn).status(old["pack_id"])["version"] == old["version"]
    prior_schedule = conn.execute("SELECT schedule_json,enabled,next_run_at_ms FROM source_pack_schedules WHERE pack_id=?", [old["pack_id"]]).fetchone()
    receipt = upgrade.apply(newer, accepted_license_sources=[v["source_id"] for v in newer["sources"]],
                            secret_available=lambda _: True, **args)
    assert receipt["candidate_version"] == "1.3.0" and receipt["retained_old_version"]
    assert conn.execute("SELECT schedule_json,enabled,next_run_at_ms FROM source_pack_schedules WHERE pack_id=?", [old["pack_id"]]).fetchone() == prior_schedule
    replay = upgrade.apply(newer, accepted_license_sources=[v["source_id"] for v in newer["sources"]],
                           secret_available=lambda _: True, **args)
    assert replay["idempotent"] and replay["receipt_hash"] == receipt["receipt_hash"]
    with pytest.raises(SourcePackError) as reused:
        upgrade.apply(newer, accepted_license_sources=[], secret_available=lambda _: True, **args)
    assert reused.value.code == "apply_conflict"
    assert upgrade.inspect_receipt(old["pack_id"], "review-1", principal_id="operator",
                                   scopes=OPERATOR)["current_matches_candidate"]
    assert conn.execute("SELECT count(*) FROM source_pack_versions WHERE pack_id=?", [old["pack_id"]]).fetchone()[0] == 2
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-source-pack-upgrade-receipt-v1.json").read_text())
    Draft7Validator(schema).validate(receipt)


def test_stale_preview_and_reused_key_are_rejected(seeded):
    conn, old, _ = seeded
    newer = copy.deepcopy(old)
    newer["version"] = "1.3.0"
    upgrade = SourcePackUpgradeStore(conn)
    preview = upgrade.preview_impact(newer, principal_id="operator", scopes=OPERATOR)
    other = copy.deepcopy(old)
    other["version"] = "1.2.1"
    SourcePackStore(conn).install(other, principal_id="operator")
    with pytest.raises(SourcePackError) as stale:
        upgrade.apply(newer, preview_hash=preview["preview"]["preview_hash"],
                      impact_hash=preview["impact_hash"], apply_key="stale", principal_id="operator",
                      scopes=OPERATOR)
    assert stale.value.code == "stale_preview"


def test_missing_retained_pin_and_explicit_schedule_migration(seeded):
    conn, old, _ = seeded
    definition = _definition(old)
    definition["source_packs"][0]["version"] = "0.9.0"
    InvestigationTemplateStore(conn).create("research", "missing-pin", definition,
                                            principal_id="operator", scopes=OPERATOR)
    newer = copy.deepcopy(old)
    newer["version"] = "1.3.0"
    upgrade = SourcePackUpgradeStore(conn)
    preview = upgrade.preview_impact(newer, principal_id="operator", scopes=OPERATOR)
    template = next(v for v in preview["effects"] if v["kind"] == "template")
    assert template["retained_status"] == "missing_retained_version"
    receipt = upgrade.apply(newer, preview_hash=preview["preview"]["preview_hash"],
                            impact_hash=preview["impact_hash"], apply_key="migrate",
                            principal_id="operator", scopes=OPERATOR, migrate_schedule=True,
                            dns_resolver=lambda _: ["8.8.8.8"])
    assert receipt["schedule_migrated"]
    schedule = json.loads(conn.execute("SELECT schedule_json FROM source_pack_schedules WHERE pack_id=?",
                                       [old["pack_id"]]).fetchone()[0])
    assert schedule["interval_s"] == old["defaults"]["schedule"]["interval_s"]


def test_disabled_pack_can_upgrade_without_enabling_it(seeded):
    conn, old, _ = seeded
    SourcePackStore(conn).set_enabled(old["pack_id"], False, principal_id="operator")
    newer = copy.deepcopy(old)
    newer["version"] = "1.3.0"
    upgrade = SourcePackUpgradeStore(conn)
    preview = upgrade.preview_impact(newer, principal_id="operator", scopes=OPERATOR)
    receipt = upgrade.apply(newer, preview_hash=preview["preview"]["preview_hash"],
                            impact_hash=preview["impact_hash"], apply_key="disabled",
                            principal_id="operator", scopes=OPERATOR,
                            dns_resolver=lambda _: ["8.8.8.8"])
    assert receipt["preflight"]["ready"] is False
    assert all(source["ready"] for source in receipt["preflight"]["sources"])
    assert SourcePackStore(conn).status(old["pack_id"])["enabled"] is False
