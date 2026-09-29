"""MCP coverage for intake operations the milestone matrix found untested (#2465).

The store-level suites already prove these behaviours. These tests drive the
same operations through the public knowledge-engine MCP server with
``fastmcp.Client`` so the milestone verification can cite a supported MCP
path: saved Awareness signal rules, Externalization procedure search and
unconfigured step automation, Iteration gap routing, Creation build
adapters, and the browser-local reconciliation report. They are deterministic
transport checks, not live or signed-in Modulo evidence.
"""

from __future__ import annotations

from fastmcp import Client

from tests.unit.tools.test_intake_native_journeys_mcp import (
    NS,
    REPORT,
    STEPS,
    Journey,
    _capture_sources,
)

AUTOMATED = [
    STEPS[0],
    {"action": "Restart the search indexer", "expected_result": "Indexer healthy",
     "recovery": "Page on-call and roll back",
     "automation": {"action": "service.restart",
                    "parameters": {"service": "search-indexer", "reason": "stale index"}}},
]


async def _verified_problem(j: Journey, client: Client, key: str) -> dict:
    _, refs = await _capture_sources(j, client, key)
    problem = await j.ok(client, "start_problem_session", namespace=NS, request_key=key,
                         symptom="Search results are stale", environment="Desktop 2.7",
                         urgency="blocking", success_check="New item searchable within a minute")
    verified = await j.ok(client, "record_problem_step", namespace=NS, session_id=problem["session_id"],
                          command_key=f"{key}-v", expected_revision=1, kind="verification",
                          summary="Searched for a new item", observation="Found after 20 seconds",
                          passed=True, references=[refs[0]])
    return await j.ok(client, "command_intake_mode", namespace=NS, session_id=problem["session_id"],
                      command_key=f"{key}-done", expected_revision=verified["revision"], action="complete")


def test_saved_signal_rule_preview_explains_matches_without_mutating_the_inbox(tmp_path, monkeypatch):
    j = Journey(tmp_path, monkeypatch)

    async def scenario(client: Client) -> None:
        await j.ok(client, "subscribe_intake_feed", namespace=NS, url="https://example.org/rss", name="Ops")
        await j.ok(client, "refresh_intake_feed_inbox", namespace=NS)
        before = await j.ok(client, "list_intake_feed_inbox", namespace=NS)
        rule = await j.ok(client, "save_intake_feed_signal_rule", namespace=NS, name="Lag", terms=["lag"])
        listed = await j.ok(client, "list_intake_feed_signal_rules", namespace=NS)
        assert [item["rule_id"] for item in listed["rules"]] == [rule["rule_id"]]
        preview = await j.ok(client, "preview_intake_feed_signal_rule", namespace=NS, rule_id=rule["rule_id"])
        assert [match["title"] for match in preview["matches"]] == ["Index lag grows"]
        assert preview["matches"][0]["matched"][0]["term"] == "lag"
        assert preview["matches"][0]["reference"]["kind"] == "intake_feed_item"
        # Explaining a signal neither marks items read nor decides them.
        assert await j.ok(client, "list_intake_feed_inbox", namespace=NS) == before
        j.principal = "bob"
        assert await j.error(client, "preview_intake_feed_signal_rule", namespace=NS,
                             rule_id=rule["rule_id"]) == "rule_not_found"

    j.run(scenario)


