"""Evaluate hard funding conditions against pinned profile and call revisions.

This is a *requirements assessment*, not a funder decision. Rules use
three-valued (Kleene) logic: a condition over a fact the owner has not stated
evaluates to *unknown*, never to false. A hard requirement without a machine
rule (or an approved human interpretation for the exact requirement text) is
*unparsed* and needs clarification. Conflicting provider assertions on the
opportunity also need clarification.

Rule grammar (JSON)::

    {"all": [rule, ...]}                 conjunction
    {"any": [rule, ...]}                 disjunction
    {"not": rule}                        negation
    {"rule": rule, "except": rule}       exception: satisfied when the
                                         exception holds, else by the rule
    {"fact": key, "op": op, "value": v}  comparison over one profile fact

``op`` is one of ``eq ne in not_in gte lte gt lt is_true is_false
intersects``; ``value`` is omitted for ``is_true``/``is_false``.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal

from src.kb.funding_records import WRITE_SCOPE, canonical, digest

CONTRACT = "noesis-funding-eligibility-v1"
REVIEW_SCOPE = "knowledge:funding:review"
DISCLAIMER = (
    "Requirements assessment against stated facts and cited call text; "
    "not a funder eligibility decision or a prediction of funding."
)
# Procedural requirements shape the application checklist, not eligibility.
PROCEDURAL = {"document", "submission-route", "duration"}
OPS = {"eq", "ne", "in", "not_in", "gte", "lte", "gt", "lt", "is_true", "is_false", "intersects"}
_DDL = """
CREATE TABLE IF NOT EXISTS funding_rule_interpretations(
 interpretation_id TEXT NOT NULL, revision BIGINT NOT NULL, namespace TEXT NOT NULL,
 opportunity_id TEXT NOT NULL, requirement_id TEXT NOT NULL, requirement_hash TEXT NOT NULL,
 rule_json TEXT NOT NULL, status TEXT NOT NULL, author TEXT NOT NULL, reviewer TEXT,
 note TEXT, created_at_ms BIGINT NOT NULL, PRIMARY KEY(interpretation_id, revision));
CREATE TABLE IF NOT EXISTS funding_assessments(
 assessment_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 profile_id TEXT NOT NULL, profile_revision BIGINT NOT NULL, opportunity_id TEXT NOT NULL,
 opportunity_revision BIGINT NOT NULL, verdict TEXT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL);
