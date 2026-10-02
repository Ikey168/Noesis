"""A researcher's ORCID-asserted works and affiliations as of a date (#2624) and an organisation's lineage, projects
and related datasets (#2629)."""

from __future__ import annotations

import copy

import pytest

from src.ingestion.research_entities_sources import RESEARCHER_ALLOWED_FIELDS
from src.kb.research_entities_queries import ASSERTED, ResearchEntityQueries
from src.kb.research_entities_records import (
    ResearchEntityError,
    ResearchEntityStore,
    forbidden_keys,
)
from tests.unit import research_entities_harness as h

EMPLOYMENT_KEYS = {"assertion", "put_code", "organisation_as_asserted", "department", "role", "start_date", "end_date",
                   "covers_as_of_date", "asserted_by", "last_modified", "organisation_record"}
WORK_KEYS = {"assertion", "put_code", "type", "title", "publication_year", "external_ids", "asserted_by",
             "last_modified", "linked_records"}


@pytest.fixture(scope="module")
def world():
    return h.accepted_world(second=True)


def test_a_researcher_answer_uses_the_record_version_in_force_and_labels_every_assertion(world):
    queries = ResearchEntityQueries(world)
    before = queries.researcher(h.NS, h.ADA, scopes=h.SCOPES, as_of="2099-03-01")
    after = queries.researcher(h.NS, h.ADA, scopes=h.SCOPES, as_of="2099-06-01")
    assert before["record_version"]["last_modified"] == "2099-02-14T09:00:00+00:00"
    assert after["record_version"]["last_modified"] == "2099-05-22T09:00:00+00:00"
    assert [w["external_ids"][0]["value"] for w in before["works"]] == [h.PAPER1, h.DS1]
    assert [w["external_ids"][0]["value"] for w in after["works"]][-1] == h.PAPER2
    assert before["citation"]["revision_id"] == before["record_version"]["revision_id"] != \
        after["record_version"]["revision_id"]
    assert {w["assertion"] for w in after["works"]} == {e["assertion"] for e in after["employments"]} == {ASSERTED}
    assert "not verified authorship" in ASSERTED
    exampla = next(e for e in after["employments"] if e["put_code"] == 1101)
    northwind = next(e for e in after["employments"] if e["put_code"] == 1102)
    assert (exampla["covers_as_of_date"], northwind["covers_as_of_date"]) == ("yes", "no")
    assert exampla["organisation_record"]["target_key"] == "research-entities:ror:0zzexa101"
    paper = next(w for w in after["works"] if w["put_code"] == 2204)
    assert paper["linked_records"][0]["target_key"] == "doc:exampla-paper-2"
    assert [v["revision_no"] for v in after["record_versions"]] == [1, 2]


def test_a_researcher_answer_returns_only_minimised_fields_and_needs_the_researcher_scope(world):
    queries = ResearchEntityQueries(world)
    answer = queries.researcher(h.NS, h.ADA, scopes=h.SCOPES, as_of="2099-06-01")
    assert set(answer["name"]) == {"given_names", "family_name", "credit_name"}
    assert all(set(e) == EMPLOYMENT_KEYS for e in answer["employments"])
    assert all(set(w) == WORK_KEYS for w in answer["works"])
    assert {"name", "employments", "works", "withheld_sections"} <= RESEARCHER_ALLOWED_FIELDS
    text = str(answer)
    assert not [p for p in h.PERSONAL if p in text] and not forbidden_keys(answer)
    with pytest.raises(ResearchEntityError) as caught:
        queries.researcher(h.NS, h.ADA, scopes=h.NO_RESEARCHERS)
    assert caught.value.code == "unauthorized"
    limited = queries.researcher(h.NS, h.CY, scopes=h.SCOPES)
    assert limited["name"] is None and limited["name_status"] == "not-public"
    gone = queries.researcher(h.NS, h.BO, scopes=h.SCOPES, as_of="2099-06-10")
    assert gone["record_status"] == "deactivated" and gone["works"] == [] and gone["name"] is None
    assert queries.researcher(h.NS, h.BO, scopes=h.SCOPES, as_of="2098-01-01")["status"] == "not_yet_published"
    assert queries.researcher(h.NS, "0000-0009-9999-0046", scopes=h.SCOPES)["status"] == "none_on_record"
    with pytest.raises(ResearchEntityError):
        queries.researcher(h.NS, "0000-0009-9999-0012", scopes=h.SCOPES)  # checksum