def test_procedure_search_and_unconfigured_automation_stay_draft(tmp_path, monkeypatch):
    j = Journey(tmp_path, monkeypatch)

    async def scenario(client: Client) -> None:
        problem = await _verified_problem(j, client, "stale")
        rule = await j.ok(client, "promote_intake_work_procedure", namespace=NS,
                          session_id=problem["session_id"], request_key="restart-rule",
                          artifact_kind="automation_rule", title="Restart the indexer",
                          prerequisites=["Admin"], environment="Desktop 2.7", steps=AUTOMATED,
                          verification="New item within a minute",
                          source_rationale="Verified troubleshooting session",
                          trigger="Index lag exceeds five minutes")
        assert rule["trust_state"] == "draft"

        found = await j.ok(client, "search_intake_playbooks", namespace=NS,
                           task="restart the search indexer", environment="Desktop")
        assert [(m["playbook_id"], m["revision"]) for m in found["matches"]] == [
            (rule["playbook_id"], rule["revision"])]
        assert found["ranking_basis"] == "lexical_overlap_only"
        assert (await j.ok(client, "search_intake_playbooks", namespace=NS,
                           task="restart the search indexer", environment="Windows"))["matches"] == []

        run = await j.ok(client, "start_guided_playbook_run", namespace=NS, playbook_id=rule["playbook_id"],
                         request_key="rule-run", playbook_revision=rule["revision"], environment="Staging")
        run = await j.ok(client, "command_guided_playbook_run", namespace=NS, run_id=run["run_id"],
                         command_key="s1", expected_revision=run["revision"], action="step",
                         payload={"step_id": "step-1", "passed": True, "observation": "Stale"})
        preview = await j.ok(client, "preview_playbook_step_automation", namespace=NS,
                             run_id=run["run_id"], step_id="step-2")
        executed = await j.ok(client, "execute_playbook_step_automation", namespace=NS, run_id=run["run_id"],
                              step_id="step-2", expected_revision=run["revision"],
                              preview_hash=preview["preview_hash"], idempotency_key="k", correlation_id="c")
        # The public server configures no adapter: nothing runs and trust stays draft.
        assert executed["status"] == "unavailable" and executed["executed"] is False
        inspected = await j.ok(client, "inspect_intake_playbook", namespace=NS, playbook_id=rule["playbook_id"])
        assert inspected["trust_state"] == "draft"

        j.principal = "bob"
        assert (await j.ok(client, "search_intake_playbooks", namespace=NS,
                           task="restart the search indexer"))["matches"] == []

    j.run(scenario)


def test_iteration_gap_routes_upstream_with_lineage_and_replays(tmp_path, monkeypatch):
    j = Journey(tmp_path, monkeypatch)

    async def scenario(client: Client) -> None:
        problem = await _verified_problem(j, client, "gap")
        playbook = await j.ok(client, "promote_problem_playbook", namespace=NS,
                              problem_session_id=problem["session_id"], request_key="gap-playbook",
                              title="Clear restart lag", prerequisites=["Admin"], environment="Desktop 2.7",
                              steps=STEPS, verification="New item within a minute",
                              source_rationale="Verified troubleshooting session")
        cycle = await j.ok(client, "start_intake_iteration", namespace=NS, request_key="gap-cycle",
                           playbook_id=playbook["playbook_id"], expected_revision=1,
                           expected="Lag under one minute", stability_criteria="Three quiet restarts",
                           intent="Did the fix hold?")
        premature = await j.error(client, "route_intake_iteration_gap", namespace=NS,
                                  session_id=cycle["session_id"], command_key="too-early",
                                  expected_revision=cycle["revision"], target_mode="Deep Research",
                                  reason="Unknown cause", intent="Why?")
        assert premature == "outcome_required"
        observed = await j.ok(client, "record_intake_iteration_outcome", namespace=NS,
                              session_id=cycle["session_id"], command_key="measured",
                              expected_revision=cycle["revision"], observed="Lag returned after a deploy",
                              learning="Deploys also cause lag",
                              measurements=[{"metric": "lag", "expected": "under 1", "observed": "4",
                                             "unit": "minutes"}],
                              uncertainty="One deploy",
                              external_causes="Unknown", evidence=[])
        route = dict(namespace=NS, session_id=cycle["session_id"], command_key="route-gap",
                     expected_revision=observed["revision"], target_mode="Deep Research",
                     reason="Deploy-induced lag is unexplained", intent="Why do deploys cause lag?")
        routed = await j.ok(client, "route_intake_iteration_gap", **route)
        child = routed["child"]
        assert child["mode"] == "Deep Research"
        assert child["origin"]["session_id"] == cycle["session_id"]
        assert routed["handoff"]["source_revision"] == observed["revision"]
        replay = await j.ok(client, "route_intake_iteration_gap", **route)
        assert replay["idempotent"] and replay["child"]["session_id"] == child["session_id"]
        assert await j.error(client, "route_intake_iteration_gap", **{**route, "target_mode": "Iteration",
                                                                      "command_key": "self"}) == "invalid_mode"
        j.principal = "bob"
        assert await j.error(client, "inspect_intake_mode", namespace=NS,
                             session_id=child["session_id"]) == "unauthorized"

    j.run(scenario)


