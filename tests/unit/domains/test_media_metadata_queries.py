"""Answer a title, creator or identifier with authority records and revision history as of a date (#2225, MM09)."""

from __future__ import annotations

import pytest

from src.evidence_bundle.builder import compute_bundle_id
from src.kb.media_metadata import MediaMetadataError, MediaMetadataIdentity, MediaMetadataQueries
from tests.unit import media_metadata_harness as h


@pytest.fixture(scope="module")
def env():
    value = h.Env()
    assert value.run()["status"] == "complete"
    MediaMetadataIdentity(value.conn).propose(h.NS, scopes=h.WRITE, principal_id="analyst")
    yield value
    value.conn.close()


def ask(env, **kwargs):
    return MediaMetadataQueries(env.conn).answer(h.NS, scopes=h.READ, **kwargs)


def sources(identity):
    return {(e["source"], e["native_id"]) for e in identity["authority_records"]}


def test_an_identifier_resolves_exactly_to_its_identity_and_authority_records(env):
    answer = ask(env, identifier=h.AUTHOR_QID)
    assert answer["status"] == "resolved" and len(answer["identities"]) == 1
    identity = answer["identities"][0]
    assert sources(identity) == {("wikidata", h.AUTHOR_QID), ("open-library", h.AUTHOR_OLID), ("dnb", h.GND),
                                 ("loc", h.LCNAF)}
    for entry in identity["authority_records"]:
        citation = entry["citation"]
        assert citation["revision_id"] and citation["revision_marker"] and citation["retrieved_at"]
        assert citation["licence"]["id"]
    assert {m["basis"] for m in identity["matches_used"]} == {"explicit-identifier"}
    wikidata = next(e for e in identity["authority_records"] if e["source"] == "wikidata")
    assert [r["marker"] for r in wikidata["revision_history"]] == ["2000000001", "2100000001"]
    same = ask(env, identifier=f"(DE-588){h.GND}", scheme="gnd")
    assert sources(same["identities"][0]) == sources(identity)


def test_as_of_selects_the_revision_current_at_the_date(env):
    answer = ask(env, identifier=h.AUTHOR_QID, as_of="2025-07-01")
    wikidata = next(e for e in answer["identities"][0]["authority_records"] if e["source"] == "wikidata")
    assert wikidata["citation"]["revision_marker"] == "2000000001" and wikidata["later_revisions"] == 1
    assert [r["marker"] for r in wikidata["revision_history"]] == ["2000000001"]
    assert ask(env, identifier=h.AUTHOR_QID, as_of="2020-01-01")["status"] == "unresolved"  # nothing known yet


def test_redirects_and_merges_are_visible(env):
    answer = ask(env, identifier=h.OLD_RECORDING_MBID)
    assert answer["status"] == "resolved"
    assert answer["redirects"][0]["chain"] == [h.OLD_RECORDING_MBID, h.RECORDING_MBID]
    assert answer["redirects"][0]["history"][0]["kind"] == "redirect"
    assert ("musicbrainz", h.RECORDING_MBID) in sources(answer["identities"][0])
    by_isrc = ask(env, identifier=h.ISRC)
    assert by_isrc["status"] == "resolved" and ("musicbrainz", h.RECORDING_MBID) in sources(by_isrc["identities"][0])
    deprecated = ask(env, identifier=h.OLD_GND, scheme="gnd")
    assert deprecated["redirects"][0]["chain"] == [h.OLD_GND, h.GND]
    old = MediaMetadataQueries(env.conn).authority_history(h.NS, f"dnb:{h.OLD_GND}", scopes=h.READ)
    assert old["revisions"][0]["status"] == "redirected" and old["redirects"][0]["to_id"] == h.GND


def test_ambiguous_invalid_and_unknown_identifiers_are_reported_as_such(env):
    conflict = ask(env, identifier=h.ISBN)
    assert conflict["status"] == "ambiguous" and len(conflict["identities"]) > 1
    assert conflict["conflicts"][0]["key"] == f"isbn:{h.ISBN}"
    assert conflict["unknowns"][0]["kind"] == "ambiguous_identifier"
    assert ask(env, identifier=h.SECOND_ISBN)["status"] == "resolved"
    assert ask(env, identifier="978-3-00-000001-1", scheme="isbn13")["status"] == "invalid"
    unknown = ask(env, identifier=h.UNKNOWN_ISBN)
    assert unknown["status"] == "unresolved" and unknown["unknowns"][0]["kind"] == "unknown_identifier"
    assert ask(env, identifier="not an identifier")["unknowns"][0]["kind"] == "unrecognized_identifier"
    with pytest.raises(MediaMetadataError):
        ask(env)


def test_title_and_creator_lookups_return_ranked_candidates_with_evidence(env):
    answer = ask(env, title="The Glass Orchard")
    assert answer["status"] == "candidates" and answer["ambiguous"] is True  # two works share the title
    assert all("title" in c["evidence"] for c in answer["candidates"])
    assert [c["rank"] for c in answer["candidates"]] == list(range(1, len(answer["candidates"]) + 1))
    narrowed = ask(env, title="The Glass Orchard", creator="Mara Quell")
    top = narrowed["candidates"][0]
    assert top["score"] == 1.0 and top["evidence"]["creator"]["matched"] == "Mara Quell"
    # The other work of the same title (another author) is a candidate by title alone, never with the creator.
    assert h.OTHER_WORK_OLID in {c["native_id"] for c in answer["candidates"]}
    assert h.OTHER_WORK_OLID not in {c["native_id"] for c in narrowed["candidates"] if c["score"] == top["score"]}
    creator = ask(env, creator="Quell, Mara")
    assert creator["candidates"][0]["level"] == "creator"
    assert ask(env, title="Completely different")["status"] == "unresolved"


def test_answers_export_as_evidence_bundles(env):
    answer = ask(env, identifier=h.AUTHOR_QID)
    bundle = MediaMetadataQueries(env.conn).export_bundle(answer, created_at_ms=1)
    assert bundle["contract"] == "noesis-evidence-bundle-v1" and bundle["bundle_id"] == compute_bundle_id(bundle)
    kinds = [o["payload"].get("kind") for o in bundle["objects"]]
    assert kinds.count("media-authority-revision") == 4 and "media-identity-match" in kinds
    assert bundle["completeness"]["status"] == "complete"
    ambiguous = MediaMetadataQueries(env.conn).export_bundle(ask(env, identifier=h.ISBN), created_at_ms=1)
    assert ambiguous["completeness"]["status"] == "partial"


def test_reads_need_the_cultural_read_scope(env):
    with pytest.raises(MediaMetadataError) as caught:
        MediaMetadataQueries(env.conn).answer(h.NS, scopes={"namespace:global:read"}, identifier=h.AUTHOR_QID)
    assert caught.value.code == "unauthorized"
