import copy
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_research_bundle import (
    IntakeResearchBundleStore,
    verify_research_bundle_export,
)
from src.kb.intake_research_progress import (
    ResearchProgressAssessmentStore,
    inspect_research_progress,
)
from src.kb.intake_research_topic import start_research_topic

SCOPES = {
    "knowledge:intake:read", "knowledge:intake:write",
    "knowledge:intake:review",
    "knowledge:projects:read", "knowledge:projects:write",
    "knowledge:recipes:read", "namespace:research:read", "namespace:research:write",
}


def _fixture(conn):
    intake = IntakeStore(conn)
    exploration = IntakeExplorationStore(conn)
    source_session = intake.create("research", "Exploration", "sources",
                                   intent="Gather", principal_id="alice", scopes=SCOPES)
    refs = []
    for index, (url, content) in enumerate((
        ("https://example.org/one", "Alpha supports finding."),
        ("https://example.net/two", "Beta corroborates finding."),
    ), 1):
        captured = exploration.capture(
            "research", source_session["session_id"], f"capture-{index}",
            expected_revision=index, url=url, title=f"Source {index}", content=content,
            saved=True, principal_id="alice", scopes=SCOPES,
        )
        refs.append(captured["references"][-1])
    started = start_research_topic(
        conn, "research", "topic", questions=["Why?"],
        success_criteria=["Explain with independent evidence"],
        scope={"domains": [], "namespaces": ["research"]},
        budget={"requests": 10}, origin=None, references=refs,
        workspace_links=None, principal_id="alice", scopes=SCOPES,
    )
    cards = []
    for index, (ref, content) in enumerate(zip(refs, (
        "Alpha supports finding.", "Beta corroborates finding.",
    )), 1):
        cards.append({
            "id": f"card-{index}",
            "source": {"namespace": "research", "id": ref["id"], "version": ref["version"],
                       "start": 0, "end": len(content)},
            "quote": content, "summary": content,
        })
    document = {
        "cards": cards,
        "claims": [{"id": "claim-1", "statement": "Both sources support the finding",
                    "supports": ["card-1", "card-2"], "contradicts": [], "confidence": "high",
                    "independence_review": {
                        "status": "independent",
                        "basis": "Reviewed the two cited reporting origins; no shared reporting origin identified.",
                        "groups": [
                            {"group_id": "origin-alpha", "card_ids": ["card-1"]},
                            {"group_id": "origin-beta", "card_ids": ["card-2"]},
                        ],
                    }}],
        "concepts": [{"id": "concept-1", "name": "Finding", "explanation": "Supported finding",
                      "card_ids": ["card-1"]}],
        "brief": {"text": "Finding in brief", "card_ids": ["card-1", "card-2"]},
        "mental_model": {"text": "Evidence converges", "card_ids": ["card-1", "card-2"]},
        "map": {"text": "Sources -> claim -> finding", "card_ids": ["card-1", "card-2"]},
        "known": [{"text": "Both texts make the finding", "card_ids": ["card-1", "card-2"]}],
        "uncertain": [{"text": "Generalizability unknown", "card_ids": []}],
        "unresolved": [{"text": "Need a third source", "card_ids": []}],
        "definition_of_done": [{"criterion": "Explain with independent evidence", "met": True,
                                "rationale": "Two separately hosted sources are cited",
                                "card_ids": ["card-1", "card-2"]}],
    }
    return started, document, exploration, source_session


