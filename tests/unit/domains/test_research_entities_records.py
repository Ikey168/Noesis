"""Research-entity records: immutable revisions, removals as revisions, write-time minimisation and as-of lookup
(#2589)."""

from __future__ import annotations

import copy

import pytest

from src.kb.research_entities_records import (
    ResearchEntitiesError,
    ResearchEntitiesStore,
    as_of_ms,
    minimisation_violations,
    validate_statement,
)
from tests.unit import research_entities_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection, later=True)
    return connection


def store(conn):
    return ResearchEntitiesStore(conn)


def test_revision_chains_keep_every_version_and_removals_are_revisions(conn):
    s = store(conn)
    m1 = s.find(h.NS, "organisation", h.M1)
    chain = s.revisions(h.NS, m1)
    assert [(r["revision"], r["status"], r["marker"]) for r in chain] == [
        (1, "active", "v9.1-2099-01-15"), (2, "inactive", "v9.2-2099-03-15")]
    assert chain[1]["previous_revision_id"] == chain[0]["revision_id"]
    r2 = s.find(h.NS, "researcher", h.R2)
    assert [r["status"] for r in s.revisions(h.NS, r2)] == ["active", "deactivated"]
    assert s.statement(h.NS, s.revisions(h.NS, r2)[0]["revision_id"])["body"]["works"]  # earlier version kept
    # A declared ID that was never held is reported, not created.
    assert s.find(h.NS, "organisation", h.MISSING) is None
    assert s.find(h.NS, "project", "HORIZON:101999003") is None


def test_each_ror_release_is_a_vintage_even_when_unchanged(conn):
    s = store(conn)
    a2 = s.find(h.NS, "organisation", h.A2)
    assert len(s.revisions(h.NS, a2)) == 1
    assert [(m["release_label"], m["outcome"]) for m in s.memberships(h.NS, a2)] == [
        ("ROR data release v9.1-2099-01-15 (declared; verify)", "revised"),
        ("ROR data release v9.2-2099-03-15 (declared; verify)", "unchanged")]


def test_as_of_selects_the_revision_in_force(conn):
    s = store(conn)
    m1 = s.find(h.NS, "organisation", h.M1)
    before, _ = s.revision_as_of(h.NS, m1, as_of_ms("2099-02-01"))
    after, known = s.revision_as_of(h.NS, m1, as_of_ms("2099-04-01"))
    assert (before["status"], after["status"], len(known)) == ("active", "inactive", 2)
    none, _ = s.revision_as_of(h.NS, m1, as_of_ms("2098-12-31"))
    assert none is None
    citation = s.citation(h.NS, after, cutoff=as_of_ms("2099-04-01"))
    assert citation["provider"] == "ror" and citation["revision_marker"] == "v9.2-2099-03-15"
    assert citation["as_of"].startswith("2099-04-01") and citation["evidence_origin"] == "fixture"
    assert citation["live_verification"] == "unverified-live"


def test_reacquisition_is_idempotent(conn):
    before = conn.execute("SELECT count(*) FROM rentity_revisions").fetchone()[0]
    results = h.apply(conn, "ror", later=True, retrieved_at_ms=h.SECOND_RETRIEVAL + 5)
    assert {r["status"] for r in results} == {"unchanged"}
    assert conn.execute("SELECT count(*) FROM rentity_revisions").fetchone()[0] == before


def test_minimisation_is_enforced_at_write_time():
    researcher = copy.deepcopy(h.fetch("orcid")[0][0]["research_entity"])
    assert minimisation_violations(researcher) == []
    for mutate in (lambda b: b.update(emails=["a@example.invalid"]),
                   lambda b: b["employments"][0].update(role_title="Researcher"),
                   lambda b: b["works"][0].update(contributors=["Someone"]),
                   lambda b: b["works"][0]["identifiers"].append({"type": "scopus-eid", "value": "1"})):
        bad = copy.deepcopy(researcher)
        mutate(bad["body"])
        with pytest.raises(ResearchEntitiesError) as refused:
            validate_statement(bad)
        assert refused.value.code == "minimisation_violation"
    dataset = copy.deepcopy(h.fetch("datacite")[0][0]["research_entity"])
    dataset["body"]["creators"][0]["name"] = "Beispiel, Ada"
    with pytest.raises(ResearchEntitiesError) as named:
        validate_statement(dataset)
    assert named.value.code == "minimisation_violation"
    removal = {**copy.deepcopy(researcher), "status": "deactivated"}
    with pytest.raises(ResearchEntitiesError):
        validate_statement(removal)


def test_metrics_and_rankings_are_refused():
    dataset = copy.deepcopy(h.fetch("datacite")[0][0]["research_entity"])
    dataset["body"]["citation_count"] = 7
    with pytest.raises(ResearchEntitiesError) as refused:
        validate_statement(dataset)
    assert refused.value.code == "forbidden_field"


def test_a_page_with_a_violating_statement_writes_nothing():
    conn = h.connection()
    item = h.source("orcid")
    records = h.fetch("orcid")[0]
    records[0]["research_entity"]["body"]["biography"] = "not allowed"
    from src.kb.research_entities_records import ResearchEntitiesProjector

    with pytest.raises(ResearchEntitiesError):
        ResearchEntitiesProjector(conn).project_page(run_id="r", manifest=None, source=item, records=records,
                                                     documents=None, page_receipt=None, principal_id="svc")
    assert conn.execute("SELECT count(*) FROM rentity_revisions").fetchone()[0] == 0
