"""Research-entity records linked to literature, funding and ownership by citation and accepted matches (#2618)."""

from __future__ import annotations

from src.kb.research_entities_links import ResearchEntityLinks
from tests.unit import research_entities_harness as h


def by_ref(links, kind):
    return {(link["source_key"], link["reference"]): link for link in links if link["kind"] == kind}


def test_links_record_their_basis_and_point_at_specific_revisions():
    conn = h.accepted_world()
    links = ResearchEntityLinks(conn, initialize=False).links(h.NS, scopes=h.SCOPES)
    assert {link["basis"]["kind"] for link in links} == {"citation", "shared-identifier", "accepted-match"}
    heads = {r["record_key"]: r["revision_id"] for r in
             ResearchEntityLinks(conn, initialize=False).store.records(h.NS, scopes=h.SCOPES)}
    for link in links:
        if not link["source_key"].startswith("research-entities:cordis-participant:"):
            assert link["source_revision_id"] == heads[link["source_key"]]
        if link["status"] == "resolved":
            assert link["target_revision"]
    works = by_ref(links, "researcher-asserted-work")
    paper = works[(f"research-entities:orcid:{h.ADA}", f"doi:{h.PAPER1}")]
    assert paper["target_side"] == "literature" and paper["target_key"] == "doc:exampla-paper-1"
    assert paper["basis"]["label"] == "ORCID-asserted work; not verified authorship"
    dataset = works[(f"research-entities:orcid:{h.ADA}", f"doi:{h.DS1}")]
    assert dataset["target_key"] == f"research-entities:doi:{h.DS1}" and dataset["basis"]["asserted_by"] == \
        "member-client"
    related = by_ref(links, "dataset-related-work")
    assert related[(f"research-entities:doi:{h.DS1}", f"IsSupplementTo:doi:{h.PAPER1}")]["status"] == "resolved"
    assert related[(f"research-entities:doi:{h.DS1}", "Cites:doi:10.99998/exampla.paper.009")]["status"] == \
        "target_missing"
    award = by_ref(links, "dataset-funded-by-project")[(f"research-entities:doi:{h.DS1}", f"award:{h.EXAMPLAR}")]
    assert award["target_key"] == f"research-entities:cordis:HORIZON:{h.EXAMPLAR}"
    funding = by_ref(links, "project-funding-record")
    assert funding[(f"research-entities:cordis:HORIZON:{h.EXAMPLAR}", "funding:HORIZON-CL6-2094-EXAMPLE-01")][
        "target_revision"] == "revision:1"
    assert funding[(f"research-entities:cordis:HORIZON:{h.NORTHWAVE}", "funding:HORIZON-CL5-2092-EXAMPLE-02")][
        "status"] == "target_missing"
    ownership = by_ref(links, "organisation-ownership-entity")
    (entity,) = ownership.values()
    assert entity["target_key"] == "lei:5299EXAMPLAUNIV00001" and entity["basis"]["kind"] == "accepted-match"
    assert entity["basis"]["decision_id"] and entity["basis"]["reviewer"] == "reviewer"


def test_missing_providers_and_targets_are_reported_not_dropped_and_re_resolved_later():
    conn = h.accepted_world(ownership=False, papers=False, funding=False)
    linker = ResearchEntityLinks(conn, initialize=False)
    result = linker.link(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert result["providers"]["literature"] == "absent" and result["providers"]["funding"] == "absent"
    works = by_ref(result["links"], "researcher-asserted-work")
    assert works[(f"research-entities:orcid:{h.ADA}", f"doi:{h.PAPER1}")]["status"] == "provider_absent"
    assert by_ref(result["links"], "project-funding-record")[
        (f"research-entities:cordis:HORIZON:{h.EXAMPLAR}", "funding:HORIZON-CL6-2094-EXAMPLE-01")]["status"] == \
        "provider_absent"
    h.seed_papers(conn)
    h.seed_funding(conn)
    again = linker.link(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert again["outcome"].get("re-resolved", 0) >= 3
    link = by_ref(again["links"], "researcher-asserted-work")[(f"research-entities:orcid:{h.ADA}", f"doi:{h.PAPER1}")]
    assert link["status"] == "resolved" and [e["status"] for e in link["history"]] == ["provider_absent", "resolved"]
    assert linker.link(h.NS, principal_id="alice", scopes=h.SCOPES)["outcome"] == {"unchanged": len(again["links"])}


def test_links_follow_new_revisions_and_researcher_links_need_the_researcher_scope():
    conn = h.accepted_world()
    linker = ResearchEntityLinks(conn, initialize=False)
    h.load_second(conn)
    result = linker.link(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    current = linker.current(h.NS, scopes=h.SCOPES, source_key=f"research-entities:orcid:{h.ADA}",
                             kind="researcher-asserted-work")
    assert {link["reference"] for link in current} == {f"doi:{h.PAPER1}", f"doi:{h.DS1}", f"doi:{h.PAPER2}"}
    older = [link for link in result["links"] if link["source_key"] == f"research-entities:orcid:{h.ADA}"
             and link["kind"] == "researcher-asserted-work" and link not in current]
    assert len(older) == 2  # the links of the earlier revision stay on record
    assert not [link for link in linker.links(h.NS, scopes=h.NO_RESEARCHERS) if "orcid" in link["source_key"]]
    skipped = linker.link(h.NS, principal_id="bob", scopes=h.NO_RESEARCHERS)
    assert skipped["providers"]["researchers"].startswith("skipped")
    kinds = {link["kind"] for link in result["links"]}
    assert not kinds - {"researcher-asserted-work", "researcher-asserted-employment", "dataset-related-work",
                        "dataset-creator-affiliation", "dataset-funded-by-project", "project-funding-record",
                        "organisation-ownership-entity", "participant-ownership-entity", "participant-organisation"}