def test_bundle_requires_exact_pinned_spans_and_gates_research_completion(tmp_path):
    conn = duckdb.connect(str(tmp_path / "research.duckdb"))
    started, document, exploration, source_session = _fixture(conn)
    store = IntakeResearchBundleStore(conn)
    project_id = started["project"]["project_id"]
    session_id = started["session"]["session_id"]

    wrong = copy.deepcopy(document)
    wrong["cards"][0]["quote"] = "Fabricated"
    with pytest.raises(IntakeError) as invalid:
        store.save("research", project_id, "bad", wrong, principal_id="alice", scopes=SCOPES)
    assert invalid.value.code == "invalid_citation"

    single_host = copy.deepcopy(document)
    single_host["claims"][0]["supports"] = ["card-1"]
    single_host["claims"][0]["independence_review"]["groups"] = [
        {"group_id": "origin-alpha", "card_ids": ["card-1"]},
    ]
    with pytest.raises(IntakeError) as insufficient:
        store.save("research", project_id, "one-host", single_host,
                   principal_id="alice", scopes=SCOPES)
    assert insufficient.value.code == "insufficient_independence"

    host_count_only = copy.deepcopy(document)
    del host_count_only["claims"][0]["independence_review"]
    host_conn = duckdb.connect(":memory:")
    host_started, _, _, _ = _fixture(host_conn)
    unreviewed_high = IntakeResearchBundleStore(host_conn).save(
        "research", host_started["project"]["project_id"], "host-count-only",
        host_count_only, principal_id="alice", scopes=SCOPES,
    )
    assert not unreviewed_high["checks"]["ready"]
    assert "source_independence_unverified" in unreviewed_high["checks"]["reasons"]
    host_conn.close()

    one_origin = copy.deepcopy(document)
    one_origin["claims"][0]["independence_review"]["groups"] = [
        {"group_id": "origin-alpha", "card_ids": ["card-1", "card-2"]},
    ]
    with pytest.raises(IntakeError) as one_group:
        store.save("research", project_id, "one-origin", one_origin,
                   principal_id="alice", scopes=SCOPES)
    assert one_group.value.code == "insufficient_independence"

    uncited = copy.deepcopy(document)
    uncited["known"][0]["card_ids"] = []
    with pytest.raises(IntakeError) as missing_citation:
        store.save("research", project_id, "uncited-known", uncited,
                   principal_id="alice", scopes=SCOPES)
    assert missing_citation.value.code == "invalid_bundle"

    saved = store.save("research", project_id, "first", document,
                       principal_id="alice", scopes=SCOPES)
    schema = json.loads((Path(__file__).resolve().parents[3] /
                         "contracts/schemas/jsonschema/noesis-intake-research-bundle-v1.json").read_text())
    jsonschema.validate(saved, schema)
    assert not saved["checks"]["ready"]
    assert "source_independence_unverified" in saved["checks"]["reasons"]
    assert saved["checks"]["source_independence"] == [{
        "claim_id": "claim-1", "status": "independent", "group_count": 2,
        "distinct_host_count": 2,
        "basis": "Reviewed the two cited reporting origins; no shared reporting origin identified.",
        "verified": False,
    }]
    unauthorized = set(SCOPES) - {"knowledge:intake:review"}
    with pytest.raises(IntakeError) as denied_review:
        store.review_independence(
            "research", saved["bundle_id"], 1, "review-1", "claim-1",
            "independent", "Checked bylines and syndication lineage.",
            document["claims"][0]["independence_review"]["groups"],
            principal_id="alice", scopes=unauthorized,
        )
    assert denied_review.value.code == "unauthorized"
    review_result = store.review_independence(
        "research", saved["bundle_id"], 1, "review-1", "claim-1",
        "independent", "Checked bylines and syndication lineage.",
        document["claims"][0]["independence_review"]["groups"],
        principal_id="alice", scopes=SCOPES,
    )
    review_schema = json.loads((Path(__file__).resolve().parents[3] /
                                "contracts/schemas/jsonschema/noesis-intake-research-independence-review-v1.json").read_text())
    jsonschema.validate(review_result["review"], review_schema)
    assert review_result["bundle"]["checks"]["ready"]
    assert review_result["bundle"]["checks"]["source_independence"][0]["verified"]
    assert review_result["bundle"]["checks"]["source_independence"][0]["reviewer"] == "alice"
    assert review_result["review"]["provenance"]["source_pins"]
    jsonschema.validate(review_result["bundle"], schema)
    replay = store.review_independence(
        "research", saved["bundle_id"], 1, "review-1", "claim-1",
        "independent", "Checked bylines and syndication lineage.",
        document["claims"][0]["independence_review"]["groups"],
        principal_id="alice", scopes=SCOPES,
    )
    assert replay["idempotent"]
    with pytest.raises(IntakeError) as replay_conflict:
        store.review_independence(
            "research", saved["bundle_id"], 1, "review-1", "claim-1",
            "independent", "Changed review basis.",
            document["claims"][0]["independence_review"]["groups"],
            principal_id="alice", scopes=SCOPES,
        )
    assert replay_conflict.value.code == "idempotency_conflict"
    with pytest.raises(IntakeError) as second_review:
        store.review_independence(
            "research", saved["bundle_id"], 1, "review-2", "claim-1",
            "independent", "A second review command.",
            document["claims"][0]["independence_review"]["groups"],
            principal_id="alice", scopes=SCOPES,
        )
    assert second_review.value.code == "independence_already_reviewed"
    with pytest.raises(IntakeError) as caller_forgery:
        forged = copy.deepcopy(document)
        forged["claims"][0]["independence_review"]["verified"] = True
        store.save("research", project_id, "forged-verified", forged,
                   principal_id="alice", scopes=SCOPES)
    assert caller_forgery.value.code == "invalid_bundle"
    replayed_save = store.save("research", project_id, "first", document,
                               principal_id="alice", scopes=SCOPES)
    assert replayed_save["idempotent"]
    assert replayed_save["checks"]["ready"]
    exported = store.export("research", saved["bundle_id"],
                            principal_id="alice", scopes=SCOPES)
    assert verify_research_bundle_export(exported)["valid"]
    exported["revisions"][0]["document"]["brief"]["text"] = "Tampered"
    assert not verify_research_bundle_export(exported)["valid"]

    intake = IntakeStore(conn, initialize=False)
    with pytest.raises(IntakeError, match="research_bundle"):
        intake.command("research", session_id, "early", expected_revision=1,
                       action="complete", payload=None, principal_id="alice", scopes=SCOPES)
    recorded = intake.command(
        "research", session_id, "link", expected_revision=1, action="record",
        payload={"references": [{"kind": "research_bundle", "id": saved["bundle_id"],
                                  "namespace": "research", "version": 1}]},
        principal_id="alice", scopes=SCOPES,
    )
    assert recorded["revision"] == 2
    exploration.capture(
        "research", source_session["session_id"], "correction", expected_revision=3,
        url="https://example.org/one", title="Corrected", content="Corrected source text",
        saved=True, principal_id="alice", scopes=SCOPES,
    )
    assert not store.inspect("research", saved["bundle_id"],
                             principal_id="alice", scopes=SCOPES)["checks"]["ready"]
    with pytest.raises(IntakeError, match="not ready"):
        intake.command("research", session_id, "finish", expected_revision=2,
                       action="complete", payload=None, principal_id="alice", scopes=SCOPES)
    conn.close()
    with duckdb.connect(str(tmp_path / "research.duckdb")) as reopened:
        historical = IntakeResearchBundleStore(reopened, initialize=False).inspect(
            "research", saved["bundle_id"], revision=1,
            principal_id="alice", scopes=SCOPES,
        )
    assert not historical["checks"]["ready"]  # the cited source has since been corrected
    assert historical["checks"]["source_independence"][0]["verified"]
    assert historical["checks"]["source_independence"][0]["review_id"] == review_result["review"]["review_id"]


