"""Access-checked task and environment search for versioned intake playbooks."""

from __future__ import annotations

import json
import re
from typing import Any

from src.kb.intake_modes import IntakeError, IntakeStore, _text
from src.kb.intake_playbooks import IntakePlaybookStore

CONTRACT = "noesis-intake-playbook-search-v1"


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", value.casefold()))


class IntakePlaybookSearch:
    def __init__(self, conn: Any):
        self.conn = conn

    def search(
        self, namespace: str, task: str, *, environment: str | None = None,
        limit: int = 20, principal_id: str, scopes: set[str],
    ) -> dict:
        task = _text(task, "task", limit=500)
        if environment is not None:
            environment = _text(environment, "environment", limit=500)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise IntakeError("invalid_limit", "limit must be 1–50")
        IntakeStore._authorize(
            {"namespace": namespace, "owner": principal_id}, principal_id, scopes,
        )
        exists = self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='main' "
            "AND table_name='intake_playbooks'",
        ).fetchone()
        if not exists:
            return {"contract": CONTRACT, "namespace": namespace, "task": task,
                    "environment": environment, "matches": [], "scanned": 0,
                    "limited": False, "ranking_basis": "lexical_overlap_only"}
        rows = self.conn.execute(
            "SELECT playbook_id,content_json FROM intake_playbooks "
            "WHERE namespace=? AND owner=? ORDER BY playbook_id LIMIT 501",
            [namespace, principal_id],
        ).fetchall()
        query = _tokens(task)
        if not query:
            raise IntakeError("invalid_query", "task needs a searchable word")
        matches = []
        for playbook_id, raw in rows[:500]:
            playbook = json.loads(raw)
            try:
                IntakePlaybookStore._authorize(playbook, principal_id, scopes)
            except IntakeError:
                continue
            text = " ".join([
                playbook.get("title", ""), playbook.get("source_rationale", ""),
                *[step.get("action", "") for step in playbook.get("steps", [])],
            ])
            overlap = len(query & _tokens(text))
            if not overlap:
                continue
            env = playbook.get("environment", "")
            env_match = environment is None or bool(_tokens(environment) & _tokens(env))
            if not env_match:
                continue
            matches.append({
                "playbook_id": playbook_id, "revision": playbook["revision"],
                "title": playbook["title"], "environment": env,
                "artifact_kind": playbook.get("artifact_kind", "playbook"),
                "trust_state": playbook.get("trust_state", "draft"),
                "concept_references": [ref for ref in playbook.get("references", [])
                                       if ref.get("kind") == "concept"],
                "relevance_terms": overlap,
            })
        matches.sort(key=lambda item: (-item["relevance_terms"], item["title"], item["playbook_id"]))
        return {"contract": CONTRACT, "namespace": namespace, "task": task,
                "environment": environment, "matches": matches[:limit],
                "scanned": min(len(rows), 500), "limited": len(rows) > 500 or len(matches) > limit,
                "ranking_basis": "lexical_overlap_only"}
