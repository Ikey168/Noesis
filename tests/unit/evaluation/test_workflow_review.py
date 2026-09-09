import json
from pathlib import Path

import pytest

from src.evaluation.workflow_review import (
    EvaluationContractError,
    adapt_entity_spans,
    aligned_words,
    constrained_report_proposal,
    e5_inputs,
    github_mcp_profile,
    human_evaluation_status,
    label_studio_export,
    label_studio_import,
    linkage_candidates,
    phoenix_trace_mapping,
    playwright_mcp_profile,
    redacted_artifact,
    rerank_candidates,
    retrieval_benchmark,
    score_ranking,
    support_benchmark,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "workflow_review"


def test_human_eval_status_does_not_claim_missing_labels():
    status = human_evaluation_status("data/argument_mining/human_eval/status.json")
    assert status["status"] == "unavailable"
    assert status["reason"] == "independent_human_labels_not_collected"


def test_ranking_metrics_and_result_count_above_20():
    result = score_ranking(["b", "x", "a"], {"a": 3, "b": 1}, k=30)
    assert result["recall_at_k"] == 1.0
    assert result["mrr_at_k"] == 1.0
    assert 0 < result["ndcg_at_k"] <= 1

    cases = json.loads((FIXTURES / "retrieval_cases.json").read_text())
    report = retrieval_benchmark(cases)
    assert report["cases"][0]["k"] == 30
    assert report["cases"][0]["runs"]["fusion"]["status"] == "partial"
    assert report["human_judgments"] is False


def test_support_harness_is_fail_closed_without_model():
    cases = json.loads((FIXTURES / "support_cases.json").read_text())
    report = support_benchmark(cases)
    assert report["status"] == "unavailable"
    assert report["human_audit_cases"] == 0


def test_label_studio_exchange_preserves_unicode_offsets_and_origin():
    text = "Behörde in Berlin – Müller"
    exported = label_studio_export(
        [
            {
                "task_id": "t1",
                "source_id": "s1",
                "source_revision": "r1",
                "text": text,
                "schema": "entity-span-v1",
            }
        ]
    )
    start = text.index("Müller")
    imported = label_studio_import(
        exported,
        [
            {
                "id": "t1",
                "reviewer_id": "reviewer-a",
                "origin": "independent-human",
                "labels": ["PERSON"],
                "spans": [
                    {"start": start, "end": start + len("Müller"), "text": "Müller"}
                ],
            }
        ],
        {"s1": "r1"},
    )
    assert imported["accepted"][0]["human_vote"] is True
    assert not imported["rejected"]


def test_label_studio_rejects_stale_revision_and_bad_offsets():
    exported = label_studio_export(
        [
            {
                "task_id": "t1",
                "source_id": "s1",
                "source_revision": "r1",
                "text": "Müller",
                "schema": "entity-span-v1",
            }
        ]
    )
    stale = label_studio_import(
        exported, [{"id": "t1", "origin": "model", "spans": []}], {"s1": "r2"}
    )
    assert stale["rejected"][0]["reason"] == "stale_source_revision"
    bad = label_studio_import(
        exported,
        [
            {
                "id": "t1",
                "origin": "assisted-human",
                "spans": [{"start": 0, "end": 1, "text": "X"}],
            }
        ],
        {"s1": "r1"},
    )
    assert bad["rejected"][0]["reason"] == "unicode_offset_mismatch"


def test_mcp_profiles_are_bounded_and_credential_free():
    github = github_mcp_profile()
    assert github["mode"] == "opt-in-read-only"
    assert github["credentials_in_evidence"] is False
    playwright = playwright_mcp_profile(["berlin.de", "europa.eu"])
    assert "bulk-crawl" in playwright["forbidden_actions"]
    with pytest.raises(EvaluationContractError):
        playwright_mcp_profile(["https://berlin.de"])


def test_phoenix_mapping_excludes_private_payload():
    result = phoenix_trace_mapping(
        {
            "project_id": "p",
            "run_id": "r",
            "operation": "answer",
            "status": "timeout",
            "evidence_ids": ["ev-1"],
            "latency_ms": 100,
            "secret": "must-not-export",
        }
    )
    assert result["private_payload_exported"] is False
    assert "secret" not in result


def test_linkage_is_review_only_and_explicit_ids_win():
    result = linkage_candidates(
        {"record_id": "a", "identifiers": {"ror": "01"}},
        [
            {
                "record_id": "b",
                "score": 0.99,
                "identifiers": {"ror": "02"},
                "field_evidence": {"name": 1.0},
            },
            {
                "record_id": "c",
                "score": 0.80,
                "identifiers": {"ror": "01"},
                "field_evidence": {"name": 0.7},
            },
        ],
    )
    assert result["automatic_merge"] is False
    assert result["candidates"][0]["record_id"] == "c"
    assert result["candidates"][1]["eligible_for_review"] is False


def test_entity_adapter_validates_unicode_source_offsets():
    text = "Berliner Behörde Müller"
    start = text.index("Müller")
    result = adapt_entity_spans(
        source_id="doc",
        source_revision="r1",
        text=text,
        model="fixture",
        model_revision="rev",
        language="de",
        entities=[
            {
                "label": "PERSON",
                "start": start,
                "end": len(text),
                "text": "Müller",
                "confidence": 0.9,
            }
        ],
        supported_labels={"PERSON"},
    )
    assert result["entities"][0]["confidence_is_correctness"] is False
    with pytest.raises(EvaluationContractError):
        adapt_entity_spans(
            source_id="d",
            source_revision="r",
            text=text,
            model="m",
            model_revision="r",
            language="de",
            entities=[
                {"label": "PERSON", "start": start, "end": len(text), "text": "Mueller"}
            ],
            supported_labels={"PERSON"},
        )


def test_redaction_is_derived_and_does_not_embed_secret():
    original = {
        "source_id": "s",
        "source_revision": "r",
        "text": "Kontakt Max Müller in Berlin",
    }
    before = dict(original)
    start = original["text"].index("Max Müller")
    artifact = redacted_artifact(
        original,
        [{"start": start, "end": start + len("Max Müller"), "entity_type": "PERSON"}],
        policy_version="p1",
    )
    assert original == before
    assert "Max Müller" not in artifact["text"]
    assert artifact["original_embedded"] is False


def test_e5_contract_prefixes_and_isolates_index_space():
    result = e5_inputs("Berlin law", ["A passage"], model_revision="abc123")
    assert result["query"].startswith("query: ")
    assert result["passages"][0].startswith("passage: ")
    assert result["mixed_embedding_spaces_allowed"] is False


def test_reranking_preserves_ids_provenance_and_stable_ties():
    items = [
        {"id": "a", "text": "x", "source_id": "s1"},
        {"id": "b", "text": "y", "source_id": "s2"},
    ]
    ranked = rerank_candidates("q", items, lambda _q, _text: 0.5)
    assert [item["id"] for item in ranked] == ["a", "b"]
    assert [item["source_id"] for item in ranked] == ["s1", "s2"]


def test_alignment_keeps_original_segments_and_reports_unaligned_words():
    result = aligned_words(
        {
            "media_id": "m",
            "duration_s": 5,
            "segments": [{"start": 0, "end": 5, "text": "Hallo Welt"}],
        },
        [
            {"word": "Hallo", "start_s": 0.1, "end_s": 0.5},
            {"word": "Welt", "start_s": None, "end_s": None},
        ],
        model_revision="align-rev",
    )
    assert result["unaligned_words"] == ["Welt"]
    assert result["transcription_accuracy_inferred"] is False


def test_constrained_proposal_rejects_unauthorized_citations_and_never_autoapproves():
    request = {
        "assertion_id": "a1",
        "base_report_revision": 4,
        "authorized_evidence_revision_ids": ["e1"],
    }
    good = constrained_report_proposal(
        request,
        lambda _: {
            "assertion_id": "a1",
            "replacement_text": "Updated",
            "evidence_revision_ids": ["e1"],
        },
    )
    assert good["status"] == "pending_review" and good["support_verified"] is False
    with pytest.raises(EvaluationContractError):
        constrained_report_proposal(
            request,
            lambda _: {
                "assertion_id": "a1",
                "replacement_text": "Bad",
                "evidence_revision_ids": ["e2"],
            },
        )