def test_bundle_completion_and_owner_access():
    conn = duckdb.connect(":memory:")
    started, document, _, _ = _fixture(conn)
    store = IntakeResearchBundleStore(conn)
    saved = store.save("research", started["project"]["project_id"], "save", document,
                       principal_id="alice", scopes=SCOPES)
    store.review_independence(
        "research", saved["bundle_id"], 1, "review", "claim-1", "independent",
        "Checked two separate reporting origins.",
        document["claims"][0]["independence_review"]["groups"],
        principal_id="alice", scopes=SCOPES,
    )
    with pytest.raises(Exception) as denied:
        store.inspect("research", saved["bundle_id"], principal_id="bob", scopes=SCOPES)
    assert denied.value.code == "unauthorized"
    intake = IntakeStore(conn, initialize=False)
    session_id = started["session"]["session_id"]
    intake.command("research", session_id, "link", expected_revision=1, action="record",
                   payload={"references": [{"kind": "research_bundle", "id": saved["bundle_id"],
                                             "namespace": "research", "version": 1}]},
                   principal_id="alice", scopes=SCOPES)
    completed = intake.command("research", session_id, "finish", expected_revision=2,
                               action="complete", payload=None, principal_id="alice", scopes=SCOPES)
    assert completed["status"] == "completed"


