import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.intake_modes import MODES, IntakeError, IntakeStore

SCOPES = {
    "knowledge:intake:read",
    "knowledge:intake:write",
    "namespace:research:read",
    "namespace:research:write",
}
SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "contracts/schemas/jsonschema/noesis-intake-session-v1.json"
    ).read_text()
)
VALIDATOR = Draft202012Validator(SCHEMA)
HANDOFF_VALIDATOR = Draft202012Validator(
    json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "contracts/schemas/jsonschema/noesis-modulo-intake-handoff-v1.json"
        ).read_text()
    )
)


def _ref(kind, namespace="research"):
    return {"kind": kind, "id": f"{kind}:1", "namespace": namespace, "version": 1}


CASES = [
    (
        "Awareness",
        {"feed_item_ids": ["one", "two"]},
        {"decisions": {"one": "watch", "two": "discard"}},
        [],
    ),
    ("Exploration", {}, {"escalation_reason": "Pursue as research"}, []),
    (
        "Deep Research",
        {},
        {
            "known": "Known",
            "uncertain": "Uncertain",
            "unresolved": "Open",
            "definition_of_done_met": True,
        },
        [
            _ref(kind)
            for kind in (
                "evidence_card",
                "concept",
                "claim_ledger",
                "brief",
                "mental_model",
                "map",
            )
        ],
    ),
    (
        "Decision Support",
        {},
        {"selected_option": "A", "rationale": "Best fit"},
        [_ref("decision")],
    ),
    (
        "Problem-Solving",
        {},
        {"verified": True, "verification": "Reproduction now passes"},
        [],
    ),
    (
        "Creation",
        {},
        {"acceptance_checks": {"build": True}},
        [_ref("created_artifact")],
    ),
    (
        "Externalization",
        {},
        {"rehearsal_or_execution": "Passed on device A"},
        [_ref("procedure")],
    ),
    (
        "Internalization",
        {},
        {"attempts": [{"answer": "Unaided", "assisted": False, "demonstrated": True}]},
        [],
    ),
    (
        "Iteration",
        {},
        {"expected": "Faster", "observed": "Faster", "learning": "Keep change"},
        [_ref("revised_artifact")],
    ),
    (
        "Maintenance",
        {},
        {
            "checklist": {"links": "done", "reviews": "deferred"},
            "health_acceptable": True,
        },
        [],
    ),
]


@pytest.mark.parametrize("mode,inputs,data,refs", CASES)
def test_mode_requires_recorded_outcome_and_survives_reopen(
    tmp_path, mode, inputs, data, refs
):
    path = str(tmp_path / "intake.duckdb")
    conn = duckdb.connect(path)
    store = IntakeStore(conn)
    state = store.create(
        "research",
        mode,
        "one",
        intent="Do the work",
        inputs=inputs,
        principal_id="alice",
        scopes=SCOPES,
    )
    VALIDATOR.validate(state)
    with pytest.raises(IntakeError, match="unmet completion checks"):
        store.command(
            "research",
            state["session_id"],
            "finish-too-early",
            expected_revision=1,
            action="complete",
            payload=None,
            principal_id="alice",
            scopes=SCOPES,
        )
    updated = store.command(
        "research",
        state["session_id"],
        "evidence",
        expected_revision=1,
        action="record",
        payload={"data": data, "references": refs},
        principal_id="alice",
        scopes=SCOPES,
    )
    assert updated["unmet_completion_checks"] == []
    VALIDATOR.validate(updated)
    conn.close()
    conn = duckdb.connect(path)
    store = IntakeStore(conn)
    completed = store.command(
        "research",
        state["session_id"],
        "finish",
        expected_revision=2,
        action="complete",
        payload=None,
        principal_id="alice",
        scopes=SCOPES,
    )
    assert completed["status"] == "completed"
    VALIDATOR.validate(completed)
    assert (
        store.inspect(
            "research",
            state["session_id"],
            revision=1,
            principal_id="alice",
            scopes=SCOPES,
        )["status"]
        == "active"
    )
    assert store.command(
        "research",
        state["session_id"],
        "finish",
        expected_revision=2,
        action="complete",
        payload=None,
        principal_id="alice",
        scopes=SCOPES,
    )["idempotent"]
    exported = store.export(
        "research", state["session_id"], principal_id="alice", scopes=SCOPES
    )
    assert [revision["revision"] for revision in exported["revisions"]] == [1, 2, 3]
    assert len(exported["sha256"]) == 64
    from src.kb.intake_modes import verify_export

    assert verify_export(exported)["valid"] is True
    exported["revisions"][1]["intent"] = "tampered"
    assert verify_export(exported)["reasons"] == ["digest_mismatch"]
    exported["revisions"][1]["owner"] = "mallory"
    from src.kb.intake_modes import _hash

    exported["sha256"] = _hash(exported["revisions"])
    assert verify_export(exported)["reasons"] == ["broken_revision_chain"]
    conn.close()


