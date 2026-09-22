"""Project-pinned decision history and bounded declared-weight sensitivity."""

import json
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext

from src.kb.research_projects import ResearchProjectStore, _hash, _json, _links, _strings

READ_SCOPE = "knowledge:decisions:read"
WRITE_SCOPE = "knowledge:decisions:write"
_DDL = """
CREATE TABLE IF NOT EXISTS research_decisions(
 decision_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 request_hash TEXT NOT NULL,revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS research_decision_revisions(
 decision_id TEXT NOT NULL,revision BIGINT NOT NULL,content_json TEXT NOT NULL,
 PRIMARY KEY(decision_id,revision));
CREATE TABLE IF NOT EXISTS decision_sensitivity_receipts(
 receipt_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL,content_json TEXT NOT NULL);
"""


class DecisionError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 10000:
        raise DecisionError("invalid_decision", "nonempty text within 10000 characters is required")
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise DecisionError("invalid_input", "quantitative inputs must be explicit decimal values")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > Decimal("1e100") or number.as_tuple().exponent < -100:
            raise InvalidOperation
        return number
    except InvalidOperation:
        raise DecisionError("invalid_input", "quantitative input exceeds finite decimal bounds") from None


def _content(content):
    fields = {"project", "options", "constraints", "assumptions", "observations", "preferences", "selected_action", "rationale", "review_conditions"}
    if not isinstance(content, dict) or set(content) != fields:
        raise DecisionError("invalid_decision", "explicit project, options, constraints, assumptions, observations, preferences, action, rationale and review conditions are required")
    project = content["project"]
    if not isinstance(project, dict) or set(project) != {"id", "namespace", "revision"} or type(project["revision"]) is not int or project["revision"] < 1:
        raise DecisionError("invalid_decision", "project requires an explicit stable id, namespace and revision")
    _text(project["id"])
    _text(project["namespace"])
    options = content["options"]
    if not isinstance(options, list) or not 2 <= len(options) <= 100:
        raise DecisionError("invalid_decision", "record two to 100 alternatives")
    ids = set()
    for option in options:
        if not isinstance(option, dict) or set(option) != {"id", "description"}:
            raise DecisionError("invalid_decision", "options require id and description")
        _text(option["id"])
        _text(option["description"])
        if option["id"] in ids:
            raise DecisionError("invalid_decision", "option ids must be unique")
        ids.add(option["id"])
    if content["selected_action"] not in ids:
        raise DecisionError("invalid_decision", "selected action must identify an explicit alternative")
    _text(content["rationale"])
    for field in ("constraints", "assumptions", "preferences", "review_conditions"):
        _strings(content[field], field)
    _links(content["observations"])
    if any(link["kind"] != "evidence" for link in content["observations"]):
        raise DecisionError("invalid_decision", "observations require revisioned evidence references")
    if len(_json(content).encode()) > 4 * 1024 * 1024:
        raise DecisionError("invalid_decision", "decision exceeds 4 MiB")
    return json.loads(_json(content))


class DecisionStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _authorize(self, state, principal_id, scopes, *, write=False):
        if not principal_id or "operator" not in scopes and ((WRITE_SCOPE if write else READ_SCOPE) not in scopes or state["owner"] != principal_id):
            raise DecisionError("unauthorized", "current decision scope and ownership are required")
        ns = state["namespace"]
        if "operator" not in scopes and f"namespace:{ns}:write" not in scopes and (write or f"namespace:{ns}:read" not in scopes):
            raise DecisionError("unauthorized", "current decision namespace access is required")
        project = state["content"]["project"]
        baseline = ResearchProjectStore(self.conn, initialize=False).inspect(project["namespace"], project["id"], revision=project["revision"], principal_id=principal_id, scopes=scopes)
        for link in state["content"]["observations"]:
            ns = link.get("namespace", state["namespace"])
            if ns not in {baseline["namespace"], *baseline["scope"]["namespaces"]}:
                raise DecisionError("scope_mismatch", "observation is outside the pinned project's namespace scope")
        return baseline

    def _state(self, namespace, decision_id, revision=None):
        row = self.conn.execute("""SELECT r.content_json FROM research_decisions d JOIN research_decision_revisions r
            ON r.decision_id=d.decision_id WHERE d.decision_id=? AND d.namespace=? AND r.revision=coalesce(?,d.revision)""",
            [decision_id, namespace, revision]).fetchone()
        if not row:
            raise DecisionError("decision_unavailable", "decision revision is unavailable")
        return json.loads(row[0])

    def inspect(self, namespace, decision_id, *, principal_id, scopes, revision=None):
        current = self._state(namespace, decision_id)
        self._authorize(current, principal_id, scopes)
        state = self._state(namespace, decision_id, revision) if revision is not None else current
        self._authorize(state, principal_id, scopes)
        return state

    def _abort(self, exc):
        import duckdb
        self.conn.execute("ROLLBACK")
        if isinstance(exc, (duckdb.TransactionException, duckdb.ConstraintException)):
            raise DecisionError("revision_conflict", "concurrent decision update; inspect and retry") from exc
        raise exc

    def create(self, namespace, request_key, content, *, principal_id, scopes):
        state = {"contract": "noesis-decision-v1", "decision_id": "decision:" + _hash([_text(namespace), principal_id, _text(request_key)])[:32],
                 "namespace": namespace, "owner": principal_id, "revision": 1, "content": _content(content)}
        self._authorize(state, principal_id, scopes, write=True)
        digest = _hash(state)
        prior = self.conn.execute("SELECT request_hash FROM research_decisions WHERE decision_id=?", [state["decision_id"]]).fetchone()
        if prior:
            if prior[0] != digest:
                raise DecisionError("idempotency_conflict", "request key already identifies a different decision")
            current = self._state(namespace, state["decision_id"])
            self._authorize(current, principal_id, scopes, write=True)
            return {**current, "idempotent": True}
        state["decided_at_ms"] = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT INTO research_decisions VALUES (?,?,?,?,1)", [state["decision_id"], namespace, principal_id, digest])
            self.conn.execute("INSERT INTO research_decision_revisions VALUES (?,1,?)", [state["decision_id"], _json(state)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return state

    def revise(self, namespace, decision_id, expected_revision, content, *, principal_id, scopes):
        content = _content(content)
        self.conn.execute("BEGIN")
        try:
            state = self._state(namespace, decision_id)
            self._authorize(state, principal_id, scopes, write=True)
            state.update(content=content, decided_at_ms=self.now())
            self._authorize(state, principal_id, scopes, write=True)
            row = self.conn.execute("UPDATE research_decisions SET revision=revision+1 WHERE decision_id=? AND revision=? RETURNING revision", [decision_id, expected_revision]).fetchone()
            if not row:
                raise DecisionError("revision_conflict", "decision changed; inspect and retry")
            state["revision"] = int(row[0])
            self.conn.execute("INSERT INTO research_decision_revisions VALUES (?,?,?)", [decision_id, state["revision"], _json(state)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return state

    def sensitivity(self, namespace, decision_id, revision, *, weights, inputs, scenarios, provenance, principal_id, scopes):
        state = self.inspect(namespace, decision_id, revision=revision, principal_id=principal_id, scopes=scopes)
        self._authorize(state, principal_id, scopes, write=True)
        if not isinstance(weights, dict) or not 1 <= len(weights) <= 100:
            raise DecisionError("invalid_input", "provide one to 100 explicit criterion weights")
        for key in weights:
            _text(key)
        base_weights = {key: _number(value) for key, value in weights.items()}
        if any(value < 0 for value in base_weights.values()) or not any(base_weights.values()):
            raise DecisionError("invalid_input", "weights must be nonnegative with positive total")
        option_ids = {option["id"] for option in state["content"]["options"]}
        if not isinstance(inputs, dict) or set(inputs) != option_ids or any(not isinstance(values, dict) or set(values) - set(weights) for values in inputs.values()):
            raise DecisionError("invalid_input", "inputs must cover every option using declared criteria")
        matrix = {option: {key: _number(value) if value is not None else None for key, value in values.items()} for option, values in inputs.items()}
        if not isinstance(scenarios, list) or len(scenarios) > 100:
            raise DecisionError("invalid_input", "at most 100 weight scenarios are allowed")
        _text(provenance)
        def calculate(weight_values):
            scores, missing = {}, {}
            with localcontext() as arithmetic:
                arithmetic.prec = 450
                arithmetic.rounding = ROUND_HALF_EVEN
                total = sum(weight_values.values())
                for option, values in matrix.items():
                    absent = [key for key, weight in weight_values.items() if weight and values.get(key) is None]
                    if absent:
                        scores[option], missing[option] = None, absent
                    else:
                        scores[option] = sum(weight * values[key] for key, weight in weight_values.items() if weight) / total
            groups = []
            for score in sorted({value for value in scores.values() if value is not None}, reverse=True):
                groups.append(sorted(option for option in scores if scores[option] == score))
            return {"scores": {key: str(value) if value is not None else None for key, value in scores.items()},
                    "ordering_with_ties": groups, "missing_inputs": missing}
        baseline = calculate(base_weights)
        evaluated = []
        for scenario in scenarios:
            if not isinstance(scenario, dict) or set(scenario) != {"assumption", "weights"} or not isinstance(scenario["weights"], dict) or set(scenario["weights"]) - set(weights):
                raise DecisionError("invalid_input", "each scenario names an assumption and overrides declared weights")
            _text(scenario["assumption"])
            changed = {**base_weights, **{key: _number(value) for key, value in scenario["weights"].items()}}
            if any(value < 0 for value in changed.values()) or not any(changed.values()):
                raise DecisionError("invalid_input", "scenario weights must be nonnegative with positive total")
            result = calculate(changed)
            evaluated.append({"assumption": scenario["assumption"], "weights": {key: str(value) for key, value in changed.items()},
                              **result, "ordering_changed": result["ordering_with_ties"] != baseline["ordering_with_ties"]})
        receipt = {"contract": "noesis-decision-sensitivity-v1", "decision_id": decision_id, "decision_revision": revision,
                   "decision_hash": _hash(state), "formula": "sum(weight * declared utility) / sum(weight)", "formula_version": 1,
                   "decimal_precision": 450,
                   "rounding": "half-even",
                   "weights": {key: str(value) for key, value in base_weights.items()},
                   "inputs": {option: {key: str(value) if value is not None else None for key, value in values.items()} for option, values in matrix.items()},
                   "provenance": provenance, "baseline": baseline, "scenarios": evaluated,
                   "limitations": ["Declared utilities must already have comparable scales and direction",
                       "Weighted ordering is not a causal simulation or a recommendation", "Missing inputs remain unranked; ties are preserved"]}
        receipt["receipt_id"] = "decision-sensitivity:" + _hash(receipt)[:32]
        self.conn.execute("INSERT OR IGNORE INTO decision_sensitivity_receipts VALUES (?,?,?)", [receipt["receipt_id"], decision_id, _json(receipt)])
        return receipt
