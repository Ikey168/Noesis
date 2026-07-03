"""Tests for relationship_path() (R11 #616)."""

from src.osint import relationship_path


def _corpus(seed):
    seed.articles(
        [
            ("d1", "Rivera and Grid Authority", "http://a/1", "Alpha Wire", "2026-01-01"),
            ("d2", "Grid Authority and Delphi", "http://b/1", "Beta Journal", "2026-01-02"),
            ("d3", "Unrelated actors", "http://c/1", "Gamma Review", "2026-01-03"),
        ]
    )
    seed.actors(
        [
            ("d1", "Jordan Rivera", "person:jr", "speaker"),
            ("d1", "Grid Authority", "org:ga", "subject"),
            ("d2", "Grid Authority", "org:ga", "subject"),
            ("d2", "Delphi Energy", "org:de", "subject"),
            ("d3", "Faraway Person", "person:fp", "speaker"),
        ]
    )


def test_path_has_cited_evidence_on_every_edge(seed):
    _corpus(seed)
    out = relationship_path(seed.conn, "Jordan Rivera", "Delphi Energy")
    assert out["connected"] is True
    assert out["path"] == ["Jordan Rivera", "Grid Authority", "Delphi Energy"]
    assert out["hops"] == 2
    for edge in out["edges"]:
        assert edge["shared_documents"] >= 1
        assert edge["evidence"]  # cited documents establishing the edge
        for cite in edge["evidence"]:
            assert cite["cited"] is True and cite["url"]


def test_disconnected_entities_report_no_path(seed):
    _corpus(seed)
    out = relationship_path(seed.conn, "Jordan Rivera", "Faraway Person")
    assert out["connected"] is False
    assert "no co-mention path" in out["note"]


def test_resolution_ambiguity_is_surfaced(seed):
    seed.articles([("d1", "doc", "http://a/1", "Alpha Wire", "2026-01-01"),
                   ("d2", "doc", "http://b/1", "Beta Journal", "2026-01-02")])
    seed.actors(
        [
            ("d1", "J. Smith", "person:smith1", "speaker"),
            ("d2", "J. Smith", "person:smith2", "speaker"),  # same name, two ids
            ("d1", "Org X", "org:x", "subject"),
            ("d2", "Org X", "org:x", "subject"),
        ]
    )
    out = relationship_path(seed.conn, "J. Smith", "Org X")
    assert out["resolution"]["a"]["ambiguous"] is True
    assert set(out["resolution"]["a"]["candidates"]) == {"person:smith1", "person:smith2"}


def test_same_entity_is_trivially_connected(seed):
    _corpus(seed)
    out = relationship_path(seed.conn, "Grid Authority", "Grid Authority")
    assert out["connected"] is True
    assert out["hops"] == 0
