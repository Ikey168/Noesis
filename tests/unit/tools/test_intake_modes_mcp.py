import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import intake
from tools.knowledge_engine_mcp import server


def test_intake_public_tools_and_access(tmp_path, monkeypatch):
    path = str(tmp_path / "intake.duckdb")
    scopes = {
        "knowledge:intake:read",
        "knowledge:intake:write",
        "namespace:research:read",
        "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    assert len(tools["discover_intake_modes"].fn()["modes"]) == 10
    assert (
        tools["route_intake_mode"].fn(answers={"decision_needed": True})["mode"]
        == "Decision Support"
    )
    created = tools["start_intake_mode"].fn(
        namespace="research",
        mode="Exploration",
        request_key="curiosity",
        intent="Browse",
        inputs={},
    )
    identity = {"namespace": "research", "session_id": created["session_id"]}
    assert tools["inspect_intake_mode"].fn(**identity)["revision"] == 1
    assert (
        tools["list_intake_modes"].fn(namespace="research")["sessions"][0]["session_id"]
        == created["session_id"]
    )
    assert (
        tools["command_intake_mode"].fn(
            **identity,
            command_key="end",
            expected_revision=1,
            action="record",
            payload={"data": {"escalation_reason": "Research this"}},
        )["revision"]
        == 2
    )
    assert (
        tools["command_intake_mode"].fn(
            **identity, command_key="done", expected_revision=2, action="complete"
        )["status"]
        == "completed"
    )
    assert len(tools["export_intake_mode"].fn(**identity)["revisions"]) == 3
    assert tools["verify_intake_mode_export"].fn(
        bundle=tools["export_intake_mode"].fn(**identity)
    )["valid"]
    assert (
        tools["export_modulo_intake_handoff"].fn(**identity)["session"]["id"]
        == created["session_id"]
    )
    v2 = tools["export_modulo_intake_handoff"].fn(**identity, contract_version="v2")
    assert v2["contract"] == "noesis-modulo-intake-handoff-v2"
    assert v2["session"]["unmet_recorded_checks"] == []
    scopes.remove("namespace:research:read")
    assert (
        tools["inspect_intake_mode"].fn(**identity)["error"]["code"] == "unauthorized"
    )
    assert _mutability("command_intake_mode") == "write"
    assert _required_scopes("knowledge_engine_mcp", "write", "command_intake_mode") == [
        "knowledge:intake:write"
    ]
    assert _required_scopes("knowledge_engine_mcp", "read", "inspect_intake_mode") == [
        "knowledge:intake:read"
    ]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "export_modulo_intake_handoff"
    ) == ["knowledge:intake:read"]


def test_jev_free_text_intake_route_mcp_explicit_precedence(tmp_path, monkeypatch):
    path = str(tmp_path / "jev-intake-routing.duckdb")
    monkeypatch.setattr(server, "_context", lambda: ("alice", {"operator"}))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    assert {
        "register_jev_intake_intent",
        "revise_jev_intake_intent",
        "inspect_jev_intake_intent",
        "suggest_jev_intake_route",
    } <= tools.keys()
    intent = tools["register_jev_intake_intent"].fn(
        namespace="research",
        request_key="mixed-request",
        intent_text="I need to decide and create something.",
    )
    assert intent["version"] == 1
    explicit = tools["suggest_jev_intake_route"].fn(
        namespace="research",
        run_id="explicit-route",
        input_id=intent["input_id"],
        answers={"decision_needed": True},
    )
    assert explicit["route"]["mode"] == "Decision Support"
    assert explicit["hosted_inference_used"] is False
    override = tools["suggest_jev_intake_route"].fn(
        namespace="research",
        run_id="override-route",
        input_id=intent["input_id"],
        answers={"decision_needed": True},
        override="Creation",
    )
    assert override["route"]["mode"] == "Creation"
    revised = tools["revise_jev_intake_intent"].fn(
        namespace="research",
        input_id=intent["input_id"],
        expected_version=1,
        intent_text="I need a deep research overview.",
    )
    assert revised["version"] == 2
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "suggest_jev_intake_route"
    ) == ["knowledge:decision:execute"]


