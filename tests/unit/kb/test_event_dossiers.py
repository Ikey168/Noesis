import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.ingestion.revisions import DocumentRevisionStore
from src.kb.authored_reports import AuthoredReportStore
from src.kb.citation_alerts import CitationAlertStore
from src.kb.event_dossiers import EventDossierError, EventDossierStore
from src.kb.events import EventKnowledgeStore
from src.osint.independence import METHOD_VERSION, ensure_independence_schema

ROOT = Path(__file__).resolve().parents[3]
AUTH = {"principal_id": "alice", "scopes": {"operator"}}


def seed(conn):
    revisions = DocumentRevisionStore(conn)
    sources = {}
    for when, document_id, provider, content in [
        (100, "guardian", "guardian", "Ten people were affected."),
        (101, "copy", "osint", "According to Guardian, ten people were affected."),
        (102, "independent", "osint", "Independent witness reports twelve affected."),
    ]:
        sources[document_id] = revisions.observe(
            {
                "document_id": document_id,
                "source_type": "news",
                "title": document_id,
                "content": content,
                "ingested_at": when,
                "metadata": {
                    "provider": provider,
                    "acquisition_representation": "article_text"
                    if document_id == "guardian"
                    else "feed_excerpt",
                    "text_coverage": "full_text"
                    if document_id == "guardian"
                    else "partial",
                },
            }
        )["revision_id"]
    events = EventKnowledgeStore(conn)
    event = events.create(
        "osint",
        {
            "event_type": "incident",
            "participants": ["city:a"],
            "location": {"city": "A"},
            "time": {"start_ms": 90, "end_ms": 110},
            "evidence": [{"source_revision_id": sources["guardian"]}],
        },
        event_key="incident-a",
        principal_id="alice",
        scopes={"knowledge:event:write"},
    )
    other = events.create(
        "osint",
        {
            "event_type": "incident",
            "participants": ["city:a"],
            "location": {"city": "B"},
            "time": {"start_ms": 900, "end_ms": 910},
            "evidence": [{"source_revision_id": sources["independent"]}],
        },
        event_key="incident-b",
        principal_id="alice",
        scopes={"knowledge:event:write"},
    )
    for document_id in ("guardian", "copy", "independent"):
        events.ingest_mentions(
            "osint",
            sources[document_id],
            [
                {
                    "text": "Incident report",
                    "event_type": "incident",
                    "participants": ["city:a"],
                    "location": {"city": "A"},
                    "time": {"start_ms": 90, "end_ms": 110},
                    "evidence": [{"source_revision_id": sources[document_id]}],
                }
            ],
            language="en",
            classifier=lambda _, eid=event["event_id"]: {
                "event_id": eid,
                "confidence": 1.0,
            },
            classifier_pin={"name": "fixture", "version": "1", "revision": "fixture-1"},
            principal_id="alice",
            scopes={"knowledge:event:write"},
        )
    for amount, document_ids in ((10, ("guardian", "copy")), (12, ("independent",))):
        events.attach_account(
            "osint",
            event["event_id"],
            "quantity",
            {"value": amount, "unit": "count"},
            role="affected",
            evidence=[
                {"document_revision_id": sources[document_id], "start": 0, "end": 45}
                for document_id in document_ids
            ],
            valid_from_ms=90,
            valid_to_ms=110,
            principal_id="alice",
            scopes={"knowledge:event:write"},
        )
    ensure_independence_schema(conn)
    for document_id, origin_id, relation in [
        ("guardian", "origin:one", "known_independent"),
        ("copy", "origin:one", "likely_dependent"),
        ("independent", "origin:two", "known_independent"),
    ]:
        conn.execute(
            "INSERT INTO document_origin_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                document_id,
                "fixture",
                METHOD_VERSION,
                origin_id,
                relation,
                0.9,
                0.8,
                1.0,
                json.dumps(
                    ["explicit-attribution"]
                    if document_id == "copy"
                    else ["independent-reporting"]
                ),
                json.dumps({"upstream": "guardian"} if document_id == "copy" else {}),
                110,
                "fixture-run",
            ],
        )
    return events, event, other, sources


