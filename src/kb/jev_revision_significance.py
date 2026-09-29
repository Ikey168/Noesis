"""Bounded semantic significance advice over exact retained revision pairs."""

from __future__ import annotations

import json
from typing import Any

from src.ingestion.corrections import classify_change

CONTRACT = "noesis-jev-revision-significance-v1"
TASK = "jev-revision-significance-v1"
MAX_TEXT = 100_000
MAX_PASSAGE = 8_000


class RevisionSignificanceError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class RevisionPairResolver:
    def __init__(self, conn):
        self.conn = conn

    def record(self, revision_id, *, scopes, require_current=False):
        row = self.conn.execute(
            "SELECT document_id,revision,predecessor_revision_id,payload_json,payload_hash,content_hash,change_class,lifecycle "
            "FROM document_revision_records WHERE revision_id=? AND committed_watermark IS NOT NULL",
            [revision_id]).fetchone()
        if not row or "operator" not in scopes and f"document:{row[0]}:read" not in scopes:
            raise RevisionSignificanceError("source_unavailable", "retained document revision is unavailable or unreadable")
        if require_current:
            current = self.conn.execute(
                "SELECT revision_id FROM document_current_revisions WHERE document_id=?", [row[0]]).fetchone()
            if not current or current[0] != revision_id:
                raise RevisionSignificanceError("source_changed", "after revision is no longer current")
        payload = json.loads(row[3])
        content = payload.get("content")
        if payload.get("_payload_reclaimed") or not isinstance(content, str) or not content:
            raise RevisionSignificanceError("source_unavailable", "retained revision text is unavailable")
        if len(content) > MAX_TEXT:
            raise RevisionSignificanceError("input_limit", "revision text exceeds the comparison bound")
        return {"document_id": row[0], "revision": int(row[1]),
                "revision_id": revision_id, "predecessor_revision_id": row[2],
                "payload_hash": row[4], "content_hash": row[5],
                "change_class": row[6], "lifecycle": row[7], "content": content}

    def __call__(self, *, namespace, principal_id, scopes, reference):
        input_id = reference.get("input_id", "")
        if input_id.startswith("document-historical-revision:"):
            record = self.record(input_id.removeprefix("document-historical-revision:"), scopes=scopes)
        elif input_id.startswith("document-current-revision:"):
            record = self.record(input_id.removeprefix("document-current-revision:"), scopes=scopes,
                                 require_current=True)
        else:
            raise RevisionSignificanceError("invalid_sources", "unsupported revision input")
        return {"version": record["revision_id"], "content_hash": record["payload_hash"],
                "content": record["content"]}


def _changed_ranges(before, after):
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < min(len(before) - prefix, len(after) - prefix) and
           before[len(before) - suffix - 1] == after[len(after) - suffix - 1]):
        suffix += 1
    old_end = len(before) - suffix
    new_end = len(after) - suffix
    # Include a little unchanged context; the covered-change metric counts only
    # the changed interval, not this context.
    old_start = min(max(0, prefix - 150), len(before) - 1)
    new_start = min(max(0, prefix - 150), len(after) - 1)
    old_stop = min(len(before), max(old_start + 1, old_end + 150))
    new_stop = min(len(after), max(new_start + 1, new_end + 150))
    return (old_start, old_stop), (new_start, new_stop), (old_end - prefix, new_end - prefix)


