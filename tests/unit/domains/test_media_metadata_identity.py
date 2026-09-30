"""Reviewable media identity and citation-only links to cultural objects and news entities (#2225, MM07-MM08)."""

from __future__ import annotations

import pytest

from src.kb.media_metadata import (
    MediaMetadataError,
    MediaMetadataIdentity,
    MediaMetadataLinks,
    authority_reference,
    name_tokens,
)
from tests.unit import media_metadata_harness as h


@pytest.fixture()
def env():
    value = h.Env()
    assert value.run()["status"] == "complete"
    value.canonical_entity("ent-mara-quell", "Mara Quell")
    value.canonical_entity("ent-harbour-authority", "Harbour Authority", "ORG")
    yield value
    value.conn.close()


def ids(env, *pairs):
    return {env.record_id(source, native) for source, native in pairs}


def between(result, env, left, right):
    wanted = ids(env, left, right)
    return next(m for m in result["matches"] if {m["left_id"], m["right_id"]} == wanted)


def test_explicit_identifier_statements_are_matches_citing_the_asserting_revision(env):
    identity = MediaMetadataIdentity(env.conn)
    result = identity.propose(h.NS, scopes=h.WRITE, principal_id="analyst")
    author = between(result, env, ("wikidata", h.AUTHOR_QID), ("dnb", h.GND))
    assert (author["basis"], author["candidate_state"], author["state"]) == (
        "explicit-identifier", "identifier-match", "accepted")
    evidence = author["evidence"][0]
    assert evidence["property"] == "P227" and evidence["rank"] == "preferred"
    wikidata_current = env.store.current_revision(h.NS, env.record_id("wikidata", h.AUTHOR_QID))
    assert evidence["revision_id"] == author["asserting_revision_id"] == wikidata_current["revision_id"]
    assert evidence["revision_marker"] == "2100000001"
    mb = between(result, env, ("musicbrainz", h.ARTIST_MBID), ("wikidata", h.BAND_QID))
    assert mb["candidate_state"] == "identifier-match"  # MusicBrainz URL relation and Wikidata P434 agree
    work = between(result, env, ("open-library", h.WORK_OLID), ("wikidata", h.WORK_QID))
    assert work["evidence"][0]["property"] == "P648"
    lccn = between(result, env, ("open-library", h.EDITION_OLID), ("loc", h.LCCN))
    assert lccn["candidate_state"] == "identifier-match"
    # A deprecated statement (the old GND on Wikidata) never creates a match.
    assert not [m for m in result["matches"] if env.record_id("dnb", h.OLD_GND) in {m["left_id"], m["right_id"]}]


def test_an_isbn_claimed_by_two_works_stays_a_visible_conflict(env):
    identity = MediaMetadataIdentity(env.conn)
    result = identity.propose(h.NS, scopes=h.WRITE, principal_id="analyst")
    conflict = next(c for c in result["conflicts"] if c["key"] == f"isbn:{h.ISBN}")
    assert set(conflict["claimed_by"]["open-library"]) == ids(env, ("open-library", h.EDITION_OLID),
                                                              ("open-library", h.CLAIMING_EDITION_OLID))
    dnb = between(result, env, ("dnb", h.IDN), ("open-library", h.CLAIMING_EDITION_OLID))
    assert (dnb["candidate_state"], dnb["state"]) == ("conflict", "unreviewed")
    assert dnb["evidence"][-1]["kind"] == "conflict"
    # The explicit Open Library -> Wikidata identifier statement outranks the conflicting shared ISBN.
    assert between(result, env, ("open-library", h.EDITION_OLID), ("wikidata", h.EDITION_QID))["state"] == "accepted"


