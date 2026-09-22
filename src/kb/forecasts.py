"""Binary forecast revisions, reviewed outcomes, and cutoff-aware scoring."""

import json
import math
import time
from decimal import Decimal, InvalidOperation

from src.kb.research_projects import _hash, _json

READ_SCOPE = "knowledge:forecasts:read"
WRITE_SCOPE = "knowledge:forecasts:write"
_DDL = """
CREATE TABLE IF NOT EXISTS research_forecasts(
 forecast_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 request_hash TEXT NOT NULL,revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS research_forecast_revisions(
 forecast_id TEXT NOT NULL,revision BIGINT NOT NULL,recorded_at_ms BIGINT NOT NULL,
 content_json TEXT NOT NULL,PRIMARY KEY(forecast_id,revision));
CREATE TABLE IF NOT EXISTS research_forecast_outcomes(
 forecast_id TEXT NOT NULL,revision BIGINT NOT NULL,recorded_at_ms BIGINT NOT NULL,
 content_json TEXT NOT NULL,PRIMARY KEY(forecast_id,revision));
"""


class ForecastError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 10000:
        raise ForecastError("invalid_forecast", "nonempty text of at most 10000 characters is required")
    return value


def _probability(value):
    if type(value) not in {float, int} or not math.isfinite(value) or not 0 <= value <= 1:
        raise ForecastError("invalid_probability", "explicit binary probability must be between zero and one")
    return float(value)


def _evidence(values):
    if not isinstance(values, list) or len(values) > 1000:
        raise ForecastError("invalid_evidence", "at most 1000 evidence references are allowed")
    for value in values:
        if not isinstance(value, dict) or set(value) != {"kind", "id", "revision", "namespace"}:
            raise ForecastError("invalid_evidence", "evidence requires kind, id, revision, and namespace")
        if value["kind"] not in {"event", "observation", "source", "snapshot"}:
            raise ForecastError("invalid_evidence", "unsupported evidence kind")
        for key in ("id", "revision", "namespace"):
            _text(value[key])
    return json.loads(_json(values))


def _match(value):
    if value is None or value == {}:
        return None
    fields = {"namespace", "metric_id", "provider", "provider_series_id", "period", "unit_id", "comparison", "threshold"}
    if not isinstance(value, dict) or set(value) != fields or value.get("comparison") not in {"gt", "gte", "lt", "lte", "eq"}:
        raise ForecastError("invalid_rule", "quantitative matching requires explicit series, period, unit, comparison, and threshold")
    for item in value.values():
        _text(item)
    try:
        if not Decimal(value["threshold"]).is_finite():
            raise InvalidOperation
    except InvalidOperation:
        raise ForecastError("invalid_rule", "threshold must be a finite decimal") from None
    return dict(value)


class ForecastStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(state, principal_id, scopes, *, write=False):
        if principal_id and "operator" in scopes:
            return
        if not principal_id or state["owner"] != principal_id or (WRITE_SCOPE if write else READ_SCOPE) not in scopes:
            raise ForecastError("unauthorized", "current forecast scope and ownership are required")
        ns = state["namespace"]
        if f"namespace:{ns}:write" not in scopes and (write or f"namespace:{ns}:read" not in scopes):
            raise ForecastError("unauthorized", "current namespace access is required")
        for item in state.get("evidence", []):
            ns = item["namespace"]
            if f"namespace:{ns}:read" not in scopes and f"namespace:{ns}:write" not in scopes:
                raise ForecastError("unauthorized", "current evidence namespace access is required")
        match = state.get("resolution_match")
        if match and f"namespace:{match['namespace']}:read" not in scopes and f"namespace:{match['namespace']}:write" not in scopes:
            raise ForecastError("unauthorized", "current resolution source namespace access is required")

    def _state(self, namespace, forecast_id, cutoff_ms=None):
        row = self.conn.execute("""SELECT r.content_json FROM research_forecasts f
            JOIN research_forecast_revisions r ON r.forecast_id=f.forecast_id
            WHERE f.namespace=? AND f.forecast_id=? AND (? IS NULL OR r.recorded_at_ms<=?)
            ORDER BY r.revision DESC LIMIT 1""", [namespace, forecast_id, cutoff_ms, cutoff_ms]).fetchone()
        if not row:
            raise ForecastError("forecast_unavailable", "no forecast revision was recorded by this cutoff")
        return json.loads(row[0])

    def inspect(self, namespace, forecast_id, *, principal_id, scopes, cutoff_ms=None, outcome_cutoff_ms=None):
        current = self._state(namespace, forecast_id)
        self._authorize(current, principal_id, scopes)
        state = self._state(namespace, forecast_id, cutoff_ms)
        self._authorize(state, principal_id, scopes)
        row = self.conn.execute("""SELECT content_json FROM research_forecast_outcomes WHERE forecast_id=?
            AND (? IS NULL OR recorded_at_ms<=?) ORDER BY revision DESC LIMIT 1""",
            [forecast_id, outcome_cutoff_ms, outcome_cutoff_ms]).fetchone()
        outcome = json.loads(row[0]) if row else {"status": "unresolved", "revision": 0, "evidence": []}
        self._authorize({**state, "evidence": outcome["evidence"]}, principal_id, scopes)
        return {**state, "outcome": outcome}

    def _abort(self, exc):
        import duckdb
        self.conn.execute("ROLLBACK")
        if isinstance(exc, (duckdb.TransactionException, duckdb.ConstraintException)):
            raise ForecastError("revision_conflict", "concurrent forecast update; inspect and retry") from exc
        raise exc

    def create(self, namespace, request_key, *, question, outcome_rule, resolution_at_ms, probability,
               evidence, principal_id, scopes, resolution_match=None):
        now = self.now()
        if type(resolution_at_ms) is not int or resolution_at_ms < 0:
            raise ForecastError("invalid_resolution_date", "resolution date must be a nonnegative integer")
        state = {"contract": "noesis-binary-forecast-v1", "forecast_id": "forecast:" + _hash([_text(namespace), principal_id, _text(request_key)])[:32],
                 "namespace": namespace, "owner": principal_id, "question": _text(question), "outcome_rule": _text(outcome_rule),
                 "resolution_at_ms": resolution_at_ms, "probability": _probability(probability),
                 "evidence": _evidence(evidence), "resolution_match": _match(resolution_match), "revision": 1, "rule_revision": 1}
        self._authorize(state, principal_id, scopes, write=True)
        digest = _hash(state)
        prior = self.conn.execute("SELECT request_hash FROM research_forecasts WHERE forecast_id=?", [state["forecast_id"]]).fetchone()
        if prior:
            if digest != prior[0]:
                raise ForecastError("idempotency_conflict", "request key identifies a different forecast")
            current = self._state(namespace, state["forecast_id"])
            self._authorize(current, principal_id, scopes, write=True)
            return {**current, "idempotent": True}
        if resolution_at_ms <= now:
            raise ForecastError("invalid_resolution_date", "a new forecast must resolve in the future")
        state["recorded_at_ms"] = now
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT INTO research_forecasts VALUES (?,?,?,?,1)", [state["forecast_id"], namespace, principal_id, digest])
            self.conn.execute("INSERT INTO research_forecast_revisions VALUES (?,1,?,?)", [state["forecast_id"], now, _json(state)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return state

    def revise(self, namespace, forecast_id, expected_revision, *, probability, evidence, rationale,
               principal_id, scopes, outcome_rule=None, resolution_match=None):
        self.conn.execute("BEGIN")
        try:
            state = self._state(namespace, forecast_id)
            self._authorize(state, principal_id, scopes, write=True)
            now = self.now()
            if now >= state["resolution_at_ms"]:
                raise ForecastError("forecast_frozen", "probabilities and rules cannot change after the resolution deadline")
            state.update(probability=_probability(probability), evidence=_evidence(evidence), rationale=_text(rationale))
            self._authorize(state, principal_id, scopes, write=True)
            changed_rule = False
            if outcome_rule is not None and outcome_rule != state["outcome_rule"]:
                state["outcome_rule"] = _text(outcome_rule)
                changed_rule = True
            if resolution_match is not None and _match(resolution_match) != state.get("resolution_match"):
                state["resolution_match"] = _match(resolution_match)
                changed_rule = True
            if changed_rule:
                state["rule_revision"] += 1
            self._authorize(state, principal_id, scopes, write=True)
            changed = self.conn.execute("UPDATE research_forecasts SET revision=revision+1 WHERE forecast_id=? AND revision=? RETURNING revision",
                                        [forecast_id, expected_revision]).fetchone()
            if not changed:
                raise ForecastError("revision_conflict", "forecast changed; inspect and retry")
            state.update(revision=int(changed[0]), recorded_at_ms=now)
            self.conn.execute("INSERT INTO research_forecast_revisions VALUES (?,?,?,?)", [forecast_id, state["revision"], now, _json(state)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return state

    def resolve(self, namespace, forecast_id, expected_outcome_revision, *, status, outcome, evidence,
                rationale, forecast_revision, principal_id, scopes):
        if status not in {"unresolved", "resolved", "disputed", "cancelled"}:
            raise ForecastError("invalid_outcome", "unsupported resolution status")
        if status == "resolved" and (type(outcome) is not int or outcome not in {0, 1}) or status != "resolved" and outcome is not None:
            raise ForecastError("invalid_outcome", "only resolved outcomes have binary values")
        evidence = _evidence(evidence)
        if status == "resolved" and not evidence:
            raise ForecastError("invalid_outcome", "resolved outcomes require sourced evidence")
        self.conn.execute("BEGIN")
        try:
            state = self._state(namespace, forecast_id)
            self._authorize(state, principal_id, scopes, write=True)
            self._authorize({**state, "evidence": evidence}, principal_id, scopes, write=True)
            if state["revision"] != forecast_revision:
                raise ForecastError("revision_conflict", "review the current forecast rule before resolution")
            now = self.now()
            if status == "resolved" and now < state["resolution_at_ms"]:
                raise ForecastError("resolution_not_due", "final resolution cannot precede the registered deadline")
            previous = self.conn.execute("SELECT coalesce(max(revision),0) FROM research_forecast_outcomes WHERE forecast_id=?", [forecast_id]).fetchone()[0]
            if previous != expected_outcome_revision:
                raise ForecastError("revision_conflict", "outcome changed; inspect and retry")
            result = {"status": status, "outcome": outcome, "evidence": evidence, "rationale": _text(rationale),
                      "forecast_revision": forecast_revision, "rule_revision": state["rule_revision"], "reviewer": principal_id,
                      "revision": previous + 1, "recorded_at_ms": now, "corrects_outcome_revision": previous or None}
            self.conn.execute("INSERT INTO research_forecast_outcomes VALUES (?,?,?,?)", [forecast_id, previous + 1, now, _json(result)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return result

    def score(self, namespace, forecast_ids, *, cutoff_ms, principal_id, scopes, outcome_cutoff_ms=None):
        if not isinstance(forecast_ids, list) or not 1 <= len(forecast_ids) <= 10000 or len(set(forecast_ids)) != len(forecast_ids):
            raise ForecastError("invalid_cohort", "provide one to 10000 unique forecast ids")
        if type(cutoff_ms) is not int or cutoff_ms < 0:
            raise ForecastError("invalid_cutoff", "an explicit nonnegative forecast cutoff is required")
        included, excluded = [], []
        for forecast_id in forecast_ids:
            # Check current access even when a forecast is absent at the cutoff.
            current = self._state(namespace, forecast_id)
            self._authorize(current, principal_id, scopes)
            try:
                state = self.inspect(namespace, forecast_id, cutoff_ms=min(cutoff_ms, current["resolution_at_ms"] - 1),
                    outcome_cutoff_ms=outcome_cutoff_ms, principal_id=principal_id, scopes=scopes)
            except ForecastError as exc:
                if exc.code != "forecast_unavailable":
                    raise
                excluded.append({"forecast_id": forecast_id, "reason": "not-recorded-before-cutoff"})
                continue
            outcome = state["outcome"]
            if outcome["status"] != "resolved" or outcome.get("rule_revision") != state["rule_revision"]:
                excluded.append({"forecast_id": forecast_id, "reason": "rule-changed-after-cutoff" if outcome["status"] == "resolved" else outcome["status"]})
                continue
            p, y = state["probability"], outcome["outcome"]
            included.append({"forecast_id": forecast_id, "forecast_revision": state["revision"], "outcome_revision": outcome["revision"],
                             "probability": p, "outcome": y, "brier": (p - y) ** 2})
        n = len(included)
        bins = []
        for index in range(10):
            items = [v for v in included if min(int(v["probability"] * 10), 9) == index]
            count = len(items)
            rate = sum(v["outcome"] for v in items) / count if count else None
            interval = None
            if count:
                z = 1.96
                center = (rate + z*z/(2*count)) / (1 + z*z/count)
                half = z * math.sqrt(rate*(1-rate)/count + z*z/(4*count*count)) / (1 + z*z/count)
                interval = [max(0.0, center-half), min(1.0, center+half)]
            bins.append({"lower": index / 10, "upper": (index + 1) / 10, "count": count,
                         "mean_probability": sum(v["probability"] for v in items) / count if count else None,
                         "observed_frequency": rate, "wilson_95_interval": interval})
        brier = sum(v["brier"] for v in included) / n if n else None
        return {"contract": "noesis-forecast-score-v1", "cutoff_ms": cutoff_ms, "outcome_cutoff_ms": outcome_cutoff_ms,
                "cohort_size": len(forecast_ids), "scored_count": n, "excluded": excluded, "scores": included,
                "mean_brier": brier, "baseline": {"kind": "constant-probability", "probability": 0.5, "mean_brier": 0.25 if n else None},
                "reliability_bins": bins, "ranking": None,
                "limitations": ["Explicit caller-selected cohort; missing resolutions can bias results",
                    "Latest reviewed outcomes used unless outcome cutoff is supplied",
                    "Wilson intervals assume independent outcomes; correlated forecasts reduce effective sample size",
                    "Small samples do not establish forecasting skill"]}

    def propose_resolution(self, namespace, forecast_id, *, principal_id, scopes):
        state = self.inspect(namespace, forecast_id, principal_id=principal_id, scopes=scopes)
        rule = state.get("resolution_match")
        result = {"forecast_id": forecast_id, "forecast_revision": state["revision"], "status": "unresolved",
                  "proposed_outcome": None, "evidence": [], "requires_review": True, "published": False}
        if not rule:
            return {**result, "reason": "manual-rule-requires-review"}
        if self.now() < state["resolution_at_ms"]:
            return {**result, "reason": "resolution-not-due"}
        exists = self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='quantitative_observations'").fetchone()
        if not exists:
            return {**result, "reason": "observations-unavailable"}
        rows = self.conn.execute("""SELECT observation_id,value_text,missing,preliminary,vintage_id,provenance_json,release_at_ms
            FROM quantitative_observations WHERE namespace=? AND metric_id=? AND provider=? AND provider_series_id=?
            AND period=? AND unit_id=? AND release_at_ms<=? ORDER BY release_at_ms DESC,observation_id LIMIT 1001""",
            [rule[key] for key in ("namespace", "metric_id", "provider", "provider_series_id", "period", "unit_id")] + [state["resolution_at_ms"]]).fetchall()
        if not rows or len(rows) > 1000:
            return {**result, "reason": "no-matching-observation" if not rows else "observation-budget-exceeded"}
        latest = [row for row in rows if row[6] == rows[0][6]]
        if any(row[2] or row[3] or not json.loads(row[5]) for row in latest):
            return {**result, "reason": "missing-preliminary-or-unsourced-observation"}
        try:
            values = {Decimal(row[1]) for row in latest}
            if not all(value.is_finite() for value in values):
                raise InvalidOperation
        except (InvalidOperation, TypeError):
            return {**result, "reason": "invalid-observation-value"}
        evidence = [{"kind": "observation", "id": row[0], "revision": row[4], "namespace": rule["namespace"]} for row in latest]
        if len(values) != 1:
            return {**result, "status": "disputed", "reason": "conflicting-latest-observations", "evidence": evidence}
        value, threshold = next(iter(values)), Decimal(rule["threshold"])
        comparisons = {"gt": value > threshold, "gte": value >= threshold, "lt": value < threshold, "lte": value <= threshold, "eq": value == threshold}
        return {**result, "status": "proposed", "proposed_outcome": int(comparisons[rule["comparison"]]),
                "reason": "registered-quantitative-rule-matched", "evidence": evidence,
                "comparison": {"rule": rule, "observed_value": str(value), "release_at_ms": rows[0][6]}}
