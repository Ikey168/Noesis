"""Offline-first contracts for the 2026-09 workflow-review evaluations.

The helpers in this module intentionally separate *contract verification* from
live/model/human evaluation.  Optional dependencies and unavailable labels are
reported explicitly and never converted into synthetic success results.
"""

from __future__ import annotations

import importlib.util
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


class EvaluationContractError(ValueError):
    """Raised when an evaluation fixture violates a bounded public contract."""


def _digest(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def optional_dependency(
    distribution: str, import_name: str | None = None
) -> dict[str, Any]:
    """Return a truthful availability record without importing heavyweight code."""
    module = import_name or distribution.replace("-", "_")
    available = importlib.util.find_spec(module) is not None
    try:
        installed = version(distribution) if available else None
    except PackageNotFoundError:
        installed = None
        available = False
    return {
        "status": "available" if available else "unavailable",
        "distribution": distribution,
        "import_name": module,
        "version": installed,
    }


def human_evaluation_status(path: str | Path) -> dict[str, Any]:
    """Verify the independent-human dataset manifest without inventing labels."""
    source = Path(path)
    if not source.exists():
        return {
            "status": "unavailable",
            "reason": "manifest_missing",
            "path": str(source),
        }
    payload = json.loads(source.read_text(encoding="utf-8"))
    declared = str(payload.get("status", "unknown"))
    records = payload.get("records")
    if declared in {"not_collected", "missing", "unavailable"}:
        return {
            "status": "unavailable",
            "reason": "independent_human_labels_not_collected",
            "declared_status": declared,
            "path": str(source),
        }
    if not isinstance(records, list) or not records:
        return {
            "status": "unavailable",
            "reason": "no_annotation_records",
            "declared_status": declared,
            "path": str(source),
        }
    required = {"id", "source_id", "domain", "language", "text", "annotations", "split"}
    errors: list[str] = []
    splits: dict[str, set[str]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not required <= set(record):
            errors.append(f"record {index} missing required fields")
            continue
        annotations = record["annotations"]
        principals = {
            item.get("annotator_id")
            for item in annotations
            if isinstance(item, dict) and item.get("origin") == "independent-human"
        }
        if len(principals - {None}) < 2:
            errors.append(
                f"record {record['id']} lacks two independent human annotators"
            )
        group = str(record.get("related_group") or record["source_id"])
        splits.setdefault(group, set()).add(str(record["split"]))
    leakage = sorted(group for group, values in splits.items() if len(values) > 1)
    if leakage:
        errors.append("related-document split leakage: " + ", ".join(leakage))
    return {
        "status": "valid" if not errors else "invalid",
        "records": len(records),
        "errors": errors,
        "languages": sorted(
            {str(r.get("language")) for r in records if isinstance(r, dict)}
        ),
        "domains": sorted(
            {str(r.get("domain")) for r in records if isinstance(r, dict)}
        ),
        "frozen_test_records": sum(
            r.get("split") == "test" for r in records if isinstance(r, dict)
        ),
        "manifest_sha256": _digest(payload),
    }


def _dcg(grades: Sequence[int]) -> float:
    return sum(
        (2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(grades)
    )


def score_ranking(
    ranked_ids: Sequence[str], judgments: Mapping[str, int], *, k: int
) -> dict[str, float]:
    """Compute deterministic recall, reciprocal rank and nDCG for fixed qrels."""
    if type(k) is not int or k < 1:
        raise EvaluationContractError("k must be a positive integer")
    if len(set(ranked_ids)) != len(ranked_ids):
        raise EvaluationContractError("ranked result ids must be unique")
    relevant = {key for key, grade in judgments.items() if int(grade) > 0}
    top = list(ranked_ids[:k])
    hits = [item for item in top if item in relevant]
    reciprocal = next(
        (1.0 / (index + 1) for index, item in enumerate(top) if item in relevant), 0.0
    )
    grades = [int(judgments.get(item, 0)) for item in top]
    ideal = sorted((int(v) for v in judgments.values()), reverse=True)[:k]
    denominator = _dcg(ideal)
    return {
        "recall_at_k": len(set(hits)) / len(relevant) if relevant else 1.0,
        "mrr_at_k": reciprocal,
        "ndcg_at_k": _dcg(grades) / denominator if denominator else 1.0,
    }


def retrieval_benchmark(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Score frozen retrieval runs, preserving partial-source diagnostics."""
    if not cases:
        raise EvaluationContractError("at least one retrieval case is required")
    results: list[dict[str, Any]] = []
    for case in cases:
        judgments = case.get("judgments")
        runs = case.get("runs")
        if not isinstance(judgments, dict) or not isinstance(runs, dict):
            raise EvaluationContractError("each case needs judgments and runs")
        k = int(case.get("k", 10))
        run_results = {}
        for name, run in sorted(runs.items()):
            ids = list(run.get("ids", []))
            diagnostics = list(run.get("sources", []))
            status = str(run.get("status", "complete"))
            if status not in {"complete", "partial", "unavailable"}:
                raise EvaluationContractError("invalid retrieval status")
            run_results[name] = {
                **score_ranking(ids, judgments, k=k),
                "returned": len(ids),
                "requested": k,
                "status": status,
                "sources": diagnostics,
                "latency_ms": run.get("latency_ms"),
                "cost": run.get("cost", {"status": "not_metered"}),
            }
        results.append({"id": str(case["id"]), "k": k, "runs": run_results})
    return {
        "contract": "noesis-retrieval-benchmark-v1",
        "status": "measured_fixture_runs",
        "cases": results,
        "human_judgments": all(
            case.get("judgment_origin") == "independent-human" for case in cases
        ),
        "limitations": ["fixture metrics are not live-provider measurements"],
        "input_sha256": _digest(cases),
    }


def support_benchmark(
    cases: Sequence[Mapping[str, Any]],
    scorer: Callable[[str, str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate evidence relevance/support separately from schema compliance.

    With no scorer, the harness reports model evaluation as unavailable while
    retaining the human-labelled cases for later execution.
    """
    labels = {"entailed", "contradicted", "insufficient", "refusal"}
    normalized = []
    for case in cases:
        expected = str(case.get("support_label"))
        if expected not in labels:
            raise EvaluationContractError(f"unsupported support label: {expected}")
        normalized.append(
            {
                "id": str(case["id"]),
                "expected": expected,
                "relevance": case.get("relevance"),
                "origin": case.get("label_origin"),
                "long_span": len(str(case.get("evidence", ""))) > 2000,
            }
        )
    if scorer is None:
        return {
            "contract": "noesis-answer-support-benchmark-v1",
            "status": "unavailable",
            "reason": "support_scorer_not_configured",
            "cases": normalized,
            "human_audit_cases": sum(
                item["origin"] == "independent-human" for item in normalized
            ),
        }
    correct = 0
    outputs = []
    for case in cases:
        result = dict(scorer(str(case["evidence"]), str(case["claim"])))
        predicted = str(result.get("label"))
        correct += predicted == case["support_label"]
        outputs.append(
            {"id": str(case["id"]), "predicted": predicted, "details": result}
        )
    return {
        "contract": "noesis-answer-support-benchmark-v1",
        "status": "measured",
        "accuracy": correct / len(cases) if cases else 0.0,
        "outputs": outputs,
        "schema_compliance_scored_separately": True,
    }


@dataclass(frozen=True)
class ModelCandidate:
    issue: int
    name: str
    distribution: str
    import_name: str
    revision: str
    kind: str
    notes: tuple[str, ...] = ()

    def readiness(self) -> dict[str, Any]:
        dep = optional_dependency(self.distribution, self.import_name)
        return {
            "issue": self.issue,
            "candidate": self.name,
            "revision": self.revision,
            "kind": self.kind,
            "dependency": dep,
            "status": "ready_for_opt_in_run"
            if dep["status"] == "available"
            else "unavailable",
            "notes": list(self.notes),
            "production_default_changed": False,
        }


MODEL_CANDIDATES = (
    ModelCandidate(
        1493,
        "fastino/gliner2-multi-v1",
        "gliner2",
        "gliner2",
        "model-card-revision-required-at-live-run",
        "entity-extraction",
        ("schema-driven adapter; German coverage must be measured",),
    ),
    ModelCandidate(
        1504,
        "intfloat/multilingual-e5-small",
        "sentence-transformers",
        "sentence_transformers",
        "model-card-revision-required-at-live-run",
        "embedding",
        ("query: and passage: prefixes required", "isolated versioned index required"),
    ),
    ModelCandidate(
        1505,
        "BAAI/bge-m3",
        "FlagEmbedding",
        "FlagEmbedding",
        "model-card-revision-required-at-live-run",
        "dense-sparse-multivector",
        ("multi-vector storage is not assumed",),
    ),
    ModelCandidate(
        1506,
        "Qwen/Qwen3-Reranker-0.6B",
        "transformers",
        "transformers",
        "model-card-revision-required-at-live-run",
        "reranker",
        ("dedicated yes/no relevance template required",),
    ),
    ModelCandidate(
        1507,
        "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli",
        "transformers",
        "transformers",
        "model-card-revision-required-at-live-run",
        "nli",
        ("premise/hypothesis and id2label must be verified",),
    ),
    ModelCandidate(
        1508,
        "lightonai/LightOnOCR-2-1B",
        "transformers",
        "transformers",
        "model-card-revision-required-at-live-run",
        "ocr",
        ("page identity retained; bbox image boxes are not text coordinates",),
    ),
)


LIBRARY_CANDIDATES = {
    1492: ("splink", "splink"),
    1494: ("presidio-analyzer", "presidio_analyzer"),
    1499: ("ragas", "ragas"),
    1500: ("arize-phoenix", "phoenix"),
    1509: ("wtpsplit", "wtpsplit"),
    1510: ("lingua-language-detector", "lingua"),
    1511: ("rapidfuzz", "rapidfuzz"),
    1512: ("datasketch", "datasketch"),
    1514: ("whisperx", "whisperx"),
    1520: ("outlines", "outlines"),
}


def candidate_readiness() -> list[dict[str, Any]]:
    rows = [candidate.readiness() for candidate in MODEL_CANDIDATES]
    for issue, (distribution, import_name) in sorted(LIBRARY_CANDIDATES.items()):
        dep = optional_dependency(distribution, import_name)
        rows.append(
            {
                "issue": issue,
                "candidate": distribution,
                "dependency": dep,
                "status": "ready_for_opt_in_run"
                if dep["status"] == "available"
                else "unavailable",
                "production_default_changed": False,
            }
        )
    return sorted(rows, key=lambda row: row["issue"])


def rapidfuzz_scores(query: str, candidates: Sequence[str]) -> dict[str, Any]:
    dep = optional_dependency("rapidfuzz", "rapidfuzz")
    if dep["status"] != "available":
        return {
            "status": "unavailable",
            "reason": "rapidfuzz_not_installed",
            "dependency": dep,
        }
    from rapidfuzz.fuzz import ratio

    return {
        "status": "measured",
        "metric": "rapidfuzz.fuzz.ratio",
        "scores": [
            {"value": value, "score": float(ratio(query, value)) / 100.0}
            for value in candidates
        ],
        "threshold_reuse_forbidden": True,
    }


def linkage_candidates(
    source: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Adapt probabilistic linkage output into reviewable, non-merging evidence."""
    source_id = str(source.get("record_id", ""))
    if not source_id:
        raise EvaluationContractError("source record_id is required")
    explicit = {
        str(k): str(v) for k, v in dict(source.get("identifiers", {})).items() if v
    }
    rows = []
    for candidate in candidates:
        candidate_id = str(candidate.get("record_id", ""))
        score = candidate.get("score")
        if (
            not candidate_id
            or not isinstance(score, (int, float))
            or not 0 <= float(score) <= 1
        ):
            raise EvaluationContractError(
                "candidate id and probability-like score are required"
            )
        identifiers = {
            str(k): str(v)
            for k, v in dict(candidate.get("identifiers", {})).items()
            if v
        }
        conflicts = sorted(
            k
            for k in explicit.keys() & identifiers.keys()
            if explicit[k] != identifiers[k]
        )
        matches = sorted(
            k
            for k in explicit.keys() & identifiers.keys()
            if explicit[k] == identifiers[k]
        )
        rows.append(
            {
                "record_id": candidate_id,
                "score": float(score),
                "field_evidence": dict(candidate.get("field_evidence", {})),
                "explicit_identifier_matches": matches,
                "explicit_identifier_conflicts": conflicts,
                "eligible_for_review": not conflicts,
                "action": "review_candidate",
            }
        )
    rows.sort(
        key=lambda row: (
            -bool(row["explicit_identifier_matches"]),
            -row["score"],
            row["record_id"],
        )
    )
    return {
        "contract": "noesis-linkage-candidates-v1",
        "source_record_id": source_id,
        "candidates": rows,
        "automatic_merge": False,
        "explicit_identifier_precedence": True,
    }


def adapt_entity_spans(
    *,
    source_id: str,
    source_revision: str,
    text: str,
    model: str,
    model_revision: str,
    language: str,
    entities: Sequence[Mapping[str, Any]],
    supported_labels: Iterable[str],
) -> dict[str, Any]:
    """Map optional extractor output to exact source-span/provenance records."""
    allowed = set(supported_labels)
    output, unsupported = [], []
    for entity in entities:
        label = str(entity.get("label", ""))
        start, end = entity.get("start"), entity.get("end")
        if label not in allowed:
            unsupported.append({"label": label, "reason": "unsupported_label"})
            continue
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(text)
        ):
            raise EvaluationContractError("entity offsets are outside source text")
        surface = text[start:end]
        if entity.get("text") is not None and entity.get("text") != surface:
            raise EvaluationContractError(
                "entity text does not match Unicode source offsets"
            )
        output.append(
            {
                "label": label,
                "text": surface,
                "start": start,
                "end": end,
                "confidence": entity.get("confidence"),
                "confidence_is_correctness": False,
            }
        )
    return {
        "contract": "noesis-extractor-spans-v1",
        "source_id": source_id,
        "source_revision": source_revision,
        "language": language,
        "model": model,
        "model_revision": model_revision,
        "entities": output,
        "unsupported": unsupported,
    }


def redacted_artifact(
    original: Mapping[str, Any],
    detections: Sequence[Mapping[str, Any]],
    *,
    policy_version: str,
) -> dict[str, Any]:
    """Create a derived redaction artifact; the caller's original is never mutated."""
    text = str(original.get("text", ""))
    spans = []
    for detection in detections:
        start, end = detection.get("start"), detection.get("end")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(text)
        ):
            raise EvaluationContractError("redaction span is outside source text")
        spans.append((start, end, str(detection.get("entity_type", "UNKNOWN"))))
    spans.sort()
    if any(spans[i][1] > spans[i + 1][0] for i in range(len(spans) - 1)):
        raise EvaluationContractError(
            "overlapping redactions require explicit adjudication"
        )
    rendered, cursor, decisions = [], 0, []
    for start, end, label in spans:
        rendered.extend([text[cursor:start], f"[REDACTED:{label}]"])
        decisions.append(
            {
                "start": start,
                "end": end,
                "entity_type": label,
                "locator_id": _digest(
                    [
                        original.get("source_id"),
                        original.get("source_revision"),
                        start,
                        end,
                        policy_version,
                    ]
                ),
            }
        )
        cursor = end
    rendered.append(text[cursor:])
    return {
        "contract": "noesis-derived-redaction-v1",
        "source_reference": {
            "sha256": _digest(
                [original.get("source_id"), original.get("source_revision")]
            ),
            "resolution": "authorized-artifact-dependencies",
        },
        "policy_version": policy_version,
        "text": "".join(rendered),
        "decisions": decisions,
        "original_embedded": False,
        "derived_only": True,
    }


def e5_inputs(
    query: str, passages: Sequence[str], *, model_revision: str
) -> dict[str, Any]:
    if not model_revision:
        raise EvaluationContractError("immutable model revision is required")
    return {
        "model": "intfloat/multilingual-e5-small",
        "model_revision": model_revision,
        "query": "query: " + query,
        "passages": ["passage: " + value for value in passages],
        "normalize_embeddings": True,
        "index_space_id": _digest(
            ["intfloat/multilingual-e5-small", model_revision, "normalized"]
        ),
        "mixed_embedding_spaces_allowed": False,
    }


def rerank_candidates(
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    scorer: Callable[[str, str], float],
) -> list[dict[str, Any]]:
    """Dedicated reranker adapter preserving IDs/provenance and stable ties."""
    scored = []
    for index, item in enumerate(candidates):
        if not item.get("id") or "text" not in item:
            raise EvaluationContractError("rerank candidates require id and text")
        score = float(scorer(query, str(item["text"])))
        if not math.isfinite(score):
            raise EvaluationContractError("reranker returned a non-finite score")
        scored.append({**item, "relevance_score": score, "input_rank": index})
    return sorted(
        scored, key=lambda item: (-item["relevance_score"], item["input_rank"])
    )


def aligned_words(
    transcript: Mapping[str, Any],
    words: Sequence[Mapping[str, Any]],
    *,
    model_revision: str,
) -> dict[str, Any]:
    """Validate forced-alignment output while retaining original transcript segments."""
    duration = float(transcript.get("duration_s", 0))
    aligned, unaligned = [], []
    for word in words:
        token = str(word.get("word", ""))
        start, end = word.get("start_s"), word.get("end_s")
        if start is None or end is None:
            unaligned.append(token)
            continue
        start, end = float(start), float(end)
        if not 0 <= start <= end <= duration:
            raise EvaluationContractError("word alignment lies outside media duration")
        aligned.append(
            {
                "word": token,
                "start_s": start,
                "end_s": end,
                "speaker": word.get("speaker"),
            }
        )
    return {
        "contract": "noesis-forced-alignment-v1",
        "media_id": transcript.get("media_id"),
        "original_segments": transcript.get("segments", []),
        "aligned_words": aligned,
        "unaligned_words": unaligned,
        "model_revision": model_revision,
        "transcription_accuracy_inferred": False,
    }


def constrained_report_proposal(
    request: Mapping[str, Any],
    generator: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Bound schema-constrained proposals to authorized evidence and pending review."""
    if generator is None:
        return {
            "status": "unavailable",
            "reason": "structured_generator_not_configured",
        }
    allowed = set(map(str, request.get("authorized_evidence_revision_ids", [])))
    proposal = dict(generator(request))
    required = {"assertion_id", "replacement_text", "evidence_revision_ids"}
    if not required <= set(proposal):
        raise EvaluationContractError("proposal schema is incomplete")
    if str(proposal["assertion_id"]) != str(request.get("assertion_id")):
        raise EvaluationContractError("proposal changed assertion identity")
    refs = set(map(str, proposal["evidence_revision_ids"]))
    if not refs <= allowed:
        raise EvaluationContractError("proposal cites unauthorized evidence")
    return {
        "status": "pending_review",
        "base_report_revision": request.get("base_report_revision"),
        "proposal": proposal,
        "schema_valid": True,
        "support_verified": False,
        "auto_approved": False,
    }


def phoenix_trace_mapping(event: Mapping[str, Any]) -> dict[str, Any]:
    """Build a privacy-minimal trace envelope; no Phoenix service is required."""
    required = {"project_id", "run_id", "operation", "status"}
    if not required <= set(event):
        raise EvaluationContractError(
            "trace event missing project/run/operation/status"
        )
    evidence_ids = [str(value) for value in event.get("evidence_ids", [])]
    return {
        "contract": "noesis-phoenix-trace-v1",
        "project_id": str(event["project_id"]),
        "run_id": str(event["run_id"]),
        "operation": str(event["operation"]),
        "status": str(event["status"]),
        "evidence_ids": evidence_ids,
        "private_payload_exported": False,
        "attributes": {k: event[k] for k in ("latency_ms", "error_type") if k in event},
    }


def github_mcp_profile() -> dict[str, Any]:
    return {
        "server": "github/github-mcp-server",
        "mode": "opt-in-read-only",
        "allowed_record_kinds": ["issue", "pull_request", "comment"],
        "required_capture_fields": [
            "repository",
            "record_id",
            "url",
            "updated_at",
            "captured_at",
            "revision",
        ],
        "credentials_in_evidence": False,
        "live_status": "unavailable_until_server_and_credentials_configured",
    }


def playwright_mcp_profile(domains: Sequence[str]) -> dict[str, Any]:
    if (
        not domains
        or len(domains) > 20
        or any(not value or "://" in value for value in domains)
    ):
        raise EvaluationContractError("one to 20 bare allowed domains required")
    return {
        "mode": "opt-in-interactive",
        "allowed_domains": sorted(set(domains)),
        "allowed_actions": ["navigate", "snapshot", "click", "wait", "screenshot"],
        "forbidden_actions": ["download-executable", "credential-entry", "bulk-crawl"],
        "capture": ["url", "observed_at", "dom_text", "screenshot_if_needed"],
        "live_status": "unavailable_until_playwright_mcp_configured",
    }


def label_studio_export(tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Export bounded annotation tasks while preserving source revision identity."""
    if not 1 <= len(tasks) <= 1000:
        raise EvaluationContractError("one to 1000 annotation tasks required")
    output = []
    for task in tasks:
        required = {"task_id", "source_id", "source_revision", "text", "schema"}
        if not required <= set(task):
            raise EvaluationContractError(
                "annotation task lacks stable source/schema fields"
            )
        text = str(task["text"])
        if len(text) > 262_144:
            raise EvaluationContractError("annotation text exceeds 262144 characters")
        output.append(
            {
                "id": str(task["task_id"]),
                "data": {
                    "text": text,
                    "source_id": str(task["source_id"]),
                    "source_revision": str(task["source_revision"]),
                    "schema": task["schema"],
                },
                "meta": {
                    "origin": "noesis",
                    "preannotation_origin": task.get("preannotation_origin"),
                },
            }
        )
    return {
        "contract": "noesis-label-studio-exchange-v1",
        "tasks": output,
        "sha256": _digest(output),
    }


def label_studio_import(
    exported: Mapping[str, Any],
    completed: Sequence[Mapping[str, Any]],
    current_revisions: Mapping[str, str],
) -> dict[str, Any]:
    """Validate imported votes; stale revisions and model labels never become human votes."""
    source_tasks = {str(item["id"]): item for item in exported.get("tasks", [])}
    accepted, rejected = [], []
    for item in completed:
        identity = str(item.get("id"))
        source = source_tasks.get(identity)
        if source is None:
            rejected.append({"id": identity, "reason": "unknown_task"})
            continue
        data = source["data"]
        if current_revisions.get(data["source_id"]) != data["source_revision"]:
            rejected.append({"id": identity, "reason": "stale_source_revision"})
            continue
        origin = str(item.get("origin", "unknown"))
        if origin not in {"independent-human", "model", "assisted-human"}:
            rejected.append({"id": identity, "reason": "unsupported_annotation_origin"})
            continue
        spans = item.get("spans", [])
        invalid = any(
            type(span.get("start")) is not int
            or type(span.get("end")) is not int
            or not 0 <= span["start"] <= span["end"] <= len(data["text"])
            or data["text"][span["start"] : span["end"]] != span.get("text")
            for span in spans
        )
        if invalid:
            rejected.append({"id": identity, "reason": "unicode_offset_mismatch"})
            continue
        accepted.append(
            {
                "id": identity,
                "reviewer_id": str(item.get("reviewer_id", "")),
                "origin": origin,
                "human_vote": origin == "independent-human",
                "labels": item.get("labels", []),
                "spans": spans,
                "effort_ms": item.get("effort_ms"),
            }
        )
    return {"accepted": accepted, "rejected": rejected, "automatic_adjudication": False}
