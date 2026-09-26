"""Offline official-record fixture workflow; no live provider or legal validation."""

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.domains.political.legislative_dossiers import DossierError, LegislativeDossierStore
from src.ingestion.connectors.political_official import PoliticalOfficialConnector
from src.ingestion.document_store import DocumentStore
from src.kb.authored_reports import AuthoredReportStore
from src.kb.citation_alerts import CitationAlertStore
from src.kb.research_projects import ResearchProjectStore
from tools.knowledge_engine_mcp.political_dossiers import register as register_political_dossiers

ROOT = Path(__file__).resolve().parents[3]
BASE_SCOPES = {
    "knowledge:political:dossier:read", "knowledge:political:dossier:write",
    "namespace:research:read", "namespace:research:write",
}


def _ref(conn, document_id):
    revision_id = conn.execute(
        "SELECT revision_id FROM document_current_revisions WHERE document_id=?",
        [document_id],
    ).fetchone()[0]
    return {"document_id": document_id, "revision_id": revision_id}


def _fixture():
    conn = duckdb.connect(":memory:")
    connector = PoliticalOfficialConnector()
    documents = list(connector.harvest({
        "offline": True, "source_ids": ["de-bundestag-dip", "eu-eurlex-regulatory"],
    }))
    for doc in documents:
        doc.ingested_at = 1000
    stored = DocumentStore(conn)
    outcome = stored.upsert(documents)
    assert outcome.inserted == 2 and outcome.invalid == 0
    de, eu = documents
    scopes = BASE_SCOPES | {f"document:{doc.document_id}:read" for doc in documents}
    return conn, stored, de.to_dict(), eu.to_dict(), scopes


def _add(stored, template, *, identity, document_type, content, political, observed_at):
    payload = dict(template)
    metadata = dict(payload["metadata"])
    metadata.update({
        "document_type": document_type, "official_identifier": identity,
        "political": json.dumps(political),
    })
    payload.update({
        "document_id": f"political:de-bundestag-dip:{identity}",
        "url": f"https://example.invalid/de/{identity}",
        "title": identity, "content": content, "metadata": metadata,
        "ingested_at": observed_at,
    })
    result = stored.upsert([payload])
    assert result.invalid == 0
    return payload


def _schema(name, value):
    schema = json.loads((ROOT / "contracts/schemas/jsonschema" / name).read_text())
    Draft202012Validator(schema).validate(value)


def test_official_fixture_dossier_replay_ambiguity_and_access_revocation():
    conn, stored, de, eu, scopes = _fixture()
    dossier = LegislativeDossierStore(conn, now=lambda: 2000)
    de_ref, eu_ref = _ref(conn, de["document_id"]), _ref(conn, eu["document_id"])
    saved = dossier.save(
        "research", "clean-heating", "DE", "proposal:de:bt-test-42", [de_ref],
        principal_id="alice", scopes=scopes,
    )
    assert saved["revision"] == 1 and saved["stages"][0]["stage"] == "vote"
    assert saved["stages"][0]["raw_document_id"] == "BT-TEST-21-42"
    assert saved["stages"][0]["citation"]["revision_id"] == de_ref["revision_id"]
    assert "proposal" in saved["missing_stages"]
    _schema("noesis-legislative-dossier-v1.json", saved)
    assert dossier.save(
        "research", "clean-heating", "DE", "proposal:de:bt-test-42", [de_ref],
        principal_id="alice", scopes=scopes,
    )["idempotent"]
    with pytest.raises(DossierError) as mixed:
        dossier.save(
            "research", "mixed", "DE", "proposal:de:bt-test-42", [de_ref, eu_ref],
            principal_id="alice", scopes=scopes,
        )
    assert mixed.value.code == "jurisdiction_mismatch"
    eu_dossier = dossier.save(
        "research", "eu-regulation", "EU", "instrument:eu:test-2026r0001", [eu_ref],
        principal_id="alice", scopes=scopes,
    )
    assert eu_dossier["procedure_identity_state"] == "instrument_anchor_only"
    assert eu_dossier["stages"][0]["normalized_instrument_id"] == "instrument:eu:test-2026r0001"
    assert eu_dossier["dossier_id"] != saved["dossier_id"]
    eu_timeline = dossier.timeline(
        "research", eu_dossier["dossier_id"], principal_id="alice", scopes=scopes,
    )
    assert any(entry["event_kind"] == "commencement" and entry["source_field"] == "metadata.effective_from" for entry in eu_timeline["entries"])
    related = dict(eu)
    related["metadata"] = dict(eu["metadata"])
    related["metadata"]["political"] = json.dumps({
        "instrument_id": "instrument:eu:test-2026r0001",
        "related_procedures": [{
            "kind": "implements", "jurisdiction": "DE",
            "procedure_id": "proposal:de:bt-test-42",
            "source_quote": "The regulation establishes a clean products reporting standard.",
        }],
    })
    related["ingested_at"] = 1500
    assert stored.upsert([related]).updated == 1
    eu_linked = dossier.save(
        "research", "eu-regulation", "EU", "instrument:eu:test-2026r0001",
        [_ref(conn, eu["document_id"])], principal_id="alice", scopes=scopes,
        dossier_id=eu_dossier["dossier_id"], expected_revision=1,
    )
    assert eu_linked["stages"][0]["attributable_relationships"][0]["jurisdiction"] == "DE"

    ambiguous = _add(
        stored, de, identity="BT-TEST-AMBIGUOUS", document_type="amendment",
        content="A proposed amendment lacks a procedure number.", political={}, observed_at=1200,
    )
    scopes.add(f"document:{ambiguous['document_id']}:read")
    second = dossier.save(
        "research", "clean-heating", "DE", "proposal:de:bt-test-42",
        [de_ref, _ref(conn, ambiguous["document_id"])],
        principal_id="alice", scopes=scopes, dossier_id=saved["dossier_id"], expected_revision=1,
    )
    assert second["revision"] == 2 and len(second["review_candidates"]) == 1
    assert len(second["stages"]) == 1
    assert dossier.inspect(
        "research", saved["dossier_id"], revision=1, principal_id="alice", scopes=scopes,
    )["revision"] == 1
    with pytest.raises(DossierError) as denied:
        dossier.inspect(
            "research", saved["dossier_id"], principal_id="alice",
            scopes=scopes - {f"document:{ambiguous['document_id']}:read"},
        )
    assert denied.value.code == "unauthorized"