def test_handoff_replay_active_limit_and_access_revocation(tmp_path):
    conn = duckdb.connect(str(tmp_path / "intake.duckdb"))
    store = IntakeStore(conn)
    awareness = store.create(
        "research",
        "Awareness",
        "awareness",
        intent="Scan",
        inputs={"feed_item_ids": ["feed:1"]},
        workspace_links=[
            {
                "system": "modulo",
                "workspace_id": "home",
                "kind": "intake_item",
                "id": "item-1",
                "version": 1,
            }
        ],
        principal_id="alice",
        scopes=SCOPES,
    )
    assert awareness["workspace_links"][0]["id"] == "item-1"
    exploration = store.create(
        "research",
        "Exploration",
        "explore",
        intent="Follow curiosity",
        origin={
            "session_id": awareness["session_id"],
            "reason": "Signal worth exploring",
        },
        principal_id="alice",
        scopes=SCOPES,
    )
    assert exploration["origin"]["session_id"] == awareness["session_id"]
    assert exploration["workspace_links"] == awareness["workspace_links"]
    handoff = store.modulo_handoff(
        "research", exploration["session_id"], principal_id="alice", scopes=SCOPES
    )
    HANDOFF_VALIDATOR.validate(handoff)
    assert handoff["correlation_key"] == exploration["session_id"]
    assert handoff["transition"]["origin"]["reason"] == "Signal worth exploring"
    assert handoff["modulo_links"] == awareness["workspace_links"]
    store.command(
        "research",
        awareness["session_id"],
        "triage",
        expected_revision=1,
        action="record",
        payload={"data": {"decisions": {"feed:1": "escalate"}}},
        principal_id="alice",
        scopes=SCOPES,
    )
    assert store.create(
        "research",
        "Exploration",
        "explore",
        intent="Follow curiosity",
        origin={
            "session_id": awareness["session_id"],
            "reason": "Signal worth exploring",
        },
        principal_id="alice",
        scopes=SCOPES,
    )["idempotent"]
    with pytest.raises(IntakeError) as conflict:
        store.create(
            "research",
            "Exploration",
            "explore",
            intent="Different",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(IntakeError) as denied:
        store.inspect(
            "research", awareness["session_id"], principal_id="bob", scopes=SCOPES
        )
    assert denied.value.code == "unauthorized"
    with pytest.raises(IntakeError) as revoked:
        store.inspect(
            "research",
            awareness["session_id"],
            principal_id="alice",
            scopes=SCOPES - {"namespace:research:read"},
        )
    assert revoked.value.code == "unauthorized"
    with pytest.raises(IntakeError) as revoked_handoff:
        store.modulo_handoff(
            "research",
            exploration["session_id"],
            principal_id="alice",
            scopes=SCOPES - {"namespace:research:read"},
        )
    assert revoked_handoff.value.code == "unauthorized"
    for number in range(3):
        store.create(
            "research",
            "Deep Research",
            f"topic-{number}",
            intent="Investigate",
            principal_id="alice",
            scopes=SCOPES,
        )
    with pytest.raises(IntakeError) as limit:
        store.create(
            "research",
            "Deep Research",
            "topic-3",
            intent="Investigate",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert limit.value.code == "active_topic_limit"
    with pytest.raises(IntakeError) as invalid_link:
        store.create(
            "research",
            "Creation",
            "bad-link",
            intent="Create",
            workspace_links=[
                {
                    "system": "modulo",
                    "workspace_id": "home",
                    "kind": "note",
                    "id": "note-1",
                    "version": 0,
                }
            ],
            principal_id="alice",
            scopes=SCOPES,
        )
    assert invalid_link.value.code == "invalid_workspace_link"
    conn.close()


def test_pause_conflicts_and_reference_access(tmp_path):
    conn = duckdb.connect(str(tmp_path / "intake.duckdb"))
    store = IntakeStore(conn)
    state = store.create(
        "research",
        "Externalization",
        "playbook",
        intent="Offload",
        principal_id="alice",
        scopes=SCOPES,
    )
    paused = store.command(
        "research",
        state["session_id"],
        "pause",
        expected_revision=1,
        action="pause",
        payload=None,
        principal_id="alice",
        scopes=SCOPES,
    )
    assert paused["status"] == "paused"
    with pytest.raises(IntakeError) as stale:
        store.command(
            "research",
            state["session_id"],
            "old",
            expected_revision=1,
            action="resume",
            payload=None,
            principal_id="alice",
            scopes=SCOPES,
        )
    assert stale.value.code == "revision_conflict"
    store.command(
        "research",
        state["session_id"],
        "resume",
        expected_revision=2,
        action="resume",
        payload=None,
        principal_id="alice",
        scopes=SCOPES,
    )
    cross = SCOPES | {"namespace:other:read"}
    store.command(
        "research",
        state["session_id"],
        "procedure",
        expected_revision=3,
        action="record",
        payload={
            "data": {"rehearsal_or_execution": "Verified"},
            "references": [_ref("procedure", "other")],
        },
        principal_id="alice",
        scopes=cross,
    )
    viewed = store.inspect(
        "research", state["session_id"], principal_id="alice", scopes=SCOPES
    )
    assert viewed["references"] == [{"redacted": True}]
    assert viewed["data"] == {"redacted": True}
    VALIDATOR.validate(viewed)
    assert "reference:procedure" in viewed["unmet_completion_checks"]
    with pytest.raises(IntakeError) as revoked:
        store.command(
            "research",
            state["session_id"],
            "complete",
            expected_revision=4,
            action="complete",
            payload=None,
            principal_id="alice",
            scopes=SCOPES,
        )
    assert revoked.value.code == "unauthorized"
    with pytest.raises(IntakeError) as export_revoked:
        store.export(
            "research", state["session_id"], principal_id="alice", scopes=SCOPES
        )
    assert export_revoked.value.code == "unauthorized"
    conn.close()


def test_mode_catalog_is_complete():
    from src.kb.intake_modes import discover_modes, route_mode

    assert [item["name"] for item in discover_modes()["modes"]] == list(MODES)
    assert all(
        item["session_ledger_ready"] and not item["native_workflow_ready"]
        for item in discover_modes()["modes"]
    )
    routed = route_mode({"urgent_or_broken": True, "curiosity_only": True})
    assert routed["mode"] == "Problem-Solving"
    assert (
        route_mode({"urgent_or_broken": True}, override="Exploration")["mode"]
        == "Exploration"
    )
    with pytest.raises(IntakeError):
        route_mode({"curiosity_only": "yes"})


def test_exploration_timebox_uses_observed_elapsed_time():
    now = [1_000_000]
    conn = duckdb.connect(":memory:")
    store = IntakeStore(conn, now=lambda: now[0])
    state = store.create(
        "research",
        "Exploration",
        "trail",
        intent="Browse",
        duration_minutes=60,
        principal_id="alice",
        scopes=SCOPES,
    )
    with pytest.raises(IntakeError) as premature:
        store.command(
            "research",
            state["session_id"],
            "premature",
            expected_revision=1,
            action="complete",
            payload=None,
            principal_id="alice",
            scopes=SCOPES,
        )
    assert premature.value.code == "incomplete_mode"
    now[0] += 60 * 60_000
    assert (
        store.inspect(
            "research", state["session_id"], principal_id="alice", scopes=SCOPES
        )["remaining_minutes"]
        == 0
    )
    done = store.command(
        "research",
        state["session_id"],
        "timebox",
        expected_revision=1,
        action="complete",
        payload=None,
        principal_id="alice",
        scopes=SCOPES,
    )
    assert done["elapsed_ms"] == 60 * 60_000
    conn.close()


def test_active_research_limit_is_configurable():
    conn = duckdb.connect(":memory:")
    store = IntakeStore(conn, active_research_limit=1)
    store.create(
        "research",
        "Deep Research",
        "topic-1",
        intent="Investigate",
        principal_id="alice",
        scopes=SCOPES,
    )
    with pytest.raises(IntakeError) as blocked:
        store.create(
            "research",
            "Deep Research",
            "topic-2",
            intent="Investigate",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert blocked.value.code == "active_topic_limit"
    conn.close()