def test_independence_review_is_bound_to_exact_bundle_claim_revision():
    conn = duckdb.connect(":memory:")
    started, document, _, _ = _fixture(conn)
    store = IntakeResearchBundleStore(conn)
    project_id = started["project"]["project_id"]
    saved = store.save("research", project_id, "initial", document,
                       principal_id="alice", scopes=SCOPES)
    reviewed = store.review_independence(
        "research", saved["bundle_id"], 1, "review", "claim-1", "independent",
        "Checked byline and syndication lineage.",
        document["claims"][0]["independence_review"]["groups"],
        principal_id="alice", scopes=SCOPES,
    )
    assert reviewed["bundle"]["checks"]["ready"]

    revised_document = copy.deepcopy(document)
    revised_document["claims"][0]["statement"] = "A revised finding statement"
    revised = store.save("research", project_id, "revised", revised_document,
                         expected_revision=1, principal_id="alice", scopes=SCOPES)
    assert revised["revision"] == 2
    assert not revised["checks"]["ready"]
    assert store.inspect("research", saved["bundle_id"], revision=1,
                         principal_id="alice", scopes=SCOPES)["checks"]["ready"]
    current = store.inspect("research", saved["bundle_id"],
                            principal_id="alice", scopes=SCOPES)
    assert not current["checks"]["ready"]
    assert current["checks"]["source_independence"][0]["verified"] is False


def test_research_progress_assessment_is_durable_and_replays_its_snapshot(tmp_path):
    conn = duckdb.connect(str(tmp_path / "research-assessment.duckdb"))
    started, document, _, _ = _fixture(conn)
    saved = IntakeResearchBundleStore(conn).save(
        "research", started["project"]["project_id"], "save", document,
        principal_id="alice", scopes=SCOPES,
    )
    IntakeResearchBundleStore(conn, initialize=False).review_independence(
        "research", saved["bundle_id"], 1, "review", "claim-1", "independent",
        "Checked separate reporting origins.",
        document["claims"][0]["independence_review"]["groups"],
        principal_id="alice", scopes=SCOPES,
    )
    session_id = started["session"]["session_id"]
    store = ResearchProgressAssessmentStore(conn, now=lambda: 1234)
    assessed = store.assess("research", session_id, "first-review",
                            principal_id="alice", scopes=SCOPES)
    schema = json.loads((Path(__file__).resolve().parents[3] /
                         "contracts/schemas/jsonschema/noesis-intake-research-assessment-v1.json").read_text())
    jsonschema.validate(assessed, schema)
    assert assessed["assessment"]["bundle_ready"]
    assert assessed["assessment"]["definition_of_done"] == [{
        "criterion": "Explain with independent evidence", "status": "met",
        "reviewed": True, "recorded_met": True, "cited_card_count": 2,
    }]
    assert "research_loop_missing" in {item["code"] for item in assessed["assessment"]["blockers"]}
    assert not assessed["assessment"]["ready"]

    IntakeStore(conn, initialize=False).command(
        "research", session_id, "link-bundle", expected_revision=1, action="record",
        payload={"references": [{"kind": "research_bundle", "id": saved["bundle_id"],
                                  "namespace": "research", "version": 1}]},
        principal_id="alice", scopes=SCOPES,
    )
    conn.close()

    with duckdb.connect(str(tmp_path / "research-assessment.duckdb")) as reopened:
        replayed = ResearchProgressAssessmentStore(reopened, initialize=False).assess(
            "research", session_id, "first-review", principal_id="alice", scopes=SCOPES,
        )
        inspected = ResearchProgressAssessmentStore(reopened, initialize=False).inspect(
            "research", assessed["assessment_id"], principal_id="alice", scopes=SCOPES,
        )
    assert replayed["idempotent"]
    assert replayed["input_hash"] == assessed["input_hash"]
    assert replayed["snapshot"]["session"]["revision"] == 1
    assert inspected["assessment_id"] == assessed["assessment_id"]