def test_timeline_late_observation_conflict_future_effect_and_revision_export():
    conn, stored, de, _eu, scopes = _fixture()
    dossier = LegislativeDossierStore(conn, now=lambda: 5000)
    procedure = "proposal:de:bt-test-42"
    first_ref = _ref(conn, de["document_id"])
    first = dossier.save(
        "research", "clean-heating", "DE", procedure, [first_ref],
        principal_id="alice", scopes=scopes,
    )
    amendment = _add(
        stored, de, identity="BT-TEST-AMEND-1", document_type="amendment",
        content="The amendment changes section 4. It commences for section 4 on 2027-01-01.",
        political={
            "proposal_id": procedure, "event_at": "2026-05-01",
            "legal_events": [{
                "kind": "commencement", "date": "2027-01-01", "section": "4",
                "jurisdiction": "DE", "source_quote": "It commences for section 4 on 2027-01-01.",
            }],
        },
        observed_at=3000,
    )
    scopes.add(f"document:{amendment['document_id']}:read")
    amended_ref = _ref(conn, amendment["document_id"])
    second = dossier.save(
        "research", "clean-heating", "DE", procedure, [first_ref, amended_ref],
        principal_id="alice", scopes=scopes,
        dossier_id=first["dossier_id"], expected_revision=1,
    )
    early = dossier.timeline(
        "research", first["dossier_id"], revision=2, observed_as_of_ms=2500,
        principal_id="alice", scopes=scopes,
    )
    assert {e["citation"]["document_id"] for e in early["entries"]} == {de["document_id"]}
    full = dossier.timeline(
        "research", first["dossier_id"], revision=2,
        principal_id="alice", scopes=scopes,
    )
    assert any(e["event_kind"] == "commencement" and e["source_quote"].startswith("It commences") for e in full["entries"])
    assert any(e["date"]["at_ms"] > e["published_at"]["at_ms"] for e in full["entries"] if e["date"])
    _schema("noesis-legislative-timeline-v1.json", full)

    conflict = _add(
        stored, de, identity="BT-TEST-AMEND-2", document_type="amendment",
        content="Section 4 commences on 2027-02-01 according to this record.",
        political={
            "proposal_id": procedure,
            "legal_events": [{
                "kind": "commencement", "date": "2027-02-01", "section": "4",
                "jurisdiction": "DE", "source_quote": "Section 4 commences on 2027-02-01",
            }],
        },
        observed_at=4000,
    )
    scopes.add(f"document:{conflict['document_id']}:read")
    third = dossier.save(
        "research", "clean-heating", "DE", procedure,
        [first_ref, amended_ref, _ref(conn, conflict["document_id"])],
        principal_id="alice", scopes=scopes,
        dossier_id=first["dossier_id"], expected_revision=2,
    )
    timeline = dossier.timeline(
        "research", first["dossier_id"], revision=3,
        principal_id="alice", scopes=scopes,
    )
    assert len([e for e in timeline["entries"] if e["event_kind"] == "commencement"]) == 2
    changes = dossier.compare(
        "research", first["dossier_id"], 1, 3,
        principal_id="alice", scopes=scopes,
    )
    assert changes["counts"]["added"] == 2 and all(c["after_citation"] for c in changes["changes"])
    _schema("noesis-legislative-comparison-v1.json", changes)
    exported = dossier.export_change_summary(
        "research", first["dossier_id"], 1, 3,
        principal_id="alice", scopes=scopes,
    )
    assert all(row["kind"] == "recorded_change" for row in exported["report_ready"])
    assert exported["interpretation"] is None
    deps = dossier.dependencies(
        "research", first["dossier_id"], principal_id="alice", scopes=scopes,
    )
    assert len(deps["report_dependencies"]) == len(deps["project_links"]) == 3
    assert deps["report_dependencies"][0]["locator"]["revision_id"]
    connected_scopes = scopes | {
        "knowledge:reports:write", "knowledge:reports:read",
        "knowledge:projects:write", "knowledge:projects:read", "domain:political:read",
    }
    report = AuthoredReportStore(conn).create(
        "research", "dossier-report", {
            "title": "Recorded legislative stages",
            "snapshot": {"id": "fixture-snapshot", "generations": {"research": 0}},
            "bibliography": [{"id": "official-record", "text": "Acquired official fixture records"}],
            "limitations": ["Offline fictional fixture; legal meaning not reviewed"],
            "sections": [{"id": "stages", "title": "Observed stages", "assertions": [{
                "id": "recorded-stages", "text": "Three pinned official records are in this dossier.",
                "kind": "sourced", "dependencies": deps["report_dependencies"],
                "citations": ["official-record"],
            }]}],
        }, principal_id="alice", scopes=connected_scopes,
    )
    project = ResearchProjectStore(conn).create(
        "research", "dossier-project", questions=["What changed?"],
        success_criteria=["Cite recorded stages"],
        scope={"domains": ["political"], "namespaces": ["research"]}, budget={},
        principal_id="alice", scopes=connected_scopes,
        _initial_links=deps["project_links"],
    )
    assert report["revision"] == project["revision"] == 1
    alert_dependencies = CitationAlertStore(conn)._dependencies(
        {"kind": "report", "namespace": "research", "id": report["report_id"], "revision": 1},
        "alice", connected_scopes,
    )
    assert len(alert_dependencies) == 3

    # A later acquired record with an old event date is absent from earlier observation cuts.
    historical = _add(
        stored, de, identity="BT-TEST-HISTORY", document_type="proposal",
        content="A proposal was filed in 2025.",
        political={"proposal_id": procedure, "event_at": "2025-01-01"}, observed_at=6000,
    )
    scopes.add(f"document:{historical['document_id']}:read")
    fourth = dossier.save(
        "research", "clean-heating", "DE", procedure,
        [first_ref, amended_ref, _ref(conn, conflict["document_id"]), _ref(conn, historical["document_id"])],
        principal_id="alice", scopes=scopes,
        dossier_id=first["dossier_id"], expected_revision=3,
    )
    assert fourth["revision"] == 4
    as_of = dossier.timeline(
        "research", first["dossier_id"], revision=4, observed_as_of_ms=5000,
        principal_id="alice", scopes=scopes,
    )
    assert historical["document_id"] not in {e["citation"]["document_id"] for e in as_of["entries"]}

    # Superseding a pinned document changes the same stage; omitting a stage marks it unavailable.
    changed = dict(de)
    changed["content"] = "A corrected roll call records the vote count."
    changed["ingested_at"] = 7000
    assert stored.upsert([changed]).updated == 1
    latest = _ref(conn, de["document_id"])
    fifth = dossier.save(
        "research", "clean-heating", "DE", procedure,
        [latest, amended_ref, _ref(conn, conflict["document_id"])],
        principal_id="alice", scopes=scopes,
        dossier_id=first["dossier_id"], expected_revision=4,
    )
    diff = dossier.compare(
        "research", first["dossier_id"], 4, 5,
        principal_id="alice", scopes=scopes,
    )
    assert diff["counts"]["changed"] == 1 and diff["counts"]["unavailable"] == 1
    assert next(c for c in diff["changes"] if c["kind"] == "changed")["before_citation"]["revision_id"] == first_ref["revision_id"]
    assert fifth["revision"] == 5 and second["revision"] == 2 and third["revision"] == 3


def test_public_mcp_dossier_tools_use_scoped_store_and_versioned_contracts():
    conn, _stored, de, _eu, scopes = _fixture()

    class MCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorate(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorate

    calls = []

    def safe(operation, *, write=False, required_scope=None):
        calls.append((write, required_scope))
        return operation(conn)

    mcp = MCP()
    register_political_dossiers(mcp, safe, lambda: ("alice", scopes))
    saved = mcp.tools["save_legislative_dossier"](
        "research", "mcp-dossier", "DE", "proposal:de:bt-test-42",
        [_ref(conn, de["document_id"])],
    )
    inspected = mcp.tools["inspect_legislative_dossier"]("research", saved["dossier_id"])
    timeline = mcp.tools["legislative_dossier_timeline"]("research", saved["dossier_id"])
    assert inspected["contract"] == "noesis-legislative-dossier-v1"
    assert timeline["contract"] == "noesis-legislative-timeline-v1"
    assert calls[0] == (True, "knowledge:political:dossier:write")
    assert calls[1] == (False, "knowledge:political:dossier:read")
