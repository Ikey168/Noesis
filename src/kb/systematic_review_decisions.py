"""Source-bound machine screening suggestions beside independent human votes."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from src.kb.research_projects import _hash, _json
from src.kb.systematic_reviews import ReviewError, SystematicReviewStore

TASKS = {"title_abstract": "review-title-abstract-screen-v1",
         "full_text": "review-full-text-screen-v1"}
CONTRACT = "noesis-review-screening-suggestion-v1"
MAX_CRITERIA = 20
MAX_TEXT = 65_536
WINDOW = 2_048
_DDL = """
CREATE TABLE IF NOT EXISTS systematic_review_machine_suggestions(
 namespace TEXT NOT NULL,principal_id TEXT NOT NULL,candidate_id TEXT NOT NULL,
 stage TEXT NOT NULL,run_id TEXT NOT NULL,request_hash TEXT NOT NULL,result_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL,PRIMARY KEY(namespace,principal_id,candidate_id,stage,run_id));
"""


def _criteria(protocol):
    inclusion = protocol["content"]["inclusion"]
    exclusion = protocol["content"]["exclusion"]
    if len(inclusion) + len(exclusion) > MAX_CRITERIA:
        raise ReviewError("criteria_limit", "machine screening supports at most 20 protocol criteria")
    return ([{"code": f"I{i:02d}", "type": "inclusion", "text": value} for i, value in enumerate(inclusion, 1)]
            + [{"code": f"E{i:02d}", "type": "exclusion", "text": value} for i, value in enumerate(exclusion, 1)])


def _evidence(stage, candidate, text):
    if stage == "title_abstract":
        spans = []
        missing = []
        for field in ("title", "abstract"):
            value = candidate[field]
            if not value.strip():
                missing.append(field)
                continue
            start = text.find(value)
            if start < 0:
                missing.append(f"{field}_not_found_in_source_revision")
                continue
            end = start + len(value)
            spans.append(
                {
                    "id": field,
                    "locator": {
                        "kind": "document_revision",
                        "document_id": candidate["publication_id"],
                        "revision_id": candidate["source_revision"],
                        "start": start,
                        "end": end,
                    },
                    "text": value,
                }
            )
        total = len(candidate["title"]) + len(candidate["abstract"])
        if total > MAX_TEXT:
            missing.append("title_abstract_over_limit")
        source_slice = None
        if spans:
            start = min(span["locator"]["start"] for span in spans)
            end = max(span["locator"]["end"] for span in spans)
            if end - start > MAX_TEXT:
                missing.append("title_abstract_span_gap_over_limit")
            else:
                source_slice = {"start": start, "end": end}
        complete = not missing and len(spans) == 2
        return spans, {
            "covered_characters": sum(len(span["text"]) for span in spans),
            "available_characters": total,
            "complete": complete,
            "missing": sorted(set(missing)),
        }, source_slice
    length = min(len(text), MAX_TEXT)
    spans = [{"id": f"window-{start // WINDOW + 1}",
              "locator": {"kind": "document_revision", "document_id": candidate["publication_id"],
                          "revision_id": candidate["source_revision"], "start": start,
                          "end": min(start + WINDOW, length)},
              "text": text[start:min(start + WINDOW, length)]}
             for start in range(0, length, WINDOW)]
    return spans, {
        "covered_characters": length,
        "available_characters": len(text),
        "complete": length == len(text),
        "missing": [] if length == len(text) else ["full_text_after_limit"],
    }, {"start": 0, "end": length}


def _questions(criteria, spans):
    locations = {span["id"]: f"Exact supplied {span['locator']}" for span in spans}
    locations["none"] = "No supplied span supports the assessment"
    result = {}
    for criterion in criteria:
        code = criterion["code"]
        result[f"assessment_{code}"] = {
            "kind": "choice", "instructions": {
                "criterion_code": code, "criterion_type": criterion["type"],
                "criterion": criterion["text"], "task": "Assess only the supplied located text. Select not_reported if evidence is insufficient."},
            "criteria": {"satisfied": "The criterion is explicitly met in the supplied evidence",
                         "not_satisfied": "The criterion is explicitly contradicted in the supplied evidence",
                         "not_reported": "The supplied evidence does not determine this criterion"},
        }
        result[f"location_{code}"] = {
            "kind": "choice", "instructions": f"Select one supplied span supporting assessment {code}; choose none if no span supports it.",
            "criteria": locations,
        }
    return result


def _render(criteria, spans, run, coverage):
    answers = (run.get("receipt") or {}).get("answers") or {}
    spans_by_id = {span["id"]: span for span in spans}
    rendered = []
    for criterion in criteria:
        code = criterion["code"]
        assessment = answers.get(f"assessment_{code}") or {}
        pointer = answers.get(f"location_{code}") or {}
        status = assessment.get("value") if assessment.get("status") == "answered" else None
        chosen = pointer.get("value") if pointer.get("status") == "answered" else None
        located = spans_by_id.get(chosen)
        if status not in {"satisfied", "not_satisfied", "not_reported"}:
            status = "abstained"
        elif status != "not_reported" and located is None:
            status = "abstained"
        if not coverage["complete"] and status != "not_reported":
            status = "abstained"
        reason = (f"Machine assessment {status} for {code}; source span {chosen}. "
                  "A reviewer must verify the selected span." if located else
                  f"Machine assessment {status} for {code}; no supporting span selected.")
        if not coverage["complete"]:
            reason += " Source coverage is incomplete, so this criterion remains unresolved."
        rendered.append({**criterion, "status": status, "reason": reason,
                         "locator": located["locator"] if located else None,
                         "evidence_text": located["text"] if located else None,
                         "selected_probability": assessment.get("selected_probability"),
                         "vendor_confidence": assessment.get("vendor_confidence")})
    if not coverage["complete"] or run.get("status") != "completed" or any(
        value["status"] in {"not_reported", "abstained"} for value in rendered
    ):
        decision = "pending"
    elif any(value["type"] == "inclusion" and value["status"] == "not_satisfied" or
             value["type"] == "exclusion" and value["status"] == "satisfied" for value in rendered):
        decision = "exclude"
    else:
        decision = "include"
    return rendered, decision


class ScreeningDecisionStore:
    def __init__(self, conn: Any, runtime: Any, *, initialize=True, now=None):
        self.conn, self.runtime = conn, runtime
        self.review = SystematicReviewStore(conn, initialize=False)
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _current(self, namespace, candidate_id, principal_id, scopes):
        candidate, pinned = self.review._candidate(namespace, candidate_id, principal_id, scopes)
        self.review._document_access(candidate, scopes)
        current = self.review.inspect(namespace, candidate["protocol_id"], principal_id=principal_id, scopes=scopes)
        return candidate, pinned, current

    def suggest(self, namespace: str, candidate_id: str, stage: str, run_id: str, *,
                principal_id: str, scopes: set[str], allow_remote=False, policy: Mapping[str, Any] | None = None,
                max_attempts=1, max_cost_usd_micros=0, deadline_s=30) -> dict:
        if stage not in TASKS or not isinstance(run_id, str) or not 1 <= len(run_id) <= 256:
            raise ReviewError("invalid_screening", "valid stage and bounded run ID required")
        candidate, protocol, current = self._current(namespace, candidate_id, principal_id, scopes)
        self.review._authorize(protocol, principal_id, scopes, write=True)
        if current["revision"] != protocol["revision"]:
            raise ReviewError("stale_protocol", "protocol amendment invalidates machine screening")
        if stage == "full_text" and self.review._result(candidate, protocol, "title_abstract")["status"] != "include":
            raise ReviewError("screening_pending", "title/abstract screening must resolve to include")
        criteria = _criteria(protocol)
        source_ref = {"document_id": candidate["publication_id"], "revision_id": candidate["source_revision"]}
        unavailable = (stage == "full_text" and not candidate["full_text_available"]) or (
            stage == "title_abstract" and (not candidate["abstract"].strip() or
                                           len(candidate["title"]) + len(candidate["abstract"]) > MAX_TEXT))
        if unavailable:
            if stage == "full_text":
                spans, source_slice = [], None
                coverage = {"covered_characters": 0, "available_characters": 0,
                            "complete": False, "missing": ["full_text"]}
            else:
                spans, source_slice = [], None
                missing = []
                if not candidate["abstract"].strip():
                    missing.append("abstract")
                if len(candidate["title"]) + len(candidate["abstract"]) > MAX_TEXT:
                    missing.append("title_abstract_over_limit")
                coverage = {
                    "covered_characters": 0,
                    "available_characters": len(candidate["title"]) + len(candidate["abstract"]),
                    "complete": False,
                    "missing": missing,
                }
            bindings, run = [{"kind": "document_revision", **source_ref}], None
        else:
            try:
                bindings, captures = self.runtime.capture_sources(
                    namespace, principal_id, [source_ref], scopes
                )
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code == "source_changed":
                    raise ReviewError("stale_source", "candidate source revision is no longer current") from exc
                if code not in {"source_unavailable", "input_limit"}:
                    raise
                spans, source_slice, bindings = [], None, [{"kind": "document_revision", **source_ref}]
                missing = "source_payload_over_remote_limit" if code == "input_limit" else "source_revision_text_unavailable"
                coverage = {
                    "covered_characters": 0,
                    "available_characters": 0,
                    "complete": False,
                    "missing": [missing],
                }
                run = None
            else:
                spans, coverage, source_slice = _evidence(stage, candidate, captures[0]["content"])
                if not coverage["complete"] or source_slice is None:
                    run = None
                elif len(_json({"spans": spans, "source_slice": source_slice}).encode()) > 400_000:
                    coverage["complete"] = False
                    coverage["missing"] = sorted(set(coverage["missing"] + ["source_payload_over_remote_limit"]))
                    run = None
                else:
                    questions = _questions(criteria, spans)
                    run = self.runtime.run(namespace, run_id, TASKS[stage],
                                       state={"stage": stage, "candidate_id": candidate_id,
                                              "candidate_hash": _hash(candidate),
                                              "protocol_id": protocol["protocol_id"],
                                              "protocol_revision": protocol["revision"],
                                              "protocol_hash": _hash(protocol),
                                              "coverage": coverage, "spans": spans},
                                       questions=questions, source_refs=[source_ref],
                                       principal_id=principal_id, scopes=scopes,
                                       allow_remote=allow_remote, policy=policy,
                                       max_attempts=max_attempts, max_cost_usd_micros=max_cost_usd_micros,
                                       deadline_s=deadline_s, source_slices=[source_slice])
                    bindings = run.get("source_binding", bindings)
            if self.review.inspect(namespace, candidate["protocol_id"], principal_id=principal_id,
                                   scopes=scopes)["revision"] != protocol["revision"]:
                raise ReviewError("stale_protocol", "protocol changed before suggestion publication")
        source_current = self.conn.execute(
            "SELECT revision_id FROM document_current_revisions WHERE document_id=?",
            [candidate["publication_id"]],
        ).fetchone()
        if source_current and source_current[0] != candidate["source_revision"]:
            raise ReviewError("stale_source", "candidate source revision is no longer current")
        if run is not None and source_current is None:
            raise ReviewError("stale_source", "candidate source revision is no longer current")
        request_hash = _hash([namespace, principal_id, candidate, _hash(protocol), stage,
                              run_id, coverage, policy, allow_remote, max_attempts,
                              max_cost_usd_micros, deadline_s])
        prior = self.conn.execute(
            "SELECT request_hash,result_json FROM systematic_review_machine_suggestions "
            "WHERE namespace=? AND principal_id=? AND candidate_id=? AND stage=? AND run_id=?",
            [namespace, principal_id, candidate_id, stage, run_id]).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise ReviewError("run_conflict", "screening run ID is bound to another request")
            return {**json.loads(prior[1]), "replayed": True}
        if unavailable or run is None:
            reason = "Required stage text is unavailable" if unavailable else "Required source evidence is incomplete or unlocated"
            assessments = [{**value, "status": "not_reported", "reason": reason,
                            "locator": None, "evidence_text": None,
                            "selected_probability": None, "vendor_confidence": None} for value in criteria]
            decision = "pending"
        else:
            assessments, decision = _render(criteria, spans, run, coverage)
        result = {"contract": CONTRACT, "namespace": namespace, "candidate_id": candidate_id,
                  "candidate_hash": _hash(candidate), "protocol_id": protocol["protocol_id"],
                  "protocol_revision": protocol["revision"], "protocol_hash": _hash(protocol),
                  "source_binding": bindings, "stage": stage, "run_id": run_id,
                  "status": "suggested" if run and run.get("status") == "completed" and coverage["complete"] else "pending",
                  "suggested_decision": decision, "coverage": coverage, "criteria": assessments,
                  "machine_only": True, "human_vote_counted": False,
                  "decision_run": run, "remote_processing_used": bool(run and run.get("remote_processing_used")),
                  "created_at_ms": self.now()}
        self.conn.execute("INSERT INTO systematic_review_machine_suggestions VALUES (?,?,?,?,?,?,?,?)",
                          [namespace, principal_id, candidate_id, stage, run_id, request_hash,
                           _json(result), result["created_at_ms"]])
        return {**result, "replayed": False}

    def inspect(self, namespace: str, candidate_id: str, stage: str, run_id: str, *,
                principal_id: str, scopes: set[str]) -> dict:
        candidate, protocol, current = self._current(namespace, candidate_id, principal_id, scopes)
        row = self.conn.execute(
            "SELECT result_json FROM systematic_review_machine_suggestions "
            "WHERE namespace=? AND principal_id=? AND candidate_id=? AND stage=? AND run_id=?",
            [namespace, principal_id, candidate_id, stage, run_id]).fetchone()
        if not row:
            raise ReviewError("suggestion_unavailable", "machine suggestion is unavailable")
        result = json.loads(row[0])
        source = self.conn.execute("SELECT revision_id FROM document_current_revisions WHERE document_id=?",
                                   [candidate["publication_id"]]).fetchone()
        stale = (current["revision"] != result["protocol_revision"] or _hash(protocol) != result["protocol_hash"]
                 or _hash(candidate) != result["candidate_hash"] or
                 source is None or source[0] != candidate["source_revision"])
        return {**result, "valid": not stale, "status": "stale" if stale else result["status"],
                "suggested_decision": None if stale else result["suggested_decision"]}
