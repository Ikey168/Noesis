"""Research-entity links to Scholarly, Funding and Ownership records by citation and accepted matches (#2618)."""

from __future__ import annotations

from src.kb.research_entities_identity import ResearchEntitiesIdentity
from src.kb.research_entities_links import ResearchEntitiesLinks
from src.kb.research_entities_records import ResearchEntitiesStore
from tests.unit import research_entities_harness as h


def build(conn, **kwargs):
    return ResearchEntitiesLinks(conn).build(h.NS, principal_id="analyst", scopes=h.SCOPES, **kwargs)


def test_missing_providers_are_reported_not_dropped():
    conn = h.connection()
    h.load_all(conn)
    result = build(conn)
    assert result["links"] and {v["target_status"] for v in result["links"]} == {"provider_absent"}
    assert result["missing"] == result["links"]
    assert {v["target_kind"] for v in result["links"]} == {"scholarly_work", "funding_record"}


def test_links_record_their_basis_and_point_at_revisions():
    conn = h.connection()
    h.load_all(conn)
    h.seed_science_and_funding(conn)
    own = h.seed_ownership(conn)
    identity = ResearchEntitiesIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    isni = next(m for m in proposed["matches"] if m["right"]["key"] == own["Universitaet Beispielstadt"]
                and m["left"].get("ror_id") == h.A1)
    identity.review(h.NS, isni["match_id"], "accept", "ISNI agrees", principal_id="rev", scopes=h.SCOPES)
    result = build(conn, ownership_namespace=h.OWN_NS)
    store = ResearchEntitiesStore(conn)
    r1 = store.find(h.NS, "researcher", h.R1)
    latest = store.revisions(h.NS, r1)[-1]["revision_id"]
    r1_links = [v for v in result["links"] if v["subject_revision_id"] == latest]
    assert {(v["basis"]["identifier"]["value"], v["target_status"]) for v in r1_links} == {
        ("10.9999/rent.paper1", "resolved"), ("10.9999/rent.paper2", "resolved")}
    link = next(v for v in r1_links if v["basis"]["identifier"]["value"] == "10.9999/rent.paper1")
    assert link["basis"]["method"] == "orcid-asserted-identifier"
    assert link["subject"]["record_id"] == r1 and link["subject"]["revision"] == 1
    assert link["target"] == {"record_id": "doc:rent-paper-1", "revision": "sha-rent-paper-1",
                              "url": "https://doi.org/10.9999/rent.paper1"}
    # R2's asserted work is not in the document store: reported as target_missing.
    r2 = store.find(h.NS, "researcher", h.R2)
    assert {v["target_status"] for v in result["links"] if v["subject_record_id"] == r2} == {"target_missing"}
    d1 = store.find(h.NS, "dataset", "10.9999/rent.data1")
    relations = {v["basis"]["relation_type"] for v in result["links"] if v["subject_record_id"] == d1}
    assert relations == {"IsSupplementTo", "Cites"}  # the URL-typed related identifier is not a paper link
    project = store.find(h.NS, "project", "HORIZON:101999001")
    funding = next(v for v in result["links"] if v["subject_record_id"] == project
                   and v["target_kind"] == "funding_record")
    assert funding["target_status"] == "resolved" and funding["target"]["revision"] == 1
    assert funding["basis"]["method"] == "shared-identifier"
    ownership = [v for v in result["links"] if v["target_kind"] == "ownership_entity"]
    assert len(ownership) == 1 and ownership[0]["basis"]["method"] == "accepted-match"
    assert ownership[0]["target"]["revision"] == 1 and ownership[0]["basis"]["match_id"] == isni["match_id"]
    # Unreviewed candidates (the VAT and name matches) never produce links.
    assert all(v["basis"]["match_id"] == isni["match_id"] for v in ownership)
    again = build(conn, ownership_namespace=h.OWN_NS)
    assert again["created"] == [] and len(again["links"]) == len(result["links"])
    assert not {k for v in result["links"] for k in v["basis"]} & {"collaborator", "coauthor", "influence"}
