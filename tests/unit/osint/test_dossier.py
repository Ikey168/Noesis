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


def test_unknown_entity_defaults_to_person_and_is_refused(seed):
    _corpus(seed)
    # An entity absent from the corpus with no explicit non-person type is
    # treated as a person (fail-closed): rather than describe an individual from
    # nothing, the guardrail refuses instead of returning an empty brief.
    out = entity_dossier(seed.conn, "Nonexistent Speaker")
    assert out.get("code") == "person_requires_documents"
    assert out["is_person"] is True


def test_person_with_office_role_is_classified_person(seed):
    _corpus(seed)
    # Only role is "president" — under the old subset test this slipped the guard
    # (not a subset of the 5-word person vocabulary). ANY person-denoting role
    # now classifies the entity as a person.
    seed.actors([("d1", "Sam Cole", "person:sc", "president")])
    out = entity_dossier(seed.conn, "Sam Cole")
    assert out["is_person"] is True
    assert out["found"] is True


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


# --- OX02 (#2042): Corporate Ownership composed into the dossier -------------

import pytest  # noqa: E402

from src.kb.entity_history import EntityHistoryStore  # noqa: E402
from src.kb.ownership_identity import _ENTITY_HISTORY_SCOPES, OwnershipIdentityService  # noqa: E402
from src.kb.ownership_store import canonical_entity_id  # noqa: E402
from tests.unit.ownership import harness  # noqa: E402

OWN = {"namespace": harness.NS, "principal_id": harness.PRINCIPAL, "scopes": sorted(harness.SCOPES),
       "as_of": "2020-01-01"}


@pytest.fixture()
def owned():
    env = harness.Env().ready()
    service = OwnershipIdentityService(env.conn, now=env.now)
    result = service.propose(harness.NS, principal_id=harness.PRINCIPAL, scopes=harness.SCOPES)
    for item in result["candidates"]:
        decision = "reject" if harness.DECOY in (item["left_key"], item["right_key"]) else "accept"
        service.review(harness.NS, item["candidate_id"], decision, "fixture review",
                       principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)
    env.conn.executemany(
        "INSERT INTO document_actors (document_id, source_type, actor_name, entity_id, role) VALUES (?,?,?,?,?)",
        [("d9", "news", "Exampla UK", "kg:exampla-uk", "organization"),
         ("d9", "news", "Exampla Holdings", "kg:exampla-hold", "organization"),
         ("d9", "news", "Pat Example", "person:pat", "director")])
    yield env
    env.conn.close()


def _link(env, kg_id, record_key):
    history = EntityHistoryStore(env.conn, now=env.now)
    other = canonical_entity_id(record_key)
    for entity in (kg_id, other):
        history.register_entity(harness.NS, entity, [entity], principal_id=harness.REVIEWER,
                                scopes=_ENTITY_HISTORY_SCOPES)
    return history.decide(harness.NS, "match", [kg_id, other], {"reason": "reviewed LEI link"},
                          reviewer_id=harness.REVIEWER, principal_id=harness.REVIEWER,
                          scopes=_ENTITY_HISTORY_SCOPES)


def test_ownership_feature_off_adds_no_section(owned):
    _link(owned, "kg:exampla-uk", harness.UK_KEYS["gleif"])
    out = entity_dossier(owned.conn, "Exampla UK")
    assert "ownership" not in out
    assert out["found"] is True


def test_ownership_section_is_cited_through_an_accepted_identity_decision(owned):
    decision = _link(owned, "kg:exampla-uk", harness.UK_KEYS["gleif"])
    section = entity_dossier(owned.conn, "Exampla UK", ownership=OWN)["ownership"]
    assert section["status"] == "assembled"
    assert section["resolution"]["resolved_by"][0]["basis"] == "accepted-identity-decision"
    assert section["resolution"]["resolved_by"][0]["via"] == decision["decision_id"]
    lei = [i for line in section["identity"] for i in line["identifiers"] if i["scheme"] == "lei"]
    assert {i["value"] for i in lei} >= {harness.UK}
    [parent] = section["direct_parents"]
    assert parent["holder"] != section["resolution"]["root"] and parent["assertions"]
    for line in parent["assertions"] + [x for s in section["subsidiaries"] for x in s["assertions"]]:
        assert line["citation"]["cited"] is True and line["citation"]["record_id"].startswith("own:")
        assert line["citation"]["provider"]
        assert "validity" in line and "as_of_status" in line
    for line in section["identity"] + section["officers"]:
        assert line["citation"]["cited"] is True
    assert "No beneficial-ownership" in section["notice"]


def test_ownership_never_resolves_by_name(owned):
    # "Exampla UK" exists in the registry by name, but no decision links it.
    section = entity_dossier(owned.conn, "Exampla UK", ownership=OWN)["ownership"]
    assert section["status"] == "not_resolved"
    assert "never matched" in section["note"]


def test_ownership_section_is_never_assembled_for_a_person(owned):
    _link(owned, "person:pat", harness.UK_KEYS["gleif"])
    out = entity_dossier(owned.conn, "Pat Example", ownership=OWN)
    assert out["is_person"] is True
    assert out["ownership"]["status"] == "not_assembled_for_person"
    assert "identity" not in out["ownership"]
    # A person with no document is still refused outright.
    refused = entity_dossier(owned.conn, "Nobody Here", ownership=OWN)
    assert refused["code"] == "person_requires_documents"


def test_ambiguous_identity_surfaces_candidates(owned):
    _link(owned, "kg:exampla-uk", harness.UK_KEYS["gleif"])
    _link(owned, "kg:exampla-uk", harness.INT_KEYS["gleif"])
    section = entity_dossier(owned.conn, "Exampla UK", ownership=OWN)["ownership"]
    assert section["status"] == "needs_selection"
    assert len(section["candidates"]) == 2
    assert "never picks" in section["note"]


def test_reverted_decision_no_longer_resolves(owned):
    decision = _link(owned, "kg:exampla-uk", harness.UK_KEYS["gleif"])
    EntityHistoryStore(owned.conn, now=owned.now).undo(
        harness.NS, decision["decision_id"], reviewer_id=harness.REVIEWER, principal_id=harness.REVIEWER,
        scopes=_ENTITY_HISTORY_SCOPES)
    section = entity_dossier(owned.conn, "Exampla UK", ownership=OWN)["ownership"]
    assert section["status"] == "not_resolved"


def test_ownership_is_inert_when_the_bundle_is_disabled(owned):
    from src.kb import ownership_bundle

    _link(owned, "kg:exampla-uk", harness.UK_KEYS["gleif"])
    ownership_bundle.set_enabled(owned.conn, harness.NS, False, principal_id="operator", scopes={"operator"})
    section = entity_dossier(owned.conn, "Exampla UK", ownership=OWN)["ownership"]
    assert section["status"] == "inert"


def test_ownership_conflicts_are_side_by_side(owned):
    _link(owned, "kg:exampla-uk", harness.UK_KEYS["ch"])
    section = entity_dossier(owned.conn, "Exampla UK", ownership={**OWN, "as_of": "2025-06-01"})["ownership"]
    assert section["status"] == "assembled"
    [conflict] = section["conflicts"]
    assert len(conflict["holders"]) == 2
    assert "not resolved" in conflict["resolution"]
