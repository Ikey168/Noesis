"""Versioned skills that gather dated practice and procedure evidence.

A skill is a stable, owner-scoped identity (``skill:<hash>``) with mastery
criteria and links to the practice cards and procedures that exercise it. The
evidence view is derived at read time from the authoritative stores and keeps
four bases separate:

* ``self_reported_unaided_recall`` – an assessed practice review the user
  marked unaided and passed;
* ``caller_reported_rehearsal`` / ``adapter_executed_procedure`` – the linked
  procedure's trust evidence (procedural execution, not unaided mastery);
* ``independent_assessment`` – a recorded assessment by a reviewer other than
  the owner, holding ``knowledge:intake:review``.

``mastery_demonstrated`` is true only when every mastery criterion has a
passing independent assessment of the current skill content (criteria and
links); revising them invalidates earlier assessments. Self-reports,
counts and procedure runs never establish it.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.kb.intake_modes import IntakeError, _bounded, _hash, _json, _plugin_links, _text

CONTRACT = "noesis-intake-skill-v1"
EVIDENCE_CONTRACT = "noesis-intake-skill-evidence-v1"
READ_SCOPE, WRITE_SCOPE, REVIEW_SCOPE = "knowledge:intake:read", "knowledge:intake:write", "knowledge:intake:review"
_DDL = """
CREATE TABLE IF NOT EXISTS intake_skills(
 skill_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 request_hash TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intake_skill_revisions(
 skill_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 PRIMARY KEY(skill_id, revision));
CREATE TABLE IF NOT EXISTS intake_skill_commands(
 skill_id TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 revision BIGINT NOT NULL, PRIMARY KEY(skill_id, command_key));
"""


def _links(practice: Any, procedures: Any) -> tuple[list[dict], list[dict]]:
    if not isinstance(practice, list) or not isinstance(procedures, list) or len(practice) + len(procedures) > 100:
        raise IntakeError("invalid_skill", "link at most 100 practice cards and procedures")
    cards, seen = [], set()
    for item in practice:
        if not isinstance(item, dict) or set(item) != {"pack_id", "card_id"}:
            raise IntakeError("invalid_skill", "practice links need pack_id and card_id")
        key = (_text(item["pack_id"], "pack_id", limit=200), _text(item["card_id"], "card_id", limit=100))
        if key not in seen:
            seen.add(key)
            cards.append({"pack_id": key[0], "card_id": key[1]})
    playbooks = []
    for item in procedures:
        if not isinstance(item, dict) or set(item) != {"playbook_id"}:
            raise IntakeError("invalid_skill", "procedure links need playbook_id")
        entry = {"playbook_id": _text(item["playbook_id"], "playbook_id", limit=200)}
        if entry not in playbooks:
            playbooks.append(entry)
    return cards, playbooks


class IntakeSkillStore:
    def __init__(self, conn: Any, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(state: dict, principal_id: str, scopes: set[str], *, write=False, review=False) -> None:
        if "operator" in scopes:
            return
        namespace = state["namespace"]
        if review:
            if REVIEW_SCOPE not in scopes or f"namespace:{namespace}:read" not in scopes and f"namespace:{namespace}:write" not in scopes:
                raise IntakeError("unauthorized", "intake review scope and namespace access are required")
            return
        needed = WRITE_SCOPE if write else READ_SCOPE
        namespace_scope = {f"namespace:{namespace}:write"} if write else {
            f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
        if state["owner"] != principal_id or needed not in scopes or not namespace_scope & scopes:
            raise IntakeError("unauthorized", "current skill ownership and scopes are required")

    def _state(self, namespace: str, skill_id: str, revision: int | None = None) -> dict:
        if revision is None:
            row = self.conn.execute(
                "SELECT content_json FROM intake_skills WHERE namespace=? AND skill_id=?", [namespace, skill_id]
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT r.content_json FROM intake_skill_revisions r JOIN intake_skills s USING(skill_id) "
                "WHERE s.namespace=? AND r.skill_id=? AND r.revision=?", [namespace, skill_id, revision]
            ).fetchone()
        if not row:
            raise IntakeError("skill_not_found", "skill revision is unavailable")
        return json.loads(row[0])

    def create(
        self, namespace: str, request_key: str, *, name: str, description: str,
        mastery_criteria: list[str], practice_links: list[dict], procedure_links: list[dict],
        plugin_links: list[dict] | None = None, principal_id: str, scopes: set[str],
    ) -> dict:
        namespace = _text(namespace, "namespace", limit=128)
        request_key = _text(request_key, "request_key", limit=256)
        if not isinstance(mastery_criteria, list) or not 1 <= len(mastery_criteria) <= 20:
            raise IntakeError("invalid_skill", "declare one to 20 mastery criteria")
        criteria = [_text(item, "mastery criterion", limit=1000) for item in mastery_criteria]
        if len(set(criteria)) != len(criteria):
            raise IntakeError("invalid_skill", "mastery criteria must be unique")
        cards, playbooks = _links(practice_links, procedure_links)
        content = {
            "contract": CONTRACT,
            "skill_id": "skill:" + _hash([namespace, principal_id, request_key])[:32],
            "namespace": namespace, "owner": principal_id, "revision": 1,
            "name": _text(name, "name", limit=300), "description": _text(description, "description", limit=5000),
            "mastery_criteria": criteria, "practice_links": cards, "procedure_links": playbooks,
            "content_revision": 1, "assessments": [],
        }
        links = _plugin_links(plugin_links or [], namespace, scopes)
        if links:
            content["plugin_links"] = links
        self._authorize(content, principal_id, scopes, write=True)
        self._check_links(content, principal_id, scopes)
        _bounded(content)
        digest = _hash(content)
        prior = self.conn.execute("SELECT request_hash FROM intake_skills WHERE skill_id=?", [content["skill_id"]]).fetchone()
        if prior:
            if prior[0] != digest:
                raise IntakeError("idempotency_conflict", "request_key identifies another skill")
            current = self._state(namespace, content["skill_id"])
            self._authorize(current, principal_id, scopes)
            return {**current, "idempotent": True}
        content["created_at_ms"] = content["updated_at_ms"] = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT INTO intake_skills VALUES (?,?,?,?,?,?)",
                              [content["skill_id"], namespace, principal_id, digest, 1, _json(content)])
            self.conn.execute("INSERT INTO intake_skill_revisions VALUES (?,?,?)", [content["skill_id"], 1, _json(content)])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**content, "idempotent": False}

    def _check_links(self, skill: dict, principal_id: str, scopes: set[str]) -> None:
        from src.kb.intake_playbooks import IntakePlaybookStore
        from src.kb.intake_practice import IntakePracticeStore

        practice = IntakePracticeStore(self.conn, now=self.now)
        for link in skill["practice_links"]:
            pack = practice.inspect_pack(skill["namespace"], link["pack_id"], principal_id=principal_id, scopes=scopes)
            if link["card_id"] not in {card["id"] for card in pack["cards"]}:
                raise IntakeError("invalid_skill", "linked practice card is not in the pack")
        playbooks = IntakePlaybookStore(self.conn, now=self.now)
        for link in skill["procedure_links"]:
            playbooks.inspect(skill["namespace"], link["playbook_id"], principal_id=principal_id, scopes=scopes)

    def _append(self, namespace: str, skill_id: str, command_key: str, expected_revision: int,
                digest: str, mutate, principal_id: str, scopes: set[str], *, review: bool = False) -> dict:
        command_key = _text(command_key, "command_key", limit=256)
        self.conn.execute("BEGIN")
        try:
            state = self._state(namespace, skill_id)
            self._authorize(state, principal_id, scopes, write=not review, review=review)
            replay = self.conn.execute(
                "SELECT request_hash,revision FROM intake_skill_commands WHERE skill_id=? AND command_key=?",
                [skill_id, command_key]).fetchone()
            if replay:
                if replay[0] != digest:
                    raise IntakeError("idempotency_conflict", "command_key identifies another skill change")
                value = self._state(namespace, skill_id, int(replay[1]))
                self.conn.execute("COMMIT")
                return {**value, "idempotent": True}
            if state["revision"] != expected_revision:
                raise IntakeError("revision_conflict", "skill changed; inspect before retry")
            mutate(state)
            state["revision"] += 1
            state["updated_at_ms"] = self.now()
            _bounded(state)
            if not self.conn.execute(
                "UPDATE intake_skills SET revision=?,content_json=? WHERE skill_id=? AND revision=? RETURNING revision",
                [state["revision"], _json(state), skill_id, expected_revision]).fetchone():
                raise IntakeError("revision_conflict", "concurrent skill update")
            self.conn.execute("INSERT INTO intake_skill_revisions VALUES (?,?,?)", [skill_id, state["revision"], _json(state)])
            self.conn.execute("INSERT INTO intake_skill_commands VALUES (?,?,?,?)", [skill_id, command_key, digest, state["revision"]])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**state, "idempotent": False}

    def revise(self, namespace: str, skill_id: str, command_key: str, *, expected_revision: int,
               mastery_criteria: list[str], practice_links: list[dict], procedure_links: list[dict],
               principal_id: str, scopes: set[str]) -> dict:
        criteria = [_text(item, "mastery criterion", limit=1000) for item in mastery_criteria or []]
        if not 1 <= len(criteria) <= 20 or len(set(criteria)) != len(criteria):
            raise IntakeError("invalid_skill", "declare one to 20 unique mastery criteria")
        cards, playbooks = _links(practice_links, procedure_links)
        self._check_links({"namespace": namespace, "practice_links": cards, "procedure_links": playbooks},
                          principal_id, scopes)

        def mutate(state: dict) -> None:
            # Assessments stay in history but apply only to the content they judged.
            state.update(mastery_criteria=criteria, practice_links=cards, procedure_links=playbooks,
                         content_revision=state["revision"] + 1)

        return self._append(namespace, skill_id, command_key, expected_revision,
                            _hash(["revise", criteria, cards, playbooks]), mutate, principal_id, scopes)

    def record_assessment(self, namespace: str, skill_id: str, command_key: str, *, expected_revision: int,
                          criterion: str, passed: bool, evidence_note: str,
                          principal_id: str, scopes: set[str]) -> dict:
        """Record an independent reviewer's judgement of one mastery criterion."""

        if type(passed) is not bool:
            raise IntakeError("invalid_input", "passed must be boolean")
        note = _text(evidence_note, "evidence note", limit=3000)
        current = self._state(namespace, skill_id)
        if current["owner"] == principal_id:
            raise IntakeError("self_assessment", "an owner cannot independently assess their own skill")
        if criterion not in current["mastery_criteria"]:
            raise IntakeError("invalid_skill", "assess a declared mastery criterion")

        def mutate(state: dict) -> None:
            state["assessments"] = [*state["assessments"], {
                "criterion": criterion, "passed": passed, "note": note, "assessor": principal_id,
                "content_revision": state["content_revision"], "at_ms": self.now(),
                "basis": "independent_assessment",
            }][-200:]

        return self._append(namespace, skill_id, command_key, expected_revision,
                            _hash(["assess", criterion, passed, note]), mutate, principal_id, scopes, review=True)

    def evidence(self, namespace: str, skill_id: str, *, principal_id: str, scopes: set[str]) -> dict:
        from src.kb.intake_playbooks import IntakePlaybookStore

        skill = self._state(namespace, skill_id)
        self._authorize(skill, principal_id, scopes)
        items: list[dict] = []
        limits: list[str] = []
        wanted = {(link["pack_id"], link["card_id"]) for link in skill["practice_links"]}
        if wanted and self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='intake_practice_reviews'").fetchone():
            rows = self.conn.execute(
                "SELECT review_id,content_json FROM intake_practice_reviews WHERE namespace=? AND owner=? "
                "ORDER BY review_id LIMIT 2001", [namespace, skill["owner"]]).fetchall()
            if len(rows) > 2000:
                limits.append("Practice review scan was limited to 2000 reviews")
            for review_id, raw in rows[:2000]:
                review = json.loads(raw)
                if (review.get("pack_id"), review.get("card_id")) not in wanted or review.get("status") != "assessed":
                    continue
                assessment = review.get("assessment") or {}
                items.append({
                    "kind": "practice_review", "id": review_id, "pack_id": review["pack_id"],
                    "card_id": review["card_id"], "passed": assessment.get("reported_passed"),
                    "at_ms": assessment.get("at_ms"),
                    "basis": "self_reported_unaided_recall" if review.get("assistance") == "reported_unaided"
                    else "self_reported_assisted_recall",
                })
        playbooks = IntakePlaybookStore(self.conn, initialize=False, now=self.now)
        for link in skill["procedure_links"]:
            try:
                playbook = playbooks.inspect(namespace, link["playbook_id"], principal_id=principal_id, scopes=scopes)
            except IntakeError:
                limits.append(f"Procedure {link['playbook_id']} is not accessible")
                continue
            trust = playbook.get("trust_evidence")
            if trust:
                items.append({
                    "kind": "procedure_run", "id": trust["run_id"], "playbook_id": link["playbook_id"],
                    "passed": True, "at_ms": trust["verified_at_ms"], "environment": trust["environment"],
                    "basis": "adapter_executed_procedure" if playbook["trust_state"] == "executed_verified"
                    else "caller_reported_rehearsal",
                })
        current = [item for item in skill["assessments"]
                   if item["content_revision"] == skill["content_revision"]]
        latest = {}
        for item in current:
            latest[item["criterion"]] = item
        items.extend({"kind": "assessment", **item} for item in skill["assessments"])
        items.sort(key=lambda item: (item.get("at_ms") or 0, item["kind"]))
        mastery = bool(skill["mastery_criteria"]) and all(
            latest.get(criterion, {}).get("passed") is True for criterion in skill["mastery_criteria"])
        return {
            "contract": EVIDENCE_CONTRACT, "skill_id": skill_id, "skill_revision": skill["revision"],
            "content_revision": skill["content_revision"],
            "evidence": items,
            "criteria": [{"criterion": criterion,
                          "independent_assessment": latest.get(criterion, {}).get("passed")}
                         for criterion in skill["mastery_criteria"]],
            "mastery_demonstrated": mastery,
            "limitations": [
                "Self-reported recall and procedure runs are recorded evidence, not demonstrated mastery.",
                *limits,
            ],
        }

    def inspect(self, namespace: str, skill_id: str, *, principal_id: str, scopes: set[str],
                revision: int | None = None) -> dict:
        current = self._state(namespace, skill_id)
        self._authorize(current, principal_id, scopes)
        return current if revision is None else self._state(namespace, skill_id, revision)


__all__ = ["IntakeSkillStore", "CONTRACT", "EVIDENCE_CONTRACT"]
