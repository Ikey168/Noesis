import duckdb
import pytest

from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_inbox import IntakeInboxStore
from src.kb.intake_modes import IntakeError, IntakeStore

SCOPES = {
    "knowledge:intake:read",
    "knowledge:intake:write",
    "knowledge:intake:fetch",
    "namespace:research:read",
    "namespace:research:write",
}


def test_lightweight_capture_versions_and_replay(tmp_path):
    path = str(tmp_path / "explore.duckdb")
    conn = duckdb.connect(path)
    session = IntakeStore(conn).create(
        "research",
        "Exploration",
        "curiosity",
        intent="Curious",
        principal_id="alice",
        scopes=SCOPES,
    )
    store = IntakeExplorationStore(conn)
    first = store.capture(
        "research",
        session["session_id"],
        "visit-one",
        expected_revision=1,
        url="https://example.org/a",
        title="Article A",
        note="Why?",
        saved=False,
        content="First version",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert len(first["data"]["trail"]) == 1
    source_id = first["data"]["trail"][0]["source_id"]
    assert first["references"][0]["id"] == source_id
    assert store.capture(
        "research",
        session["session_id"],
        "visit-one",
        expected_revision=1,
        url="https://example.org/a",
        title="Article A",
        note="Why?",
        saved=False,
        content="First version",
        principal_id="alice",
        scopes=SCOPES,
    )["idempotent"]
    with pytest.raises(IntakeError) as conflict:
        store.capture(
            "research",
            session["session_id"],
            "visit-one",
            expected_revision=1,
            url="https://example.org/a",
            title="Article A",
            note="Changed",
            saved=False,
            content="First version",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert conflict.value.code == "idempotency_conflict"
    second = store.capture(
        "research",
        session["session_id"],
        "visit-two",
        expected_revision=2,
        url="https://example.org/a",
        title="Article A",
        saved=True,
        content="Corrected version",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert len(second["data"]["trail"]) == 2
    assert second["data"]["trail"][1]["version"] == 2
    assert (
        store.inspect_source(
            "research",
            source_id,
            version=1,
            principal_id="alice",
            scopes=SCOPES,
        )["content"]
        == "First version"
    )
    assert (
        store.inspect_source(
            "research",
            source_id,
            principal_id="alice",
            scopes=SCOPES,
        )["content"]
        == "Corrected version"
    )
    later = store.capture(
        "research", session["session_id"], "visit-three", expected_revision=3,
        url="https://example.org/a", title="Article A", saved=False,
        principal_id="alice", scopes=SCOPES,
    )
    assert later["data"]["trail"][-1]["snapshot_preserved"] is True
    assert later["data"]["trail"][-1]["version"] == 2
    assert store.inspect_source(
        "research", source_id, principal_id="alice", scopes=SCOPES,
    )["content"] == "Corrected version"
    with pytest.raises(IntakeError) as hidden:
        store.inspect_source("research", source_id, principal_id="bob", scopes=SCOPES)
    assert hidden.value.code == "source_not_found"
    conn.close()
    conn = duckdb.connect(path)
    assert (
        IntakeExplorationStore(conn, initialize=False).inspect_source(
            "research",
            source_id,
            principal_id="alice",
            scopes=SCOPES,
        )["version"]
        == 2
    )
    conn.close()


def test_fetch_replay_avoids_network_and_rejects_unsafe_urls(tmp_path):
    conn = duckdb.connect(str(tmp_path / "explore.duckdb"))
    session = IntakeStore(conn).create(
        "research",
        "Exploration",
        "rabbit",
        intent="Rabbit hole",
        principal_id="alice",
        scopes=SCOPES,
    )
    store = IntakeExplorationStore(conn)
    with pytest.raises(IntakeError) as unsafe:
        store.capture(
            "research",
            session["session_id"],
            "unsafe",
            expected_revision=1,
            url="https://localhost/private",
            title="Local",
            fetch_readable=True,
            principal_id="alice",
            scopes=SCOPES,
        )
    assert unsafe.value.code == "unsafe_source_url"
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return "Readable body", "Fetched title", "fixture"

    first = store.capture(
        "research",
        session["session_id"],
        "fetch",
        expected_revision=1,
        url="https://example.org/article",
        title="https://example.org/article",
        fetch_readable=True,
        principal_id="alice",
        scopes=SCOPES,
        page_fetch=fake_fetch,
    )
    assert first["data"]["trail"][0]["title"] == "Fetched title"
    assert store.capture(
        "research",
        session["session_id"],
        "fetch",
        expected_revision=1,
        url="https://example.org/article",
        title="https://example.org/article",
        fetch_readable=True,
        principal_id="alice",
        scopes=SCOPES,
        page_fetch=fake_fetch,
    )["idempotent"]
    assert fetched == ["https://example.org/article"]
    conn.close()


def test_escalated_feed_item_keeps_identity_in_exploration(tmp_path):
    conn = duckdb.connect(str(tmp_path / "explore.duckdb"))
    inbox = IntakeInboxStore(conn)
    sub = inbox.subscribe(
        "research",
        "https://example.org/rss",
        "Feed",
        "rss_atom",
        principal_id="alice",
        scopes=SCOPES,
    )
    inbox.ingest(
        "research",
        sub["subscription_id"],
        [{"url": "https://example.org/article", "title": "Article"}],
        principal_id="alice",
        scopes=SCOPES,
    )
    item_id = inbox.list("research", principal_id="alice", scopes=SCOPES)["items"][0][
        "item_id"
    ]
    awareness = inbox.start_awareness(
        "research",
        "today",
        intent="Triage",
        principal_id="alice",
        scopes=SCOPES,
    )
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
        reason="Interesting",
        intent="Browse",
        principal_id="alice",
        scopes=SCOPES,
    )
    visited = IntakeExplorationStore(conn).link_feed_item(
        "research",
        exploration["session_id"],
        item_id,
        "visit",
        expected_revision=1,
        note="Follow this",
        saved=True,
        principal_id="alice",
        scopes=SCOPES,
    )
    assert visited["data"]["trail"][0]["source_id"] == item_id
    assert {ref["kind"] for ref in visited["references"]} == {"intake_feed_item"}
    assert IntakeExplorationStore(conn).link_feed_item(
        "research",
        exploration["session_id"],
        item_id,
        "visit",
        expected_revision=1,
        note="Follow this",
        saved=True,
        principal_id="alice",
        scopes=SCOPES,
    )["idempotent"]
    conn.close()
