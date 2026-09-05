import copy

import duckdb
import pytest

from src.kb.systematic_reviews import SystematicReviewStore, ReviewError

SCOPES = {"knowledge:reviews:read", "knowledge:reviews:write", "namespace:r:write", "document:paper1:read"}
OWNER = {"principal_id": "coordinator", "scopes": SCOPES}
PROTOCOL = {"question": "Does intervention help?", "inclusion": ["Controlled studies"], "exclusion": ["Editorials"],
    "databases": ["PubMed"], "search_expressions": ["intervention AND controlled"], "date_from": "2020-01-01",
    "date_to": "2026-09-05", "reviewers": ["alice", "bob"], "fields": ["population", "outcome"]}


def setup():
    store = SystematicReviewStore(duckdb.connect())
    protocol = store.create("r", "p", PROTOCOL, **OWNER)
    return store, protocol["protocol_id"]


def candidate(store, pid, publication_id="paper1", **kwargs):
    return store.add_candidate("r", pid, 1, publication_id=publication_id, source_revision="source-v1",
        source_namespace="r", search_run_id="search-run-1", study_id="study-1", title="Controlled study",
        abstract="Participants received an intervention", **{"full_text_available": True, **kwargs}, **OWNER)


def screen(store, cid, who, decision, stage="title_abstract", revision=0):
    return store.screen("r", cid, stage, revision, decision, "Reviewed eligibility criteria", principal_id=who, scopes=SCOPES)


def test_protocol_amendments_and_related_publications_keep_identity():
    store, pid = setup()
    first = candidate(store, pid)
    second = candidate(store, pid, "paper2")
    assert first["candidate_id"] != second["candidate_id"]
    assert candidate(store, pid)["candidate_id"] == first["candidate_id"]
    amended = copy.deepcopy(PROTOCOL)
    amended["inclusion"].append("Adults only")
    store.amend("r", pid, 1, amended, "Clarify population", **OWNER)
    exported = store.export("r", pid, **OWNER)
    assert exported["candidate_count"] == exported["publication_count"] == 2
    assert exported["study_count"] == 1 and len(exported["amendments"]) == 2
    assert all(row["protocol_revision"] == 1 and row["search_run_id"] == "search-run-1" for row in exported["candidates"])
    assert sum(exported["counts"].values()) == 2
    assert "candidate_id,title,abstract" in exported["asreview_unlabeled_csv"]
    assert "included" not in exported["asreview_unlabeled_csv"]


def test_independent_screening_conflict_adjudication_and_stale_review():
    store, pid = setup()
    cid = candidate(store, pid)["candidate_id"]
    screen(store, cid, "alice", "include")
    assert store.export("r", pid, **OWNER)["candidates"][0]["status"] == "pending"
    screen(store, cid, "bob", "exclude")
    before = store.export("r", pid, **OWNER)["candidates"][0]["screening"]["title_abstract"]
    assert before["status"] == "disputed"
    screen(store, cid, "bob", "pending", revision=1)
    with pytest.raises(ReviewError, match="changed"):
        store.adjudicate("r", cid, "title_abstract", before["screening_hash"], "include", "Consensus review", **OWNER)
    current = store.export("r", pid, **OWNER)["candidates"][0]["screening"]["title_abstract"]
    store.adjudicate("r", cid, "title_abstract", current["screening_hash"], "include", "Consensus review", **OWNER)
    screen(store, cid, "alice", "include", stage="full_text")
    screen(store, cid, "bob", "include", stage="full_text")
    assert store.export("r", pid, **OWNER)["counts"] == {"include": 1}
    with pytest.raises(ReviewError, match="changed"):
        screen(store, cid, "alice", "exclude", stage="full_text")


def test_missing_full_text_remains_unavailable_and_access_revocation_applies():
    store, pid = setup()
    cid = candidate(store, pid, full_text_available=False)["candidate_id"]
    for reviewer in ("alice", "bob"):
        screen(store, cid, reviewer, "include")
    with pytest.raises(ReviewError, match="missing full text"):
        screen(store, cid, "alice", "exclude", stage="full_text")
    assert store.export("r", pid, **OWNER)["counts"] == {"full_text_unavailable": 1}
    amended = {**PROTOCOL, "reviewers": ["bob", "carol"]}
    store.amend("r", pid, 1, amended, "Reviewer reassignment", **OWNER)
    with pytest.raises(ReviewError, match="participation"):
        screen(store, cid, "alice", "pending", stage="full_text")
    with pytest.raises(ReviewError):
        store.export("r", pid, principal_id="bob", scopes=SCOPES)


def test_export_bounds_and_invalid_protocol_are_explicit():
    store, pid = setup()
    candidate(store, pid)
    candidate(store, pid, "paper2")
    with pytest.raises(ReviewError, match="budget"):
        store.export("r", pid, limit=1, **OWNER)
    with pytest.raises(ReviewError, match="distinct"):
        store.create("r", "bad", {**PROTOCOL, "reviewers": ["alice", "alice"]}, **OWNER)


def test_evidence_fields_pin_source_spans_and_keep_review_state():
    from src.ingestion.document_store import DocumentStore
    store, pid = setup()
    text = "Participants were adults. The outcome improved."
    DocumentStore(store.conn).upsert([{"document_id": "paper1", "source_type": "paper", "language": "en", "ingested_at": 1, "content": text}])
    revision = store.conn.execute("SELECT revision_id FROM document_revision_records WHERE document_id='paper1'").fetchone()[0]
    item = store.add_candidate("r", pid, 1, publication_id="paper1", source_revision=revision, source_namespace="r",
        search_run_id="search1", study_id="study1", title="Controlled study", abstract="Abstract", full_text_available=True, **OWNER)
    field = store.extract_field("r", item["candidate_id"], "population", "Adults", 0, 25, principal_id="alice", scopes=SCOPES)
    assert field["quote"] == text[:25] and field["locator"]["revision_id"] == revision
    with pytest.raises(ReviewError, match="explicit document"):
        store.extract_field("r", item["candidate_id"], "population", "Adults", 0, 25,
            principal_id="alice", scopes=SCOPES - {"document:paper1:read"})
    with pytest.raises(ReviewError, match="another"):
        store.review_field("r", field["field_id"], 0, "accepted", "Checked", principal_id="alice", scopes=SCOPES)
    store.review_field("r", field["field_id"], 0, "accepted", "Verified against the quoted source", principal_id="bob", scopes=SCOPES)
    exported = store.export("r", pid, **OWNER)
    assert exported["candidates"][0]["fields"][0]["review_state"] == "accepted"
    assert "16a" in exported["prisma_reporting_map"]
    with pytest.raises(ReviewError, match="protocol"):
        store.extract_field("r", item["candidate_id"], "unplanned", "Value", 0, 10, **OWNER)
