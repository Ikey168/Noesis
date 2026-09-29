"""Generated scholarly fixtures; no live provider or human review is implied."""

import json

import duckdb
import pytest

from src.domains.research.paper_families import PaperFamilyError, PaperFamilyStore
from src.ingestion.revisions import DocumentRevisionStore


OWNER = {"operator"}
LINK = {"source_identifier": "10.1234/pre", "source_identifier_type": "DOI",
        "predicate": "IsPreprintOf", "target_identifier": "10.1234/pub",
        "target_identifier_type": "DOI", "provider": "datacite"}


def setup():
    conn = duckdb.connect()
    revisions = DocumentRevisionStore(conn)
    refs = {}
    for document_id, title, doi, stage, links in [
        ("pre", "Paper preprint", "10.1234/pre", "plain-text-abstract", [LINK]),
        ("pub", "Published paper", "10.1234/pub", "full-text", []),
        ("other", "Similar title", "10.1234/pub", "plain-text-abstract", []),
        ("fourth", "Different paper", "10.1234/fourth", "plain-text-abstract", []),
    ]:
        record = revisions.observe({"document_id": document_id, "title": title,
                                    "content": "Captured " + title, "authors": ["A. Researcher"],
                                    "metadata": {"content_representation": stage,
                                                 "related_resources_json": json.dumps(links)}})
        refs[document_id] = record["revision_id"]
    store = PaperFamilyStore(conn)
    root = {"document_id": "pre", "revision_id": refs["pre"], "stage": "preprint",
            "identifiers": [{"kind": "doi", "value": "10.1234/pre"},
                            {"kind": "arxiv", "value": "2501.00001v2"}]}
    family = store.create("research", "contribution-one", root,
                          principal_id="alice", scopes=OWNER)
    return conn, revisions, store, refs, family


def member(document_id, revision_id, doi, stage="version-of-record"):
    return {"document_id": document_id, "revision_id": revision_id, "stage": stage,
            "identifiers": [{"kind": "doi", "value": doi}]}


def add_publication(store, refs, family):
    return store.add_member("research", family["family_id"], "publication", member("pub", refs["pub"], "10.1234/pub"),
                            source_member_id=family["members"][0]["member_id"], relation_type="is-preprint-of",
                            provenance={"kind": "provider", "relation": LINK}, expected_revision=family["revision"],
                            principal_id="alice", scopes=OWNER)


def test_provider_link_versions_bibliography_and_immutable_selection():
    conn, _, store, refs, family = setup()
    linked = add_publication(store, refs, family)
    assert linked["members"][1]["status"] == "active"
    assert linked["relations"][0]["status"] == "accepted_provider"
    assert [m["availability"] for m in linked["members"]] == ["abstract-only", "full-text"]
    assert store.add_member("research", family["family_id"], "publication",
                            member("pub", refs["pub"], "10.1234/pub"),
                            source_member_id=family["members"][0]["member_id"], relation_type="is-preprint-of",
                            provenance={"kind": "provider", "relation": LINK}, expected_revision=1,
                            principal_id="alice", scopes=OWNER)["idempotent"]
    selected = store.select_citation("research", family["family_id"], "cite-pub", linked["members"][1]["member_id"],
                                     expected_revision=linked["revision"], principal_id="alice", scopes=OWNER,
                                     locator={"page": 2})
    original = selected["selections"][0]
    bibliography = store.export("research", family["family_id"], principal_id="alice", scopes=OWNER)
    assert bibliography["bibliography"][0]["source"]["revision_id"] == refs["pub"]
    assert bibliography["bibliography"][0]["locator"] == {"page": 2}
    changed = store.remove_member("research", family["family_id"], "split", linked["members"][1]["member_id"],
                                  "Publisher confirms distinct work", expected_revision=selected["revision"],
                                  principal_id="alice", scopes=OWNER)
    assert changed["relations"][0]["status"] == "superseded"
    assert changed["selections"][0] == original
    assert store.inspect("research", family["family_id"], principal_id="alice", scopes=OWNER, revision=2)["revision"] == 2
    with pytest.raises(PaperFamilyError) as error:
        store.select_citation("research", family["family_id"], "cite-again", linked["members"][1]["member_id"],
                              expected_revision=changed["revision"], principal_id="alice", scopes=OWNER)
    assert error.value.code == "member_unavailable"
    conn.close()