def test_awareness_mcp_uses_persistent_feed_inbox(tmp_path, monkeypatch):
    from src.kb import intake_inbox

    path = str(tmp_path / "inbox.duckdb")
    scopes = {
        "knowledge:intake:read",
        "knowledge:intake:write",
        "knowledge:intake:fetch",
        "namespace:research:read",
        "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    monkeypatch.setattr(
        intake_inbox,
        "_fetch_public_feed",
        lambda _: (
            b'<rss version="2.0"><channel><item><title>One</title><link>https://example.org/one</link></item></channel></rss>'
        ),
    )
    tools = asyncio.run(server.mcp.get_tools())
    subscription = tools["subscribe_intake_feed"].fn(
        namespace="research", url="https://example.org/rss", name="Example"
    )
    assert subscription["subscription_id"].startswith("subscription:")
    assert (
        tools["refresh_intake_feed_inbox"].fn(namespace="research")["results"][0][
            "created"
        ]
        == 1
    )
    page = tools["list_intake_feed_inbox"].fn(namespace="research")
    assert page["remaining_unprocessed"] == 1
    item_id = page["items"][0]["item_id"]
    session = tools["start_awareness_from_inbox"].fn(
        namespace="research", request_key="today"
    )
    triaged = tools["triage_awareness_item"].fn(
        namespace="research",
        session_id=session["session_id"],
        item_id=item_id,
        command_key="discard-one",
        expected_revision=1,
        decision="discard",
    )
    assert triaged["data"]["decisions"][item_id] == "discard"
    assert (
        tools["list_intake_feed_inbox"].fn(namespace="research")[
            "remaining_unprocessed"
        ]
        == 0
    )
    assert (
        tools["command_intake_mode"].fn(
            namespace="research",
            session_id=session["session_id"],
            command_key="finish",
            expected_revision=2,
            action="complete",
        )["status"]
        == "completed"
    )
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "refresh_intake_feed_inbox"
    ) == ["knowledge:intake:write", "knowledge:intake:fetch"]


def test_awareness_newsletter_input_over_mcp(tmp_path, monkeypatch):
    path = str(tmp_path / "newsletter.duckdb")
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    subscribed = tools["subscribe_intake_newsletter_input"].fn(
        namespace="research", sender="editor@example.org", name="Weekly"
    )
    kwargs = {
        "namespace": "research", "subscription_id": subscribed["subscription_id"],
        "message_id": "weekly-1@example.org", "sender": "editor@example.org",
        "subject": "Weekly news", "body": "A relevant development",
        "published_at_ms": 1_789_000_000_000,
    }
    first = tools["ingest_intake_newsletter_message"].fn(**kwargs)
    assert first["created"] == 1
    assert first["authentication_state"] == "caller_supplied_unverified"
    assert tools["ingest_intake_newsletter_message"].fn(**kwargs)["created"] == 0
    item = tools["list_intake_feed_inbox"].fn(namespace="research")["items"][0]
    assert item["original_url"].startswith("mid:")
    assert item["published_at_ms"] == kwargs["published_at_ms"]
    assert tools["ingest_intake_newsletter_message"].fn(
        **{**kwargs, "sender": "other@example.org"}
    )["error"]["code"] == "sender_mismatch"


