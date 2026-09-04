from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.events import EventKnowledgeStore, EventResolutionError

READ = {"knowledge:event:read"}
WRITE = {"knowledge:event:write"}
REVIEW = {"knowledge:event:review"}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _event(**updates):
    value = {
        "event_type": "incident",
        "participants": ["entity:a"],
        "location": {"country": "DE"},
        "time": {"start_ms": 10, "end_ms": 20},
        "evidence": [{"source_revision_id": "document-revision:1"}],
    }
    value.update(updates)
    return value


def test_event_identity_lifecycle_corrections_cancellation_and_recurrence():
    conn = duckdb.connect(":memory:")
    ticks = iter([100, 200, 300])
    store = EventKnowledgeStore(conn, now=lambda: next(ticks))
    ongoing = store.create(
        "technical",
        _event(recurrence_key="daily:2026-01-01"),
        principal_id="extractor",
        scopes=WRITE,
        event_key="outage:2026-01-01",
        lifecycle="ongoing",
        generation=3,
    )
    assert ongoing["lifecycle"] == "ongoing" and ongoing["revision"] == 1
    _validate("noesis-event-record-v2.json", ongoing)
    assert store.create(
        "technical",
        _event(recurrence_key="daily:2026-01-01"),
        principal_id="extractor",
        scopes=WRITE,
        event_key="outage:2026-01-01",
    )["idempotent"]
    corrected = store.revise(
        "technical",
        ongoing["event_id"],
        1,
        {"time": {"start_ms": 9, "end_ms": 21}},
        reason="Late telemetry corrected the time boundary.",
        principal_id="reviewer",
        scopes=WRITE,
        lifecycle="corrected",
    )
    cancelled = store.revise(
        "technical",
        ongoing["event_id"],
        2,
        {},
        reason="The planned continuation was formally cancelled.",
        principal_id="reviewer",
        scopes=WRITE,
        lifecycle="cancelled",
    )
    assert corrected["event_id"] == ongoing["event_id"]
    assert cancelled["revision"] == 3 and cancelled["lifecycle"] == "cancelled"
    history = store.get(
        "technical", ongoing["event_id"], scopes=READ, include_history=True
    )
    assert [item["lifecycle"] for item in history["revisions"]] == [
        "ongoing",
        "corrected",
        "cancelled",
    ]
    diff = store.diff("technical", ongoing["event_id"], 1, 2, scopes=READ)
    assert "time" in diff["changes"]
    assert (
        store.as_of("technical", ongoing["event_id"], 150, scopes=READ)["revision"] == 1
    )
    replay = store.replay("technical", ongoing["event_id"], scopes=READ)
    assert replay["deterministic"] and replay["revision_count"] == 3
    conn.close()


def test_near_duplicate_evolving_multilingual_mentions_and_pinned_model():
    conn = duckdb.connect(":memory:")
    store = EventKnowledgeStore(conn, now=lambda: 100)
    first = store.ingest_mentions(
        "political",
        "document-revision:de:1",
        [{"text": "Der Gipfel begann", **_event()}],
        language="de",
        principal_id="extractor",
        scopes=WRITE,
    )
    second = store.ingest_mentions(
        "political",
        "document-revision:en:1",
        [
            {
                "text": "The summit began",
                **_event(evidence=[{"source_revision_id": "document-revision:2"}]),
            }
        ],
        language="en",
        principal_id="extractor",
        scopes=WRITE,
    )
    assert second["items"][0]["event_id"] == first["items"][0]["event_id"]
    assert second["items"][0]["classifier"]["kind"] == "rules"
    _validate("noesis-event-mention-v1.json", second["items"][0])
    modeled = store.ingest_mentions(
        "political",
        "document-revision:model:1",
        [{"text": "Updated report", **_event()}],
        language="en",
        principal_id="extractor",
        scopes=WRITE,
        classifier=lambda _: {
            "event_id": first["items"][0]["event_id"],
            "confidence": 0.9,
        },
        classifier_pin={"name": "clusterer", "version": "2", "revision": "abc"},
    )
    assert modeled["items"][0]["classifier"]["revision"] == "abc"
    with pytest.raises(EventResolutionError, match="classifier name"):
        store.ingest_mentions(
            "political",
            "document-revision:bad",
            [_event()],
            language="en",
            principal_id="extractor",
            scopes=WRITE,
            classifier=lambda _: {},
        )
    assert (
        store.ingest_mentions(
            "political",
            "document-revision:cancel",
            [_event()],
            language="en",
            principal_id="extractor",
            scopes=WRITE,
            cancel_requested=True,
        )["status"]
        == "cancelled"
    )
    conn.close()


