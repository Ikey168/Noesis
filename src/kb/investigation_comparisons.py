"""Immutable completed-run comparisons built on pinned project assessments."""

import json

from src.kb.authored_reports import AuthoredReportStore
from src.kb.project_branches import ProjectBranchStore
from src.kb.project_comparison import assess, differences
from src.kb.research_projects import ResearchProjectError, _hash, _json

CONTRACT = "noesis-investigation-comparison-v1"
_DDL = """CREATE TABLE IF NOT EXISTS investigation_comparisons(
 comparison_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 request_hash TEXT NOT NULL, artifact_json TEXT NOT NULL);"""


def _delta(left, right, *, complete=True, states=False):
    result = []
    for identity in sorted(left.keys() | right.keys()):
        a, b = left.get(identity), right.get(identity)
        if a == b:
            continue
        kind = "added" if a is None else "removed" if b is None else "revised"
        if not complete and (a is None or b is None):
            kind = "unavailable_or_outside_coverage"
        elif states:
            if b and b.get("status") == "open" and a and a.get("status") != "open":
                kind = "reopened"
            elif b and b.get("status") in {"resolved", "closed"}:
                kind = "resolved"
            elif a is None and b and b.get("status") == "open":
                kind = "opened"
        result.append({"id": identity, "kind": kind, "before": a, "after": b})
    return result


