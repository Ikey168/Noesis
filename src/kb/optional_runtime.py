"""Revision-safe optional analysis jobs published into the existing artifact graph.

This is an opt-in execution boundary, not a second source of document truth.
No output can overwrite sources, silently publish reports or merge identities.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from src.evaluation.runtime_errors import BackendError
from src.evaluation.runtime_jobs import execute_job
from src.evaluation.runtime_outcomes import normalize_outcome
from src.kb.artifacts import ArtifactGraph
from src.kb.research_projects import _hash, _json
from src.kb.review_inbox import ReviewInboxStore

TEXT_OPERATIONS = frozenset({"gliner2", "sat", "presidio", "stance", "frames"})
BINARY_OPERATIONS = frozenset({"paddleocr", "lightonocr", "whisperx"})
EXECUTE_SCOPE = "knowledge:optional:execute"
READ_SCOPE = "knowledge:optional:read"


class OptionalAnalysisStore:
    def __init__(self, conn, *, executor=None):
        self.conn, self.graph = conn, ArtifactGraph(conn)
        self.executor = executor or execute_job
        # Fixture executors remain explicitly labelled in the artifact producer.
        self.mode = "injected-executor" if executor else "native-runtime-jobs"
        self.sources = ReviewInboxStore(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS optional_analysis_runs(namespace TEXT,owner TEXT,run_id TEXT,request_hash TEXT,sources_json TEXT,result_json TEXT,PRIMARY KEY(namespace,owner,run_id))"
        )

    @staticmethod
    def authorize(namespace, principal_id, scopes, *, execute=False):
        needed = EXECUTE_SCOPE if execute else READ_SCOPE
        if (
            not principal_id
            or "operator" not in scopes
            and (
                needed not in scopes
                or f"namespace:{namespace}:{'write' if execute else 'read'}"
                not in scopes
            )
        ):
            raise BackendError(
                "unauthorized",
                "current optional-analysis and namespace authorization required",
            )

    def _source(self, spec, scopes):
        self.sources._sources([spec], scopes)
        raw = self.conn.execute(
            "SELECT payload_json FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
            [spec["document_id"], spec["revision_id"]],
        ).fetchone()
        return json.loads(raw[0])

    def _current(self, specs, scopes):
        self.sources._sources(specs, scopes)
        for spec in specs:
            row = self.conn.execute(
                "SELECT revision_id FROM document_current_revisions WHERE document_id=?",
                [spec["document_id"]],
            ).fetchone()
            if not row or row[0] != spec["revision_id"]:
                raise BackendError(
                    "source_changed",
                    "source changed before optional analysis publication",
                )

    @staticmethod
    def _checked_run(saved):
        """Expose legacy outcomes safely without rewriting immutable history."""
        result = dict(saved)
        result["outcome"] = normalize_outcome(result["operation"], result["outcome"])
        if (
            result["operation"] in {"splink", "rapidfuzz"}
            and result["outcome"]["status"] == "completed"
        ):
            value = result["outcome"].get("result", {})
            if not isinstance(value, dict) or not value.get("source_binding"):
                result["outcome"] = {
                    **result["outcome"],
                    "status": "partial",
                    "failure_code": "legacy_entity_inputs_unbound",
                }
        if result["outcome"]["status"] != "completed" and result.get("artifact"):
            result["suppressed_artifact_id"] = result["artifact"].get("artifact_id")
            result["artifact"] = None
        return result

    def inspect(self, namespace, run_id, *, principal_id, scopes):
        self.authorize(namespace, principal_id, scopes)
        row = self.conn.execute(
            "SELECT sources_json,result_json FROM optional_analysis_runs WHERE namespace=? AND owner=? AND run_id=?",
            [namespace, principal_id, run_id],
        ).fetchone()
        if not row:
            raise BackendError(
                "run_unavailable", "optional analysis run is unavailable"
            )
        self.sources._sources(json.loads(row[0]), scopes)
        return self._checked_run(json.loads(row[1]))

    def run(
        self,
        namespace,
        run_id,
        operation,
        payload,
        *,
        source_refs,
        principal_id,
        scopes,
        timeout_s=60,
        max_rss_bytes=6 * 1024**3,
        cancelled=None,
        trace_sink=None,
    ):
        self.authorize(namespace, principal_id, scopes, execute=True)
        if (
            not isinstance(run_id, str)
            or not 1 <= len(run_id) <= 256
            or not isinstance(payload, dict)
        ):
            raise ValueError("bounded run identity and payload required")
        if operation not in TEXT_OPERATIONS | BINARY_OPERATIONS | {
            "splink",
            "rapidfuzz",
            "mdeberta",
            "e5",
            "bge-m3",
            "qwen3-reranker",
            "ragas",
        }:
            raise ValueError("this operation is not a source-bound optional analysis")
        if any(key in payload for key in ("path", "credential", "api_key", "token")):
            raise ValueError("source-bound jobs cannot supply paths or credentials")
        if len(_json(payload).encode()) > 16 * 1024**2:
            raise ValueError("analysis payload exceeds 16 MiB")
        self.sources._sources(source_refs, scopes)
        request = {
            "operation": operation,
            "payload": payload,
            "sources": source_refs,
            "mode": self.mode,
            "timeout_s": timeout_s,
            "max_rss_bytes": max_rss_bytes,
        }
        key = _hash(request)
        previous = self.conn.execute(
            "SELECT request_hash,result_json FROM optional_analysis_runs WHERE namespace=? AND owner=? AND run_id=?",
            [namespace, principal_id, run_id],
        ).fetchone()
        if previous:
            if previous[0] != key:
                raise BackendError(
                    "run_conflict",
                    "run ID is bound to a different source/configuration",
                )
            return {**self._checked_run(json.loads(previous[1])), "replayed": True}
        self._current(source_refs, scopes)
        prepared = dict(payload)
        entity_binding = None
        if operation in {"splink", "rapidfuzz"}:
            from src.kb.entity_analysis_inputs import prepare_inputs

            sources = {
                (ref["document_id"], ref["revision_id"]): self._source(ref, scopes)
                for ref in source_refs
            }
            prepared, entity_binding = prepare_inputs(
                self.conn, namespace, operation, payload, sources
            )
        source = self._source(source_refs[0], scopes)
        content = source.get("content", "")
        if operation in TEXT_OPERATIONS:
            if len(source_refs) != 1 or not isinstance(content, str) or not content:
                raise ValueError(
                    "text analysis requires exactly one nonempty captured source"
                )
            if operation == "gliner2":
                prepared.update(
                    text=content,
                    source_id=source_refs[0]["document_id"],
                    source_revision=source_refs[0]["revision_id"],
                    language=source.get("language") or payload.get("language", "und"),
                )
            elif operation == "presidio":
                prepared["original"] = {
                    "text": content,
                    "source_id": source_refs[0]["document_id"],
                    "source_revision": source_refs[0]["revision_id"],
                }
            else:
                prepared["text"] = content
        if operation in {"e5", "bge-m3"}:
            prepared["texts"] = [
                self._source(ref, scopes).get("content", "") for ref in source_refs
            ]
            prepared["mode"] = "passage"
        if operation == "qwen3-reranker":
            prepared["candidates"] = [
                {
                    "id": ref["document_id"],
                    "text": self._source(ref, scopes).get("content", ""),
                    "revision": ref["revision_id"],
                }
                for ref in source_refs
            ]
        if operation == "mdeberta":
            if len(source_refs) != 1:
                raise ValueError(
                    "NLI requires exactly one source with complete context"
                )
            claims = payload.get("claims")
            if (
                not isinstance(claims, list)
                or not 1 <= len(claims) <= 64
                or any(not isinstance(v, str) or not v for v in claims)
            ):
                raise ValueError("bounded explicit hypotheses required")
            prepared = {
                "premise": content,
                "claims": claims,
                "max_windows": payload.get("max_windows", 64),
            }
        if operation == "ragas":
            from src.evaluation.ragas_metrics import validate_cases

            cases = json.loads(_json(payload.get("cases")))
            validate_cases(cases)
            frozen = {
                ref["document_id"]: (ref, self._source(ref, scopes))
                for ref in source_refs
            }
            for case in cases:
                for context in case["contexts"]:
                    if context["id"] not in frozen:
                        raise BackendError(
                            "source_identity",
                            "Ragas context is not an authorized captured source",
                        )
                    ref, original = frozen[context["id"]]
                    if context["revision"] != ref["revision_id"] or context[
                        "text"
                    ] != original.get("content"):
                        raise BackendError(
                            "source_identity",
                            "Ragas context differs from captured revision",
                        )
            prepared["cases"] = cases
        with tempfile.TemporaryDirectory(prefix="noesis-analysis-input-") as directory:
            if operation in BINARY_OPERATIONS:
                blob = payload.get("sha256")
                if (
                    not isinstance(blob, str)
                    or len(blob) != 64
                    or len(source_refs) != 1
                ):
                    raise ValueError(
                        "one source and its captured binary digest required"
                    )
                # A caller must not claim arbitrary captured bytes as another
                # document's evidence. Bind against stored authoritative metadata.
                metadata = source.get("metadata", {})
                allowed = {
                    metadata[k]
                    for k in (
                        "original_sha256",
                        "native_capture_sha256",
                        "media_sha256",
                        "binary_sha256",
                    )
                    if isinstance(metadata.get(k), str)
                }
                if blob not in allowed:
                    raise BackendError(
                        "source_identity",
                        "binary hash does not belong to this source revision",
                    )
                row = self.conn.execute(
                    "SELECT payload FROM source_binary_blobs WHERE digest=?", [blob]
                ).fetchone()
                if (
                    not row
                    or len(row[0]) > 50_000_000
                    or hashlib.sha256(bytes(row[0])).hexdigest() != blob
                ):
                    raise BackendError(
                        "source_unavailable",
                        "captured binary is missing, changed or oversized",
                    )
                path = Path(directory) / (
                    "media.bin" if operation == "whisperx" else "document.pdf"
                )
                path.write_bytes(bytes(row[0]))
                prepared["path"] = str(path)
                prepared["sha256"] = blob
                if operation == "whisperx":
                    original_transcript = metadata.get("transcript_json")
                    if not isinstance(original_transcript, str):
                        raise BackendError(
                            "transcript_unavailable",
                            "alignment requires a captured original transcript",
                        )
                    prepared["transcript"] = json.loads(original_transcript)
            result = self.executor(
                operation,
                prepared,
                timeout_s=timeout_s,
                max_rss_bytes=max_rss_bytes,
                cancelled=cancelled,
            )
        result = normalize_outcome(operation, result)
        if entity_binding is not None and result["status"] == "completed":
            from src.kb.entity_analysis_inputs import bind_output

            result["result"] = bind_output(operation, result["result"], entity_binding)
        if len(_json(result).encode()) > 32 * 1024**2:
            raise BackendError(
                "output_limit", "optional result exceeds publication limit"
            )
        output = {
            "operation": operation,
            "run_id": run_id,
            "outcome": result,
            "artifact": None,
            "mode": self.mode,
            "automatic_merge": False,
            "automatic_publish": False,
        }
        self.conn.execute("BEGIN")
        try:
            self._current(source_refs, scopes)
            if entity_binding is not None:
                from src.kb.entity_analysis_inputs import check_current

                check_current(self.conn, namespace, entity_binding)
            if cancelled and cancelled():
                raise BackendError("cancelled", "analysis cancelled before publication")
            if result["status"] == "completed":
                from src.argument_mining.model_registry import (
                    OPTIONAL_PINS,
                    optional_model_spec,
                )

                model = (
                    optional_model_spec(operation) if operation in OPTIONAL_PINS else {}
                )
                output["artifact"] = self.graph.register(
                    namespace,
                    "enrichment",
                    "optional:" + _hash([principal_id, run_id])[:32],
                    result["result"],
                    configuration={"request_hash": key, "mode": self.mode},
                    producer={
                        "name": operation,
                        "version": "injected-test-only"
                        if self.mode == "injected-executor"
                        else model.get("revision")
                        or (
                            str(result["result"].get("version", "runtime-v1"))
                            if isinstance(result["result"], dict)
                            else "runtime-v1"
                        ),
                    },
                    dependencies=[
                        {
                            "dependency_id": ref["document_id"],
                            "kind": "source",
                            "detail": {"revision_id": ref["revision_id"]},
                        }
                        for ref in source_refs
                    ],
                )
            self.conn.execute(
                "INSERT INTO optional_analysis_runs VALUES (?,?,?,?,?,?)",
                [
                    namespace,
                    principal_id,
                    run_id,
                    key,
                    _json(source_refs),
                    _json(output),
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        if trace_sink is not None:
            try:
                trace_sink.emit(
                    {
                        "project_id": namespace,
                        "run_id": run_id,
                        "operation": "evaluate",
                        "status": "success"
                        if result["status"] == "completed"
                        else result["status"],
                        "evidence_ids": [ref["document_id"] for ref in source_refs],
                    }
                )
            except Exception:  # noqa: BLE001 - optional telemetry cannot corrupt source evidence
                output["telemetry_status"] = "unavailable"
        return output

    def queue_entity_candidate(
        self,
        namespace,
        run_id,
        candidate_index,
        *,
        principal_id,
        scopes,
        domain,
        impact=0.5,
        uncertainty=0.5,
    ):
        """Explicitly enqueue one machine suggestion through the existing review inbox."""
        from src.kb.entity_history import REVIEW_SCOPE, EntityHistoryStore

        self.authorize(namespace, principal_id, scopes, execute=True)
        if "operator" not in scopes and REVIEW_SCOPE not in scopes:
            raise BackendError("unauthorized", "entity-history review scope required")
        run = self.inspect(namespace, run_id, principal_id=principal_id, scopes=scopes)
        if (
            run["operation"] not in {"splink", "rapidfuzz"}
            or run["outcome"]["status"] != "completed"
        ):
            raise ValueError("a completed native candidate-scoring run is required")
        from src.kb.entity_analysis_inputs import bind_output, check_current

        evidence = run["outcome"]["result"]
        binding = evidence.get("source_binding")
        check_current(self.conn, namespace, binding)
        bind_output(run["operation"], evidence, binding)
        rows = evidence["candidates"]
        if type(candidate_index) is not int or not 0 <= candidate_index < len(rows):
            raise ValueError("candidate index out of range")
        candidate = rows[candidate_index]
        if not candidate.get("eligible_for_review"):
            raise ValueError(
                "conflicting identity/type candidate cannot enter the match queue"
            )
        ids = (
            [candidate["left_id"], candidate["right_id"]]
            if run["operation"] == "splink"
            else [run["outcome"]["result"]["source_id"], candidate["id"]]
        )
        history = EntityHistoryStore(self.conn)
        # Existing canonical IDs are required; this call cannot invent entities.
        for identity in ids:
            history._entity(namespace, identity)
        source_json = self.conn.execute(
            "SELECT sources_json FROM optional_analysis_runs WHERE namespace=? AND owner=? AND run_id=?",
            [namespace, principal_id, run_id],
        ).fetchone()[0]
        refs = json.loads(source_json)
        self._current(refs, scopes)
        decision = history.decide(
            namespace,
            "review",
            ids,
            {
                "origin": "machine",
                "artifact_id": run["artifact"]["artifact_id"],
                "candidate": candidate,
                "status": "proposed",
                "automatic_merge": False,
            },
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=scopes,
            event_key="optional-candidate:"
            + _hash([run_id, candidate_index, principal_id]),
        )
        return self.sources.create(
            namespace,
            {"kind": "entity", "namespace": namespace, "id": decision["decision_id"]},
            sources=refs,
            domain=domain,
            impact=impact,
            uncertainty=uncertainty,
            rationale="Optional native model candidate; requires independent review.",
            principal_id=principal_id,
            scopes=scopes,
        )
