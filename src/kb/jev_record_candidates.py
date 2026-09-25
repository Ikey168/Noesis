"""Versioned, record-backed Jev suggestions for methods and identities."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from src.kb.entity_history import EntityHistoryStore, READ_SCOPE as ENTITY_READ, REVIEW_SCOPE as ENTITY_REVIEW
from src.kb.jev_tasks import suggest_task
from src.kb.methodology_provenance import MethodologyStore, READ_SCOPE as METHOD_READ
from src.kb.research_projects import _hash, _json
from src.kb.source_identity import SourceIdentityStore, READ_SCOPE as SOURCE_READ, REVIEW_SCOPE as SOURCE_REVIEW

CONTRACT = "noesis-jev-record-candidate-v1"
DESIGNS = {
    "randomized_controlled": "Randomized controlled intervention design",
    "nonrandomized_intervention": "Intervention without random assignment",
    "observational_cohort": "Observational cohort design",
    "case_control": "Case-control design",
    "cross_sectional": "Cross-sectional observational design",
    "qualitative": "Qualitative study design",
    "mixed_methods": "Integrated qualitative and quantitative design",
    "other": "An explicitly described design outside these categories",
    "not_reported": "The passage does not establish a design",
}


class CandidateError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _access(namespace, scopes, required):
    if "operator" not in scopes and (required not in scopes or
        f"namespace:{namespace}:read" not in scopes and f"namespace:{namespace}:write" not in scopes):
        raise CandidateError("unauthorized", "current record and namespace read access required")


def _reference_ids(reference_id, candidate_ids):
    if (not isinstance(reference_id, str) or not reference_id or
        not isinstance(candidate_ids, (list, tuple)) or not 1 <= len(candidate_ids) <= 10 or
        any(not isinstance(value, str) or not value for value in candidate_ids) or
        len(set(candidate_ids)) != len(candidate_ids) or reference_id in candidate_ids or
        any(value in {"none", "uncertain"} for value in candidate_ids)):
        raise CandidateError("invalid_candidates", "one to ten distinct existing candidates required")
    return [reference_id, *candidate_ids]


def _identity_description(kind, record, identity, *, limit=500):
    if kind == "source":
        description = {
            "identity_id": identity,
            "primary_name": record.get("display_name", ""),
            "localized_names": dict(sorted((record.get("names") or {}).items())[:20]),
            "registered_ids": dict(sorted((record.get("native_ids") or {}).items())[:20]),
            "reviewed_aliases": (record.get("aliases") or [])[:20],
        }
    else:
        description = {
            "identity_id": identity,
            "aliases": (record.get("aliases") or [])[:20],
        }
    return _json(description)[:limit]


class RecordInputResolver:
    """Re-read current persisted records for DecisionRuntime user-input bindings."""

    def __init__(self, conn):
        self.conn = conn

    @staticmethod
    def _bounded(record, version):
        content = _json(record)
        if len(content.encode()) > 16_000:
            raise CandidateError("input_limit", "record candidate exceeds 16 KiB")
        return {"version": str(version), "content_hash": _hash(record), "content": content}

    def source(self, namespace, source_id, scopes):
        _access(namespace, scopes, SOURCE_READ)
        store = SourceIdentityStore(self.conn, initialize=False)
        record = store.get(namespace, source_id, scopes=scopes)
        if not record or record.get("lifecycle") != "active":
            raise CandidateError("candidate_unavailable", "source identity is unavailable or inactive")
        aliases = self.conn.execute(
            "SELECT d.decision_id,d.alias_type,d.normalized_alias,d.language,d.action "
            "FROM source_alias_decisions d WHERE d.namespace=? AND d.source_id=? "
            "AND NOT EXISTS (SELECT 1 FROM source_alias_decisions child "
            "WHERE child.predecessor_decision_id=d.decision_id) "
            "ORDER BY d.alias_type,d.normalized_alias,d.language,d.decision_id LIMIT 101",
            [namespace, source_id],
        ).fetchall()
        if len(aliases) > 100:
            raise CandidateError("input_limit", "source alias history exceeds candidate bound")
        record["aliases"] = [
            {"decision_id": row[0], "type": row[1], "value": row[2],
             "language": row[3], "action": row[4]}
            for row in aliases
        ]
        return self._bounded(record, record["revision_id"])

    def entity(self, namespace, entity_id, scopes):
        _access(namespace, scopes, ENTITY_READ)
        store = EntityHistoryStore(self.conn, initialize=False)
        record = store._entity(namespace, entity_id)
        if record["status"] != "active":
            raise CandidateError("candidate_unavailable", "entity identity is inactive")
        record = {"namespace": namespace, **record}
        return self._bounded(record, _hash(record))

    def methodology(self, namespace, statement_id, scopes):
        _access(namespace, scopes, METHOD_READ)
        row = self.conn.execute(
            "SELECT study_id,kind,text,locator_json,provenance_json FROM methodology_statements "
            "WHERE namespace=? AND statement_id=?", [namespace, statement_id]).fetchone()
        if not row:
            raise CandidateError("candidate_unavailable", "methodology statement is unavailable")
        study = MethodologyStore(self.conn, initialize=False).study(namespace, row[0], scopes=scopes)
        record = {"namespace": namespace, "study_id": row[0], "study_revision_id": study["study_revision_id"],
                  "statement_id": statement_id, "kind": row[1], "text": row[2],
                  "locator": json.loads(row[3]), "provenance": json.loads(row[4])}
        return self._bounded(record, study["study_revision_id"])

    def __call__(self, *, namespace, principal_id, scopes, reference):
        input_id = reference.get("input_id", "")
        if input_id.startswith("source-identity:"):
            return self.source(namespace, input_id.removeprefix("source-identity:"), scopes)
        if input_id.startswith("entity-identity:"):
            return self.entity(namespace, input_id.removeprefix("entity-identity:"), scopes)
        if input_id.startswith("methodology-statement:"):
            return self.methodology(namespace, input_id.removeprefix("methodology-statement:"), scopes)
        raise CandidateError("invalid_sources", "unsupported record input reference")

    def ref(self, kind, namespace, identity, scopes):
        prefix = {"source": "source-identity", "entity": "entity-identity",
                  "methodology": "methodology-statement"}.get(kind)
        if prefix is None:
            raise CandidateError("invalid_sources", "unsupported record input kind")
        record = getattr(self, kind)(namespace, identity, scopes)
        return {"input_id": f"{prefix}:{identity}",
                "input_version": record["version"], "content_hash": record["content_hash"]}


class JevRecordCandidateAdvisor:
    def __init__(self, conn: Any, runtime: Any):
        self.conn, self.runtime = conn, runtime
        self.resolver = RecordInputResolver(conn)

    def suggest_methodology(self, namespace: str, statement_id: str, run_id: str, *,
                            principal_id: str, scopes: set[str], allow_remote: bool,
                            policy: Mapping[str, Any], max_attempts=1,
                            max_cost_usd_micros=1000, deadline_s=30) -> dict:
        record = self.resolver.methodology(namespace, statement_id, scopes)
        value = json.loads(record["content"])
        locator = value["locator"]
        document_id = locator.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise CandidateError("source_unavailable", "methodology statement lacks a document locator")
        source = self.conn.execute(
            "SELECT r.revision_id,r.payload_json FROM document_current_revisions c "
            "JOIN document_revision_records r ON r.document_id=c.document_id AND r.revision_id=c.revision_id "
            "WHERE c.document_id=? AND r.committed_watermark IS NOT NULL", [document_id]).fetchone()
        if not source:
            raise CandidateError("source_unavailable", "methodology source revision is unavailable")
        full_text = json.loads(source[1]).get("content")
        passage = value["text"]
        if not isinstance(full_text, str) or not passage or len(passage) > 8_000:
            raise CandidateError("source_unavailable", "bounded source passage is unavailable")
        start = full_text.find(passage)
        if start < 0 or full_text.find(passage, start + 1) >= 0:
            raise CandidateError("locator_ambiguous", "statement is not a unique verbatim source passage")
        input_ref = {"input_id": "methodology-statement:" + statement_id,
                     "input_version": record["version"], "content_hash": record["content_hash"]}
        source_refs = [input_ref, {"document_id": document_id, "revision_id": source[0]}]
        questions = {
            "study_design": {"kind": "choice", "instructions": "Classify only the exact cited passage; do not infer sample sizes or effects.",
                             "criteria": DESIGNS},
            "method": {"kind": "choice", "instructions": "Which methodological family is explicitly described?",
                       "criteria": {"experimental": "Manipulated intervention", "observational": "Observed without assigned intervention",
                                    "qualitative": "Qualitative collection or analysis", "mixed": "Mixed methods",
                                    "not_reported": "No method is established"}},
            "limitation": {"kind": "choice", "instructions": "Does the passage explicitly report a study limitation?",
                           "criteria": {"reported": "Explicit limitation", "not_reported": "No explicit limitation"}},
        }
        run = self.runtime.run(
            namespace, run_id, "jev-methodology-record-v1",
            state={"study_id": value["study_id"], "study_revision_id": value["study_revision_id"],
                   "statement_id": statement_id, "locator": locator},
            questions=questions, source_refs=source_refs,
            source_slices=[{"start": 0, "end": len(record["content"])},
                           {"start": start, "end": start + len(passage)}],
            principal_id=principal_id, scopes=scopes, allow_remote=allow_remote,
            policy=policy, max_attempts=max_attempts,
            max_cost_usd_micros=max_cost_usd_micros, deadline_s=deadline_s)
        answers = (run.get("receipt") or {}).get("answers") or {}
        values = {}
        for key, question in questions.items():
            answer = answers.get(key)
            selected_value = answer.get("value") if isinstance(answer, Mapping) and answer.get("status") == "answered" else None
            allowed = question["criteria"]
            values[key] = selected_value if isinstance(selected_value, str) and selected_value in allowed else None
        active = run.get("status") == "completed" and run.get("rollout_mode") != "shadow"
        complete = all(value is not None for value in values.values())
        status = ("shadow" if run.get("rollout_mode") == "shadow" else
                  "suggested" if active and complete else "unavailable")
        if status != "suggested":
            values = {key: None for key in questions}
        return {"contract": CONTRACT, "task": "methodology", "status": status,
                "accepted": False, "study_id": value["study_id"], "study_revision_id": value["study_revision_id"],
                "statement_id": statement_id, "source_revision_id": source[0],
                "source_locator": {"document_id": document_id, "revision_id": source[0],
                                   "start": start, "end": start + len(passage)},
                "categories": values, "numeric_values_generated": False,
                "source_binding": run.get("source_binding", []), "decision_run": run}

    def _identity(self, kind, namespace, reference_id, candidate_ids, run_id, *,
                  principal_id, scopes, allow_remote, policy, max_attempts,
                  max_cost_usd_micros, deadline_s):
        ids = _reference_ids(reference_id, candidate_ids)
        getter = self.resolver.source if kind == "source" else self.resolver.entity
        records = [getter(namespace, identity, scopes) for identity in ids]
        parsed = [json.loads(record["content"]) for record in records]
        labels = {identity: _identity_description(kind, record, identity)
                  for identity, record in zip(ids[1:], parsed[1:])}
        refs = [{"input_id": ("source-identity:" if kind == "source" else "entity-identity:") + identity,
                 "input_version": record["version"], "content_hash": record["content_hash"]}
                for identity, record in zip(ids, records)]
        suggestion = suggest_task(
            self.runtime, namespace, run_id,
            "source_matching" if kind == "source" else "entity_matching",
            {"reference_identity": _identity_description(kind, parsed[0], reference_id, limit=1000),
             "candidates": labels}, refs,
            principal_id=principal_id, scopes=scopes, allow_remote=allow_remote,
            policy=policy, max_attempts=max_attempts,
            max_cost_usd_micros=max_cost_usd_micros, deadline_s=deadline_s)
        answer = suggestion["answers"].get("match") or {}
        chosen = answer.get("value") if answer.get("status") == "answered" else None
        if chosen not in {*candidate_ids, "none", "uncertain"}:
            chosen = "uncertain"
        return {"contract": CONTRACT, "task": kind + "_identity", "status": suggestion["status"],
                "accepted": False, "reference_id": reference_id, "candidate_ids": list(candidate_ids),
                "record_versions": {identity: record["version"] for identity, record in zip(ids, records)},
                "selected_candidate_id": chosen if chosen in candidate_ids else None,
                "assessment": "match" if chosen in candidate_ids else chosen,
                "ownership_assessed": False, "independence_assessed": False,
                "corroboration_assessed": False,
                "source_binding": suggestion["source_binding"],
                "decision_run": suggestion["decision_run"]}

    def suggest_source_identity(self, namespace, reference_id, candidate_ids, run_id, *,
                                principal_id, scopes, allow_remote, policy,
                                max_attempts=1, max_cost_usd_micros=1000, deadline_s=30):
        return self._identity("source", namespace, reference_id, candidate_ids, run_id,
                              principal_id=principal_id, scopes=scopes, allow_remote=allow_remote,
                              policy=policy, max_attempts=max_attempts,
                              max_cost_usd_micros=max_cost_usd_micros, deadline_s=deadline_s)

    def suggest_entity_identity(self, namespace, reference_id, candidate_ids, run_id, *,
                                principal_id, scopes, allow_remote, policy,
                                max_attempts=1, max_cost_usd_micros=1000, deadline_s=30):
        return self._identity("entity", namespace, reference_id, candidate_ids, run_id,
                              principal_id=principal_id, scopes=scopes, allow_remote=allow_remote,
                              policy=policy, max_attempts=max_attempts,
                              max_cost_usd_micros=max_cost_usd_micros, deadline_s=deadline_s)

    def _reviewable_match(self, namespace, run_id, kind, accepted_candidate_id,
                          principal_id, scopes):
        required = SOURCE_REVIEW if kind == "source" else ENTITY_REVIEW
        if "operator" not in scopes and (required not in scopes or f"namespace:{namespace}:write" not in scopes):
            raise CandidateError("unauthorized", "current identity review and namespace write access required")
        run = self.runtime.inspect(namespace, run_id, principal_id=principal_id, scopes=scopes)
        if run.get("status") != "completed" or not run.get("artifact"):
            raise CandidateError("suggestion_unavailable", "a retained completed suggestion is required")
        if run.get("rollout_mode") != "suggestion" or not run.get("evaluation_ref"):
            raise CandidateError("evaluation_required", "accepted identity review requires an evaluated suggestion rollout")
        artifact = self.runtime.graph.inspect(run["artifact"]["artifact_id"])
        expected_task = "jev-source_matching-v1" if kind == "source" else "jev-entity_matching-v1"
        if artifact["content"].get("task") != expected_task:
            raise CandidateError("invalid_suggestion", "run is not the expected identity task")
        prefix = "source-identity:" if kind == "source" else "entity-identity:"
        bindings = run.get("source_binding") or []
        if not 2 <= len(bindings) <= 11 or any(
            binding.get("kind") != "user_input_version" or
            not binding.get("input_id", "").startswith(prefix) for binding in bindings
        ):
            raise CandidateError("invalid_suggestion", "identity candidates are not version-bound records")
        ids = [binding["input_id"].removeprefix(prefix) for binding in bindings]
        chosen = ((run.get("receipt") or {}).get("answers") or {}).get("match") or {}
        if (artifact["content"].get("answers") or {}).get("match") != chosen:
            raise CandidateError("invalid_suggestion", "retained artifact and receipt answers disagree")
        if chosen.get("status") != "answered" or chosen.get("value") != accepted_candidate_id or accepted_candidate_id not in ids[1:]:
            raise CandidateError("invalid_suggestion", "human acceptance must name the model-selected existing candidate")
        return ids[0], accepted_candidate_id, run

    def accept_source_alias(self, namespace, run_id, accepted_candidate_id, reason, *,
                            principal_id, scopes):
        """Record a human-confirmed alias through the existing source review method."""
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise CandidateError("invalid_review", "substantive human review reason required")
        reference_id, target_id, run = self._reviewable_match(
            namespace, run_id, "source", accepted_candidate_id, principal_id, scopes)
        reference = SourceIdentityStore(self.conn, initialize=False).get(
            namespace, reference_id, scopes=scopes)
        return SourceIdentityStore(self.conn, initialize=False).decide_alias(
            namespace, target_id, "name", reference["display_name"],
            reason=reason, reviewer_id=principal_id, scopes=scopes,
            confidence=1.0, provenance={"machine_run_id": run_id,
                                        "source_binding": run["source_binding"],
                                        "human_confirmed": True})

    def queue_entity_merge_review(self, namespace, run_id, accepted_candidate_id, reason, *,
                                  principal_id, scopes):
        """Queue a human-confirmed proposal; existing admin approval owns merging."""
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise CandidateError("invalid_review", "substantive human review reason required")
        reference_id, target_id, _ = self._reviewable_match(
            namespace, run_id, "entity", accepted_candidate_id, principal_id, scopes)
        from src.knowledge_graph.entity_corrections import CorrectionType, EntityCorrectionStore

        queued = EntityCorrectionStore(self.conn).submit(
            target_id, CorrectionType.MERGE, {"merge_from": reference_id},
            reason, principal_id)
        return {"status": "pending_human_admin_review", "correction": queued.to_dict(),
                "source_run_id": run_id, "merge_applied": False}
