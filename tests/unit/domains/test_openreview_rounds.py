"""Public OpenReview fixture records; no live API or independent model labels."""

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.domains.research.openreview_rounds import OpenReviewError, OpenReviewRoundStore
from src.ingestion.openreview_api import records
from src.ingestion.document_store import DocumentStore
from src.ingestion.source_pack_runtime import HTTPSPageAdapter, SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.review_inbox import ReviewInboxStore

ROOT = Path(__file__).resolve().parents[3]
SCOPES = {
    "knowledge:openreview:read", "knowledge:openreview:write",
    "knowledge:inbox:read", "knowledge:inbox:write", "knowledge:inbox:review",
    "namespace:research:read", "namespace:research:write",
}


def note(identity, kind, *, round_number=None, replyto=None, content="A public text.", forum="S"):
    path = f"Venue/2026/Conference/Review_Round_{round_number}/-/" if round_number else "Venue/2026/Conference/-/"
    key = {"submission": "abstract", "review": "review", "rebuttal": "response", "decision": "decision"}[kind]
    invitation = {"submission": "Blind_Submission", "review": "Official_Review",
                  "rebuttal": "Author_Response", "decision": "Decision"}[kind]
    return {
        "id": identity, "forum": forum, "replyto": replyto,
        "invitations": [path + invitation], "readers": ["everyone"],
        "signatures": ["Venue/Anonymous_Reviewer1"] if kind == "review" else ["Venue/Authors"],
        "tmdate": 1000, "content": {key: {"value": content}},
    }


def _put(conn, raw_notes, *, observed_at=1000):
    mapped, _ = records({"notes": raw_notes}, cursor=None, limit=100)
    docs = []
    for record in mapped:
        docs.append({
            "document_id": f"spdoc:openreview:{record['id']}", "source_type": "paper",
            "language": "en", "ingested_at": observed_at, "created_at": None,
            "source_id": "openreview-notes", "url": record["url"],
            "title": record["title"], "content": record["content"], "authors": [],
            "metadata": {
                "source_pack_id": "openreview-research",
                "source_pack_native_json": json.dumps(record),
            },
        })
    summary = DocumentStore(conn).upsert(docs)
    assert summary.invalid == 0
    refs = []
    for record in mapped:
        document_id = f"spdoc:openreview:{record['id']}"
        revision_id = conn.execute(
            "SELECT revision_id FROM document_current_revisions WHERE document_id=?",
            [document_id],
        ).fetchone()[0]
        # These unit fixtures seed the committed revision layer directly. The
        # source-pack end-to-end case below exercises the real commit path.
        conn.execute("UPDATE document_revision_records SET committed_watermark=0 WHERE revision_id=?", [revision_id])
        refs.append({"document_id": document_id, "revision_id": revision_id})
    return refs


def _scopes(refs):
    return SCOPES | {f"document:{ref['document_id']}:read" for ref in refs}


def _schema(name, value):
    schema = json.loads((ROOT / "contracts/schemas/jsonschema" / name).read_text())
    Draft202012Validator(schema).validate(value)


def test_round_membership_native_provenance_duplicates_and_access():
    conn = duckdb.connect(":memory:")
    refs = _put(conn, [
        note("S", "submission", content="Manuscript one"),
        note("R1", "review", round_number=1, replyto="S", content="The method is unclear."),
        note("B1", "rebuttal", replyto="R1", content="We will clarify the method."),
        note("R2", "review", round_number=2, replyto="S", content="The data are incomplete."),
    ])
    store = OpenReviewRoundStore(conn, now=lambda: 2000)
    scopes = _scopes(refs)
    saved = store.save("research", "forum", "S", [*refs, refs[1]],
                       principal_id="alice", scopes=scopes)
    assert len(saved["notes"]) == 4
    assert saved["round_ids"] == ["round:1", "round:2"]
    assert saved["ambiguous_membership_count"] == 1  # submission has no round marker
    rebuttal = next(n for n in saved["notes"] if n["note_id"] == "B1")
    assert rebuttal["round_id"] == "round:1" and rebuttal["membership_basis"] == "reply_parent"
    assert next(n for n in saved["notes"] if n["note_id"] == "R1")["signatures"] == ["Venue/Anonymous_Reviewer1"]
    assert saved["coverage"] == "selected_accessible_public_notes_only"
    assert store.save("research", "forum", "S", refs, principal_id="alice", scopes=scopes)["idempotent"]
    _schema("noesis-openreview-rounds-v1.json", saved)
    inspected = store.inspect_round("research", saved["round_set_id"], "round:1",
                                    principal_id="alice", scopes=scopes)
    assert {n["note_id"] for n in inspected["notes"]} == {"R1", "B1"}
    _schema("noesis-openreview-round-v1.json", inspected)
    with pytest.raises(OpenReviewError) as denied:
        store.inspect("research", saved["round_set_id"], principal_id="alice",
                      scopes=scopes - {"document:spdoc:openreview:R2:read"})
    assert denied.value.code == "unauthorized"