"""


class EligibilityError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_rule(rule, depth=0):
    if depth > 20:
        raise EligibilityError("invalid_rule", "rule nesting is too deep")
    if not isinstance(rule, dict):
        raise EligibilityError("invalid_rule", "rule must be an object")
    if set(rule) in ({"all"}, {"any"}):
        items = next(iter(rule.values()))
        if not isinstance(items, list) or not items:
            raise EligibilityError("invalid_rule", "all/any need a nonempty list")
        for item in items:
            validate_rule(item, depth + 1)
    elif set(rule) == {"not"}:
        validate_rule(rule["not"], depth + 1)
    elif set(rule) == {"rule", "except"}:
        validate_rule(rule["rule"], depth + 1)
        validate_rule(rule["except"], depth + 1)
    elif "fact" in rule:
        if set(rule) - {"fact", "op", "value"} or rule.get("op") not in OPS:
            raise EligibilityError("invalid_rule", "comparison needs fact, supported op and value")
        if rule["op"] in {"is_true", "is_false"} and "value" in rule:
            raise EligibilityError("invalid_rule", f"{rule['op']} takes no value")
        if rule["op"] not in {"is_true", "is_false"} and "value" not in rule:
            raise EligibilityError("invalid_rule", f"{rule['op']} requires a value")
        if rule["op"] in {"in", "not_in", "intersects"} and not isinstance(rule.get("value"), list):
            raise EligibilityError("invalid_rule", f"{rule['op']} compares with a list")
    else:
        raise EligibilityError("invalid_rule", "unsupported rule node")
    return rule


def _number(value):
    if isinstance(value, dict) and "amount" in value:
        return Decimal(value["amount"])
    if isinstance(value, bool):
        raise TypeError
    return Decimal(str(value))


def _compare(op, actual, expected):
    if op == "is_true":
        return actual is True
    if op == "is_false":
        return actual is False
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "in":
        return actual in expected
    if op == "not_in":
        return actual not in expected
    if op == "intersects":
        return bool(set(actual if isinstance(actual, list) else [actual]) & set(expected))
    left, right = _number(actual), _number(expected)
    return {"gte": left >= right, "lte": left <= right, "gt": left > right, "lt": left < right}[op]


def evaluate_rule(rule, facts):
    """Return (True|False|None, used facts, missing facts)."""
    if "fact" in rule:
        key = rule["fact"]
        if key not in facts:
            return None, set(), {key}
        try:
            return _compare(rule["op"], facts[key], rule.get("value")), {key}, set()
        except (TypeError, ValueError, ArithmeticError):
            return None, {key}, {key}
    if "not" in rule:
        value, used, missing = evaluate_rule(rule["not"], facts)
        return (None if value is None else not value), used, missing
    if "except" in rule:
        return evaluate_rule({"any": [rule["except"], rule["rule"]]}, facts)
    conjunction = "all" in rule
    results = [evaluate_rule(item, facts) for item in rule["all" if conjunction else "any"]]
    used = set().union(*(r[1] for r in results))
    missing = set().union(*(r[2] for r in results))
    values = [r[0] for r in results]
    decisive = False if conjunction else True
    if decisive in values:
        # A decisive branch settles the outcome even when other facts are unknown.
        return decisive, used, set()
    if None in values:
        return None, used, missing
    return (not decisive), used, missing


def assess(opportunity, profile_facts, *, interpretations=None, conflicts=None):
    """Pure assessment of one opportunity revision against pinned facts."""
    interpretations = interpretations or {}
    findings, verdicts = [], []
    requirement_conflict = [c for c in conflicts or [] if c["field"] == "requirements"]
    for requirement in opportunity["record"].get("requirements") or []:
        if requirement["category"] in PROCEDURAL:
            findings.append({"requirement_id": requirement["requirement_id"], "category": requirement["category"],
                             "hard": requirement.get("hard"), "result": "procedural", "text": requirement["text"],
                             "citation": _citation(opportunity, requirement)})
            continue
        if requirement.get("hard") is False:
            findings.append({"requirement_id": requirement["requirement_id"], "hard": False,
                             "result": "consideration", "text": requirement["text"],
                             "citation": _citation(opportunity, requirement)})
            continue
        rule, source = requirement.get("machine_rule"), "adapter-pattern"
        if rule is None and requirement["requirement_id"] in interpretations:
            interpretation = interpretations[requirement["requirement_id"]]
            rule, source = interpretation["rule"], f"human-interpretation:{interpretation['interpretation_id']}@{interpretation['revision']}"
        finding = {"requirement_id": requirement["requirement_id"], "category": requirement["category"],
                   "hard": requirement.get("hard"), "text": requirement["text"],
                   "citation": _citation(opportunity, requirement), "rule_source": None}
        if rule is None:
            finding.update(result="unparsed", reason="no machine rule or approved interpretation for this requirement text")
        else:
            value, used, missing = evaluate_rule(rule, profile_facts)
            finding.update(rule_source=source, facts_used=sorted(used), missing_facts=sorted(missing),
                           result={True: "met", False: "not_met", None: "unknown"}[value])
        findings.append(finding)
        verdicts.append(finding["result"])
    if requirement_conflict:
        verdicts.append("conflict")
        findings.append({"requirement_id": None, "category": "requirements", "result": "conflict",
                         "reason": "provider pages state different requirements: "
                                   + ", ".join(v["source_url"] for v in requirement_conflict[0]["values"]),
                         "citation": None})
    if requirement_hard_failed := [f for f in findings if f.get("result") == "not_met" and f.get("hard") is True]:
        verdict = "ineligible"
    elif any(v in {"unknown", "unparsed", "conflict", "not_met"} for v in verdicts):
        # A failed condition whose hardness the source leaves unclear is not a
        # disqualification; it needs clarification.
        verdict = "needs_clarification"
    elif not verdicts:
        verdict = "needs_clarification"
        findings.append({"requirement_id": None, "result": "unparsed",
                         "reason": "the call revision states no applicant/project requirements", "citation": None})
    else:
        verdict = "eligible"
    return {
        "contract": CONTRACT, "verdict": verdict, "disclaimer": DISCLAIMER,
        "disqualifiers": [f["requirement_id"] for f in requirement_hard_failed],
        "clarifications": [f for f in findings if f.get("result") in {"unknown", "unparsed", "conflict"}
                           or (f.get("result") == "not_met" and f.get("hard") is None)],
        "missing_facts": sorted({m for f in findings for m in f.get("missing_facts", [])}),
        "findings": findings,
    }


def _citation(opportunity, requirement):
    return {"opportunity_id": opportunity["opportunity_id"], "revision": opportunity["revision"],
            "source_url": opportunity["record"]["source_url"], "requirement_id": requirement["requirement_id"],
            "locator": requirement["locator"]}


class InterpretationStore:
    """Versioned, reviewable human interpretations of free-text requirements.

    An interpretation binds to the exact requirement text hash; when a call
    amendment changes the text the interpretation stops applying.
    """

    def __init__(self, conn, *, initialize=True, now=None):
        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def propose(self, namespace, opportunity, requirement_id, rule, *, note, principal_id, scopes):
        if WRITE_SCOPE not in scopes or f"namespace:{namespace}:write" not in scopes:
            raise EligibilityError("unauthorized", "funding write and namespace write scopes are required")
        requirement = next((r for r in opportunity["record"].get("requirements") or []
                            if r["requirement_id"] == requirement_id), None)
        if requirement is None:
            raise EligibilityError("requirement_not_found", "requirement is not in this call revision")
        validate_rule(rule)
        interpretation_id = "funding-interpretation:" + digest([namespace, opportunity["opportunity_id"], requirement_id])[:24]
        revision = (self.conn.execute(
            "SELECT max(revision) FROM funding_rule_interpretations WHERE interpretation_id=?",
            [interpretation_id]).fetchone()[0] or 0) + 1
        self.conn.execute(
            "INSERT INTO funding_rule_interpretations VALUES (?,?,?,?,?,?,?,'proposed',?,NULL,?,?)",
            [interpretation_id, revision, namespace, opportunity["opportunity_id"], requirement_id,
             digest(requirement["text"]), canonical(rule), principal_id, note or "", self.now()])
        return {"interpretation_id": interpretation_id, "revision": revision, "status": "proposed"}

    def review(self, namespace, interpretation_id, revision, *, approve, principal_id, scopes):
        if REVIEW_SCOPE not in scopes or f"namespace:{namespace}:write" not in scopes:
            raise EligibilityError("unauthorized", "funding review scope is required")
        row = self.conn.execute(
            "SELECT author, status FROM funding_rule_interpretations WHERE interpretation_id=? AND revision=? AND namespace=?",
            [interpretation_id, revision, namespace]).fetchone()
        if not row or row[1] != "proposed":
            raise EligibilityError("interpretation_unavailable", "no pending interpretation revision")
        if row[0] == principal_id:
            raise EligibilityError("self_review", "an interpretation needs a reviewer other than its author")
        status = "approved" if approve else "rejected"
        self.conn.execute(
            "UPDATE funding_rule_interpretations SET status=?, reviewer=? WHERE interpretation_id=? AND revision=?",
            [status, principal_id, interpretation_id, revision])
        return {"interpretation_id": interpretation_id, "revision": revision, "status": status}

    def applicable(self, namespace, opportunity):
        """Latest approved interpretation per requirement whose text is unchanged."""
        texts = {r["requirement_id"]: digest(r["text"]) for r in opportunity["record"].get("requirements") or []}
        rows = self.conn.execute(
            """SELECT interpretation_id, revision, requirement_id, requirement_hash, rule_json
               FROM funding_rule_interpretations WHERE namespace=? AND opportunity_id=? AND status='approved'
               ORDER BY revision""", [namespace, opportunity["opportunity_id"]]).fetchall()
        result = {}
        for interpretation_id, revision, requirement_id, text_hash, rule in rows:
            if texts.get(requirement_id) == text_hash:
                result[requirement_id] = {"interpretation_id": interpretation_id, "revision": revision,
                                          "rule": json.loads(rule)}
        return result


class EligibilityService:
    """Pins profile and call revisions and records replayable assessments."""

    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.funding_opportunities import FundingOpportunityStore
        from src.kb.funding_profiles import FundingProfileStore

        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.profiles = FundingProfileStore(conn, initialize=initialize, now=self.now)
        self.opportunities = FundingOpportunityStore(conn, initialize=initialize, now=self.now)
        self.interpretations = InterpretationStore(conn, initialize=initialize, now=self.now)

    def assess(self, namespace, profile_id, opportunity_id, *, principal_id, scopes,
               profile_revision=None, opportunity_revision=None, as_of=None):
        pinned = self.profiles.pinned_facts(
            namespace, profile_id,
            profile_revision or self.profiles.inspect(namespace, profile_id, principal_id=principal_id, scopes=scopes)["revision"],
            principal_id=principal_id, scopes=scopes, as_of=as_of)
        opportunity = self.opportunities.get(namespace, opportunity_id, revision=opportunity_revision, scopes=scopes)
        interpretations = self.interpretations.applicable(namespace, opportunity)
        result = assess(opportunity, pinned["facts"], interpretations=interpretations,
                        conflicts=opportunity.get("conflicts"))
        result.update(
            profile={"profile_id": profile_id, "revision": pinned["profile_revision"],
                     "unreviewed_facts": pinned["unreviewed"]},
            opportunity={"opportunity_id": opportunity_id, "revision": opportunity["revision"],
                         "record_kind": opportunity["record"]["record_kind"]},
            interpretations=sorted(f"{v['interpretation_id']}@{v['revision']}" for v in interpretations.values()),
        )
        if opportunity["record"]["record_kind"] in {"directory_entry", "award"}:
            result["verdict"] = "needs_clarification"
            result["clarifications"].append({"result": "not_an_opportunity", "reason":
                                             "directory listings and awards are not application calls"})
        assessment_id = "funding-assessment:" + digest(
            [namespace, principal_id, profile_id, pinned["profile_revision"], opportunity_id,
             opportunity["revision"], result["interpretations"], as_of])[:32]
        result["assessment_id"] = assessment_id
        self.conn.execute(
            "INSERT INTO funding_assessments VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [assessment_id, namespace, principal_id, profile_id, pinned["profile_revision"], opportunity_id,
             opportunity["revision"], result["verdict"], canonical(result), self.now()])
        return result
