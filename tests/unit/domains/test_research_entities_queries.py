"""Researcher assertions and organisation lineage, projects and datasets as of a date (#2624, #2629)."""

from __future__ import annotations

import copy

import pytest

from src.kb.research_entities_identity import ResearchEntitiesIdentity
from src.kb.research_entities_links import ResearchEntitiesLinks
from src.kb.research_entities_queries import ResearchEntitiesQueries
from src.kb.research_entities_records import (
    ResearchEntitiesError,
    ResearchEntitiesStore,
    forbidden_keys,
)
from tests.unit import research_entities_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection, later=True)
    h.seed_science_and_funding(connection)
    identity = ResearchEntitiesIdentity(connection)
    for match in identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)["matches"]:
        identity.review(h.NS, match["match_id"], "accept", "same website and country", principal_id="rev",
                        scopes=h.SCOPES)
    ResearchEntitiesLinks(connection).build(h.NS, principal_id="analyst", scopes=h.SCOPES)
    return connection


def test_researcher_assertions_as_of_a_date_cite_the_record_version(conn):
    q = ResearchEntitiesQueries(conn)
    early = q.researcher(h.NS, h.R1, scopes=h.SCOPES, as_of="2099-03-01")
    late = q.researcher(h.NS, h.R1, scopes=h.SCOPES, as_of="2099-06-01")
    assert early["status"] == late["status"] == "answered"
    assert early["record_revision"]["revision"] == 1 and late["record_revision"]["revision"] == 2
    assert early["record_revision"]["revision_marker"] == "2099-02-01T00:00:00+00:00"
    assert len(early["researcher"]["works"]) == 2 and len(late["researcher"]["works"]) == 3
    assert early["researcher"]["employments"][0]["end"] is None and late["researcher"]["employments"][0]["end"] == \
        "2099-04"
    assert {w["assertion"] for w in late["researcher"]["works"]} == {"orcid-asserted"}
    assert {w["authorship_verified"] for w in late["researcher"]["works"]} == {False}
    assert set(early["researcher"]) == {"orcid", "display_name", "display_name_withheld", "employments", "works"}
    assert {p["doi"] for p in early["linked_papers"]} == {"10.9999/rent.paper1", "10.9999/rent.paper2"}
    assert {d["doi"] for d in early["datasets"]} == {"10.9999/rent.data1", "10.9999/rent.data3"}
    assert all(c["as_of"].startswith("2099-03-01") for c in early["citations"])
    assert forbidden_keys(late) == [] and "no author disambiguation by name" in late["never"]
    before = q.researcher(h.NS, h.R1, scopes=h.SCOPES, as_of="2099-01-01")
    assert before["status"] == "none_on_record"


def test_researcher_answers_need_the_researchers_scope_and_withhold_names_after_deactivation(conn):
    q = ResearchEntitiesQueries(conn)
    with pytest.raises(ResearchEntitiesError) as refused:
        q.researcher(h.NS, h.R1, scopes=h.READ_ONLY)
    assert refused.value.code == "unauthorized"
    earlier = q.researcher(h.NS, h.R2, scopes=h.SCOPES, as_of="2099-03-01")
    assert earlier["status"] == "answered" and earlier["researcher"]["display_name"] is None
    assert earlier["researcher"]["display_name_withheld"] is True
    now = q.researcher(h.NS, h.R2, scopes=h.SCOPES)
    assert now["status"] == "removed" and now["record_status"] == "deactivated" and now["researcher"] is None
    unknown = q.researcher(h.NS, h.R3, scopes=h.SCOPES)
    assert unknown["status"] == "none_on_record" and "names" in unknown["reason"]


def test_organisation_lineage_follows_ror_relationships_per_release(conn):
    q = ResearchEntitiesQueries(conn)
    before = q.organisation(h.NS, h.M1, scopes=h.READ_ONLY, as_of="2099-02-01")
    after = q.organisation(h.NS, h.M1, scopes=h.READ_ONLY, as_of="2099-04-01")
    assert before["organisation"]["status"] == "active" and before["lineage"] == []
    assert after["organisation"]["status"] == "inactive"
    successor = after["lineage"][0]
    assert (successor["type"], successor["to"], successor["to_status"]) == ("successor", h.A1, "held")
    assert successor["stated_in"]["revision_marker"] == "v9.2-2099-03-15"
    assert after["release_vintages"][-1]["release_label"].startswith("ROR data release v9.2")
    uni = q.organisation(h.NS, h.A1, scopes=h.READ_ONLY, as_of="2099-04-01")
    assert {(r["type"], r["id"]) for r in uni["organisation"]["relationships"]} == {("child", h.A2),
                                                                                  ("predecessor", h.M1)}
    assert q.organisation(h.NS, h.MISSING, scopes=h.READ_ONLY)["status"] == "none_on_record"