def test_similarity_only_proposes_scored_candidates_and_records_are_never_merged(env):
    identity = MediaMetadataIdentity(env.conn)
    result = identity.propose(h.NS, scopes=h.WRITE, principal_id="analyst")
    candidate = between(result, env, ("dnb", h.GND), ("loc", h.LCNAF))
    assert (candidate["basis"], candidate["candidate_state"], candidate["state"]) == (
        "similarity", "candidate", "unreviewed")
    assert candidate["evidence"][0]["components"]["name"] == 1.0  # "Quell, Mara" meets "Quell, Mara,"
    second = between(result, env, ("open-library", h.SECOND_EDITION_OLID), ("dnb", h.IDN))
    assert 0.5 <= second["score"] < 1 and second["evidence"][0]["method"] == "title-creator-date-v1"
    assert len(env.store.records(h.NS)) == 23  # nothing merged
    news = [m for m in result["matches"] if m["right_kind"] == "canonical-entity"]
    assert {m["right_id"] for m in news} == {"ent-mara-quell"} and all(m["state"] == "unreviewed" for m in news)
    before = env.conn.execute("SELECT * FROM canonical_entities ORDER BY canonical_id").fetchall()
    identity.propose(h.NS, scopes=h.WRITE, principal_id="analyst")  # idempotent, read-only on News
    assert env.conn.execute("SELECT * FROM canonical_entities ORDER BY canonical_id").fetchall() == before


def test_reviews_are_recorded_and_reversible(env):
    identity = MediaMetadataIdentity(env.conn)
    result = identity.propose(h.NS, scopes=h.WRITE, principal_id="analyst")
    conflict = between(result, env, ("dnb", h.IDN), ("open-library", h.EDITION_OLID))
    with pytest.raises(MediaMetadataError) as caught:
        identity.review(h.NS, conflict["match_id"], "accepted", "", scopes=h.REVIEW, principal_id="reviewer")
    assert caught.value.code == "invalid_decision"
    with pytest.raises(MediaMetadataError) as caught:
        identity.review(h.NS, conflict["match_id"], "accepted", "same", scopes=h.WRITE, principal_id="reviewer")
    assert caught.value.code == "unauthorized"
    accepted = identity.review(h.NS, conflict["match_id"], "accepted", "same title, author and year",
                               scopes=h.REVIEW, principal_id="reviewer")
    assert accepted["state"] == "accepted" and accepted["candidate_state"] == "conflict"  # conflict stays visible
    decision = env.conn.execute("SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
                                [accepted["decision_id"]]).fetchone()
    assert decision == ("match",)
    other = between(result, env, ("dnb", h.IDN), ("open-library", h.CLAIMING_EDITION_OLID))
    identity.review(h.NS, other["match_id"], "rejected", "different work (1988)", scopes=h.REVIEW,
                    principal_id="reviewer")
    cluster = identity.cluster(h.NS, env.record_id("dnb", h.IDN))
    assert env.record_id("open-library", h.EDITION_OLID) in cluster
    assert env.record_id("open-library", h.CLAIMING_EDITION_OLID) not in cluster
    reverted = identity.revert(h.NS, conflict["match_id"], "reviewed too early", scopes=h.REVIEW,
                               principal_id="reviewer")
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]] == ["accepted", "reverted"]
    assert env.record_id("open-library", h.EDITION_OLID) not in identity.cluster(h.NS, env.record_id("dnb", h.IDN))
    identity.propose(h.NS, scopes=h.WRITE, principal_id="analyst")  # a later proposal never overrides a review
    assert identity._row(h.NS, conflict["match_id"])["state"] == "reverted"