def test_creation_build_types_are_explicit_without_a_configured_adapter(tmp_path, monkeypatch):
    j = Journey(tmp_path, monkeypatch)

    async def finished(kind: str) -> dict:
        report = await j.ok(client_ref["c"], "create_authored_report", namespace=NS,
                            request_key=f"{kind}-report", content=REPORT)
        project = await j.ok(client_ref["c"], "start_intake_creation", namespace=NS,
                             request_key=f"{kind}-project", title="Recovery", audience="Operators",
                             artifact_type=kind, purpose="Explain recovery", criteria=["Accurate"], inputs=[])
        step = project
        for key, action, payload in (
            ("attach", "attach_report", {"report_id": report["report_id"], "revision": report["revision"]}),
            ("review", "review", {"checks": {"Accurate": True}, "notes": "Checked"}),
            ("finish", "finish", None),
        ):
            step = await j.ok(client_ref["c"], "command_intake_creation", namespace=NS,
                              project_id=project["project_id"], command_key=f"{kind}-{key}",
                              expected_revision=step["revision"], action=action, payload=payload)
        return step

    client_ref: dict = {}

    async def scenario(client: Client) -> None:
        client_ref["c"] = client
        assert await j.error(client, "start_intake_creation", namespace=NS, request_key="app",
                             title="App", audience="Users", artifact_type="mobile_app",
                             purpose="Ship", criteria=["Works"], inputs=[]) == "unsupported_artifact"
        documentation = await finished("documentation")
        assert await j.error(client, "build_intake_creation_artifact", namespace=NS,
                             project_id=documentation["project_id"],
                             expected_revision=documentation["revision"],
                             idempotency_key="doc-build") == "not_build_type"
        deck = await finished("slide_deck")
        built = await j.ok(client, "build_intake_creation_artifact", namespace=NS,
                           project_id=deck["project_id"], expected_revision=deck["revision"],
                           idempotency_key="deck-build")
        assert built["status"] == "unavailable" and built["built"] is False
        assert built["reason"] == "build_adapter_unavailable"

    j.run(scenario)


def _inventory(persistence: str, records: list[dict]) -> dict:
    return {
        "workspace_id": "personal", "account_id": None, "observed_at_ms": 1000,
        "source": "fixture",
        "plugins": [{"plugin_id": "feeds-reading-inbox", "installed_version": None,
                     "collections": [{"collection": "items", "schema_id": "modulo.feed-item",
                                      "schema_version": 1, "persistence": persistence,
                                      "records": records}]}],
    }


def _item(record_id: str, digest: str) -> dict:
    return {"record_id": record_id, "authoritative_version": 1, "fields": ["title", "url"],
            "relation_ids": [], "attachment_ids": [], "content_sha256": digest,
            "source_locator": {"url": f"https://example.org/{record_id}"}}


def test_browser_local_reconciliation_over_mcp_reports_outcomes_and_writes_nothing(tmp_path, monkeypatch):
    j = Journey(tmp_path, monkeypatch)

    async def scenario(client: Client) -> None:
        local = await j.ok(client, "preview_modulo_intake_migration", namespace=NS, request_key="browser",
                           inventory=_inventory("legacy_local", [_item("a", "a" * 64), _item("b", "b" * 64)]),
                           mappings=[])
        durable = await j.ok(client, "preview_modulo_intake_migration", namespace=NS, request_key="plugin",
                             inventory=_inventory("authenticated_plugin_state", [_item("a", "a" * 64)]),
                             mappings=[])
        report = await j.ok(client, "reconcile_modulo_intake_migration", namespace=NS, request_key="r1",
                            legacy_preview_id=local["preview_id"], plugin_state_preview_id=durable["preview_id"])
        assert {o["record_id"]: o["outcome"] for o in report["outcomes"]} == {
            "a": "already_durable", "b": "import_required"}
        assert report["cross_device_replacement"] == "blocked_by_unreconciled_records"
        assert report["remote_mutations"] == 0 and not report["cross_device_replacement_claimed"]
        assert await j.ok(client, "inspect_modulo_intake_reconciliation", namespace=NS,
                          reconciliation_id=report["reconciliation_id"]) == report
        j.principal = "bob"
        assert await j.error(client, "inspect_modulo_intake_reconciliation", namespace=NS,
                             reconciliation_id=report["reconciliation_id"]) == "unauthorized"

    j.run(scenario)