def test_organisation_projects_datasets_and_contributions_per_currency(conn):
    q = ResearchEntitiesQueries(conn)
    answer = q.organisation(h.NS, h.A1, scopes=h.READ_ONLY)
    assert [p["project"]["project_id"] for p in answer["projects"]] == ["101999001"]
    participation = answer["projects"][0]["participation"][0]
    assert participation["role"] == "coordinator" and participation["identity"]["method"] == "website-domain-country"
    assert answer["projects"][0]["funding_links"][0]["target_status"] == "resolved"
    assert answer["contributions_by_currency"]["ec_contribution"] == {"EUR": "2050000"}
    assert {d["doi"] for d in answer["datasets"]} == {"10.9999/rent.data1", "10.9999/rent.data3"}
    assert answer["identity"]["status"] == "matched"
    # A second project in another currency is grouped apart, never summed or converted.
    store = ResearchEntitiesStore(conn)
    statement = copy.deepcopy(store.statement(h.NS, store.revisions(
        h.NS, store.find(h.NS, "project", "HORIZON:101999001"))[-1]["revision_id"]))
    statement["native_id"], statement["body"]["project_id"] = "HORIZON:101999099", "101999099"
    for participant in statement["body"]["participants"]:
        for field in ("ec_contribution", "net_ec_contribution", "total_cost"):
            participant[field] = {**participant[field], "currency": "CHF"}
    header = {"provider": "cordis", "format": "cordis-csv-zip", "document": {"label": "synthetic CHF file"},
              "label": "synthetic", "file_sha256": "f" * 64, "item_count": 1, "evidence_origin": "fixture"}
    store.apply_release(h.NS, header, [statement], run_id="chf", source_id="test")
    mixed = q.organisation(h.NS, h.A1, scopes=h.READ_ONLY)
    assert mixed["contributions_by_currency"]["ec_contribution"] == {"EUR": "2050000", "CHF": "2050000"}
    assert "never summed" in mixed["contribution_note"]


def test_datasets_for_a_paper_history_and_evidence_bundle(conn):
    q = ResearchEntitiesQueries(conn)
    related = q.datasets_for_paper(h.NS, "https://doi.org/10.9999/rent.paper1", scopes=h.READ_ONLY)
    assert {(d["doi"], tuple(d["basis"]["relation_types"])) for d in related["datasets"]} == {
        ("10.9999/rent.data1", ("IsSupplementTo",)), ("10.9999/rent.data3", ("IsReferencedBy",))}
    assert q.datasets_for_paper(h.NS, "10.9999/none", scopes=h.READ_ONLY)["status"] == "none_on_record"
    history = q.record_history(h.NS, "dataset", "10.9999/rent.data1", scopes=h.READ_ONLY)
    assert [r["record"]["metadata_version"] for r in history["revisions"]] == [1, 2]
    with pytest.raises(ResearchEntitiesError):
        q.record_history(h.NS, "researcher", h.R1, scopes=h.READ_ONLY)
    bundle = q.export_bundle(q.organisation(h.NS, h.A1, scopes=h.READ_ONLY, as_of="2099-04-01"), created_at_ms=1)
    evidence = [o for o in bundle["objects"] if o["type"] == "evidence"]
    assert evidence and all({"source", "revision", "as_of"} <= set(o["payload"]) for o in evidence)
    assert all(o["payload"]["as_of"].startswith("2099-04-01") for o in evidence)
    researcher = q.export_bundle(q.researcher(h.NS, h.R2, scopes=h.SCOPES, as_of="2099-03-01"), created_at_ms=1)
    assert researcher["completeness"]["status"] == "partial"  # R2's asserted work is not held
