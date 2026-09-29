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
        "research",
        session["session_id"],
        "visit-three",
        expected_revision=3,
        url="https://example.org/a",
        title="Article A",
        saved=False,
        principal_id="alice",
        scopes=SCOPES,
    )
    assert later["data"]["trail"][-1]["snapshot_preserved"] is True
    assert later["data"]["trail"][-1]["version"] == 2
    assert (
        store.inspect_source(
            "research",
            source_id,
            principal_id="alice",
            scopes=SCOPES,
        )["content"]
        == "Corrected version"
    )
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


def test_annotations_follow_source_versions_and_owner(tmp_path):
    conn = duckdb.connect(str(tmp_path / "annotations.duckdb"))
    session = IntakeStore(conn).create(
        "research",
        "Exploration",
        "notes",
        intent="Browse",
        principal_id="alice",
        scopes=SCOPES,
    )
    store = IntakeExplorationStore(conn)
    first = store.capture(
        "research",
        session["session_id"],
        "page-v1",
        expected_revision=1,
        url="https://example.org/topic",
        title="Topic",
        content="Original",
        principal_id="alice",
        scopes=SCOPES,
    )
    source_id = first["references"][0]["id"]
    note = store.annotate_source(
        "research",
        source_id,
        "note-1",
        "Check this claim",
        locator={"section": "Methods"},
        principal_id="alice",
        scopes=SCOPES,
    )
    assert note["source_version"] == 1
    assert note["reference"]["kind"] == "exploration_annotation"
    assert (
        store.annotate_source(
            "research",
            source_id,
            "note-1",
            "Check this claim",
            locator={"section": "Methods"},
            principal_id="alice",
            scopes=SCOPES,
        )["idempotent"]
        is True
    )
    with pytest.raises(IntakeError) as conflict:
        store.annotate_source(
            "research",
            source_id,
            "note-1",
            "Changed",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert conflict.value.code == "idempotency_conflict"
    store.capture(
        "research",
        session["session_id"],
        "page-v2",
        expected_revision=2,
        url="https://example.org/topic",
        title="Topic",
        content="Corrected",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert (
        store.inspect_source(
            "research",
            source_id,
            principal_id="alice",
            scopes=SCOPES,
        )["annotations"]
        == []
    )
    assert (
        store.inspect_source(
            "research",
            source_id,
            version=1,
            principal_id="alice",
            scopes=SCOPES,
        )["annotations"][0]["annotation_id"]
        == note["annotation_id"]
    )
    with pytest.raises(IntakeError) as hidden:
        store.annotate_source(
            "research",
            source_id,
            "steal",
            "Mine",
            principal_id="bob",
            scopes=SCOPES,
        )
    assert hidden.value.code == "source_not_found"
    conn.close()


def test_related_source_provenance_follow_dismiss_and_replay(tmp_path):
    path = str(tmp_path / "related.duckdb")
    conn = duckdb.connect(path)
    ledger = IntakeStore(conn)
    store = IntakeExplorationStore(conn)
    library = ledger.create(
        "research",
        "Exploration",
        "library",
        intent="Keep sources",
        principal_id="alice",
        scopes=SCOPES,
    )
    for revision, (key, url, title) in enumerate(
        [
            ("first", "https://example.net/heat", "Urban heat adaptation methods"),
            ("second", "https://example.com/heat", "Urban heat adaptation policies"),
        ],
        start=1,
    ):
        store.capture(
            "research",
            library["session_id"],
            key,
            expected_revision=revision,
            url=url,
            title=title,
            content="Urban heat adaptation in cities",
            principal_id="alice",
            scopes=SCOPES,
        )
    exploration = ledger.create(
        "research",
        "Exploration",
        "current",
        intent="Explore climate",
        principal_id="alice",
        scopes=SCOPES,
    )
    current = store.capture(
        "research",
        exploration["session_id"],
        "anchor",
        expected_revision=1,
        url="https://example.org/climate",
        title="Urban climate heat adaptation",
        content="Cities compare adaptation methods and policies",
        principal_id="alice",
        scopes=SCOPES,
    )
    # A later correction must not rewrite the terms or version of this visit.
    store.capture(
        "research",
        library["session_id"],
        "correct-anchor",
        expected_revision=3,
        url="https://example.org/climate",
        title="Unrelated music theory",
        content="Harmony counterpoint rhythm melody",
        principal_id="alice",
        scopes=SCOPES,
    )
    session_id = exploration["session_id"]
    suggestions = store.related_sources(
        "research",
        session_id,
        principal_id="alice",
        scopes=SCOPES,
    )["suggestions"]
    assert len(suggestions) == 2
    assert suggestions[0]["method"] == "lexical_overlap_v1"
    assert suggestions[0]["cross_domain"] is True
    assert "adaptation" in suggestions[0]["shared_terms"]
    assert suggestions[0]["anchor"]["reference"]["id"] == current["references"][0]["id"]
    dismissed = store.decide_related_source(
        "research",
        session_id,
        suggestions[0]["suggestion_id"],
        "skip",
        expected_revision=2,
        decision="dismiss",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert len(dismissed["data"]["trail"]) == 1
    assert (
        store.decide_related_source(
            "research",
            session_id,
            suggestions[0]["suggestion_id"],
            "skip",
            expected_revision=2,
            decision="dismiss",
            principal_id="alice",
            scopes=SCOPES,
        )["idempotent"]
        is True
    )
    remaining = store.related_sources(
        "research",
        session_id,
        principal_id="alice",
        scopes=SCOPES,
    )["suggestions"]
    assert len(remaining) == 1
    followed = store.decide_related_source(
        "research",
        session_id,
        remaining[0]["suggestion_id"],
        "follow",
        expected_revision=3,
        decision="follow",
        saved=True,
        principal_id="alice",
        scopes=SCOPES,
    )
    assert (
        followed["data"]["trail"][-1]["source_id"]
        == remaining[0]["candidate"]["source_id"]
    )
    assert followed["data"]["trail"][-1]["saved"] is True
    assert remaining[0]["candidate"]["reference"] in followed["references"]
    assert (
        store.related_sources(
            "research",
            session_id,
            principal_id="alice",
            scopes=SCOPES,
        )["suggestions"]
        == []
    )
    conn.close()
    conn = duckdb.connect(path)
    assert (
        len(
            IntakeStore(conn).inspect(
                "research",
                session_id,
                principal_id="alice",
                scopes=SCOPES,
            )["data"]["suggestion_actions"]
        )
        == 2
    )
    with pytest.raises(IntakeError) as hidden:
        IntakeExplorationStore(conn, initialize=False).related_sources(
            "research",
            session_id,
            principal_id="bob",
            scopes=SCOPES,
        )
    assert hidden.value.code == "unauthorized"
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


def test_feed_visit_anchors_related_sources_without_recapture(tmp_path):
    conn = duckdb.connect(str(tmp_path / "feed-related.duckdb"))
    inbox = IntakeInboxStore(conn)
    subscription = inbox.subscribe(
        "research",
        "https://example.org/rss",
        "Reading",
        "rss_atom",
        principal_id="alice",
        scopes=SCOPES,
    )
    inbox.ingest(
        "research",
        subscription["subscription_id"],
        [
            {
                "url": "https://example.org/a",
                "title": "Urban climate adaptation",
                "content": "Cities compare urban climate adaptation methods",
            },
            {
                "url": "https://example.net/b",
                "title": "Urban climate methods",
                "content": "Urban adaptation methods for cities",
            },
        ],
        principal_id="alice",
        scopes=SCOPES,
    )
    items = inbox.list("research", principal_id="alice", scopes=SCOPES)["items"]
    anchor_id = next(
        item["item_id"] for item in items if item["original_url"].endswith("/a")
    )
    candidate_id = next(
        item["item_id"] for item in items if item["original_url"].endswith("/b")
    )
    session = IntakeStore(conn).create(
        "research",
        "Exploration",
        "from-feed",
        intent="Follow a signal",
        principal_id="alice",
        scopes=SCOPES,
    )
    store = IntakeExplorationStore(conn)
    visited = store.link_feed_item(
        "research",
        session["session_id"],
        anchor_id,
        "visit",
        expected_revision=1,
        principal_id="alice",
        scopes=SCOPES,
    )
    assert visited["data"]["trail"][0]["source_id"] == anchor_id
    # Revise the feed item after the visit; the suggestion must cite v1.
    inbox.ingest(
        "research",
        subscription["subscription_id"],
        [
            {
                "url": "https://example.org/a",
                "title": "Unrelated music theory",
                "content": "Harmony counterpoint melody rhythm",
            },
        ],
        principal_id="alice",
        scopes=SCOPES,
    )
    suggestions = store.related_sources(
        "research",
        session["session_id"],
        principal_id="alice",
        scopes=SCOPES,
    )["suggestions"]
    feed_suggestion = next(
        suggestion
        for suggestion in suggestions
        if suggestion["candidate"]["source_id"] == candidate_id
    )
    assert feed_suggestion["anchor"]["reference"]["kind"] == "intake_feed_item"
    assert feed_suggestion["anchor"]["reference"]["version"] == 1
    assert feed_suggestion["candidate"]["reference"]["kind"] == "intake_feed_item"
    inbox.ingest(
        "research",
        subscription["subscription_id"],
        [
            {
                "url": "https://example.net/b",
                "title": "Urban climate methods updated",
                "content": "Urban adaptation methods for cities revised",
            },
        ],
        principal_id="alice",
        scopes=SCOPES,
    )
    with pytest.raises(IntakeError) as stale:
        store.decide_related_source(
            "research",
            session["session_id"],
            feed_suggestion["suggestion_id"],
            "follow-stale",
            expected_revision=2,
            decision="follow",
            saved=True,
            expected_candidate_version=1,
            principal_id="alice",
            scopes=SCOPES,
        )
    assert stale.value.code == "suggestion_stale"
    feed_suggestion = next(
        suggestion
        for suggestion in store.related_sources(
            "research",
            session["session_id"],
            principal_id="alice",
            scopes=SCOPES,
        )["suggestions"]
        if suggestion["candidate"]["source_id"] == candidate_id
    )
    followed = store.decide_related_source(
        "research",
        session["session_id"],
        feed_suggestion["suggestion_id"],
        "follow-feed",
        expected_revision=2,
        decision="follow",
        saved=True,
        expected_candidate_version=feed_suggestion["candidate"]["reference"]["version"],
        principal_id="alice",
        scopes=SCOPES,
    )
    assert followed["data"]["trail"][-1]["source_id"] == candidate_id
    assert feed_suggestion["candidate"]["reference"] in followed["references"]
    conn.close()
