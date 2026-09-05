"""Persistent, owner-scoped investigations linking existing research objects."""

from __future__ import annotations

import hashlib
import json
import time

CONTRACT = "noesis-research-project-v1"
READ_SCOPE = "knowledge:projects:read"
WRITE_SCOPE = "knowledge:projects:write"
_KINDS = {"plan", "run", "hypothesis", "evidence", "snapshot", "finding"}
_COSTS = {"tokens", "requests", "usd_micros"}
_DDL = """
CREATE TABLE IF NOT EXISTS research_projects(
 project_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 request_hash TEXT NOT NULL, revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS research_project_revisions(
 project_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, PRIMARY KEY(project_id,revision));
CREATE TABLE IF NOT EXISTS research_project_expenditures(
 project_id TEXT NOT NULL, receipt_id TEXT NOT NULL, content_json TEXT NOT NULL,
 PRIMARY KEY(project_id,receipt_id));
CREATE TABLE IF NOT EXISTS research_project_reservations(
 project_id TEXT NOT NULL,reservation_id TEXT NOT NULL,status TEXT NOT NULL,
 reserved_json TEXT NOT NULL,settled_json TEXT,created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(project_id,reservation_id));
"""


class ResearchProjectError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _strings(value, field, *, required=False):
    if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ResearchProjectError("invalid_project", f"{field} must be a bounded list of nonempty strings")
    if required and not value:
        raise ResearchProjectError("invalid_project", f"{field} cannot be empty")
    return list(dict.fromkeys(value))


def _cost(value):
    if not isinstance(value, dict) or set(value) - _COSTS or any(type(v) is not int or v < 0 for v in value.values()):
        raise ResearchProjectError("invalid_budget", "costs use nonnegative integer tokens, requests, and usd_micros")
    return {key: value.get(key, 0) for key in sorted(_COSTS)}


def _links(values):
    if not isinstance(values, list) or len(values) > 1000:
        raise ResearchProjectError("invalid_links", "at most 1000 stable references are allowed")
    result = []
    for link in values:
        if not isinstance(link, dict) or set(link) - {"kind", "id", "namespace", "generation", "revision", "locator", "question_revision"}:
            raise ResearchProjectError("invalid_links", "references accept stable identities and locators only; bearer tokens are not stored")
        if link.get("kind") not in _KINDS or not isinstance(link.get("id"), str) or not link["id"]:
            raise ResearchProjectError("invalid_links", "reference kind and id are required")
        if "namespace" in link and (not isinstance(link["namespace"], str) or not link["namespace"]):
            raise ResearchProjectError("invalid_links", "reference namespace must be a nonempty string")
        if link["kind"] in {"snapshot", "evidence"} and "generation" not in link and "revision" not in link:
            raise ResearchProjectError("invalid_links", "evidence and snapshot references require a revision or generation")
        for key in ("generation", "revision", "question_revision"):
            if key in link and (type(link[key]) is not int or link[key] < 0):
                raise ResearchProjectError("invalid_links", f"{key} must be a nonnegative integer")
        if "locator" in link and (not isinstance(link["locator"], dict) or set(link["locator"]) - {"document_id", "revision_id", "start", "end", "page", "section"}):
            raise ResearchProjectError("invalid_links", "unsupported evidence locator")
        for key, value in link.get("locator", {}).items():
            if key in {"start", "end", "page"}:
                valid = type(value) is int and value >= 0
            else:
                valid = isinstance(value, str)
            if not valid:
                raise ResearchProjectError("invalid_links", "locator fields must be text or nonnegative coordinates")
        if link not in result:
            result.append(json.loads(_json(link)))
    return result


class ResearchProjectStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(state, principal_id, scopes, *, write=False):
        required = WRITE_SCOPE if write else READ_SCOPE
        if not principal_id or required not in scopes and "operator" not in scopes:
            raise ResearchProjectError("unauthorized", f"{required} is required")
        if "operator" in scopes:
            return
        if state["owner"] != principal_id:
            raise ResearchProjectError("unauthorized", "project belongs to another principal")
        for namespace in {state["namespace"], *state["scope"]["namespaces"]}:
            if f"namespace:{namespace}:write" not in scopes and (write or f"namespace:{namespace}:read" not in scopes):
                raise ResearchProjectError("unauthorized", "current namespace access is required")
        for domain in state["scope"]["domains"]:
            if f"domain:{domain}:read" not in scopes:
                raise ResearchProjectError("unauthorized", "current domain access is required")

    def _state(self, namespace, project_id, *, revision=None):
        row = self.conn.execute("""SELECT r.content_json FROM research_projects p
            JOIN research_project_revisions r ON r.project_id=p.project_id
            WHERE p.project_id=? AND p.namespace=? AND r.revision=coalesce(?,p.revision)""",
            [project_id, namespace, revision]).fetchone()
        if not row:
            raise ResearchProjectError("project_not_found", "project revision is unavailable")
        return json.loads(row[0])

    def _append(self, state, expected_revision):
        changed = self.conn.execute("""UPDATE research_projects SET revision=revision+1
            WHERE project_id=? AND revision=? RETURNING revision""",
            [state["project_id"], expected_revision]).fetchone()
        if not changed:
            raise ResearchProjectError("revision_conflict", "project changed; inspect the current revision")
        state = {**state, "revision": int(changed[0]), "updated_at_ms": self.now()}
        self.conn.execute("INSERT INTO research_project_revisions VALUES (?,?,?,?)",
                          [state["project_id"], state["revision"], _json(state), state["updated_at_ms"]])
        return state

    def _abort(self, exc):
        import duckdb
        self.conn.execute("ROLLBACK")
        if isinstance(exc, (duckdb.TransactionException, duckdb.ConstraintException)):
            raise ResearchProjectError("revision_conflict", "concurrent project update; inspect and retry") from exc
        raise exc

    def create(self, namespace, request_key, *, questions, success_criteria, scope, budget,
               principal_id, scopes):
        if not isinstance(namespace, str) or not namespace or not request_key:
            raise ResearchProjectError("invalid_project", "namespace and request_key are required")
        if not isinstance(scope, dict) or set(scope) != {"domains", "namespaces"}:
            raise ResearchProjectError("invalid_project", "scope requires domains and namespaces")
        scope = {key: _strings(scope[key], key) for key in scope}
        if not scope["domains"] and not scope["namespaces"]:
            raise ResearchProjectError("invalid_project", "an explicit research scope is required")
        state = {"contract": CONTRACT, "project_id": "project:" + _hash([namespace, principal_id, request_key])[:32],
                 "namespace": namespace, "owner": principal_id, "scope": scope,
                 "questions": _strings(questions, "questions", required=True),
                 "success_criteria": _strings(success_criteria, "success_criteria", required=True),
                 "budget": _cost(budget), "spent": _cost({}), "links": [], "status": "active",
                 "question_revision": 1, "revision": 1, "retention_policy": "references-only"}
        self._authorize(state, principal_id, scopes, write=True)
        digest = _hash(state)
        prior = self.conn.execute("SELECT request_hash FROM research_projects WHERE project_id=?", [state["project_id"]]).fetchone()
        if prior:
            if prior[0] != digest:
                raise ResearchProjectError("idempotency_conflict", "request_key already identifies a different project request")
            current = self._state(namespace, state["project_id"])
            self._authorize(current, principal_id, scopes, write=True)
            return {**current, "idempotent": True}
        state["updated_at_ms"] = self.now()
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute("INSERT INTO research_projects VALUES (?,?,?,?,1)", [state["project_id"], namespace, principal_id, digest])
            self.conn.execute("INSERT INTO research_project_revisions VALUES (?,1,?,?)", [state["project_id"], _json(state), state["updated_at_ms"]])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return {**state, "idempotent": False}

    def inspect(self, namespace, project_id, *, principal_id, scopes, revision=None):
        current = self._state(namespace, project_id)
        self._authorize(current, principal_id, scopes)
        state = self._state(namespace, project_id, revision=revision) if revision is not None else current
        self._authorize(state, principal_id, scopes)
        availability = []
        for link in state["links"]:
            status = "not_checked"
            if link["kind"] == "snapshot":
                exists = self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='research_snapshot_sessions'").fetchone()
                row = self.conn.execute("SELECT status,expires_at_ms,principal_id FROM research_snapshot_sessions WHERE session_id=?", [link["id"]]).fetchone() if exists else None
                status = "unavailable" if not row else "inaccessible" if row[2] != principal_id and "operator" not in scopes else "expired" if row[1] <= self.now() else row[0]
            availability.append({"kind": link["kind"], "id": link["id"], "status": status, "generation_verified": False})
        return {**state, "reference_availability": availability}

    def list(self, namespace, *, principal_id, scopes, limit=50, offset=0):
        if READ_SCOPE not in scopes and "operator" not in scopes:
            raise ResearchProjectError("unauthorized", f"{READ_SCOPE} is required")
        # Filter by owner before paging so another owner's existence is not exposed.
        rows = self.conn.execute("""SELECT project_id FROM research_projects WHERE namespace=?
            AND (owner=? OR ?) ORDER BY project_id LIMIT ? OFFSET ?""",
            [namespace, principal_id, "operator" in scopes, min(max(int(limit), 1), 100), max(int(offset), 0)]).fetchall()
        result = []
        for (project_id,) in rows:
            try:
                result.append(self.inspect(namespace, project_id, principal_id=principal_id, scopes=scopes))
            except ResearchProjectError as exc:
                if exc.code != "unauthorized":
                    raise
        return {"projects": result}

    def revise(self, namespace, project_id, expected_revision, *, principal_id, scopes,
               questions=None, success_criteria=None, add_links=None, status=None, replace_links=None):
        self.conn.execute("BEGIN TRANSACTION")
        try:
            state = self._state(namespace, project_id)
            self._authorize(state, principal_id, scopes, write=True)
            if state["status"] == "archived":
                raise ResearchProjectError("project_archived", "archived projects are immutable")
            if questions is not None:
                state["questions"] = _strings(questions, "questions", required=True)
                state["question_revision"] += 1
            if success_criteria is not None:
                state["success_criteria"] = _strings(success_criteria, "success_criteria", required=True)
            if replace_links is not None:
                if add_links is not None:
                    raise ResearchProjectError("invalid_links", "choose replacement or addition of references")
                state["links"] = []
            for link in _links(replace_links if replace_links is not None else add_links or []):
                if link.get("namespace", namespace) not in {namespace, *state["scope"]["namespaces"]}:
                    raise ResearchProjectError("scope_mismatch", "reference is outside the project namespace scope")
                link.setdefault("question_revision", state["question_revision"])
                if link not in state["links"]:
                    state["links"].append(link)
            state["links"] = _links(state["links"])
            if status is not None:
                if status not in {"active", "paused", "complete", "archived"}:
                    raise ResearchProjectError("invalid_status", "unsupported project lifecycle state")
                state["status"] = status
            state = self._append(state, expected_revision)
            self.conn.execute("COMMIT")
            return state
        except Exception as exc:
            self._abort(exc)

    def record_expenditure(self, namespace, project_id, receipt_id, costs, expected_revision, *, principal_id, scopes):
        costs = _cost(costs)
        if not isinstance(receipt_id, str) or not receipt_id:
            raise ResearchProjectError("invalid_receipt", "stable execution receipt_id is required")
        self.conn.execute("BEGIN TRANSACTION")
        try:
            state = self._state(namespace, project_id)
            self._authorize(state, principal_id, scopes, write=True)
            prior = self.conn.execute("SELECT content_json FROM research_project_expenditures WHERE project_id=? AND receipt_id=?", [project_id, receipt_id]).fetchone()
            if prior:
                if json.loads(prior[0]) != costs:
                    raise ResearchProjectError("idempotency_conflict", "execution receipt already has different costs")
                self.conn.execute("COMMIT")
                return {**state, "idempotent": True}
            if state["status"] != "active":
                raise ResearchProjectError("project_inactive", "new expenditure requires an active project")
            reserved = self._reserved(project_id)
            for key, cost in costs.items():
                state["spent"][key] += cost
                if state["spent"][key] + reserved[key] > state["budget"][key]:
                    raise ResearchProjectError("budget_exceeded", f"{key} budget exceeded")
            self.conn.execute("INSERT INTO research_project_expenditures VALUES (?,?,?)", [project_id, receipt_id, _json(costs)])
            state = self._append(state, expected_revision)
            self.conn.execute("COMMIT")
            return {**state, "idempotent": False}
        except Exception as exc:
            self._abort(exc)

    def _reserved(self, project_id):
        total = _cost({})
        rows = self.conn.execute("SELECT reserved_json FROM research_project_reservations WHERE project_id=? AND status='held'", [project_id]).fetchall()
        for (encoded,) in rows:
            for key, value in json.loads(encoded).items():
                total[key] += value
        return total

    def reserve_budget(self, namespace, project_id, reservation_id, costs, *, principal_id, scopes):
        costs = _cost(costs)
        if not isinstance(reservation_id, str) or not reservation_id or len(reservation_id)>1000:
            raise ResearchProjectError('invalid_reservation', 'bounded stable reservation id required')
        self.conn.execute('BEGIN')
        try:
            state = self._state(namespace, project_id)
            self._authorize(state, principal_id, scopes, write=True)
            prior = self.conn.execute('SELECT status,reserved_json,settled_json FROM research_project_reservations WHERE project_id=? AND reservation_id=?', [project_id, reservation_id]).fetchone()
            if prior:
                if json.loads(prior[1]) != costs:
                    raise ResearchProjectError('idempotency_conflict', 'reservation already has different limits')
                self.conn.execute('COMMIT')
                return {'reservation_id': reservation_id, 'status': prior[0], 'reserved': costs, 'idempotent': True}
            if state['status'] != 'active':
                raise ResearchProjectError('project_inactive', 'budget reservation requires an active project')
            held = self._reserved(project_id)
            if any(state['spent'][key]+held[key]+costs[key] > state['budget'][key] for key in costs):
                raise ResearchProjectError('budget_exceeded', 'project spending plus in-flight reservations exceeds its budget')
            # Touch the shared project revision so concurrent reservations for
            # different keys conflict instead of both reading the same balance.
            self._append(state, state['revision'])
            self.conn.execute("INSERT INTO research_project_reservations VALUES (?,?,'held',?,NULL,?)", [project_id, reservation_id, _json(costs), self.now()])
            self.conn.execute('COMMIT')
            return {'reservation_id': reservation_id, 'status': 'held', 'reserved': costs, 'idempotent': False}
        except Exception as exc:
            self._abort(exc)

    def settle_budget(self, namespace, project_id, reservation_id, costs, *, principal_id, scopes):
        # Unknown provider usage must retain the reservation or conservatively
        # settle its full ceiling. It must never silently release in-flight work.
        costs = _cost(costs)
        self.conn.execute('BEGIN')
        try:
            state = self._state(namespace, project_id)
            self._authorize(state, principal_id, scopes, write=True)
            row = self.conn.execute('SELECT status,reserved_json,settled_json FROM research_project_reservations WHERE project_id=? AND reservation_id=?', [project_id, reservation_id]).fetchone()
            if not row:
                raise ResearchProjectError('reservation_unavailable', 'budget reservation is unavailable')
            if row[0] == 'settled':
                if json.loads(row[2]) != costs:
                    raise ResearchProjectError('idempotency_conflict', 'reservation already settled with different costs')
                self.conn.execute('COMMIT')
                return {'reservation_id': reservation_id, 'status': 'settled', 'costs': costs, 'idempotent': True}
            reserved = json.loads(row[1])
            if any(costs[key]>reserved[key] for key in costs):
                raise ResearchProjectError('reservation_exceeded', 'actual usage exceeds its reserved ceiling')
            for key, value in costs.items():
                state['spent'][key] += value
            # Settlement remains allowed after pause/archive: usage already
            # incurred must be accounted for without authorizing new work.
            self._append(state, state['revision'])
            self.conn.execute('INSERT INTO research_project_expenditures VALUES (?,?,?)', [project_id, 'reservation:'+reservation_id, _json(costs)])
            self.conn.execute("UPDATE research_project_reservations SET status='settled',settled_json=? WHERE project_id=? AND reservation_id=?", [_json(costs), project_id, reservation_id])
            self.conn.execute('COMMIT')
            return {'reservation_id': reservation_id, 'status': 'settled', 'costs': costs, 'idempotent': False}
        except Exception as exc:
            self._abort(exc)

    def inspect_budget(self, namespace, project_id, *, principal_id, scopes):
        state = self.inspect(namespace, project_id, principal_id=principal_id, scopes=scopes)
        held = self._reserved(project_id)
        return {'project_id': project_id, 'budget': state['budget'], 'spent': state['spent'], 'reserved': held,
            'available': {key: state['budget'][key]-state['spent'][key]-held[key] for key in held}}
