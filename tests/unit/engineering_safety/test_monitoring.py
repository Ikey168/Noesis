"""Monitors for new directives, revisions, final reports and recommendation status changes (ES14, #2075)."""

from __future__ import annotations

import pytest

from src.kb.engineering_safety_monitoring import EngineeringSafetyMonitor
from src.kb.engineering_safety_records import EngineeringSafetyError
from tests.unit.engineering_safety import harness as h

SCOPES = h.READ | {
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    f"namespace:{h.NS}:write",
}


def kinds(result):
    return sorted((n["kind"], n["cites"]["native_id"]) for n in result["notifications"])


@pytest.fixture()
def env():
    env = h.Env()
    base = h.variants()
    env.run(
        "r1",
        source_ids=[
            "faa-airworthiness-directives",
            "ntsb-investigations",
            "nhtsa-odi-investigations",
        ],
        overrides={"nhtsa-odi-investigations": h.odi_pages(base["odi_open_file"])},
    )
    return env


def monitor(env):
    return EngineeringSafetyMonitor(env.conn, now=lambda: next(env.clock))


def test_watched_subjects_numbers_and_authorities_produce_cited_events_across_runs(env):
    m = monitor(env)
    created = m.create(
        h.NS,
        "ex100",
        watch={
            "subjects": [h.EX100],
            "recommendations": ["ntsb:A-26-015"],
            "directives": ["faa-ad:2025-12-05"],
        },
        principal_id="alice",
        scopes=SCOPES,
    )
    baseline = m.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert baseline["baseline"] and ("new_directive", "2026-04-12") in kinds(baseline)
    assert ("directive_revised", "2026-04-12") in kinds(baseline)  # the correction
    assert ("directive_supersedes", "2026-04-12") in kinds(baseline)
    assert ("directive_superseded", "2026-04-12") in kinds(
        baseline
    )  # watched number 2025-12-05
    assert ("new_recommendation", "A-26-015") in kinds(baseline)
    replay = m.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert replay["notifications"] == []  # replaying the same watermark adds nothing
    variants = h.variants()
    env.run(
        "r2",
        source_ids=["ntsb-investigations"],
        overrides={
            "ntsb-investigations": h.ntsb_pages(
                case=variants["ntsb_case_final"],
                statuses=variants["ntsb_recommendation_statuses"][0],
            )
        },
    )
    second = m.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert ("report_final", "ERA26FA101") in kinds(second)
    assert ("finding_changed", "ERA26FA101") in kinds(second)
    status = [
        n
        for n in second["notifications"]
        if n["kind"] == "recommendation_status_changed"
    ]
    assert [(n["status"], n["status_date"]) for n in status] == [
        ("Open - Acceptable Response", "2026-11-02")
    ]
    assert (
        status[0]["cites"]["previous_revision_id"] != status[0]["cites"]["revision_id"]
    )
    env.run(
        "r3",
        source_ids=["ntsb-investigations"],
        overrides={
            "ntsb-investigations": h.ntsb_pages(
                case=variants["ntsb_case_final"],
                statuses=variants["ntsb_recommendation_statuses"][1],
            )
        },
    )
    third = m.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert kinds(third) == [("recommendation_status_changed", "A-26-015")]
    polled = m.poll(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert polled["events"]


def test_defect_investigation_upgrades_are_events_and_late_older_data_is_not(env):
    m = monitor(env)
    created = m.create(
        h.NS,
        "velomark",
        watch={
            "subjects": [{"kind": "vehicle", "make": "VELOMARK", "model": "CITYRUNNER"}]
        },
        principal_id="alice",
        scopes=SCOPES,
    )
    first = m.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert kinds(first) == [("new_defect_investigation", "PE26003")]
    env.run(
        "r2", source_ids=["nhtsa-odi-investigations"]
    )  # the closing file: PE upgraded, EA opened
    second = m.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert ("defect_investigation_upgraded", "PE26003") in kinds(second)
    assert ("new_defect_investigation", "EA26002") in kinds(second)
    env.run(
        "r3",
        source_ids=["nhtsa-odi-investigations"],
        overrides={
            "nhtsa-odi-investigations": h.odi_pages(h.variants()["odi_open_file"])
        },
    )
    late = m.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert (
        late["notifications"] == []
    )  # the older (open) statement is kept as history only


def test_a_partial_run_is_never_evaluated_and_watches_are_validated(env):
    m = monitor(env)
    with pytest.raises(EngineeringSafetyError):
        m.create(
            h.NS,
            "bad",
            watch={"authorities": ["us-nowhere"]},
            principal_id="alice",
            scopes=SCOPES,
        )
    with pytest.raises(EngineeringSafetyError):
        m.create(
            h.NS,
            "bad2",
            watch={"subjects": [{"kind": "vehicle"}]},
            principal_id="alice",
            scopes=SCOPES,
        )
    created = m.create(
        h.NS,
        "faa",
        watch={"authorities": ["us-faa"]},
        principal_id="alice",
        scopes=SCOPES,
    )
    assert m.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)[
        "notifications"
    ]
    native = h.pages("faa-airworthiness-directives")
    native[0]["status"] = 503
    failed = env.run(
        "r-fail",
        source_ids=["faa-airworthiness-directives"],
        overrides={"faa-airworthiness-directives": native},
    )
    assert failed["status"] != "complete"
    with pytest.raises(EngineeringSafetyError) as refused:
        m.run(
            created["subscription_id"],
            failed.get("watermark") or 0,
            principal_id="alice",
            scopes=SCOPES,
        )
    assert refused.value.code in {"incomplete_run", "watermark_uncommitted"}


def test_monitors_are_not_ready_before_a_source_ran():
    import duckdb

    m = EngineeringSafetyMonitor(duckdb.connect(":memory:"))
    with pytest.raises(EngineeringSafetyError) as caught:
        m.create(
            h.NS,
            "k",
            watch={"authorities": ["us-faa"]},
            principal_id="alice",
            scopes=SCOPES,
        )
    assert caught.value.code == "not_ready"
    with pytest.raises(EngineeringSafetyError) as polled:
        m.poll("subscription:none", principal_id="alice", scopes=SCOPES)
    assert polled.value.code == "not_ready"