def test_concern_states_review_gate_and_round_comparison():
    conn = duckdb.connect(":memory:")
    base = [
        note("S", "submission", content="Manuscript one"),
        note("R1", "review", round_number=1, replyto="S", content="The method is unclear. The data are incomplete."),
        note("B1", "rebuttal", replyto="R1", content="We clarified the method only."),
        note("R2", "review", round_number=2, replyto="S", content="The data remain incomplete."),
    ]
    refs = _put(conn, base)
    scopes = _scopes(refs)
    store = OpenReviewRoundStore(conn, now=lambda: 2000)
    review_ref, response_ref, manuscript_ref = refs[1], refs[2], refs[0]
    concern = {
        "key": "method", "review_note_id": "R1", "review_revision_id": review_ref["revision_id"],
        "quote": "The method is unclear.", "response_note_id": "B1",
        "response_revision_id": response_ref["revision_id"],
        "response_quote": "We clarified the method only.",
        "manuscript_before": manuscript_ref, "manuscript_after": manuscript_ref,
        "correspondence_origin": "author_claimed",
    }
    missing = {
        "key": "data", "review_note_id": "R1", "review_revision_id": review_ref["revision_id"],
        "quote": "The data are incomplete.", "manuscript_before": manuscript_ref,
        "correspondence_origin": "none",
    }
    first = store.save("research", "forum", "S", refs, concerns=[concern, missing],
                       principal_id="alice", scopes=scopes)
    assert {c["status"] for c in first["concerns"]} == {"unresolved", "unassessable"}
    target = store.concern_review_target("research", first["round_set_id"], "method",
                                         principal_id="alice", scopes=scopes)
    assert target["target"]["kind"] == "openreview_concern"
    assert len(target["sources"]) == 3

    edited = list(base)
    edited[0] = note("S", "submission", content="Manuscript two clarifies the method.")
    new_ref = _put(conn, [edited[0]], observed_at=3000)[0]
    revised = dict(concern)
    revised["manuscript_after"] = new_ref
    revised["claimed_scope"] = "partial"
    second = store.save("research", "forum", "S", [*refs, new_ref],
                        concerns=[revised, missing], principal_id="alice", scopes=scopes,
                        round_set_id=first["round_set_id"], expected_revision=1)
    assert second["revision"] == 2
    assert next(c for c in second["concerns"] if c["key"] == "method")["status"] == "author_claimed_partial"
    before = {"revision": 1, "round_id": "round:1"}
    after = {"revision": 2, "round_id": "round:1"}
    compared = store.compare("research", first["round_set_id"], before, after,
                             principal_id="alice", scopes=scopes)
    assert len(compared["concern_changes"]) == 1  # only the method claim changed
    assert compared["manuscript_coverage"] == "available"
    assert len(compared["manuscript_changes"]) == 1
    _schema("noesis-openreview-round-comparison-v1.json", compared)
    exported = store.export("research", first["round_set_id"], before, after,
                            principal_id="alice", scopes=scopes)
    assert len(exported["report_ready"]) == 2
    assert all(row["citations"] for row in exported["report_ready"])
    assert exported["interpretation"] is None

    # A second snapshot with only a round-two edit leaves round one unchanged.
    edited_review = note("R2", "review", round_number=2, replyto="S", content="The data remain incomplete; more tests needed.")
    r2_new = _put(conn, [edited_review], observed_at=4000)[0]
    third = store.save("research", "forum", "S", [*refs[:3], r2_new, new_ref],
                       concerns=[revised, missing], principal_id="alice", scopes=scopes,
                       round_set_id=first["round_set_id"], expected_revision=2)
    unchanged = store.compare("research", first["round_set_id"], after,
                              {"revision": 3, "round_id": "round:1"},
                              principal_id="alice", scopes=scopes)
    assert unchanged["note_changes"] == [] and unchanged["concern_changes"] == []
    assert third["revision"] == 3


