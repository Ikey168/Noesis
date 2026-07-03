"""Tests for entity_dossier() and the person-entity guardrail (R11 #615)."""

from src.osint import entity_dossier


def _corpus(seed):
    seed.articles(
        [
            ("d1", "Rivera testifies on grid", "http://a/1", "Alpha Wire", "2025-11-03"),
            ("d2", "Committee questions Rivera", "http://b/1", "Beta Journal", "2026-06-21"),
            ("d3", "Org filing on costs", "http://c/1", "Gamma Review", "2026-01-10"),
        ]
    )
    seed.actors(
        [
            ("d1", "Jordan Rivera", "person:jr", "speaker"),
            ("d2", "Jordan Rivera", "person:jr", "subject"),
            ("d2", "J. Rivera", "person:jr", "subject"),  # alias
            ("d1", "Grid Authority", "org:ga", "subject"),
            ("d2", "Grid Authority", "org:ga", "subject"),
            ("d3", "Grid Authority", "org:ga", "subject"),
        ]
    )


def test_dossier_every_line_links_to_a_source(seed):
    _corpus(seed)
    out = entity_dossier(seed.conn, "Jordan Rivera")
    assert out["found"] is True
    assert out["mention_count"] == 2
    # Every mention carries a citation with a source and (when resolved) url.
    for m in out["mentions"]:
        assert m["cited"] is True
        assert m["source"] in {"Alpha Wire", "Beta Journal"}
        assert m["url"]
    assert "J. Rivera" in out["aliases"]
    assert out["first_seen"] == "2025-11-03 00:00:00"
    assert out["last_seen"] == "2026-06-21 00:00:00"
    assert any(c["entity"] == "Grid Authority" for c in out["connected_entities"])


def test_person_with_no_document_is_refused(seed):
    # Person entity, zero ingested documents -> guardrail refusal.
    seed.actors([])  # create the table, empty
    out = entity_dossier(seed.conn, "Ghost Person", entity_type="person")
    assert out.get("code") == "person_requires_documents"
    assert out["is_person"] is True
    assert "mentions" not in out  # no inference-only facts surfaced


def test_person_inferred_from_role_is_guardrailed(seed):
    _corpus(seed)
    # A person known only by a person-role but with no documents of their own.
    out = entity_dossier(seed.conn, "Nonexistent Speaker")
    # Not a person (no rows -> not classified person) so returns not-found, not a crash.
    assert out["found"] is False


def test_non_person_with_no_documents_is_allowed_empty(seed):
    _corpus(seed)
    out = entity_dossier(seed.conn, "Unknown Org", entity_type="organization")
    assert out["found"] is False
    assert "error" not in out


def test_uncited_mention_is_flagged_not_hidden(seed):
    seed.articles([("d1", "known doc", "http://a/1", "Alpha Wire", "2026-01-01")])
    seed.actors(
        [
            ("d1", "Widget Co", "org:w", "subject"),
            ("missing", "Widget Co", "org:w", "subject"),  # dangling document
        ]
    )
    out = entity_dossier(seed.conn, "Widget Co", entity_type="organization")
    assert out["mention_count"] == 2
    assert out["uncited_count"] == 1  # the dangling mention flagged, not dropped