def test_exploration_capture_over_mcp(tmp_path, monkeypatch):
    path = str(tmp_path / "exploration.duckdb")
    scopes = {
        "knowledge:intake:read",
        "knowledge:intake:write",
        "namespace:research:read",
        "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    started = tools["start_intake_mode"].fn(
        namespace="research",
        mode="Exploration",
        request_key="curious",
        intent="Explore",
    )
    captured = tools["capture_exploration_page"].fn(
        namespace="research",
        session_id=started["session_id"],
        command_key="page-one",
        expected_revision=1,
        url="https://example.org/article",
        title="Article",
        note="Interesting",
        saved=True,
        content="Caller-provided readable content",
    )
    assert captured["data"]["trail"][0]["saved"] is True
    source_id = captured["references"][0]["id"]
    source = tools["inspect_exploration_source"].fn(
        namespace="research",
        source_id=source_id,
    )
    assert source["acquisition"] == "caller_supplied"
    assert source["content"] == "Caller-provided readable content"
    note = tools["annotate_exploration_source"].fn(
        namespace="research",
        source_id=source_id,
        request_key="note-one",
        body="Read more",
        locator={"section": "Introduction"},
    )
    assert note["source_version"] == source["version"]
    assert (
        tools["inspect_exploration_source"].fn(
            namespace="research",
            source_id=source_id,
        )["annotations"][0]["annotation_id"]
        == note["annotation_id"]
    )
    library = tools["start_intake_mode"].fn(
        namespace="research",
        mode="Exploration",
        request_key="library",
        intent="Save reading",
    )
    tools["capture_exploration_page"].fn(
        namespace="research",
        session_id=library["session_id"],
        command_key="related",
        expected_revision=1,
        url="https://example.net/related",
        title="Readable article content",
        content="Readable article content for further reading",
    )
    related_page = tools["suggest_exploration_sources"].fn(
        namespace="research",
        session_id=started["session_id"],
    )
    assert "suggestions" in related_page, related_page
    related = related_page["suggestions"]
    assert len(related) == 1
    acted = tools["decide_exploration_suggestion"].fn(
        namespace="research",
        session_id=started["session_id"],
        suggestion_id=related[0]["suggestion_id"],
        command_key="follow",
        expected_revision=2,
        decision="follow",
        saved=True,
        expected_candidate_version=related[0]["candidate"]["reference"]["version"],
    )
    assert (
        acted["data"]["trail"][-1]["source_id"] == related[0]["candidate"]["source_id"]
    )
    denied = tools["capture_exploration_page"].fn(
        namespace="research",
        session_id=started["session_id"],
        command_key="fetch",
        expected_revision=3,
        url="https://example.org/other",
        title="Other",
        fetch_readable=True,
    )
    assert denied["error"]["code"] == "unauthorized"


def test_modulo_plugin_link_recheck_mcp_is_read_only_scoped_and_fail_closed(
    tmp_path, monkeypatch,
):
    path = str(tmp_path / "modulo-link-check.duckdb")
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    monkeypatch.setattr(intake, "MODULO_PLUGIN_LINK_PROVIDER", None)
    tools = asyncio.run(server.mcp.get_tools())
    assert "recheck_modulo_plugin_link" in tools
    link = {
        "workspace_id": "workspace:personal", "account_id": "account:alice",
        "plugin_id": "notes-editor", "collection": "notes",
        "record_id": "note:7", "authoritative_version": 4,
        "representation": "linked_projection", "authority": "modulo",
    }
    session = tools["start_intake_mode"].fn(
        namespace="research", mode="Exploration", request_key="linked-note",
        intent="Review the note", plugin_links=[link],
    )
    args = {
        "namespace": "research", "session_id": session["session_id"],
        "expected_session_revision": 1, "link_index": 0,
    }
    unavailable = tools["recheck_modulo_plugin_link"].fn(**args)
    assert unavailable["status"] == "unavailable"
    assert unavailable["content_included"] is False

    class Reader:
        def __init__(self):
            self.identity = None

        def read_exact_record(self, identity):
            self.identity = identity
            return {
                "identity": identity, "status": "accessible",
                "authoritative_version": 5,
            }

    class Provider:
        def __init__(self):
            self.principal_id = None
            self.reader = Reader()

        def for_caller(self, principal_id):
            self.principal_id = principal_id
            return self.reader

    provider = Provider()
    monkeypatch.setattr(intake, "MODULO_PLUGIN_LINK_PROVIDER", provider)
    checked = tools["recheck_modulo_plugin_link"].fn(**args)
    assert checked["status"] == "version_changed"
    assert checked["current_authoritative_version"] == 5
    assert provider.principal_id == "alice"
    assert provider.reader.identity == {
        "workspace_id": "workspace:personal", "account_id": "account:alice",
        "plugin_id": "notes-editor", "collection": "notes", "record_id": "note:7",
    }
    assert set(tools["recheck_modulo_plugin_link"].parameters["properties"]) == {
        "namespace", "session_id", "expected_session_revision", "link_index",
    }
    assert _mutability("recheck_modulo_plugin_link") == "read"
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "recheck_modulo_plugin_link",
    ) == ["knowledge:intake:read"]