def test_same_name_events_copies_conflicts_membership_and_lost_source():
    conn = duckdb.connect()
    events, event, other, sources = seed(conn)
    store = EventDossierStore(conn, now=lambda: 120)
    first = store.create("osint", "case-a", event["event_id"], **AUTH)
    assert first["event_id"] != other["event_id"]
    assert len(first["sources"]) == 3
    assert all(
        source["representation"] in {"article_text", "feed_excerpt"}
        for source in first["sources"]
    )
    group = next(
        group
        for group in first["account_groups"]
        if group["attribute_type"] == "quantity"
    )
    assert group["publication_count"] == 3
    assert group["supported_independent_origin_count"] == 2
    assert group["probable_origin_count"] == 2
    assert any(item["classification"] == "contradiction" for item in group["conflicts"])
    assert group["lineage"]["likely_dependent_count"] == 1
    assert any(
        "explicit-attribution" in link["reason_codes"]
        for link in group["lineage"]["dependency_evidence"]
    )
    Draft7Validator(
        json.loads(
            (
                ROOT / "contracts/schemas/jsonschema/noesis-event-dossier-v1.json"
            ).read_text()
        )
    ).validate(first)

    second = store.revise(
        "osint",
        first["dossier_id"],
        1,
        overrides=[
            {
                "document_revision_id": sources["copy"],
                "decision": "exclude",
                "rationale": "Syndicated copy contributes no new account",
                "reviewed_by": "alice",
            }
        ],
        **AUTH,
    )
    comparison = store.compare("osint", first["dossier_id"], 1, 2, **AUTH)
    assert comparison["source_changes"]
    assert (
        next(
            source
            for source in second["sources"]
            if source["document_revision_id"] == sources["copy"]
        )["membership"]["decision"]
        == "exclude"
    )
    Draft7Validator(
        json.loads(
            (
                ROOT
                / "contracts/schemas/jsonschema/noesis-event-dossier-comparison-v1.json"
            ).read_text()
        )
    ).validate(comparison)
    report_after_exclusion = store.create_report(
        "osint", "without-copy-report", first["dossier_id"], revision=2, **AUTH
    )["report"]
    assert sources["copy"] not in {
        entry["id"] for entry in report_after_exclusion["content"]["bibliography"]
    }
    earlier = store.export_comparison("osint", first["dossier_id"], 1, 2, **AUTH)
    conn.execute(
        "DELETE FROM document_revision_records WHERE revision_id=?", [sources["copy"]]
    )
    third = store.revise("osint", first["dossier_id"], 2, **AUTH)
    assert any(source["status"] == "unavailable" for source in third["sources"])
    assert (
        next(
            item
            for item in store.inspect("osint", first["dossier_id"], revision=1, **AUTH)[
                "current_availability"
            ]
            if item["document_revision_id"] == sources["copy"]
        )["retained_now"]
        is False
    )
    assert (
        store.export_comparison("osint", first["dossier_id"], 1, 2, **AUTH) == earlier
    )
    assert store.timeline("osint", first["dossier_id"], revision=3, **AUTH)["items"]
    conn.close()


