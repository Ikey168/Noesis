from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.events import EventResolutionError, EventResolver

ROOT=Path(__file__).resolve().parents[3]


@pytest.fixture()
def resolver(tmp_path):
    conn=duckdb.connect(str(tmp_path/"events.duckdb"));value=EventResolver(conn)
    yield value
    conn.close()


def report(**overrides):
    value={"event_type":"election","participants":["a","b"],"location":{"country":"DE"},"time":{"start_ms":1000,"end_ms":2000},"evidence":[{"source":"official"}]};value.update(overrides);return value


def test_domain_fixtures_validate() -> None:
    schema=json.loads((ROOT/"contracts/schemas/jsonschema/noesis-canonical-event-v1.json").read_text());fixtures=json.loads((ROOT/"contracts/examples/knowledge-engine/events.json").read_text());Draft7Validator.check_schema(schema)
    assert {item.pop("domain") for item in fixtures}=={"political","economic","osint","technical","scientific"}
    assert all(not list(Draft7Validator(schema).iter_errors(item)) for item in fixtures)


def test_resolution_confidence_alternatives_and_no_forced_merge(resolver: EventResolver) -> None:
    first=resolver.resolve_report("political",report(),report_id="r1",now_ms=10);assert not first["linked"]
    second=resolver.resolve_report("political",report(evidence=[{"source":"press"}]),report_id="r2",now_ms=20);assert second["linked"] and second["event_id"]==first["event_id"]
    distinct=resolver.resolve_report("political",report(participants=["c"],location={"country":"FR"}),report_id="r3",now_ms=30)
    assert not distinct["linked"] and distinct["alternatives"] and not distinct["forced_merge"]


def test_recurring_events_revisions_merge_and_reversal(resolver: EventResolver) -> None:
    one=resolver.resolve_report("economic",report(event_type="rate",recurrence_key="2026-01"),report_id="a",now_ms=10)
    two=resolver.resolve_report("economic",report(event_type="rate",recurrence_key="2026-02",time={"start_ms":3000,"end_ms":3000}),report_id="b",now_ms=20)
    with pytest.raises(EventResolutionError,match="occurrences"):resolver.merge([one["event_id"],two["event_id"]],reason="bad")
    three=resolver.resolve_report("economic",report(event_type="rate",recurrence_key="2026-01",participants=["a","c"],time={"start_ms":1500,"end_ms":2200}),report_id="c",auto_link=False,now_ms=30)
    merged=resolver.merge([one["event_id"],three["event_id"]],reason="same occurrence",now_ms=40)
    assert merged["reversible"] and merged["canonical_event"]["revision"]==1
    revised=resolver.revise(merged["canonical_event"]["event_id"],{"time":{"start_ms":900,"end_ms":2300}},reason="boundary",now_ms=50);assert revised["after"]["revision"]==2
    reversed_result=resolver.reverse(merged["operation_id"],reason="split correction",now_ms=60);assert reversed_result["status"]=="reversed"
    assert resolver.conn.execute("SELECT event_id FROM canonical_event_reports WHERE report_id='a'").fetchone()==(one["event_id"],)


def test_invalid_interval_and_missing_evidence(resolver: EventResolver) -> None:
    with pytest.raises(EventResolutionError):resolver.resolve_report("x",report(time={"start_ms":2,"end_ms":1}),report_id="x")
    with pytest.raises(EventResolutionError):resolver.resolve_report("x",report(evidence=[]),report_id="y")