def test_an_organisation_answer_follows_ror_lineage_as_published_per_release(world):
    queries = ResearchEntityQueries(world)
    april = queries.organisation(h.NS, h.NORTHWIND_POLY, scopes=h.SCOPES, as_of="2099-04-01")
    july = queries.organisation(h.NS, h.NORTHWIND_POLY, scopes=h.SCOPES, as_of="2099-07-01")
    assert (april["record_status"], april["release"]["label"], april["lineage"]["successors"]) == (
        "active", "v9.1", [])
    assert july["record_status"] == "withdrawn" and july["release"]["label"] == "v9.2"
    (successor,) = july["lineage"]["successors"]
    assert (successor["ror_id"], successor["status"]) == (h.NORTHWIND_TECH, "active")
    assert successor["citation"]["native_revision"] == "release:v9.2"
    tech = queries.organisation(h.NS, h.NORTHWIND_TECH, scopes=h.SCOPES, as_of="2099-07-01")
    assert tech["lineage"]["predecessors"][0]["ror_id"] == h.NORTHWIND_POLY
    assert queries.organisation(h.NS, h.NORTHWIND_TECH, scopes=h.SCOPES, as_of="2099-04-01")["status"] == \
        "not_yet_published"
    exampla = queries.organisation(h.NS, h.EXAMPLA, scopes=h.SCOPES, as_of="2099-07-01")
    child = next(r for r in exampla["lineage"]["relationships"] if r["type"] == "child")
    assert child["id"] == h.MARINE and child["related_citation"]["native_revision"] == "release:v9.2"
    assert queries.organisation(h.NS, "https://ror.org/0zzzzz999", scopes=h.SCOPES)["status"] == "none_on_record"


def test_projects_are_reached_through_accepted_matches_and_contributions_stay_per_currency(world):
    queries = ResearchEntityQueries(world)
    answer = queries.organisation(h.NS, h.EXAMPLA, scopes=h.SCOPES, as_of="2099-07-01")
    assert [(p["project_id"], p["participant"]["role"]) for p in answer["projects"]] == [
        (h.EXAMPLAR, "coordinator"), (h.NORTHWAVE, "participant")]
    assert answer["projects"][0]["match"]["method"] == "website-domain"
    totals = answer["contribution_totals"]
    assert totals["currencies"] == ["EUR"] and totals["per_currency"]["EUR"]["sum"] == "1450000"
    assert [i["amount"] for i in totals["per_currency"]["EUR"]["inputs"]] == ["1200000", "250000"]
    assert "never converted or summed across currencies" in totals["basis"]
    projects = copy.deepcopy(answer["projects"])
    projects.append({**projects[0], "project_key": "x",
                     "participant": {**projects[0]["participant"],
                                     "ec_contribution": {"amount": "10", "currency": "USD"}}})
    mixed = ResearchEntityQueries._totals(projects)
    assert mixed["currencies"] == ["EUR", "USD"] and mixed["per_currency"]["USD"]["sum"] == "10"
    assert [d["doi"] for d in answer["datasets"]] == [h.DS1]
    assert {v["basis"][:20] for v in answer["datasets"][0]["via"]} == {"the dataset metadata"}
    assert answer["ownership"][0]["target_key"] == "lei:5299EXAMPLAUNIV00001"
    assert answer["asserted_employments"]["assertions"][0]["orcid"] == h.ADA
    hidden = queries.organisation(h.NS, h.EXAMPLA, scopes=h.NO_RESEARCHERS, as_of="2099-07-01")
    assert hidden["asserted_employments"]["withheld"] is True and h.ADA not in str(hidden)


def test_answers_cite_each_record_version_and_unreviewed_participation_is_listed_not_used():
    conn = h.connection()
    h.load_all(conn)
    from src.kb.research_entities_identity import ResearchEntityIdentity

    ResearchEntityIdentity(conn).propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    queries = ResearchEntityQueries(conn)
    answer = queries.organisation(h.NS, h.EXAMPLA, scopes=h.SCOPES)
    assert answer["projects"] == [] and answer["participation_candidates_pending_review"][0]["participant_key"] \
        .endswith(h.PIC_EXAMPLA)
    world = h.accepted_world(second=True)
    queries = ResearchEntityQueries(world)
    answer = queries.organisation(h.NS, h.EXAMPLA, scopes=h.SCOPES, as_of="2099-07-01")
    bundle = queries.evidence_bundle(answer)
    cited = {c for a in bundle["sections"][0]["assertions"] for c in a["citations"]}
    expected = {answer["citation"]["revision_id"]} | {p["citation"]["revision_id"] for p in answer["projects"]} | {
        d["citation"]["revision_id"] for d in answer["datasets"]}
    assert expected <= cited and {b["id"] for b in bundle["bibliography"]} == cited
    assert all("as of" in b["text"] and "observed" in b["text"] for b in bundle["bibliography"])
    paper = queries.datasets_for_paper(h.NS, h.PAPER1, scopes=h.SCOPES)
    assert [d["doi"] for d in paper["datasets"]] == [h.DS1]  # ds.002 is no longer served in the v2 world
    assert queries.datasets_for_paper(h.NS, "10.99998/none", scopes=h.SCOPES)["status"] == "none_on_record"
    researcher = queries.researcher(h.NS, h.ADA, scopes=h.SCOPES)
    assert queries.evidence_bundle(researcher)["bibliography"][0]["id"] == researcher["citation"]["revision_id"]
    store = ResearchEntityStore(world, initialize=False)
    assert store.records(h.NS, scopes=h.SCOPES, record_keys=[f"research-entities:doi:{h.DS2}"])[0]["status"] == \
        "unavailable"
