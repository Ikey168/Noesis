"""Rank funding opportunities by fit, usable support and effort.

Hard eligibility (``funding_eligibility``) and competitive fit are separate
dimensions. The match score is a weighted mean over *known* criteria in
``[0, 1]``; it is an ordering aid, not a probability of receiving funding.
Loans, guarantees, equity and credits never count as usable cash. Programme
or topic budgets never count as an applicant's award size. Ineligible,
closed or non-opportunity records can never be bucketed ``apply_now``.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal

from src.kb.funding_records import CASH_EQUIVALENT, READ_SCOPE, canonical, digest

CONTRACT = "noesis-funding-shortlist-v1"
DEFAULT_WEIGHTS = {
    "topic_fit": 3, "usable_funding": 3, "deadline_feasibility": 2,
    "application_effort": 1, "cash_flow": 1, "obligations": 1,
}
BUCKETS = ("apply_now", "consider", "watch", "excluded")
_EFFORT_DAYS = {"low": 7, "medium": 21, "high": 45}
_STOP = {"and", "the", "for", "of", "in", "to", "a", "an", "on", "with", "und", "der", "die", "das", "für"}
_DDL = """
CREATE TABLE IF NOT EXISTS funding_shortlists(
 shortlist_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 profile_id TEXT NOT NULL, profile_revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL);
