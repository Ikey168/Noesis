"""Reference-preserving project branches and explicit baseline comparisons."""

import json

from src.kb.research_projects import ResearchProjectStore, ResearchProjectError, _hash, _json, _cost, _strings

_DDL = """CREATE TABLE IF NOT EXISTS research_project_branches(
 branch_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, lineage_json TEXT NOT NULL)"""


class ProjectBranchStore(ResearchProjectStore):
    def __init__(self, conn, **kwargs):
        super().__init__(conn, **kwargs)
        if kwargs.get("initialize", True):
            conn.execute(_DDL)

    def _generations(self, baseline, state, principal_id, scopes):
        if not isinstance(baseline, dict) or not baseline or len(baseline) > 100:
            raise ResearchProjectError("invalid_baseline", "pin one to 100 namespace generations")
        availability = []
        exists = self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='derived_object_generations'").fetchone()
        for namespace, generation in sorted(baseline.items()):
            if namespace not in {state["namespace"], *state["scope"]["namespaces"]}:
                raise ResearchProjectError("scope_mismatch", "baseline is outside the project scope")
            if type(generation) is not int or generation < 1:
                raise ResearchProjectError("invalid_baseline", "generation must be a positive integer")
            row = self.conn.execute("SELECT status,input_hash,change_hash FROM derived_object_generations WHERE namespace=? AND generation=?",
                                    [namespace, generation]).fetchone() if exists else None
            availability.append({"namespace": namespace, "generation": generation,
                "status": "available" if row and row[0] == "committed" else "unavailable",
                "fingerprint": _hash(list(row[1:])) if row and row[0] == "committed" else None})
        return availability

    def branch(self, namespace, project_id, revision, request_key, *, baseline, changes,
               budget, principal_id, scopes):
        parent = self.inspect(namespace, project_id, revision=revision, principal_id=principal_id, scopes=scopes)
        self._authorize(parent, principal_id, scopes, write=True)
        if not isinstance(request_key, str) or not request_key:
            raise ResearchProjectError("invalid_request", "a branch request key is required")
        if not isinstance(changes, dict) or set(changes) - {"questions", "methods", "sources", "assumptions"}:
            raise ResearchProjectError("invalid_changes", "declare questions, methods, sources, or assumptions")
        changes = {key: _strings(value, key, required=key == "questions") for key, value in changes.items()}
        availability = self._generations(baseline, parent, principal_id, scopes)
        branch_id = "project:" + _hash([namespace, principal_id, project_id, request_key])[:32]
        request = {"parent_id": project_id, "parent_revision": revision, "baseline": baseline,
                   "changes": changes, "budget": _cost(budget)}
        digest = _hash(request)
        prior = self.conn.execute("SELECT request_hash,lineage_json FROM research_project_branches WHERE branch_id=?", [branch_id]).fetchone()
        if prior:
            if prior[0] != digest:
                raise ResearchProjectError("idempotency_conflict", "branch key already identifies a different request")
            state = self.inspect(namespace, branch_id, principal_id=principal_id, scopes=scopes)
            return {"project": state, "lineage": json.loads(prior[1]), "baseline_availability": availability, "idempotent": True}
        if any(item["status"] != "available" for item in availability):
            raise ResearchProjectError("baseline_unavailable", "all pinned generations must be committed and retained")
        state = {key: value for key, value in parent.items() if key not in {"reference_availability", "idempotent"}}
        state.update(project_id=branch_id, owner=principal_id, revision=1, status="active",
                     budget=_cost(budget), spent=_cost({}), updated_at_ms=self.now())
        if "questions" in changes:
            state["questions"] = changes["questions"]
            state["question_revision"] += 1
        lineage = {**request, "baseline_fingerprints": availability,
                   "inherited_spending": parent["spent"], "retention_policy": "references-only"}
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute("INSERT INTO research_projects VALUES (?,?,?,?,1)", [branch_id, namespace, principal_id, digest])
            self.conn.execute("INSERT INTO research_project_revisions VALUES (?,1,?,?)", [branch_id, _json(state), state["updated_at_ms"]])
            self.conn.execute("INSERT INTO research_project_branches VALUES (?,?,?)", [branch_id, digest, _json(lineage)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return {"project": state, "lineage": lineage, "baseline_availability": availability, "idempotent": False}

    def compare(self, namespace, left_id, right_id, *, principal_id, scopes):
        states = [self.inspect(namespace, pid, principal_id=principal_id, scopes=scopes) for pid in (left_id, right_id)]
        lineages = []
        for pid in (left_id, right_id):
            row = self.conn.execute("SELECT lineage_json FROM research_project_branches WHERE branch_id=?", [pid]).fetchone()
            lineages.append(json.loads(row[0]) if row else None)
        lineage = next((item for item in lineages if item), None)
        if not lineage:
            raise ResearchProjectError("incompatible_baseline", "at least one project must have explicit branch lineage")
        key = lambda value: (value["parent_id"], value["parent_revision"], _json(value["baseline"]))
        for pid, item in zip((left_id, right_id), lineages):
            if item and key(item) != key(lineage) or item is None and pid != lineage["parent_id"]:
                raise ResearchProjectError("incompatible_baseline", "projects do not share an explicit baseline")
        base = self.inspect(namespace, lineage["parent_id"], revision=lineage["parent_revision"], principal_id=principal_id, scopes=scopes)
        availability = self._generations(lineage["baseline"], base, principal_id, scopes)
        expected = {(v["namespace"], v["generation"]): v["fingerprint"] for v in lineage["baseline_fingerprints"]}
        for item in availability:
            if item["status"] == "available" and item["fingerprint"] != expected[(item["namespace"], item["generation"])]:
                item["status"] = "changed"
        def delta(state, branch):
            identity = lambda link: (link["kind"], link.get("namespace", namespace), link["id"])
            before = {identity(link): link for link in base["links"]}
            after = {identity(link): link for link in state["links"]}
            added = [after[key] for key in sorted(after.keys() - before.keys())]
            removed = [before[key] for key in sorted(before.keys() - after.keys())]
            revised = [{"before": before[key], "after": after[key]} for key in sorted(before.keys() & after.keys()) if before[key] != after[key]]
            return {"project_id": state["project_id"], "revision": state["revision"], "added": added,
                    "removed": removed, "revised": revised,
                    "declared_changes": branch["changes"] if branch else {},
                    "questions_changed": state["questions"] != base["questions"],
                    "incremental_costs": state["spent"] if branch else {k: state["spent"][k] - base["spent"][k] for k in state["spent"]},
                    "reference_availability": state["reference_availability"]}
        deltas = [delta(state, branch) for state, branch in zip(states, lineages)]
        from src.kb.project_comparison import assess, differences
        baseline_assessment = assess(self.conn, base, scopes)
        assessments = [assess(self.conn, state, scopes) for state in states]
        for item, assessment in zip(deltas, assessments):
            item['assessment'] = assessment
            item['finding_changes_from_baseline'] = differences(baseline_assessment, assessment)
        comparable = all(item['status'] == 'available' for item in availability) and all(a['complete'] for a in [baseline_assessment, *assessments]) and states[0]['questions'] == states[1]['questions']
        evidence_sets = [{_json(link) for link in state["links"] if link["kind"] == "evidence"} for state in states]
        return {"baseline": {"project_id": lineage["parent_id"], "revision": lineage["parent_revision"], "generations": lineage["baseline"]},
                "baseline_availability": availability, "left": deltas[0], "right": deltas[1],
                "evidence_references_equal": evidence_sets[0] == evidence_sets[1],
                "baseline_assessment": baseline_assessment,
                "finding_differences": differences(*assessments),
                "coverage_comparable": comparable,
                "coverage_equal": assessments[0]['coverage'] == assessments[1]['coverage'] if comparable else None,
                "winner": None,
                "limitations": ["Coverage measures explicitly linked retained evidence, not all research or verified source independence",
                    "Finding hashes distinguish recorded changes; semantic entailment and scientific validity are not certified",
                    "Declared method, source and assumption changes are author statements",
                    "References-only retention does not pin or reconstruct expired evidence"]}