def test_competing_accounts_roles_locations_uncertain_time_and_quantity_units():
    conn = duckdb.connect(":memory:")
    store = EventKnowledgeStore(conn, now=lambda: 100)
    event = store.create(
        "osint", _event(), principal_id="analyst", scopes=WRITE, event_key="incident"
    )
    commander = store.attach_account(
        "osint",
        event["event_id"],
        "participant",
        {"entity_id": "person:1"},
        role="commander",
        confidence=0.6,
        uncertainty=0.4,
        evidence=[{"citation": "report:a"}],
        principal_id="analyst",
        scopes=WRITE,
    )
    store.attach_account(
        "osint",
        event["event_id"],
        "participant",
        {"entity_id": "person:1"},
        role="spokesperson",
        confidence=0.5,
        uncertainty=0.5,
        evidence=[{"citation": "report:b"}],
        principal_id="analyst",
        scopes=WRITE,
    )
    for location, citation in (("Berlin", "report:a"), ("Potsdam", "report:b")):
        store.attach_account(
            "osint",
            event["event_id"],
            "location",
            {"name": location},
            confidence=0.5,
            uncertainty=0.5,
            evidence=[{"citation": citation}],
            principal_id="analyst",
            scopes=WRITE,
        )
    quantity = store.attach_account(
        "osint",
        event["event_id"],
        "quantity",
        {"value": 2, "unit": "km"},
        role="affected-distance",
        confidence=0.7,
        uncertainty=0.3,
        evidence=[{"citation": "measurement:1"}],
        principal_id="analyst",
        scopes=WRITE,
    )
    store.attach_account(
        "osint",
        event["event_id"],
        "time",
        {"earliest_ms": 5, "latest_ms": 15},
        uncertainty=0.7,
        evidence=[{"citation": "estimate:1"}],
        principal_id="analyst",
        scopes=WRITE,
    )
    accounts = store.accounts("osint", event["event_id"], scopes=READ)
    assert len(accounts) == 6 and commander["role"] == "commander"
    assert quantity["value"]["normalized_value"] == 2000
    assert quantity["value"]["normalized_unit"] == "m"
    _validate("noesis-event-account-v1.json", quantity)
    conn.close()


def test_conflicting_casualties_attribution_retraction_and_late_evidence():
    conn = duckdb.connect(":memory:")
    ticks = iter([100, 101, 102, 103, 104, 105, 106, 107, 108, 109])
    store = EventKnowledgeStore(conn, now=lambda: next(ticks))
    event = store.create(
        "research", _event(), principal_id="analyst", scopes=WRITE, event_key="case"
    )
    low = store.attach_account(
        "research",
        event["event_id"],
        "quantity",
        {"value": 10, "unit": "count"},
        role="casualties",
        confidence=0.7,
        evidence=[{"citation": "hospital"}],
        principal_id="analyst",
        scopes=WRITE,
    )
    high = store.attach_account(
        "research",
        event["event_id"],
        "quantity",
        {"value": 30, "unit": "count"},
        role="casualties",
        confidence=0.4,
        uncertainty=0.6,
        evidence=[{"citation": "local-report"}],
        principal_id="analyst",
        scopes=WRITE,
    )
    late = store.attach_account(
        "research",
        event["event_id"],
        "quantity",
        {"value": 30, "unit": "count"},
        role="casualties",
        confidence=0.8,
        uncertainty=0.2,
        evidence=[{"citation": "local-report"}, {"citation": "late-registry"}],
        principal_id="analyst",
        scopes=WRITE,
    )
    assert late["account_id"] == high["account_id"] and late["revision"] == 2
    retracted = store.retract_account(
        "research",
        low["account_id"],
        "Hospital corrected its preliminary count.",
        principal_id="reviewer",
        scopes=REVIEW,
    )
    assert retracted["lifecycle"] == "retracted"
    assert len(store.accounts("research", event["event_id"], scopes=READ)) == 1
    history = store.accounts(
        "research",
        event["event_id"],
        scopes=READ,
        include_retracted=True,
        include_history=True,
    )
    assert (
        len([item for item in history if item["account_id"] == high["account_id"]]) == 2
    )
    conn.close()


def test_search_pagination_snapshot_timeline_relations_cross_domain_and_auth():
    conn = duckdb.connect(":memory:")
    store = EventKnowledgeStore(conn, now=lambda: 100)
    one = store.create(
        "scientific",
        _event(event_type="experiment"),
        principal_id="x",
        scopes=WRITE,
        event_key="one",
        generation=1,
    )
    two = store.create(
        "scientific",
        _event(event_type="publication", time={"start_ms": 30, "end_ms": 30}),
        principal_id="x",
        scopes=WRITE,
        event_key="two",
        generation=2,
    )
    external = store.create(
        "economic",
        _event(event_type="market"),
        principal_id="x",
        scopes=WRITE,
        event_key="external",
        generation=1,
    )
    relation = store.relate(
        "scientific",
        one["event_id"],
        two["event_id"],
        "successor",
        principal_id="x",
        scopes=WRITE,
        evidence=[{"citation": "paper"}],
    )
    _validate("noesis-event-relation-v1.json", relation)
    store.relate(
        "scientific",
        two["event_id"],
        external["event_id"],
        "consequence",
        principal_id="x",
        scopes=WRITE,
        evidence=[{"citation": "market-study"}],
    )
    pinned = store.search("scientific", scopes=READ, snapshot_generation=1, limit=1)
    _validate("noesis-event-search-v1.json", pinned)
    assert [item["event_id"] for item in pinned["items"]] == [one["event_id"]]
    first = store.search("scientific", scopes=READ, limit=1)
    second = store.search(
        "scientific", scopes=READ, limit=1, cursor=first["next_cursor"]
    )
    assert {first["items"][0]["event_id"], second["items"][0]["event_id"]} == {
        one["event_id"],
        two["event_id"],
    }
    assert [item["event_id"] for item in store.timeline("scientific", scopes=READ)] == [
        one["event_id"],
        two["event_id"],
    ]
    neighborhood = store.neighborhood(one["event_id"], scopes=READ, max_depth=2)
    assert external["event_id"] in neighborhood["event_ids"]
    with pytest.raises(EventResolutionError, match="required scope"):
        store.search("scientific", scopes=set())
    conn.close()