class InvestigationComparisonStore:
    def __init__(self, conn, *, initialize=True):
        self.conn = conn
        self.projects = ProjectBranchStore(conn, initialize=initialize)
        self.reports = AuthoredReportStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    def _resolve(self, namespace, selector, principal_id, scopes):
        if (
            not isinstance(selector, dict)
            or set(selector)
            != {"project_id", "project_revision", "run_id", "generations"}
            or type(selector["project_revision"]) is not int
            or selector["project_revision"] < 1
        ):
            raise ResearchProjectError(
                "invalid_selector",
                "pin project revision, completed run and namespace generations",
            )
        project = self.projects.inspect(
            namespace,
            selector["project_id"],
            revision=selector["project_revision"],
            principal_id=principal_id,
            scopes=scopes,
        )
        run_id = selector["run_id"]
        if not any(
            link["kind"] == "run"
            and link["id"] == run_id
            and link.get("namespace", namespace) == namespace
            for link in project["links"]
        ):
            raise ResearchProjectError(
                "run_not_linked", "run must be linked in the selected project revision"
            )
        if "operator" not in scopes and "knowledge:recipes:read" not in scopes:
            raise ResearchProjectError(
                "unauthorized", "current recipe read access required"
            )
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        row = (
            self.conn.execute(
                "SELECT status,recipe_revision_id,input_hash,receipt_json,principal_id,updated_at_ms FROM research_recipe_runs WHERE namespace=? AND run_id=?",
                [namespace, run_id],
            ).fetchone()
            if "research_recipe_runs" in tables
            else None
        )
        if not row or row[0] != "completed":
            raise ResearchProjectError(
                "run_unavailable", "retained completed recipe run required"
            )
        if row[4] != principal_id and "operator" not in scopes:
            raise ResearchProjectError("unauthorized", "current run ownership required")
        run_completed_at = row[5]
        generations = self.projects._generations(
            selector["generations"], project, principal_id, scopes
        )
        assessment = assess(self.conn, project, scopes)
        if any(v["reason"] == "inaccessible_source" for v in assessment["omissions"]):
            raise ResearchProjectError(
                "unauthorized", "current access to all compared sources required"
            )
        # Scope generation pins to every finding, not just the root namespace.
        for finding in assessment["findings"].values():
            ns = finding["reference"].get("namespace", namespace)
            gen = self.conn.execute(
                "SELECT generation FROM derived_object_revisions WHERE revision_id=?",
                [finding["revision_id"]],
            ).fetchone()[0]
            if ns not in selector["generations"] or gen > selector["generations"][ns]:
                raise ResearchProjectError(
                    "generation_mismatch",
                    "finding revision lies outside selected generations",
                )
        questions, contradictions = {}, {}
        linked = {
            project["project_id"],
            *(
                link["id"]
                for link in project["links"]
                if link["kind"] in {"finding", "hypothesis"}
            ),
        }
        for ns, generation in selector["generations"].items():
            if "research_gap_revisions" in tables:
                if "operator" not in scopes and "knowledge:gaps:read" not in scopes:
                    raise ResearchProjectError(
                        "unauthorized", "current gap read access required"
                    )
                rows = self.conn.execute(
                    """SELECT g.gap_id,r.gap_revision_id,r.status,g.object_id,r.generation
                    FROM research_gap_revisions r JOIN research_gaps g ON g.gap_id=r.gap_id
                    JOIN derived_object_generations d ON d.namespace=r.namespace AND d.generation=r.generation AND d.status='committed'
                    WHERE r.namespace=? AND r.generation<=? AND r.created_at_ms<=? AND g.object_id IN (SELECT unnest(?))
                    QUALIFY row_number() OVER(PARTITION BY g.gap_id ORDER BY r.revision DESC)=1 LIMIT 1001""",
                    [ns, generation, run_completed_at, sorted(linked)],
                ).fetchall()
                if len(rows) > 1000:
                    raise ResearchProjectError(
                        "comparison_limit", "narrow the project gap scope"
                    )
                for identity, revision, status, obj, gen in rows:
                    questions[ns + ":" + identity] = {
                        "id": identity,
                        "namespace": ns,
                        "revision_id": revision,
                        "status": status,
                        "object_id": obj,
                        "generation": gen,
                    }
        # Relation findings retain their native predicate and lifecycle. No new
        # semantic contradiction classifier is run during comparison.
        for identity, finding in assessment["findings"].items():
            row = self.conn.execute(
                "SELECT object_type,CASE WHEN length(content_json)<=65536 THEN content_json ELSE NULL END,lifecycle FROM derived_object_revisions WHERE revision_id=?",
                [finding["revision_id"]],
            ).fetchone()
            content = json.loads(row[1]) if row[1] else {}
            finding["content"] = content
            finding["content_availability"] = (
                "available" if row[1] else "display_limit_exceeded"
            )
            if row[0] == "relation" and str(
                content.get("predicate") or content.get("relation")
            ).casefold() in {"contradicts", "contradiction", "refutes"}:
                contradictions[identity] = {
                    "reference": finding["reference"],
                    "revision_id": finding["revision_id"],
                    "status": content.get("status", row[2]),
                    "predicate": content.get("predicate") or content.get("relation"),
                    "supports": finding["supports"],
                }
        return {
            "selector": selector,
            "project": project,
            "generations": generations,
            "assessment": assessment,
            "questions": questions,
            "contradictions": contradictions,
        }

    def _side(self, namespace, selector, principal_id, scopes):
        side = self._resolve(namespace, selector, principal_id, scopes)
        row = self.conn.execute(
            "SELECT recipe_revision_id,input_hash,receipt_json,updated_at_ms FROM research_recipe_runs WHERE namespace=? AND run_id=?",
            [namespace, selector["run_id"]],
        ).fetchone()
        side["run"] = {
            "run_id": selector["run_id"],
            "recipe_revision_id": row[0],
            "input_hash": row[1],
            "receipt_sha256": _hash(json.loads(row[2]) if row[2] else None),
            "completed_at_ms": row[3],
        }
        lineage = self.conn.execute(
            "SELECT lineage_json FROM research_project_branches WHERE branch_id=?",
            [selector["project_id"]],
        ).fetchone()
        side["declared_changes"] = json.loads(lineage[0])["changes"] if lineage else {}
        side["run_fingerprint"] = _hash(side["run"])
        return side

    def create(self, namespace, request_key, left, right, *, principal_id, scopes):
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 1000:
            raise ResearchProjectError(
                "invalid_request", "bounded comparison request key required"
            )
        identity = (
            "run-comparison:" + _hash([namespace, principal_id, request_key])[:32]
        )
        digest = _hash([left, right])
        prior = self.conn.execute(
            "SELECT request_hash FROM investigation_comparisons WHERE comparison_id=?",
            [identity],
        ).fetchone()
        if prior:
            if prior[0] != digest:
                raise ResearchProjectError(
                    "idempotency_conflict", "comparison request key reused"
                )
            return self.inspect(
                namespace, identity, principal_id=principal_id, scopes=scopes
            )
        self.conn.execute("BEGIN")
        try:
            sides = [
                self._side(namespace, value, principal_id, scopes)
                for value in (left, right)
            ]
            for side in sides:
                self.projects._authorize(
                    side["project"], principal_id, scopes, write=True
                )
            a, b = sides
            compatible = (
                a["project"]["scope"] == b["project"]["scope"]
                and a["project"]["questions"] == b["project"]["questions"]
            )
            available = all(
                g["status"] == "available"
                for side in sides
                for g in side["generations"]
            )
            complete = available and all(
                side["assessment"]["complete"] for side in sides
            )

            def sources(side):
                grouped = {}
                for ref in side["assessment"]["sources"]:
                    grouped.setdefault(ref["document_id"], []).append(ref)
                return {
                    key: sorted(values, key=_json) for key, values in grouped.items()
                }

            changes = differences(a["assessment"], b["assessment"])
            if not complete:
                for change in changes:
                    if change["before"] is None or change["after"] is None:
                        change["kind"] = "unavailable_or_outside_coverage"
            artifact = {
                "contract": CONTRACT,
                "comparison_id": identity,
                "namespace": namespace,
                "owner": principal_id,
                "producer": "noesis-pinned-run-comparison-v1",
                "left": a,
                "right": b,
                "source_changes": _delta(sources(a), sources(b), complete=complete),
                "finding_changes": changes,
                "contradiction_changes": _delta(
                    a["contradictions"], b["contradictions"], complete=complete
                ),
                "question_changes": _delta(
                    a["questions"], b["questions"], complete=complete, states=True
                ),
                "scope_compatible": compatible,
                "coverage_comparable": compatible and complete,
                "coverage_equal": a["assessment"]["coverage"]
                == b["assessment"]["coverage"]
                if compatible and complete
                else None,
                "method_changed": a["run"]["recipe_revision_id"]
                != b["run"]["recipe_revision_id"],
                "declared_changes": {
                    "left": a["declared_changes"],
                    "right": b["declared_changes"],
                    "changed": a["declared_changes"] != b["declared_changes"],
                    "basis": "recorded branch declarations, not inferred causes",
                },
                "cost_difference": {
                    k: b["project"]["spent"][k] - a["project"]["spent"][k]
                    for k in a["project"]["spent"]
                },
                "cost_basis": "cumulative spending at selected project revisions; not isolated run billing",
                "winner": None,
                "limitations": [
                    "Only explicitly linked findings and related recorded gaps are compared",
                    "Missing historical state and unequal coverage are not evidence of improvement",
                    "No semantic contradiction classifier or causal explanation is inferred",
                    "Source currentness is observed at artifact creation; pinned references are not refreshed",
                ],
            }
            if len(_json(artifact).encode()) > 8 * 1024**2:
                raise ResearchProjectError(
                    "comparison_limit", "comparison exceeds 8 MiB"
                )
            self.conn.execute(
                "INSERT INTO investigation_comparisons VALUES (?,?,?,?,?)",
                [identity, namespace, principal_id, digest, _json(artifact)],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return artifact

    def inspect(self, namespace, comparison_id, *, principal_id, scopes):
        row = self.conn.execute(
            "SELECT owner,artifact_json FROM investigation_comparisons WHERE namespace=? AND comparison_id=?",
            [namespace, comparison_id],
        ).fetchone()
        if not row or row[0] != principal_id and "operator" not in scopes:
            raise ResearchProjectError(
                "comparison_unavailable", "owned comparison is unavailable"
            )
        artifact = json.loads(row[1])
        availability = []
        for key in ("left", "right"):
            stored = artifact[key]
            current = self._side(namespace, stored["selector"], principal_id, scopes)
            if current["run_fingerprint"] != stored["run_fingerprint"]:
                raise ResearchProjectError(
                    "run_changed",
                    "retained completed run no longer matches comparison input",
                )
            availability.append(
                {
                    "side": key,
                    "generations": current["generations"],
                    "omissions": current["assessment"]["omissions"],
                }
            )
        return {**artifact, "current_availability": availability}

    def export(self, namespace, comparison_id, *, principal_id, scopes):
        artifact = self.inspect(
            namespace, comparison_id, principal_id=principal_id, scopes=scopes
        )
        current_availability = artifact.pop("current_availability")

        # Reuse the report renderer without persisting an authored report or
        # inventing an evidence snapshot. Structured artifact remains attached.
        def describe(value):
            if value is None:
                return "Absent from the selected retained references"
            if isinstance(value, list):
                return "; ".join(describe(v) for v in value)
            content = value.get("content") or {}
            statement = content.get("statement") or content.get("text")
            if statement:
                return (
                    str(statement)
                    + " (revision "
                    + str(value.get("revision_id", "unknown"))
                    + ")"
                )
            if value.get("document_id"):
                return (
                    str(value["document_id"])
                    + " at "
                    + str(value.get("revision_id", "unknown"))
                )
            if value.get("status"):
                return (
                    str(
                        value.get("id")
                        or value.get("reference", {}).get("id", "record")
                    )
                    + ": "
                    + str(value["status"])
                )
            return (
                str(value.get("reference", {}).get("id", "finding"))
                + " at "
                + str(value.get("revision_id", "unavailable"))
            )

        sections = [
            {
                "id": "context",
                "title": "Scope and comparability",
                "assertions": [
                    {
                        "id": "context:1",
                        "kind": "commentary",
                        "dependencies": [],
                        "citations": [],
                        "text": "Scope compatible: "
                        + str(artifact["scope_compatible"])
                        + ". Coverage comparable: "
                        + str(artifact["coverage_comparable"])
                        + ". Method changed: "
                        + str(artifact["method_changed"])
                        + ". Cumulative project cost difference: "
                        + _json(artifact["cost_difference"])
                        + ". Declared changes: "
                        + _json(artifact["declared_changes"]),
                    }
                ],
            }
        ]
        for field in (
            "source_changes",
            "finding_changes",
            "contradiction_changes",
            "question_changes",
        ):
            sections.append(
                {
                    "id": field,
                    "title": field.replace("_", " ").title(),
                    "assertions": [
                        {
                            "id": field + ":" + str(i),
                            "text": change["kind"].replace("_", " ")
                            + ": "
                            + describe(change["before"])
                            + " → "
                            + describe(change["after"]),
                            "kind": "commentary",
                            "dependencies": [],
                            "citations": [],
                        }
                        for i, change in enumerate(artifact[field])
                    ],
                }
            )
        state = {
            "report_id": comparison_id,
            "revision": 1,
            "namespace": namespace,
            "owner": principal_id,
            "content": {
                "title": "Investigation run comparison",
                "sections": sections,
                "limitations": artifact["limitations"],
                "bibliography": [],
            },
        }
        from src.kb.authored_reports import render_export

        rendered = render_export(state)
        return {
            "contract": "noesis-investigation-comparison-export-v1",
            "markdown": rendered["markdown"],
            "limitations": artifact["limitations"],
            "comparison": artifact,
            "comparison_sha256": _hash(artifact),
            "current_availability": current_availability,
            "export_kind": "derived-comparison; not an authored evidence report",
        }
