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
CREATE TABLE IF NOT EXISTS decision_evidence_commands(
 namespace TEXT NOT NULL,decision_id TEXT NOT NULL,principal_id TEXT NOT NULL,
 command_key TEXT NOT NULL,request_hash TEXT NOT NULL,response_json TEXT NOT NULL,
 PRIMARY KEY(namespace,decision_id,principal_id,command_key));
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
    optional = {"decision_context", "evidence_budget", "evidence_assessments"}
    if not isinstance(content, dict) or not fields <= set(content) or set(content) - fields - optional:
        raise DecisionError("invalid_decision", "explicit project, options, constraints, assumptions, observations, preferences, action, rationale and review conditions are required")
    project = content["project"]
    if project is not None and (not isinstance(project, dict) or set(project) != {"id", "namespace", "revision"} or type(project["revision"]) is not int or project["revision"] < 1):
        raise DecisionError("invalid_decision", "project requires an explicit stable id, namespace and revision")
    if project is not None:
        _text(project["id"])
        _text(project["namespace"])
    context = content.get("decision_context")
    if project is None and context is None:
        raise DecisionError("invalid_decision", "a standalone decision needs decision_context")
    if context is not None:
        required = {"question", "stakes", "required_confidence", "stop_condition", "uncertainty", "missing_inputs", "deadline_at_ms"}
        if not isinstance(context, dict) or set(context) != required:
            raise DecisionError("invalid_decision", "decision_context requires question, stakes, confidence, stop condition, uncertainty, missing inputs and deadline")
        for field in ("question", "stakes", "required_confidence", "stop_condition", "uncertainty"):
            _text(context[field])
        _strings(context["missing_inputs"], "missing_inputs")
        deadline = context["deadline_at_ms"]
        if deadline is not None and (type(deadline) is not int or deadline < 0):
            raise DecisionError("invalid_decision", "deadline_at_ms must be a nonnegative Unix millisecond timestamp or null")
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
    if "evidence_budget" in content:
        budget = content["evidence_budget"]
        if not isinstance(budget, dict) or set(budget) != {"max_items", "criteria", "stop_condition"}:
            raise DecisionError("invalid_evidence_budget", "evidence_budget requires max_items, criteria, and stop_condition")
        maximum = budget["max_items"]
        criteria = budget["criteria"]
        if type(maximum) is not int or not 1 <= maximum <= 50:
            raise DecisionError("invalid_evidence_budget", "max_items must be from 1 to 50")
        if (not isinstance(criteria, list) or not 1 <= len(criteria) <= 20
                or any(not isinstance(value, str) or not value.strip() for value in criteria)
                or len(set(criteria)) != len(criteria)):
            raise DecisionError("invalid_evidence_budget", "criteria must contain one to 20 unique nonempty labels")
        _text(budget["stop_condition"])
        assessments = content.get("evidence_assessments")
        if not isinstance(assessments, list) or len(assessments) != len(content["observations"]):
            raise DecisionError("invalid_evidence_assessment", "every bounded observation needs exactly one relevance assessment")
        if len(assessments) > maximum:
            raise DecisionError("evidence_budget_exceeded", "decision evidence exceeds its declared item limit")
        observed = {_json(link) for link in content["observations"]}
        assessed = set()
        for item in assessments:
            if not isinstance(item, dict) or set(item) != {"reference", "criterion", "assessment", "rationale"}:
                raise DecisionError("invalid_evidence_assessment", "assessments require reference, criterion, assessment, and rationale")
            refs = _links([item["reference"]])
            if len(refs) != 1 or refs[0]["kind"] != "evidence":
                raise DecisionError("invalid_evidence_assessment", "assessment reference must be revisioned evidence")
            key = _json(refs[0])
            if key not in observed or key in assessed:
                raise DecisionError("invalid_evidence_assessment", "each observation must have one matching assessment")
            assessed.add(key)
            if item["criterion"] not in criteria:
                raise DecisionError("invalid_evidence_assessment", "assessment criterion must be declared in the evidence budget")
            if (not isinstance(item["assessment"], str)
                    or item["assessment"] not in {"supports", "contradicts", "context", "irrelevant"}):
                raise DecisionError("invalid_evidence_assessment", "assessment must classify evidence as supports, contradicts, context, or irrelevant")
            _text(item["rationale"])
        if assessed != observed:
            raise DecisionError("invalid_evidence_assessment", "every observation must have one matching assessment")
    elif "evidence_assessments" in content:
        raise DecisionError("invalid_evidence_assessment", "evidence assessments require a declared evidence budget")
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
        baseline = ResearchProjectStore(self.conn, initialize=False).inspect(project["namespace"], project["id"], revision=project["revision"], principal_id=principal_id, scopes=scopes) if project is not None else None
        for link in state["content"]["observations"]:
            ns = link.get("namespace", state["namespace"])
            allowed = ({baseline["namespace"], *baseline["scope"]["namespaces"]} if baseline is not None else {state["namespace"]})
            if ns not in allowed:
                raise DecisionError("scope_mismatch", "observation is outside the decision's namespace scope")
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
        validated = _content(content)
        contract = ("noesis-decision-v3" if "evidence_budget" in validated else
                    "noesis-decision-v2" if "decision_context" in validated else
                    "noesis-decision-v1")
        state = {"contract": contract, "decision_id": "decision:" + _hash([_text(namespace), principal_id, _text(request_key)])[:32],
                 "namespace": namespace, "owner": principal_id, "revision": 1, "content": validated}
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

    def revise(
        self, namespace, decision_id, expected_revision, content, *, principal_id,
        scopes, iteration_receipt=None, _within_transaction=False,
    ):
        content = _content(content)
        if iteration_receipt is not None:
            required_receipt = {
                "contract", "session_id", "session_revision", "decision_id",
                "before_revision", "expected", "outcome", "before_after_rationale",
                "proposal_sha256", "proposal_recorded_at_ms",
            }
            if (not isinstance(iteration_receipt, dict)
                    or set(iteration_receipt) != required_receipt
                    or iteration_receipt.get("contract") != "noesis-intake-iteration-decision-v1"
                    or iteration_receipt.get("decision_id") != decision_id
                    or iteration_receipt.get("before_revision") != expected_revision
                    or not isinstance(iteration_receipt.get("outcome"), dict)
                    or not isinstance(iteration_receipt.get("proposal_sha256"), str)
                    or len(iteration_receipt["proposal_sha256"]) != 64):
                raise DecisionError("invalid_iteration_receipt", "iteration receipt must identify this reviewed decision revision")
            try:
                iteration_receipt = json.loads(_json(iteration_receipt))
            except (TypeError, ValueError) as exc:
                raise DecisionError("invalid_iteration_receipt", "iteration receipt must be finite JSON") from exc
        if not _within_transaction:
            self.conn.execute("BEGIN")
        try:
            state = self._state(namespace, decision_id)
            self._authorize(state, principal_id, scopes, write=True)
            state.update(content=content, decided_at_ms=self.now())
            if "evidence_budget" in content:
                state["contract"] = "noesis-decision-v3"
            elif "decision_context" in content and state.get("contract") != "noesis-decision-v3":
                state["contract"] = "noesis-decision-v2"
            self._authorize(state, principal_id, scopes, write=True)
            row = self.conn.execute("UPDATE research_decisions SET revision=revision+1 WHERE decision_id=? AND revision=? RETURNING revision", [decision_id, expected_revision]).fetchone()
            if not row:
                raise DecisionError("revision_conflict", "decision changed; inspect and retry")
            state["revision"] = int(row[0])
            if iteration_receipt is not None:
                history = [*state.get("iteration_history", []), iteration_receipt]
                if len(history) > 100:
                    raise DecisionError("iteration_history_limit", "decision iteration history is limited to 100 receipts")
                state["iteration_history"] = history
            self.conn.execute("INSERT INTO research_decision_revisions VALUES (?,?,?)", [decision_id, state["revision"], _json(state)])
            if not _within_transaction:
                self.conn.execute("COMMIT")
        except Exception as exc:
            if not _within_transaction:
                self._abort(exc)
            raise
        return state

    def record_evidence(
        self, namespace, decision_id, expected_revision, command_key, reference,
        criterion, assessment, rationale, *, principal_id, scopes,
    ):
        """Append one reviewed evidence item to a decision with a declared cap."""
        _text(command_key)
        _text(criterion)
        _text(rationale)
        refs = _links([reference])
        if len(refs) != 1 or refs[0]["kind"] != "evidence":
            raise DecisionError("invalid_evidence", "record one revisioned evidence reference")
        reference = refs[0]
        if (not isinstance(assessment, str)
                or assessment not in {"supports", "contradicts", "context", "irrelevant"}):
            raise DecisionError("invalid_evidence_assessment", "assessment must be supports, contradicts, context, or irrelevant")
        request = {
            "decision_id": decision_id,
            "expected_revision": expected_revision,
            "reference": reference,
            "criterion": criterion,
            "assessment": assessment,
            "rationale": rationale,
        }
        request_hash = _hash(request)
        self.conn.execute("BEGIN")
        try:
            current = self._state(namespace, decision_id)
            self._authorize(current, principal_id, scopes, write=True)
            prior = self.conn.execute(
                "SELECT request_hash,response_json FROM decision_evidence_commands "
                "WHERE namespace=? AND decision_id=? AND principal_id=? AND command_key=?",
                [namespace, decision_id, principal_id, command_key],
            ).fetchone()
            if prior:
                if prior[0] != request_hash:
                    raise DecisionError("idempotency_conflict", "command key already records different decision evidence")
                replay = json.loads(prior[1])
                self._authorize(replay, principal_id, scopes, write=True)
                self.conn.execute("COMMIT")
                return {**replay, "idempotent": True}
            if type(expected_revision) is not int or current["revision"] != expected_revision:
                raise DecisionError("revision_conflict", "decision changed; inspect and retry with its current revision")
            content = current["content"]
            budget = content.get("evidence_budget")
            if not budget:
                raise DecisionError("evidence_budget_missing", "declare an evidence budget, relevance criteria, and stop condition first")
            if criterion not in budget["criteria"]:
                raise DecisionError("criterion_unavailable", "choose one of the decision's declared evidence criteria")
            observations = list(content["observations"])
            if reference in observations:
                raise DecisionError("evidence_already_recorded", "evidence reference is already recorded for this decision")
            if len(observations) >= budget["max_items"]:
                raise DecisionError("evidence_budget_exceeded", "decision evidence item limit reached; follow its declared stop condition")
            assessments = list(content.get("evidence_assessments", []))
            observations.append(reference)
            assessments.append({
                "reference": reference,
                "criterion": criterion,
                "assessment": assessment,
                "rationale": rationale,
            })
            revised_content = {**content, "observations": observations,
                               "evidence_assessments": assessments}
            validated = _content(revised_content)
            next_state = {**current, "content": validated, "revision": expected_revision + 1,
                          "decided_at_ms": self.now()}
            self._authorize(next_state, principal_id, scopes, write=True)
            row = self.conn.execute(
                "UPDATE research_decisions SET revision=revision+1 "
                "WHERE decision_id=? AND revision=? RETURNING revision",
                [decision_id, expected_revision],
            ).fetchone()
            if not row:
                raise DecisionError("revision_conflict", "decision changed; inspect and retry")
            next_state["revision"] = int(row[0])
            self.conn.execute(
                "INSERT INTO research_decision_revisions VALUES (?,?,?)",
                [decision_id, next_state["revision"], _json(next_state)],
            )
            receipt = {
                "contract": "noesis-decision-evidence-receipt-v1",
                "decision_id": decision_id,
                "decision_revision": next_state["revision"],
                "recorded_by": principal_id,
                "recorded_at_ms": next_state["decided_at_ms"],
                "command_key": command_key,
                "request_hash": request_hash,
                "reference": reference,
                "criterion": criterion,
                "assessment": assessment,
                "rationale": rationale,
                "remaining_items": budget["max_items"] - len(observations),
                "max_items": budget["max_items"],
                "stop_condition": budget["stop_condition"],
            }
            response = {**next_state, "evidence_receipt": receipt, "idempotent": False}
            self.conn.execute(
                "INSERT INTO decision_evidence_commands VALUES (?,?,?,?,?,?)",
                [namespace, decision_id, principal_id, command_key, request_hash, _json(response)],
            )
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return response

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

    def comparative_matrix(self, namespace, decision_id, receipt_id, *, principal_id, scopes):
        """Project a pinned sensitivity receipt alongside the exact decision revision."""
        row = self.conn.execute(
            "SELECT content_json FROM decision_sensitivity_receipts "
            "WHERE receipt_id=? AND decision_id=?", [receipt_id, decision_id]
        ).fetchone()
        if not row:
            raise DecisionError("matrix_unavailable", "decision comparison receipt is unavailable")
        receipt = json.loads(row[0])
        decision = self.inspect(
            namespace, decision_id, revision=receipt["decision_revision"],
            principal_id=principal_id, scopes=scopes,
        )
        if receipt["decision_hash"] != _hash(decision):
            raise DecisionError("matrix_mismatch", "decision revision no longer matches the comparison")
        content = decision["content"]
        baseline = receipt["baseline"]
        options = []
        for option in content["options"]:
            option_id = option["id"]
            values = receipt["inputs"][option_id]
            advantages, disadvantages = [], []
            for criterion, value in values.items():
                if value is None:
                    continue
                peers = [other[criterion] for peer_id, other in receipt["inputs"].items()
                         if peer_id != option_id and other.get(criterion) is not None]
                if not peers:
                    continue
                own = Decimal(value)
                compared = [Decimal(peer) for peer in peers]
                if own > max(compared):
                    advantages.append(criterion)
                elif own < min(compared):
                    disadvantages.append(criterion)
            options.append({
                "id": option_id, "description": option["description"],
                "declared_utilities": values,
                "weighted_score": baseline["scores"][option_id],
                "missing_inputs": baseline["missing_inputs"].get(option_id, []),
                "relative_advantages": advantages,
                "relative_disadvantages": disadvantages,
                "chosen": option_id == content["selected_action"],
            })
        context = content.get("decision_context") or {}
        return {
            "contract": "noesis-decision-comparative-matrix-v1",
            "decision_id": decision_id, "decision_revision": decision["revision"],
            "decision_hash": receipt["decision_hash"],
            "receipt_id": receipt_id, "question": context.get("question"),
            "stop_condition": context.get("stop_condition"),
            "uncertainty": context.get("uncertainty"),
            "missing_inputs": context.get("missing_inputs", []),
            "constraints": content["constraints"],
            "assumptions": content["assumptions"],
            "observation_refs": content["observations"],
            "weights": receipt["weights"], "options": options,
            "ordering_with_ties": baseline["ordering_with_ties"],
            "scenarios": receipt["scenarios"],
            "selected_action": content["selected_action"],
            "rationale": content["rationale"],
            "review_conditions": content["review_conditions"],
            "limitations": [*receipt["limitations"],
                            "Relative advantages compare only supplied declared utilities"],
        }