"""


class RankingError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _tokens(values):
    text = " ".join(values if isinstance(values, list) else [values or ""]).lower()
    return {t for t in re.findall(r"[a-zäöüß0-9]{3,}", text) if t not in _STOP}


def _topic_fit(record, facts):
    themes = facts.get("project.themes")
    if not themes:
        return None, ["project themes are unknown"]
    if not record.get("themes"):
        return None, ["the source states no themes for this record"]
    call = _tokens(record["themes"]) | _tokens(record["title"])
    matched = [t for t in themes if _tokens(t) & call]
    return len(matched) / len(themes), [f"matches project theme '{t}'" for t in matched] or ["no project theme matches call themes/title"]


def _usable_funding(record, facts):
    kinds = set((record.get("instrument") or {}).get("kinds") or ["unknown"])
    terms = record.get("financial_terms") or {}
    need = facts.get("project.funding_need")
    if not kinds <= CASH_EQUIVALENT:
        non_cash = sorted(kinds - CASH_EQUIVALENT)
        if "unknown" in non_cash:
            return None, ["instrument kind is unknown; usable cash cannot be assessed"]
        return 0.0, [f"{', '.join(non_cash)} support is not a cash grant and is not counted as usable funding"]
    award = terms.get("award_range") or {}
    if not need:
        return None, ["funding need is unknown"]
    if award.get("max") is None:
        reasons = ["award size per applicant is not stated"]
        if terms.get("programme_budget"):
            reasons.append("programme/topic budget is not an award size and is not used")
        return None, reasons
    if terms.get("currency") != need["currency"]:
        return None, [f"award currency {terms.get('currency')} differs from need currency {need['currency']}; no conversion applied"]
    maximum = Decimal(award["max"])
    if award["basis"] == "per-person-month":
        months, people = facts.get("project.duration_months"), facts.get("applicant.team_size")
        if not months or not people:
            return None, ["per-person-month support needs duration and team size"]
        maximum = maximum * months * people
        basis = f"up to {award['max']} {terms['currency']} per person-month × {people} × {months} months"
        if award.get("min") is not None and award["min"] != award["max"]:
            basis += f" (upper bound; stated rates range from {award['min']}, depending on applicant status)"
    elif award["basis"] == "per-project":
        basis = f"up to {award['max']} {terms['currency']} per project"
    else:
        return None, ["award basis is unknown"]
    wanted = Decimal(need["amount"])
    if wanted <= 0:
        return None, ["funding need must be positive"]
    score = float(min(maximum, wanted) / wanted)
    reasons = [basis + f" covers {round(score * 100)}% of the stated need"]
    if award.get("min") is not None and Decimal(award["min"]) > wanted:
        reasons.append(f"minimum award {award['min']} exceeds the stated need")
    return score, reasons


def _effort(record, status):
    requirements = record.get("requirements") or []
    stages = len(status.get("stages") or [])
    consortium = any(r["category"] == "consortium" for r in requirements)
    points = len(requirements) + 2 * max(stages - 1, 0) + (4 if consortium else 0) + len(record.get("documents") or []) // 3
    level = "high" if points >= 8 or consortium else "medium" if points >= 4 else "low"
    return level, [f"{len(requirements)} requirements, {max(stages, 1)} stage(s)" + (", consortium required" if consortium else "")]


def _deadline(status, facts, effort_level, as_of_ms):
    if status["state"] == "rolling":
        return 1.0, ["rolling submissions"]
    deadline = status.get("next_deadline")
    if not deadline:
        return None, ["no upcoming deadline is known"]
    if deadline.get("instant"):
        end = datetime.fromisoformat(deadline["instant"])
    else:
        end = datetime.fromisoformat(deadline["date"] + "T00:00:00+00:00")
    days = (end - datetime.fromtimestamp(as_of_ms / 1000, tz=UTC)).total_seconds() / 86400
    needed = facts.get("preferences.min_days_to_deadline") or _EFFORT_DAYS[effort_level]
    source = "your minimum preparation time" if "preferences.min_days_to_deadline" in facts else f"estimated {effort_level}-effort preparation"
    if days < needed:
        return 0.0, [f"{days:.0f} days left; below {source} of {needed} days"]
    return (1.0 if days >= 2 * needed else 0.6), [f"{days:.0f} days until {deadline['text']} ({source}: {needed} days)"]


def _cash_flow(record, facts):
    terms = record.get("financial_terms") or {}
    co = terms.get("co_financing") or {}
    reasons, score = [], 1.0
    if co.get("required") is None and terms.get("reimbursement") in (None, "unknown"):
        return None, ["co-financing and payment timing are not stated"]
    if co.get("required"):
        if facts.get("preferences.accept_co_financing") is False:
            return 0.0, ["co-financing is required but you do not accept co-financing"]
        need, own = facts.get("project.funding_need"), facts.get("project.matching_funds_available")
        if co.get("min_percent") and need and own and own["currency"] == need["currency"]:
            required = Decimal(need["amount"]) * Decimal(co["min_percent"]) / 100
            if Decimal(own["amount"]) < required:
                return 0.0, [f"requires {co['min_percent']}% co-financing (~{required:.0f} {need['currency']}); {own['amount']} available"]
            reasons.append(f"co-financing of {co['min_percent']}% is covered by available matching funds")
        else:
            score -= 0.3
            reasons.append("co-financing required; coverage by matching funds unknown")
    if terms.get("reimbursement") == "arrears":
        score -= 0.3
        reasons.append("paid in arrears; pre-financing needed")
    return max(score, 0.0), reasons or ["no co-financing or pre-financing burden stated"]


def _obligations(record, facts):
    kinds = set((record.get("instrument") or {}).get("kinds") or [])
    obligations = []
    if kinds & {"loan"}:
        obligations.append("repayment")
    if kinds & {"equity"}:
        obligations.append("equity dilution")
    for requirement in record.get("requirements") or []:
        if requirement["category"] == "licensing":
            obligations.append("open licensing of results")
    if not obligations:
        return 1.0, ["no repayment, dilution or licensing obligation stated"]
    return max(1.0 - 0.3 * len(obligations), 0.0), ["obligation: " + o for o in obligations]


def score_opportunity(opportunity, assessment, facts, *, weights=None, as_of_ms):
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    if set(weights) - set(DEFAULT_WEIGHTS) or any(type(v) not in (int, float) or v < 0 for v in weights.values()):
        raise RankingError("invalid_weights", "weights are nonnegative numbers for declared criteria")
    record, status = opportunity["record"], opportunity["status"]
    effort_level, effort_reasons = _effort(record, status)
    max_effort = facts.get("preferences.max_effort")
    effort_score = {"low": 1.0, "medium": 0.6, "high": 0.3}[effort_level]
    if max_effort and ["low", "medium", "high"].index(effort_level) > ["low", "medium", "high"].index(max_effort):
        effort_score = 0.0
        effort_reasons.append(f"exceeds your maximum effort ({max_effort})")
    criteria = {
        "topic_fit": _topic_fit(record, facts),
        "usable_funding": _usable_funding(record, facts),
        "deadline_feasibility": _deadline(status, facts, effort_level, as_of_ms),
        "application_effort": (effort_score, effort_reasons),
        "cash_flow": _cash_flow(record, facts),
        "obligations": _obligations(record, facts),
    }
    known = {k: v[0] for k, v in criteria.items() if v[0] is not None}
    total = sum(weights[k] for k in known)
    score = round(sum(weights[k] * v for k, v in known.items()) / total, 4) if total else None
    state, verdict = status["state"], assessment["verdict"]
    if verdict == "ineligible" or state in {"closed", "not_an_opportunity"} and record["record_kind"] != "directory_entry":
        bucket = "excluded"
    elif record["record_kind"] == "directory_entry":
        bucket = "watch"
    elif state in {"open", "rolling"} and verdict == "eligible" and criteria["deadline_feasibility"][0] != 0.0:
        bucket = "apply_now"
    elif state in {"open", "rolling"}:
        bucket = "consider"
    else:
        bucket = "watch"
    reasons = {k: {"score": v[0], "reasons": v[1], "weight": weights[k]} for k, v in criteria.items()}
    return {
        "opportunity_id": opportunity["opportunity_id"], "revision": opportunity["revision"],
        "title": record["title"], "provider": record["provider"], "record_kind": record["record_kind"],
        "source_url": record["source_url"], "state": state, "state_reasons": status["reasons"],
        "next_deadline": status.get("next_deadline"), "verdict": verdict,
        "disqualifiers": assessment["disqualifiers"], "clarifications": assessment["clarifications"],
        "bucket": bucket, "match_score": score,
        "score_coverage": round(total / sum(weights.values()), 4) if sum(weights.values()) else 0,
        "criteria": reasons, "unknown_criteria": sorted(k for k, v in criteria.items() if v[0] is None),
        "effort": effort_level, "instrument_kinds": (record.get("instrument") or {}).get("kinds"),
        "source_freshness": opportunity.get("source_freshness"),
        "assessment_id": assessment.get("assessment_id"),
    }


def _order(items):
    return sorted(items, key=lambda i: (BUCKETS.index(i["bucket"]), -(i["match_score"] or 0), i["opportunity_id"]))


def sensitivity(opportunities, assessments, facts, *, weights, as_of_ms):
    """How the order changes when each criterion is dropped or doubled."""
    base = [i["opportunity_id"] for i in _order([
        score_opportunity(o, assessments[o["opportunity_id"]], facts, weights=weights, as_of_ms=as_of_ms) for o in opportunities])]
    result = {}
    for criterion in DEFAULT_WEIGHTS:
        for label, factor in (("dropped", 0), ("doubled", 2)):
            changed = {**DEFAULT_WEIGHTS, **(weights or {})}
            changed[criterion] = changed[criterion] * factor
            order = [i["opportunity_id"] for i in _order([
                score_opportunity(o, assessments[o["opportunity_id"]], facts, weights=changed, as_of_ms=as_of_ms) for o in opportunities])]
            moved = [oid for oid in base if order.index(oid) != base.index(oid)]
            result[f"{criterion}:{label}"] = {"changed_positions": len(moved), "top_changed": bool(order[:1] != base[:1])}
    return result


class ShortlistService:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.funding_eligibility import EligibilityService

        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.eligibility = EligibilityService(conn, initialize=initialize, now=self.now)

    def build(self, namespace, profile_id, *, principal_id, scopes, weights=None, providers=None, as_of_ms=None):
        as_of_ms = as_of_ms or self.now()
        as_of_date = datetime.fromtimestamp(as_of_ms / 1000, tz=UTC).date().isoformat()
        profile = self.eligibility.profiles.inspect(namespace, profile_id, principal_id=principal_id, scopes=scopes)
        pinned = self.eligibility.profiles.pinned_facts(namespace, profile_id, profile["revision"],
                                                        principal_id=principal_id, scopes=scopes, as_of=as_of_date)
        opportunities = self.eligibility.opportunities.list(namespace, scopes=scopes, providers=providers,
                                                            kinds=["call", "programme", "directory_entry"], as_of_ms=as_of_ms)
        assessments = {o["opportunity_id"]: self.eligibility.assess(
            namespace, profile_id, o["opportunity_id"], principal_id=principal_id, scopes=scopes,
            profile_revision=profile["revision"], opportunity_revision=o["revision"], as_of=as_of_date)
            for o in opportunities}
        items = _order([score_opportunity(o, assessments[o["opportunity_id"]], pinned["facts"], weights=weights, as_of_ms=as_of_ms)
                        for o in opportunities])
        content = {
            "contract": CONTRACT, "namespace": namespace, "profile_id": profile_id,
            "profile_revision": profile["revision"], "as_of_ms": as_of_ms,
            "weights": {**DEFAULT_WEIGHTS, **(weights or {})},
            "score_semantics": "weighted mean of known criteria in [0,1]; not a probability of funding",
            "eligibility_semantics": "requirements assessment, not a funder decision",
            "items": items,
            "buckets": {b: [i["opportunity_id"] for i in items if i["bucket"] == b] for b in BUCKETS},
            "sensitivity": sensitivity(opportunities, assessments, pinned["facts"], weights=weights, as_of_ms=as_of_ms) if opportunities else {},
            "unknown_profile_facts": profile["unknown_facts"],
            "providers": {p: self.eligibility.opportunities.provider_state(namespace, p)
                          for p in sorted({o["record"]["provider"] for o in opportunities})},
        }
        shortlist_id = "funding-shortlist:" + digest([namespace, principal_id, profile_id, profile["revision"], as_of_ms, content["weights"]])[:32]
        content["shortlist_id"] = shortlist_id
        self.conn.execute("INSERT INTO funding_shortlists VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [shortlist_id, namespace, principal_id, profile_id, profile["revision"], canonical(content), self.now()])
        self.eligibility.opportunities.register_view(
            namespace, shortlist_id, principal_id, {o["opportunity_id"]: o["revision"] for o in opportunities})
        return content

    def inspect(self, namespace, shortlist_id, *, principal_id, scopes):
        if READ_SCOPE not in scopes:
            raise RankingError("unauthorized", "funding read scope is required")
        row = self.conn.execute(
            "SELECT owner, profile_id, content_json FROM funding_shortlists WHERE shortlist_id=? AND namespace=?",
            [shortlist_id, namespace]).fetchone()
        if not row or row[0] != principal_id:
            raise RankingError("shortlist_not_found", "shortlist is unavailable")
        # Re-check current profile access so revocation/withdrawal applies.
        self.eligibility.profiles.inspect(namespace, row[1], principal_id=principal_id, scopes=scopes)
        return {**json.loads(row[2]), "freshness": self.eligibility.opportunities.view_status(shortlist_id)}