def test_collisions_candidates_and_independent_review():
    conn, _, store, refs, family = setup()
    linked = add_publication(store, refs, family)
    candidate = store.add_member("research", family["family_id"], "collision",
                                 member("other", refs["other"], "10.1234/pub"),
                                 source_member_id=family["members"][0]["member_id"], relation_type="related",
                                 provenance={"kind": "inference", "reason": "similar title"},
                                 expected_revision=linked["revision"], principal_id="alice", scopes=OWNER)
    assert candidate["members"][2]["status"] == "candidate"
    assert candidate["relations"][1]["collision"]
    review_scopes = {"knowledge:paper-family:review", "namespace:research:write",
                     "document:pre:read", "document:pub:read", "document:other:read"}
    reviewed = store.review_relation("research", family["family_id"], "reject-collision",
                                     candidate["relations"][1]["relation_id"], "reject", "Identifier collision",
                                     expected_revision=candidate["revision"], principal_id="reviewer", scopes=review_scopes)
    assert reviewed["members"][2]["status"] == "rejected"
    with pytest.raises(PaperFamilyError) as error:
        store.add_member("research", family["family_id"], "bad-provider",
                         member("fourth", refs["fourth"], "10.1234/fourth"),
                         source_member_id=family["members"][0]["member_id"], relation_type="related",
                         provenance={"kind": "provider", "relation": LINK}, expected_revision=reviewed["revision"],
                         principal_id="alice", scopes=OWNER)
    assert error.value.code == "invalid_provenance"
    conn.close()


def test_member_targeted_notices_and_access_loss():
    conn, revisions, store, refs, family = setup()
    linked = add_publication(store, refs, family)
    conn.execute("CREATE TABLE crossref_notices(notice_id TEXT,document_id TEXT,notice_document_id TEXT,notice_json TEXT,observed_at_ms BIGINT)")
    for notice_id, target, kind, doc in [
        ("corrected-pub", "10.1234/pub", "correction", "pub"),
        ("withdrawn-pre", "10.1234/pre", "withdrawal", "pre"),
        ("ambiguous", "10.1234/unknown", "retraction", None),
    ]:
        record = {"notice_id": notice_id, "notice_type": kind, "target_doi": target,
                  "notice_doi": "10.1234/notice", "status": "supported", "snapshot": {"digest": notice_id},
                  "record_id": notice_id}
        conn.execute("INSERT INTO crossref_notices VALUES (?,?,?,?,?)",
                     [notice_id, doc, "notice:" + notice_id, json.dumps(record), 1])
    state = linked
    for notice_id in ("corrected-pub", "withdrawn-pre", "ambiguous"):
        state = store.attach_notice("research", family["family_id"], notice_id, notice_id,
                                    expected_revision=state["revision"], principal_id="alice", scopes=OWNER)
    lifecycle = store.export("research", family["family_id"], principal_id="alice", scopes=OWNER)
    assert lifecycle["member_lifecycle"][linked["members"][0]["member_id"]] == ["withdrawal"]
    assert lifecycle["member_lifecycle"][linked["members"][1]["member_id"]] == ["correction"]
    assert lifecycle["unresolved_notices"][0]["notice_id"] == "ambiguous"
    assert store.compare("research", family["family_id"], [m["member_id"] for m in linked["members"]],
                         principal_id="alice", scopes=OWNER)["revision"] == state["revision"]
    with pytest.raises(PaperFamilyError) as error:
        store.inspect("research", family["family_id"], principal_id="alice",
                      scopes={"knowledge:paper-family:read", "namespace:research:read"})
    assert error.value.code == "unauthorized"
    conn.close()


def test_corrected_relationship_retains_prior_revision():
    conn, _, store, refs, family = setup()
    linked = add_publication(store, refs, family)
    old_relation = linked["relations"][0]
    corrected = store.correct_relation(
        "research", family["family_id"], "relation-correction", old_relation["relation_id"],
        "related", {"kind": "inference", "reason": "provider relation disputed"},
        "Publisher correction changed relationship", expected_revision=linked["revision"],
        principal_id="alice", scopes=OWNER,
    )
    assert corrected["relations"][0]["status"] == "superseded"
    assert corrected["relations"][1]["status"] == "candidate"
    assert corrected["relations"][1]["supersedes_relation_id"] == old_relation["relation_id"]
    historical = store.inspect("research", family["family_id"], revision=linked["revision"],
                               principal_id="alice", scopes=OWNER)
    assert historical["relations"][0]["status"] == "accepted_provider"
    conn.close()
