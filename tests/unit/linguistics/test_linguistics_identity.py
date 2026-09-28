"""Reviewable language and lexeme identity with Glottocodes and ISO 639-3 (LG06, #2184)."""

from __future__ import annotations

import pytest

from src.kb.linguistics_identity import LinguisticsIdentity
from src.kb.linguistics_records import LinguisticsError
from src.kb.review_targets import ReviewTargets
from tests.unit import linguistics_harness as h

NS = h.NS


@pytest.fixture()
def identity():
    conn = h.connect()
    h.load_all(conn)
    return LinguisticsIdentity(conn, now=lambda: 5_000)


def test_stated_codes_resolve_deterministically(identity):
    assert (
        identity.resolve_languoid(NS, "glottocode", "coas1122")["status"] == "resolved"
    )
    item = identity.resolve_languoid(NS, "wikidata-item", "Q900001")
    assert (
        item["status"] == "resolved"
        and item["glottocode"] == "nort3456"
        and item["basis"] == "item-stated-glottocode"
    )
    wiktionary = identity.resolve_languoid(NS, "wiktionary-code", "qsv")
    assert (
        wiktionary["glottocode"] == "sout7890"
        and wiktionary["basis"] == "glottolog-stated-iso639-3"
    )
    assert identity.resolve_languoid(NS, "wals-code", "nve")["glottocode"] == "nort3456"
    proto = identity.resolve_languoid(NS, "wiktionary-code", "qvl-pro")
    assert proto["status"] == "unresolved" and "Wiktionary-specific" in proto["reason"]
    assert identity.resolve_languoid(NS, "iso639-3", "qzz")["status"] == "unresolved"
    with pytest.raises(LinguisticsError):
        identity.resolve_languoid(NS, "guess", "x")


def test_macrolanguage_and_retired_code_stay_proposed_until_reviewed(identity):
    macro = identity.resolve_languoid(NS, "iso639-3", "qmv")
    assert macro["status"] == "ambiguous" and {
        c["glottocode"] for c in macro["candidates"]
    } == {"nort3456", "sout7890"}
    retired = identity.resolve_languoid(NS, "iso639-3", "qrv")
    assert retired["status"] == "retired" and retired["events"][0]["reason"] == "split"
    assert {c["iso639_3"] for c in retired["candidates"]} == {"qnv", "qsv"}
    proposed = identity.propose_languoid_matches(NS, principal_id="p", scopes=h.SCOPES)
    candidates = proposed["candidates"]
    assert len(candidates) == 4 and {c["state"] for c in candidates} == {"proposed"}
    assert {c["basis"] for c in candidates} == {
        "macrolanguage-member",
        "retired-code-successor",
    }
    again = identity.propose_languoid_matches(NS, principal_id="p", scopes=h.SCOPES)
    assert again["proposed"] == []  # re-proposing unchanged evidence changes nothing
    # an accepted review resolves the macrolanguage reference for that reviewer decision only
    target = next(
        c
        for c in candidates
        if c["left_key"] == "language-ref:iso639-3:qmv"
        and c["right_key"] == "languoid:glottolog:nort3456"
    )
    identity.review(
        NS,
        target["candidate_id"],
        "accept",
        "context shows the northern variety",
        principal_id="r",
        scopes=h.SCOPES,
    )
    assert identity.languoid_for(NS, "language-ref:iso639-3:qmv") == "nort3456"
    assert (
        identity.resolve_languoid(NS, "iso639-3", "qmv")["status"] == "ambiguous"
    )  # source data unchanged


def test_iso_retirements_are_identity_history_events(identity):
    first = identity.record_iso_events(NS, principal_id="p", scopes=h.SCOPES)
    assert first[0]["decision_type"] == "split" and first[0]["event"]["successors"] == [
        "qnv",
        "qsv",
    ]
    again = identity.record_iso_events(NS, principal_id="p", scopes=h.SCOPES)
    assert (
        again[0]["decision_id"] == first[0]["decision_id"]
        and again[0]["idempotent"] is True
    )
    history = identity.history.history(
        NS, "iso639-3:qrv", scopes={"knowledge:entity-history:read"}
    )
    assert [item["decision_type"] for item in history["items"]] == ["split"]


def test_lexeme_candidates_never_merge_homographs(identity):
    result = identity.propose_lexeme_matches(NS, principal_id="p", scopes=h.SCOPES)
    pairs = {(c["left_key"], c["right_key"]): c for c in result["candidates"]}
    noun = (
        "lexeme:kaikki-wiktextract:qnv:tamo:noun:1",
        "lexeme:wikidata-lexemes:L90001",
    )
    verb = (
        "lexeme:kaikki-wiktextract:qnv:tamo:verb:2",
        "lexeme:wikidata-lexemes:L90003",
    )
    assert pairs[noun]["basis"] == "source-stated-sense-item" and pairs[noun][
        "evidence"
    ][0]["shared_sense_items"] == ["Q4022"]
    assert pairs[verb]["basis"] == "same-languoid-lemma-category"
    keys = {k for pair in pairs for k in pair}
    assert not any(
        "L90001" in a and "verb" in b or "L90003" in a and "noun" in b for a, b in pairs
    )
    assert (
        "lexeme:wikidata-lexemes:L90001",
        "lexeme:wikidata-lexemes:L90003",
    ) not in pairs  # same-source homographs
    assert "lexeme:kaikki-wiktextract:qnv:banka:noun:0" not in keys
    assert {c["state"] for c in result["candidates"]} == {"proposed"}
    unmatched = {u["record_key"]: u for u in result["unmatched"]}
    assert (
        unmatched["lexeme:kaikki-wiktextract:en:river:noun:0"]["languoid"]
        == "unresolved"
    )