def test_cultural_objects_link_by_identifier_or_provider_relation_and_overlap_is_a_candidate(env):
    by_gnd = env.cultural_object("FIXTUREDDBMEDIA0000000000000001", "Portrait photograph",
                                 creators=[{"name": "Quell, Mara", "role": "creator",
                                            "authority_id": f"https://d-nb.info/gnd/{h.GND}"}])
    by_relation = env.cultural_object("FIXTUREDDBMEDIA0000000000000002", "Poster",
                                      same_as=[f"https://www.wikidata.org/wiki/{h.WORK_QID}"])
    by_words = env.cultural_object("FIXTUREDDBMEDIA0000000000000003", "Glass Orchard festival poster")
    unrelated = env.cultural_object("FIXTUREDDBMEDIA0000000000000004", "Street scene")
    links = MediaMetadataLinks(env.conn)
    result = links.link_cultural_objects(h.NS, scopes=h.WRITE, principal_id="analyst")
    by_subject = {}
    for link in result["links"]:
        by_subject.setdefault(link["subject_id"], []).append(link)
    gnd_link = by_subject[by_gnd][0]
    assert (gnd_link["basis"], gnd_link["state"], gnd_link["record_id"]) == (
        "identifier", "linked", env.record_id("dnb", h.GND))
    object_revision = env.conn.execute("SELECT revision_id FROM cultural_current WHERE object_id=?",
                                       [by_gnd]).fetchone()[0]
    assert gnd_link["subject_revision"] == gnd_link["evidence"]["object_revision_id"] == object_revision
    assert gnd_link["record_revision_id"] == env.store.current_revision(h.NS, gnd_link["record_id"])["revision_id"]
    relation = by_subject[by_relation][0]
    assert (relation["basis"], relation["state"]) == ("provider-relation", "linked")
    assert {link["state"] for link in by_subject[by_words]} == {"candidate"}
    assert all(link["basis"] == "keyword-overlap" for link in by_subject[by_words])
    assert unrelated not in by_subject
    rejected = links.review_link(h.NS, by_subject[by_words][0]["link_id"], "rejected", "a festival, not the novel",
                                 scopes=h.REVIEW, principal_id="reviewer")
    assert rejected["state"] == "rejected"
    links.link_cultural_objects(h.NS, scopes=h.WRITE, principal_id="analyst")
    assert links.link(h.NS, rejected["link_id"])["state"] == "rejected"  # a later suggestion never re-labels it
    asserted = links.assert_link(h.NS, "cultural-object", unrelated, f"open-library:{h.WORK_OLID}",
                                 "caption names the novel", scopes=h.REVIEW, principal_id="reviewer")
    assert (asserted["basis"], asserted["state"]) == ("reviewed-assertion", "linked")


def test_news_entities_get_reviewable_candidates_never_relabelled(env):
    identity = MediaMetadataIdentity(env.conn)
    identity.propose(h.NS, scopes=h.WRITE, principal_id="analyst")
    links = MediaMetadataLinks(env.conn)
    candidates = links.link_news_entities(h.NS, scopes=h.WRITE, principal_id="analyst")["links"]
    assert candidates and {c["state"] for c in candidates} == {"candidate"}
    assert {c["subject_id"] for c in candidates} == {"ent-mara-quell"}
    match = next(m for m in identity.matches(h.NS, right_kind="canonical-entity")
                 if m["left_id"] == env.record_id("dnb", h.GND))
    identity.review(h.NS, match["match_id"], "accepted", "same person per GND", scopes=h.REVIEW,
                    principal_id="reviewer")
    linked = links.link_news_entities(h.NS, scopes=h.WRITE, principal_id="analyst")["links"]
    confirmed = next(link for link in linked if link["record_id"] == env.record_id("dnb", h.GND))
    assert (confirmed["state"], confirmed["basis"]) == ("linked", "reviewed-assertion")
    assert confirmed["subject_revision"].startswith("canonical-entity:")
    assert confirmed["record_revision_id"] == env.store.current_revision(h.NS, confirmed["record_id"])["revision_id"]
    assert env.conn.execute("SELECT preferred_name FROM canonical_entities WHERE canonical_id='ent-mara-quell'"
                            ).fetchone() == ("Mara Quell",)


def test_authority_references_and_name_tokens():
    assert authority_reference("https://d-nb.info/gnd/1099990001") == {"scheme": "gnd", "value": "1099990001"}
    assert authority_reference("http://www.wikidata.org/entity/Q999900002") == {"scheme": "wikidata",
                                                                                 "value": "Q999900002"}
    assert authority_reference("isbn13:9783000000010") == {"scheme": "isbn13", "value": "9783000000010"}
    assert authority_reference("Berlin") is None
    assert name_tokens("Quell, Mara,") == name_tokens("Mara Quell")