def test_progress_assessment_joins_failed_recipe_receipts_and_coverage():
    from src.kb.research_loops import ResearchLoopStore

    conn = duckdb.connect(":memory:")
    started, _, _, _ = _fixture(conn)
    project_id = started["project"]["project_id"]
    project = started["project"]
    ResearchLoopStore(conn)
    loop_id = "research-loop:blocked-fixture"
    action = {"gap_namespace": "research", "plan_namespace": "research",
              "domain": "study", "recipe_revision_id": "recipe:blocked-fixture"}
    definition = {"project_id": project_id, "namespace": "research", "owner": "alice",
                  "question_revision": project["question_revision"], "actions": [action],
                  "limits": {"independent_sources_per_domain": 2}}
    state = {"coverage": {"study": ["source-a"]}, "stop_reason": "provider_unavailable",
             "completed_iterations": 0, "results": 0}
    conn.execute("INSERT INTO research_loops VALUES (?,?,?,?,'blocked',false,?,NULL)",
                 [loop_id, "research", project_id, json.dumps(definition), json.dumps(state)])
    conn.execute("INSERT INTO research_loop_actions VALUES (?,0,1,'blocked',NULL)", [loop_id])
    run_id = "recipe-run:blocked-fixture"
    conn.execute(
        "INSERT INTO research_recipe_runs "
        "(run_id,namespace,recipe_revision_id,run_key,input_hash,status,cancel_requested,"
        "state_json,error_json,receipt_json,principal_id,started_at_ms,updated_at_ms) "
        "VALUES (?,?,?,?,?,'blocked',false,'{}',NULL,NULL,?,1,2)",
        [run_id, "research", action["recipe_revision_id"], f"{loop_id}:0", "input", "alice"],
    )
    conn.execute(
        "INSERT INTO research_recipe_checkpoints "
        "(checkpoint_id,run_id,step_id,ordinal,status,attempt,input_hash,output_hash,"
        "output_json,error_json,tool_version,started_at_ms,completed_at_ms) "
        "VALUES ('checkpoint:acquire',?,'acquire',0,'failed',1,'input',NULL,NULL,?,'v1',1,NULL)",
        [run_id, json.dumps({"code": "provider_unavailable"})],
    )
    progress = inspect_research_progress(
        conn, "research", started["session"]["session_id"],
        principal_id="alice", scopes=SCOPES,
    )
    assert progress["loops"][0]["actions"][0]["recipe_run_status"] == "blocked"
    assert progress["loops"][0]["actions"][0]["stages"][0]["error_code"] == "provider_unavailable"

    assessed = ResearchProgressAssessmentStore(conn).assess(
        "research", started["session"]["session_id"], "blocked-review",
        principal_id="alice", scopes=SCOPES,
    )
    blockers = assessed["assessment"]["blockers"]
    assert assessed["assessment"]["stage_receipt_count"] == 3
    assert assessed["assessment"]["completed_stage_receipt_count"] == 0
    assert any(item["code"] == "research_loop_blocked" for item in blockers)
    assert any(item["code"] == "source_coverage_incomplete" for item in blockers)
    assert any(item["code"] == "research_stage_incomplete"
               and item["target"]["error_code"] == "provider_unavailable" for item in blockers)
    assert sum(item["code"] == "research_stage_receipt_missing" for item in blockers) == 2