def test_review_revert_and_equivalence_from_either_side(identity):
    result = identity.propose_lexeme_matches(NS, principal_id="p", scopes=h.SCOPES)
    noun = next(c for c in result["candidates"] if "L90001" in c["right_key"])
    accepted = identity.review(
        NS,
        noun["candidate_id"],
        "accept",
        "same word",
        principal_id="r",
        scopes=h.SCOPES,
    )
    assert accepted["state"] == "accepted"
    for side in (noun["left_key"], noun["right_key"]):
        assert identity.equivalents(NS, side) == sorted(
            [noun["left_key"], noun["right_key"]]
        )
    with pytest.raises(LinguisticsError):
        identity.review(
            NS,
            noun["candidate_id"],
            "reject",
            "again",
            principal_id="r",
            scopes=h.SCOPES,
        )
    reverted = identity.revert(
        NS, noun["candidate_id"], "reviewed in error", principal_id="r", scopes=h.SCOPES
    )
    assert reverted["state"] == "reverted" and identity.equivalents(
        NS, noun["left_key"]
    ) == [noun["left_key"]]
    # proposing again with the same evidence never reactivates the reverted decision
    identity.propose_lexeme_matches(NS, principal_id="p", scopes=h.SCOPES)
    assert (
        identity.candidates(NS, scopes=h.SCOPES, record_key=noun["left_key"])[0][
            "state"
        ]
        == "reverted"
    )
    with pytest.raises(LinguisticsError):
        identity.review(
            NS, "ling-idc:missing", "accept", "x", principal_id="r", scopes=h.SCOPES
        )
    with pytest.raises(LinguisticsError) as caught:
        identity.review(
            NS,
            noun["candidate_id"],
            "accept",
            "x",
            principal_id="r",
            scopes={"knowledge:linguistics:read"},
        )
    assert caught.value.code == "unauthorized"


def test_stronger_evidence_upgrades_a_pending_candidate(identity):
    left, right = (
        "lexeme:kaikki-wiktextract:qnv:tamo:verb:2",
        "lexeme:wikidata-lexemes:L90003",
    )
    evidence = [{"note": "same lemma"}]
    created = identity.offer(
        NS,
        kind="lexeme",
        left_key=left,
        right_key=right,
        basis="same-languoid-lemma-category",
        evidence=evidence,
        principal_id="p",
    )
    upgraded = identity.offer(
        NS,
        kind="lexeme",
        left_key=right,
        right_key=left,
        basis="source-stated-sense-item",
        evidence=[{"note": "shared item"}],
        principal_id="p",
    )
    assert created["change"] == "created" and upgraded["change"] == "upgraded"
    assert upgraded["candidate_id"] == created["candidate_id"]  # found from either side
    weaker = identity.offer(
        NS,
        kind="lexeme",
        left_key=left,
        right_key=right,
        basis="same-languoid-lemma-category",
        evidence=evidence,
        principal_id="p",
    )
    assert weaker["change"] is None


def test_a_reviewer_can_decide_through_the_review_inbox_target(identity):
    result = identity.propose_lexeme_matches(NS, principal_id="p", scopes=h.SCOPES)
    candidate = result["candidates"][0]
    scopes = {
        "knowledge:entity-history:read",
        "knowledge:entity-history:review",
        f"namespace:{NS}:read",
    }
    ReviewTargets(identity.conn).route(
        {"kind": "entity", "namespace": NS, "id": candidate["decision_id"]},
        {"decision": "non-match"},
        rationale="different words",
        principal_id="r",
        scopes=scopes,
        task_id="inbox-task:x",
    )
    assert (
        identity.candidates(NS, scopes=h.SCOPES, record_key=candidate["left_key"])[0][
            "state"
        ]
        == "rejected"
    )


def test_languoid_points_project_into_geospatial_reviewably(identity):
    projected = identity.project_locations(
        NS, geo_namespace="global", principal_id="p", scopes=h.SCOPES
    )
    links = {link["glottocode"]: link for link in projected["links"]}
    assert set(links) == {"nort3456", "sout7890", "coas1122"} and {
        link["state"] for link in links.values()
    } == {"proposed"}
    again = identity.project_locations(
        NS, geo_namespace="global", principal_id="p", scopes=h.SCOPES
    )
    assert [link["resolution_id"] for link in again["links"]] == [
        link["resolution_id"] for link in projected["links"]
    ]
    identity.review_location(
        NS,
        links["nort3456"]["resolution_id"],
        "accept",
        reason="cited point",
        principal_id="r",
        scopes=h.SCOPES,
    )
    accepted = identity.place_links(NS, "nort3456")[0]
    assert (
        accepted["state"] == "accepted"
        and accepted["selected_place_id"] == links["nort3456"]["place_id"]
    )
    with pytest.raises(LinguisticsError):
        identity.project_locations(
            NS,
            geo_namespace="global",
            principal_id="p",
            scopes={"knowledge:linguistics:write", f"namespace:{NS}:write"},
        )


def test_not_ready_before_any_run():
    identity = LinguisticsIdentity(h.connect())
    with pytest.raises(LinguisticsError) as caught:
        identity.propose_lexeme_matches(NS, principal_id="p", scopes=h.SCOPES)
    assert caught.value.code == "not_ready"