def test_review_inbox_requires_attributable_human_resolution():
    conn = duckdb.connect(":memory:")
    notes = [note("S", "submission", content="Original"),
             note("R1", "review", round_number=1, replyto="S", content="No robustness test."),
             note("B1", "rebuttal", replyto="R1", content="We added a robustness test.")]
    refs = _put(conn, notes)
    scopes = _scopes(refs)
    store = OpenReviewRoundStore(conn)
    concern = {"key": "robustness", "review_note_id": "R1", "review_revision_id": refs[1]["revision_id"],
               "quote": "No robustness test.", "response_note_id": "B1",
               "response_revision_id": refs[2]["revision_id"],
               "response_quote": "We added a robustness test.",
               "manuscript_before": refs[0], "manuscript_after": refs[0],
               "correspondence_origin": "author_claimed"}
    saved = store.save("research", "forum", "S", refs, concerns=[concern],
                       principal_id="alice", scopes=scopes)
    target = store.concern_review_target("research", saved["round_set_id"], "robustness",
                                         principal_id="alice", scopes=scopes)
    inbox = ReviewInboxStore(conn)
    task = inbox.create("research", target["target"], sources=target["sources"],
                        domain="openreview", impact=0.5, uncertainty=0.8,
                        rationale="Check whether the claimed response resolves the concern",
                        principal_id="alice", scopes=scopes)
    with pytest.raises(OpenReviewError) as pending:
        store.assess_concern("research", saved["round_set_id"], "robustness",
                             review_task_id=task["task_id"], principal_id="alice", scopes=scopes)
    assert pending.value.code == "review_unavailable"
    inbox.assign("research", task["task_id"], ["bob", "carol"], principal_id="alice", scopes=scopes)
    for reviewer in ("bob", "carol"):
        inbox.submit("research", task["task_id"], task["target_revision_hash"],
                     {"decision": "unresolved"}, "The manuscript is unchanged", 1000,
                     "human", principal_id=reviewer, scopes=scopes)
    inbox.resolve("research", task["task_id"], "Both reviewers found no manuscript change",
                  principal_id="alice", scopes=scopes)
    result = store.assess_concern("research", saved["round_set_id"], "robustness",
                                  review_task_id=task["task_id"], principal_id="alice", scopes=scopes)
    assert result["status"] == "unresolved" and not result["independently_verified"]


def test_acquisition_to_round_export_with_public_fixture():
    conn = duckdb.connect(":memory:")
    pack = validate_source_pack(json.loads((ROOT / "config/source_packs/openreview.json").read_text()))
    source = pack["sources"][0]
    SourcePackStore(conn).install(pack, principal_id="operator", enable=True)
    runtime = SourcePackRuntime(conn)
    runtime.accept_license(pack["pack_id"], source["source_id"], principal_id="operator")
    public_notes = [
        note("S", "submission", content="Manuscript"),
        note("R", "review", round_number=1, replyto="S", content="Missing test."),
        note("B", "rebuttal", replyto="R", content="We will add a test."),
        note("R2", "review", round_number=2, replyto="S", content="Test remains missing."),
    ]
    adapter = HTTPSPageAdapter(
        source,
        transport=lambda **_: {"content": json.dumps({"notes": public_notes})},
        secret="unused",
    )
    run = runtime.run({
        "pack_id": pack["pack_id"], "run_key": "public-fixture",
        "operation": "search", "source_ids": [source["source_id"]],
        "required_sources": [source["source_id"]],
        "parameters": {"forum": "S"}, "max_pages": 2, "max_results": 10,
    }, principal_id="operator", adapters={source["source_id"]: adapter},
        dns_resolver=lambda _: ["8.8.8.8"])
    assert run["status"] == "complete"
    rows = conn.execute(
        "SELECT document_id,revision_id FROM document_current_revisions "
        "WHERE document_id LIKE 'spdoc:%'"
    ).fetchall()
    refs = [{"document_id": doc, "revision_id": revision} for doc, revision in rows]
    assert len(refs) == 4
    store = OpenReviewRoundStore(conn)
    saved = store.save("research", "fixture", "S", refs, principal_id="alice", scopes=_scopes(refs))
    compared = store.export(
        "research", saved["round_set_id"],
        {"revision": 1, "round_id": "round:2"},
        {"revision": 1, "round_id": "round:1"},
        principal_id="alice", scopes=_scopes(refs),
    )
    assert compared["note_changes"]
    assert compared["report_ready"] == []  # no concern correspondence was authored
    _schema("noesis-openreview-rounds-v1.json", saved)
    _schema("noesis-openreview-round-comparison-v1.json", compared)