def test_correction_time_scope_identity_and_report_subscription():
    conn = duckdb.connect()
    events, event, _, sources = seed(conn)
    store = EventDossierStore(conn)
    first = store.create("osint", "report-case", event["event_id"], **AUTH)
    report = store.create_report("osint", "authored", first["dossier_id"], **AUTH)[
        "report"
    ]
    exported = AuthoredReportStore(conn).export("osint", report["report_id"], **AUTH)
    subscription = CitationAlertStore(conn).create(
        {
            "kind": "report",
            "namespace": "osint",
            "id": report["report_id"],
            "revision": 1,
        },
        "watch",
        ["revised", "unavailable"],
        50,
        principal_id="alice",
        scopes={"operator"},
    )
    assert subscription["subscription_id"]
    CitationAlertStore(conn).evaluate(subscription["subscription_id"], **AUTH)
    corrected_revision = DocumentRevisionStore(conn).observe(
        {
            "document_id": "guardian",
            "content": "Corrected count: eleven",
            "ingested_at": 200,
        }
    )["revision_id"]
    result = CitationAlertStore(conn).evaluate(subscription["subscription_id"], **AUTH)
    assert result
    assert (
        AuthoredReportStore(conn).export(
            "osint", report["report_id"], revision=1, **AUTH
        )
        == exported
    )
    preliminary = next(
        account
        for account in events.accounts(
            "osint", event["event_id"], scopes={"knowledge:event:read"}
        )
        if account["attribute_type"] == "quantity" and account["value"]["value"] == 10
    )
    events.retract_account(
        "osint",
        preliminary["account_id"],
        "Guardian corrected the preliminary count.",
        principal_id="alice",
        scopes={"knowledge:event:review"},
    )
    events.attach_account(
        "osint",
        event["event_id"],
        "quantity",
        {"value": 11, "unit": "count"},
        role="affected",
        valid_from_ms=90,
        valid_to_ms=110,
        evidence=[{"document_revision_id": corrected_revision, "start": 0, "end": 24}],
        principal_id="alice",
        scopes={"knowledge:event:write"},
    )
    events.attach_account(
        "osint",
        event["event_id"],
        "quantity",
        {"value": 15, "unit": "count"},
        role="affected",
        valid_from_ms=200,
        valid_to_ms=210,
        evidence=[
            {"document_revision_id": sources["independent"], "start": 0, "end": 40}
        ],
        principal_id="alice",
        scopes={"knowledge:event:write"},
    )
    events.attach_account(
        "osint",
        event["event_id"],
        "participant",
        {"entity_id": "person:a"},
        role="commander",
        evidence=[{"document_revision_id": sources["guardian"]}],
        principal_id="alice",
        scopes={"knowledge:event:write"},
    )
    events.attach_account(
        "osint",
        event["event_id"],
        "participant",
        {"entity_id": "person:b"},
        role="commander",
        evidence=[{"document_revision_id": sources["independent"]}],
        principal_id="alice",
        scopes={"knowledge:event:write"},
    )
    revised = store.revise("osint", first["dossier_id"], 1, **AUTH)
    classes = {
        conflict["classification"]
        for group in revised["account_groups"]
        for conflict in group["conflicts"]
    }
    assert "different_time_scope" in classes
    assert "unresolved_identity" in classes
    compared = store.compare("osint", first["dossier_id"], 1, 2, **AUTH)
    assert compared["changed_accounts"]
    assert any(
        item["document_revision_id"] == corrected_revision
        for item in compared["source_changes"]
    )
    conn.close()


def test_reviewed_ambiguity_and_current_source_access():
    conn = duckdb.connect()
    _, event, _, sources = seed(conn)
    store = EventDossierStore(conn)
    with pytest.raises(EventDossierError) as error:
        store.create(
            "osint",
            "bad",
            event["event_id"],
            overrides=[
                {
                    "document_revision_id": sources["copy"],
                    "decision": "ambiguous",
                    "rationale": "Identity unclear",
                }
            ],
            **AUTH,
        )
    assert error.value.code == "invalid_request"
    dossier = store.create(
        "osint",
        "good",
        event["event_id"],
        overrides=[
            {
                "document_revision_id": sources["copy"],
                "decision": "ambiguous",
                "rationale": "Identity unclear",
                "reviewed_by": "alice",
            }
        ],
        **AUTH,
    )
    assert dossier["coverage"] == "incomplete"
    scopes = {
        "knowledge:event-dossier:read",
        "knowledge:event:read",
        "namespace:osint:read",
    }
    with pytest.raises(EventDossierError) as error:
        store.inspect(
            "osint", dossier["dossier_id"], principal_id="alice", scopes=scopes
        )
    assert error.value.code == "unauthorized"
    conn.close()


def test_copied_publication_adds_coverage_without_new_supported_origin():
    conn = duckdb.connect()
    _, event, _, sources = seed(conn)
    store = EventDossierStore(conn)
    first = store.create(
        "osint",
        "without-copy",
        event["event_id"],
        overrides=[
            {
                "document_revision_id": sources["copy"],
                "decision": "exclude",
                "rationale": "Copy was outside the initial capture",
                "reviewed_by": "alice",
            }
        ],
        **AUTH,
    )
    second = store.revise("osint", first["dossier_id"], 1, **AUTH)
    before = next(
        group
        for group in first["account_groups"]
        if group["attribute_type"] == "quantity"
    )
    after = next(
        group
        for group in second["account_groups"]
        if group["attribute_type"] == "quantity"
    )
    assert before["publication_count"] == 2
    assert after["publication_count"] == 3
    assert (
        before["supported_independent_origin_count"]
        == after["supported_independent_origin_count"]
        == 2
    )
    conn.close()
