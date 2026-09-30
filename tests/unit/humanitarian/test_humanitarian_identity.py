"""HR07 (#2260): reviewable place, crisis, organisation and actor identity; nothing auto-merged."""

from __future__ import annotations

import pytest

from src.kb.humanitarian_identity import HumanitarianIdentity
from src.kb.humanitarian_records import HumanitarianError
from src.kb.humanitarian_store import HumanitarianStore
from tests.unit.humanitarian.harness import NS, REVIEWER_SCOPES, SCOPES, world


@pytest.fixture(scope="module")
def proposed():
    value = world()
    identity = HumanitarianIdentity(value.conn)
    result = identity.propose(NS, principal_id="alice", scopes=SCOPES)
    return value, identity, result


def by(identity, subject_key, target_kind=None):
    return [a for a in identity.assertions(NS, scopes=SCOPES, subject_key=subject_key, target_kind=target_kind)]


def test_pcodes_iso3_and_names_resolve_against_geospatial_with_the_boundary_vintage(proposed):
    _, identity, result = proposed
    assert result["proposed"] and all(a["state"] == "proposed" for a in identity.assertions(NS, scopes=SCOPES))
    iso3 = by(identity, "place-ref:iso3:SDN", "geospatial-place")
    assert [a["method"] for a in iso3] == ["exact-iso3"]
    assert iso3[0]["boundary_vintage"] == "COD-AB SDN fixture 2098-03-01" and iso3[0]["confidence"] == 0.95
    khartoum = by(identity, "place-ref:name:admin1:khartoum", "geospatial-place")
    assert [(a["method"], a["target"]["pcode"]) for a in khartoum] == [("normalised-name", "SD01")]
    assert khartoum[0]["subject"]["name"] == "Khartoum state"  # the published name is kept
    assert khartoum[0]["evidence"]["records"]


def test_nothing_is_linked_until_a_different_principal_reviews(proposed):
    world_, identity, _ = proposed
    khartoum = by(identity, "place-ref:name:admin1:khartoum", "geospatial-place")[0]
    assert "place-ref:name:admin1:khartoum" not in identity.accepted_place_links(NS, scopes=SCOPES)
    with pytest.raises(HumanitarianError) as own:
        identity.review(NS, khartoum["assertion_id"], "accept", "p-code area matches", principal_id="alice",
                        scopes=REVIEWER_SCOPES)
    assert own.value.code == "self_review"
    with pytest.raises(HumanitarianError):
        identity.review(NS, khartoum["assertion_id"], "accept", "ok", principal_id="bob", scopes=SCOPES)  # no review scope
    accepted = identity.review(NS, khartoum["assertion_id"], "accept", "admin-1 name matches COD-AB",
                               principal_id="bob", scopes=REVIEWER_SCOPES)
    assert accepted["state"] == "accepted" and accepted["decision_id"] and accepted["reviewer"] == "bob"
    links = identity.accepted_place_links(NS, scopes=SCOPES)["place-ref:name:admin1:khartoum"]
    assert links[0]["boundary_vintage"] == "COD-AB SDN fixture 2098-03-01"
    reverted = identity.revert(NS, khartoum["assertion_id"], "reviewer error", principal_id="bob", scopes=REVIEWER_SCOPES)
    assert reverted["state"] == "reverted"
    assert "place-ref:name:admin1:khartoum" not in identity.accepted_place_links(NS, scopes=SCOPES)
    # The record itself was never rewritten.
    store = HumanitarianStore(world_.conn, initialize=False)
    event = store.revision(NS, "ucdp:conflict_event:990001", scopes=SCOPES)["content"]
    assert {p["name"] for p in event["places"]} >= {"Khartoum state"}
    assert "place_id" not in str(event["places"])


def test_hdx_crisis_tag_and_reliefweb_disaster_link_only_by_reviewable_glide_assertion(proposed):
    _, identity, _ = proposed
    crisis = by(identity, "hdx:dataset:hdx-fixture-0001", "crisis")
    assert [(a["method"], a["target_id"]) for a in crisis] == [("shared-identifier:glide", "reliefweb:crisis:99001")]
    assert crisis[0]["evidence"]["glide"] == "CE-2098-000001-SDN" and crisis[0]["state"] == "proposed"
    rejected = identity.review(NS, crisis[0]["assertion_id"], "reject", "tag names the crisis but dataset predates it",
                               principal_id="bob", scopes=REVIEWER_SCOPES)
    assert rejected["state"] == "rejected"


def test_actors_stay_coder_labels_and_unmatched_items_stay_visible(proposed):
    _, identity, _ = proposed
    unmatched = {u["subject_key"]: u for u in identity.unmatched(NS, scopes=SCOPES)}
    actor = unmatched["actor-ref:ucdp:fixture militia"]
    assert actor["state"] == "unmatched" and actor["published"]["label"] == "Fixture Militia"
    assert "org-ref:reliefweb:fixture coordination office" in unmatched
    # Organisations published by two sources are offered as a pair, never merged.
    pair = by(identity, "org-ref:hdx:fixture coordination office", "organisation-ref")
    assert [a["method"] for a in pair] == ["same-name-across-sources"]


def test_actor_entity_match_is_a_candidate_from_canonical_entities():
    from src.kb.entities import add_manual_alias

    value = world(boundaries=False)
    add_manual_alias(value.conn, "Fixture Armed Forces", "Fixture Armed Forces", "organization")
    identity = HumanitarianIdentity(value.conn)
    identity.propose(NS, principal_id="alice", scopes=SCOPES)
    candidates = by(identity, "actor-ref:ucdp:fixture armed forces", "canonical-entity")
    assert [a["method"] for a in candidates] == ["entity-alias"] and candidates[0]["state"] == "proposed"
