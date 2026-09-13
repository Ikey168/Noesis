import duckdb
import pytest

from src.kb.intake_inbox import IntakeInboxStore
from src.kb.intake_modes import IntakeError

SCOPES = {
    "knowledge:intake:read",
    "knowledge:intake:write",
    "namespace:research:read",
    "namespace:research:write",
}


def test_feed_inbox_preserves_decisions_across_refresh_and_restart(tmp_path):
    path = str(tmp_path / "inbox.duckdb")
    conn = duckdb.connect(path)
    inbox = IntakeInboxStore(conn)
    source = inbox.subscribe(
        "research",
        "https://example.org/feed.xml",
        "Example",
        "rss_atom",
        principal_id="alice",
        scopes=SCOPES,
    )
    subscription_id = source["subscription_id"]
    doc = {
        "url": "https://example.org/post",
        "title": "New finding",
        "content": "Summary",
        "published_at_ms": 1_789_000_000_000,
    }
    assert (
        inbox.ingest(
            "research", subscription_id, [doc], principal_id="alice", scopes=SCOPES
        )["created"]
        == 1
    )
    item = inbox.list("research", principal_id="alice", scopes=SCOPES)["items"][0]
    assert item["read_at_ms"] is None and item["decision"] is None
    item_id = item["item_id"]
    inbox.mark_read(
        "research", item_id, "read-one", read=True, principal_id="alice", scopes=SCOPES
    )
    decided = inbox.decide(
        "research",
        item_id,
        "triage-one",
        decision="escalate",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert decided["decision"] == "escalate"
    assert inbox.decide(
        "research",
        item_id,
        "triage-one",
        decision="escalate",
        principal_id="alice",
        scopes=SCOPES,
    )["idempotent"]
    with pytest.raises(IntakeError) as conflict:
        inbox.decide(
            "research",
            item_id,
            "triage-one",
            decision="discard",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert conflict.value.code == "idempotency_conflict"
    assert (
        inbox.ingest(
            "research",
            subscription_id,
            [{**doc, "content": "Corrected"}],
            principal_id="alice",
            scopes=SCOPES,
        )["updated"]
        == 1
    )
    conn.close()
    conn = duckdb.connect(path)
    inbox = IntakeInboxStore(conn, initialize=False)
    current = inbox.inspect("research", item_id, principal_id="alice", scopes=SCOPES)
    assert current["source_version"] == 2
    historical = inbox.inspect(
        "research", item_id, revision=1, principal_id="alice", scopes=SCOPES
    )
    assert historical["content"] == "Summary"
    assert historical["reference"]["version"] == 1
    assert current["read_at_ms"] is not None
    assert current["decision"] == "escalate"
    assert (
        inbox.list("research", principal_id="alice", scopes=SCOPES)[
            "remaining_unprocessed"
        ]
        == 0
    )
    with pytest.raises(IntakeError) as denied:
        inbox.inspect("research", item_id, principal_id="bob", scopes=SCOPES)
    assert denied.value.code == "item_not_found"
    conn.close()


def test_feed_scope_and_unsafe_source(tmp_path):
    conn = duckdb.connect(str(tmp_path / "inbox.duckdb"))
    inbox = IntakeInboxStore(conn)
    with pytest.raises(IntakeError) as unsafe:
        inbox.subscribe(
            "research",
            "http://localhost/rss",
            "Local",
            "rss_atom",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert unsafe.value.code == "unsafe_feed_url"
    with pytest.raises(IntakeError) as denied:
        inbox.subscribe(
            "research",
            "https://example.org/rss",
            "Public",
            "rss_atom",
            principal_id="alice",
            scopes=SCOPES - {"namespace:research:write"},
        )
    assert denied.value.code == "unauthorized"
    conn.close()


def test_saved_signals_are_versioned_scoped_and_non_mutating(tmp_path):
    conn = duckdb.connect(str(tmp_path / "inbox.duckdb"))
    inbox = IntakeInboxStore(conn)
    subscription = inbox.subscribe(
        "research",
        "https://example.org/rss",
        "Example",
        "rss_atom",
        principal_id="alice",
        scopes=SCOPES,
    )
    with pytest.raises(IntakeError) as unsafe:
        inbox.ingest(
            "research",
            subscription["subscription_id"],
            [{"url": "javascript:alert(1)", "title": "Bad"}],
            principal_id="alice",
            scopes=SCOPES,
        )
    assert unsafe.value.code == "invalid_item_url"
    inbox.ingest(
        "research",
        subscription["subscription_id"],
        [
            {
                "url": "https://example.org/one",
                "title": "A useful finding",
                "content": "Summary",
            }
        ],
        principal_id="alice",
        scopes=SCOPES,
    )
    first = inbox.save_signal_rule(
        "research",
        "Useful",
        ["finding"],
        principal_id="alice",
        scopes=SCOPES,
    )
    assert first["version"] == 1
    assert inbox.save_signal_rule(
        "research",
        "Useful",
        ["finding"],
        principal_id="alice",
        scopes=SCOPES,
    )["idempotent"]
    assert (
        inbox.preview_signal_rule(
            "research",
            first["rule_id"],
            principal_id="alice",
            scopes=SCOPES,
        )["matches"][0]["matched"][0]["field"]
        == "title"
    )
    assert (
        inbox.list("research", principal_id="alice", scopes=SCOPES)[
            "remaining_unprocessed"
        ]
        == 1
    )
    second = inbox.save_signal_rule(
        "research",
        "Useful",
        ["summary"],
        principal_id="alice",
        scopes=SCOPES,
    )
    assert second["rule_id"] == first["rule_id"] and second["version"] == 2
    assert (
        inbox.signal_rules("research", principal_id="bob", scopes=SCOPES)["rules"] == []
    )
    with pytest.raises(IntakeError) as hidden:
        inbox.preview_signal_rule(
            "research",
            first["rule_id"],
            principal_id="bob",
            scopes=SCOPES,
        )
    assert hidden.value.code == "rule_not_found"
    conn.close()


def test_refresh_parses_configured_feed_and_retains_triage(tmp_path):
    conn = duckdb.connect(str(tmp_path / "inbox.duckdb"))
    inbox = IntakeInboxStore(conn)
    inbox.subscribe(
        "research",
        "https://example.org/rss",
        "Example",
        "rss_atom",
        principal_id="alice",
        scopes=SCOPES,
    )
    fixture = b"""<rss version="2.0"><channel><title>Example</title><item>
    <title>A real entry</title><link>https://example.org/one</link>
    <pubDate>Sun, 13 Sep 2026 00:00:00 GMT</pubDate><description>Summary</description>
    </item></channel></rss>"""
    fetching = SCOPES | {"knowledge:intake:fetch"}
    first = inbox.refresh(
        "research", principal_id="alice", scopes=fetching, http_get=lambda _: fixture
    )
    assert first["results"][0]["created"] == 1
    item = inbox.list("research", principal_id="alice", scopes=SCOPES)["items"][0]
    assert item["original_url"] == "https://example.org/one"
    preview = inbox.signal_preview(
        "research", ["real entry"], principal_id="alice", scopes=SCOPES
    )
    assert preview["matches"][0]["matched"][0]["field"] == "title"
    assert preview["matches"][0]["reference"]["id"] == item["item_id"]
    inbox.decide(
        "research",
        item["item_id"],
        "decision",
        decision="discard",
        principal_id="alice",
        scopes=SCOPES,
    )
    again = inbox.refresh(
        "research", principal_id="alice", scopes=fetching, http_get=lambda _: fixture
    )
    assert again["results"][0]["unchanged"] == 1
    assert (
        inbox.signal_preview(
            "research", ["real entry"], principal_id="alice", scopes=SCOPES
        )["matches"]
        == []
    )
    assert (
        inbox.list("research", principal_id="alice", scopes=SCOPES)[
            "remaining_unprocessed"
        ]
        == 0
    )
    conn.close()


def test_awareness_triage_updates_queue_and_inbox_atomically(tmp_path):
    conn = duckdb.connect(str(tmp_path / "inbox.duckdb"))
    inbox = IntakeInboxStore(conn)
    source = inbox.subscribe(
        "research",
        "https://example.org/rss",
        "Example",
        "rss_atom",
        principal_id="alice",
        scopes=SCOPES,
    )
    inbox.ingest(
        "research",
        source["subscription_id"],
        [{"url": "https://example.org/one", "title": "One", "content": "Summary"}],
        principal_id="alice",
        scopes=SCOPES,
    )
    session = inbox.start_awareness(
        "research", "today", intent="Triage", principal_id="alice", scopes=SCOPES
    )
    item_id = session["inputs"]["feed_item_ids"][0]
    with pytest.raises(IntakeError) as outside:
        inbox.triage_awareness(
            "research",
            session["session_id"],
            "feed:missing",
            "wrong",
            expected_revision=1,
            decision="discard",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert outside.value.code == "invalid_decision"
    assert (
        inbox.inspect("research", item_id, principal_id="alice", scopes=SCOPES)[
            "decision"
        ]
        is None
    )
    updated = inbox.triage_awareness(
        "research",
        session["session_id"],
        item_id,
        "triage",
        expected_revision=1,
        decision="discard",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert updated["unmet_completion_checks"] == []
    assert (
        inbox.inspect("research", item_id, principal_id="alice", scopes=SCOPES)[
            "decision"
        ]
        == "discard"
    )
    assert inbox.triage_awareness(
        "research",
        session["session_id"],
        item_id,
        "triage",
        expected_revision=1,
        decision="discard",
        principal_id="alice",
        scopes=SCOPES,
    )["idempotent"]
    assert inbox.start_awareness(
        "research", "today", intent="Triage", principal_id="alice", scopes=SCOPES
    )["idempotent"]
    conn.close()


def test_escalation_preserves_source_and_annotation_identity(tmp_path):
    conn = duckdb.connect(str(tmp_path / "inbox.duckdb"))
    inbox = IntakeInboxStore(conn)
    source = inbox.subscribe(
        "research",
        "https://example.org/rss",
        "Example",
        "rss_atom",
        principal_id="alice",
        scopes=SCOPES,
    )
    inbox.ingest(
        "research",
        source["subscription_id"],
        [{"url": "https://example.org/one", "title": "One", "content": "Summary"}],
        principal_id="alice",
        scopes=SCOPES,
    )
    item_id = inbox.list("research", principal_id="alice", scopes=SCOPES)["items"][0][
        "item_id"
    ]
    annotation = inbox.annotate(
        "research",
        item_id,
        "note-one",
        "Check the underlying study",
        locator={"section": "summary"},
        principal_id="alice",
        scopes=SCOPES,
    )
    assert inbox.annotate(
        "research",
        item_id,
        "note-one",
        "Check the underlying study",
        locator={"section": "summary"},
        principal_id="alice",
        scopes=SCOPES,
    )["idempotent"]
    awareness = inbox.start_awareness(
        "research", "today", intent="Triage", principal_id="alice", scopes=SCOPES
    )
    with pytest.raises(IntakeError) as not_triaged:
        inbox.promote_awareness_item(
            "research",
            awareness["session_id"],
            item_id,
            "explore",
            target_mode="Exploration",
            reason="Follow up",
            intent="Explore",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert not_triaged.value.code == "not_escalated"
    inbox.triage_awareness(
        "research",
        awareness["session_id"],
        item_id,
        "escalate",
        expected_revision=1,
        decision="escalate",
        principal_id="alice",
        scopes=SCOPES,
    )
    exploration = inbox.promote_awareness_item(
        "research",
        awareness["session_id"],
        item_id,
        "explore",
        target_mode="Exploration",
        reason="Follow up",
        intent="Explore",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert exploration["origin"]["session_id"] == awareness["session_id"]
    assert {ref["kind"] for ref in exploration["references"]} == {
        "intake_feed_item",
        "intake_annotation",
    }
    assert annotation["annotation_id"] in [
        ref["id"] for ref in exploration["references"]
    ]
    inbox.ingest(
        "research",
        source["subscription_id"],
        [{"url": "https://example.org/one", "title": "One", "content": "Corrected"}],
        principal_id="alice",
        scopes=SCOPES,
    )
    prior = inbox.inspect(
        "research", item_id, revision=1, principal_id="alice", scopes=SCOPES
    )
    assert prior["content"] == "Summary"
    assert prior["annotations"][0]["source_version"] == 1
    assert (
        next(
            ref
            for ref in exploration["references"]
            if ref["kind"] == "intake_feed_item"
        )["version"]
        == 1
    )
    assert inbox.promote_awareness_item(
        "research",
        awareness["session_id"],
        item_id,
        "explore",
        target_mode="Exploration",
        reason="Follow up",
        intent="Explore",
        principal_id="alice",
        scopes=SCOPES,
    )["idempotent"]
    conn.close()


def test_duplicate_feed_item_and_batch_triage(tmp_path):
    conn = duckdb.connect(str(tmp_path / "inbox.duckdb"))
    inbox = IntakeInboxStore(conn)
    sources = [
        inbox.subscribe(
            "research",
            f"https://example.org/{name}.xml",
            name,
            "rss_atom",
            principal_id="alice",
            scopes=SCOPES,
        )["subscription_id"]
        for name in ("first", "second")
    ]
    same = {"url": "https://example.org/same", "title": "Same", "content": "Shared"}
    inbox.ingest("research", sources[0], [same], principal_id="alice", scopes=SCOPES)
    inbox.ingest(
        "research",
        sources[1],
        [same, {"url": "https://example.org/other", "title": "Other"}],
        principal_id="alice",
        scopes=SCOPES,
    )
    items = inbox.list("research", principal_id="alice", scopes=SCOPES)["items"]
    assert len(items) == 2
    duplicate = next(item for item in items if item["title"] == "Same")
    assert set(duplicate["source_ids"]) == set(sources)
    session = inbox.start_awareness(
        "research", "batch", intent="Daily scan", principal_id="alice", scopes=SCOPES
    )
    decisions = {
        item["item_id"]: "discard" if item["title"] == "Same" else "watch"
        for item in items
    }
    result = inbox.triage_awareness_batch(
        "research",
        session["session_id"],
        decisions,
        "batch-command",
        expected_revision=1,
        principal_id="alice",
        scopes=SCOPES,
    )
    assert result["unmet_completion_checks"] == []
    assert (
        inbox.list("research", principal_id="alice", scopes=SCOPES)[
            "remaining_unprocessed"
        ]
        == 0
    )
    assert inbox.triage_awareness_batch(
        "research",
        session["session_id"],
        decisions,
        "batch-command",
        expected_revision=1,
        principal_id="alice",
        scopes=SCOPES,
    )["idempotent"]
    conn.close()