def suggest_revision_significance(runtime: Any, resolver: RevisionPairResolver,
                                  namespace: str, before_revision_id: str,
                                  after_revision_id: str, run_id: str, *,
                                  principal_id: str, scopes: set[str], allow_remote=False,
                                  policy=None, max_attempts=1,
                                  max_cost_usd_micros=0, deadline_s=30):
    before = resolver.record(before_revision_id, scopes=scopes)
    after = resolver.record(after_revision_id, scopes=scopes, require_current=True)
    if before["document_id"] != after["document_id"] or after["predecessor_revision_id"] != before_revision_id:
        raise RevisionSignificanceError("invalid_pair", "adjacent ordered revisions of one document required")
    deterministic = classify_change(before["content"], after["content"])
    if before["content"] == after["content"]:
        return {"contract": CONTRACT, "status": "unchanged", "suggested_category": None,
                "semantic_score": None, "classification_authoritative": after["change_class"],
                "document_id": before["document_id"], "before_revision_id": before_revision_id,
                "after_revision_id": after_revision_id, "coverage": {"complete": True, "changed_characters": 0},
                "accepted": False, "decision_run": None}
    old_range, new_range, changed = _changed_ranges(before["content"], after["content"])
    complete = all(end - start <= MAX_PASSAGE for start, end in (old_range, new_range))
    if not complete:
        old_range = (old_range[0], min(old_range[0] + MAX_PASSAGE, old_range[1]))
        new_range = (new_range[0], min(new_range[0] + MAX_PASSAGE, new_range[1]))
    coverage = {"complete": complete, "changed_characters": {"before": changed[0], "after": changed[1]},
                "before": {"start": old_range[0], "end": old_range[1]},
                "after": {"start": new_range[0], "end": new_range[1]},
                "missing": [] if complete else ["changed_passage_beyond_limit"]}
    refs = [
        {"input_id": "document-historical-revision:" + before_revision_id,
         "input_version": before_revision_id, "content_hash": before["payload_hash"]},
        {"input_id": "document-current-revision:" + after_revision_id,
         "input_version": after_revision_id, "content_hash": after["payload_hash"]},
    ]
    questions = {
        "significance": {"kind": "score", "instructions": "How materially did the meaning of a factual claim change in these exact before/after passages?",
                         "criteria": ["Cosmetic wording", "Minor nuance", "Material factual change", "Reversal or retraction"]},
        "claim_category": {"kind": "choice", "instructions": "Choose the semantic change category supported by the supplied passages. Do not override recorded notices or retractions.",
                           "criteria": {"cosmetic": "No meaningful claim change", "factual_update": "A factual assertion changed",
                                        "numeric_change": "A quantity or estimate changed", "claim_reversal": "A factual claim reversed",
                                        "scope_change": "Applicability or qualifying scope changed",
                                        "uncertain": "Passages do not determine the category"}},
    }
    run = runtime.run(namespace, run_id, TASK,
                      state={"document_id": before["document_id"], "before_revision_id": before_revision_id,
                             "after_revision_id": after_revision_id, "coverage": coverage,
                             "deterministic_change_class": after["change_class"],
                             "lifecycle": after["lifecycle"]},
                      questions=questions, source_refs=refs,
                      source_slices=[{"start": old_range[0], "end": old_range[1]},
                                     {"start": new_range[0], "end": new_range[1]}],
                      principal_id=principal_id, scopes=scopes, allow_remote=allow_remote,
                      policy=policy, max_attempts=max_attempts,
                      max_cost_usd_micros=max_cost_usd_micros, deadline_s=deadline_s)
    answers = (run.get("receipt") or {}).get("answers") or {}
    score = answers.get("significance") or {}
    category = answers.get("claim_category") or {}
    value = score.get("value") if score.get("status") == "answered" else None
    chosen = category.get("value") if category.get("status") == "answered" else None
    if type(value) is not int or value not in range(4):
        value = None
    if chosen not in questions["claim_category"]["criteria"]:
        chosen = None
    eligible = complete and run.get("status") == "completed" and run.get("rollout_mode") != "shadow"
    return {"contract": CONTRACT, "status": "suggested" if eligible and value is not None and chosen != "uncertain" else "pending",
            "document_id": before["document_id"], "before_revision_id": before_revision_id,
            "after_revision_id": after_revision_id, "before_payload_hash": before["payload_hash"],
            "after_payload_hash": after["payload_hash"], "source_binding": run.get("source_binding", []),
            "coverage": coverage, "semantic_score": value if eligible else None,
            "suggested_category": chosen if eligible and chosen != "uncertain" else None,
            "classification_authoritative": after["change_class"],
            "lifecycle_authoritative": after["lifecycle"],
            "notice_text_detected": deterministic.notice,
            "retraction_text_detected": deterministic.retraction,
            "accepted": False, "captured_text_rewritten": False,
            "revision_identity_rewritten": False, "decision_run": run}
