"""Rank procurement opportunities per lot by fit, value and effort, with award history as context.

The structure is the Funding & Grants ranking (:mod:`src.kb.funding_ranking`):
eligibility and fit are separate dimensions, every criterion reports its
score, weight and reasons, the match score is a weighted mean over *known*
criteria in ``[0, 1]`` with its coverage, items fall into the same buckets
(``apply_now`` here means "prepare a bid now"), and a sensitivity table shows
how the order moves when a criterion is dropped or doubled. The score is an
ordering aid, never a probability of winning.

Ranking inputs per lot: CPV overlap, jurisdiction, lot value, submission
effort, time to deadline and incumbent history, each with its source. Past
awards for the same buyer or CPV are listed as *context*; they never make a
procedure open. A shortlist pins the profile revision, every procedure
revision and the award rows it used, so :meth:`ShortlistService.replay`
reproduces it exactly from those pins.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal

from src.kb.funding_ranking import BUCKETS
from src.kb.funding_records import canonical, digest
from src.kb.procurement_eligibility import ProcurementEligibilityService, assess_lot
from src.kb.procurement_identity import ProcurementIdentityService
from src.kb.procurement_notices import effective_status, lot_ids, lot_view
from src.kb.procurement_records import READ_SCOPE, cpv_relation

CONTRACT = "noesis-procurement-shortlist-v1"
DEFAULT_WEIGHTS = {"cpv_fit": 3, "jurisdiction": 2, "value_fit": 2, "submission_effort": 1,
                   "deadline_feasibility": 2, "incumbency_context": 1}
SOURCES = {
    "cpv_fit": "notice/lot CPV classifications vs profile supplier.cpv_interests",
    "jurisdiction": "notice place of performance (or buyer country) vs profile supplier.jurisdictions",
    "value_fit": "lot estimated value (currency and VAT basis as stated) vs profile preferences.min/max_contract_value",
    "submission_effort": "count of cited requirements, documents and procedure type vs profile preferences.max_effort",
    "deadline_feasibility": "lot submission deadline (original text and offset) vs profile preferences.min_days_to_deadline",
    "incumbency_context": "acquired award history for the same buyer and CPV branch (context only)",
}
_EFFORT_DAYS = {"low": 10, "medium": 20, "high": 35}
_TWO_STAGE = {"restricted", "negotiated-with-call", "competitive-dialogue", "innovation-partnership", "competitive-flexible"}
_DDL = """
CREATE TABLE IF NOT EXISTS procurement_shortlists(
 shortlist_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, profile_id TEXT NOT NULL,
 profile_revision BIGINT NOT NULL, content_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
"""


class RankingError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _cpv_fit(view, facts):
    interests = facts.get("supplier.cpv_interests")
    if not interests:
        return None, ["CPV interests are unknown"]
    if not view["cpv"]:
        return None, ["the notice states no CPV code for this lot"]
    best, reasons = 0.0, []
    for code in view["cpv"]:
        for interest in interests:
            relation = cpv_relation(interest, code)
            score = {"covers": 1.0, "same-division": 0.4}.get(relation, 0.0)
            if score:
                reasons.append(f"CPV {code} {'lies within' if relation == 'covers' else 'shares the division of'} your interest {interest}")
            best = max(best, score)
    return best, reasons or [f"CPV {', '.join(view['cpv'])} is outside your stated interests"]


def _jurisdiction(record, facts):
    wanted = facts.get("supplier.jurisdictions")
    places = (record.get("place_of_performance") or {}).get("countries") or ([record["buyer"]["country"]] if record["buyer"].get("country") else [])
    if not wanted:
        return None, ["your jurisdictions are unknown"]
    if not places:
        return None, ["the notice states no place of performance or buyer country"]
    inside = sorted(set(places) & set(wanted))
    if inside:
        return 1.0, [f"performed in {', '.join(inside)} (within your jurisdictions)"]
    return 0.0, [f"performed in {', '.join(places)}; outside your stated jurisdictions {', '.join(wanted)}"]


def _value_fit(view, facts):
    estimated = view.get("estimated_value")
    low, high = facts.get("preferences.min_contract_value"), facts.get("preferences.max_contract_value")
    if not estimated or estimated.get("amount") is None:
        return None, ["the lot's estimated value is not stated"]
    basis = f"estimated {estimated['amount']} {estimated['currency']} (VAT {estimated.get('vat', 'unknown')}; estimated, not awarded)"
    if not low and not high:
        return None, [basis, "your contract value range is unknown"]
    amount = Decimal(estimated["amount"])
    for bound in (low, high):
        if bound and bound["currency"] != estimated["currency"]:
            return None, [basis, f"your range is in {bound['currency']}; no currency conversion is applied"]
    if high and amount > Decimal(high["amount"]):
        return 0.0, [basis, f"above your maximum of {high['amount']} {high['currency']}"]
    if low and amount < Decimal(low["amount"]):
        return 0.3, [basis, f"below your minimum of {low['amount']} {low['currency']}"]
    return 1.0, [basis, "within your contract value range"]


def _effort(record, view):
    requirements = [r for r in view["requirements"]]
    two_stage = (record.get("procedure") or {}).get("type") in _TWO_STAGE
    unparsed = sum(1 for r in requirements if r.get("machine_rule") is None and r["category"] not in {"document", "submission-route"})
    points = len(requirements) + 3 * two_stage + len(record.get("documents") or []) + 2 * unparsed
    level = "high" if points >= 12 else "medium" if points >= 6 else "low"
    return level, [f"{len(requirements)} cited requirement(s), {len(record.get('documents') or [])} document set(s)"
                   + (", two-stage procedure" if two_stage else "") + (f", {unparsed} criterion/criteria needing manual review" if unparsed else "")]


def _deadline(lot_status, facts, effort_level, as_of_ms):
    deadline = lot_status.get("next_deadline")
    if not deadline:
        return None, ["no upcoming submission deadline is known"]
    end = datetime.fromisoformat((deadline.get("instant") or deadline["date"] + "T00:00:00+00:00").replace("Z", "+00:00"))
    days = (end - datetime.fromtimestamp(as_of_ms / 1000, tz=UTC)).total_seconds() / 86400
    needed = facts.get("preferences.min_days_to_deadline") or _EFFORT_DAYS[effort_level]
    source = "your minimum preparation time" if "preferences.min_days_to_deadline" in facts else f"estimated {effort_level}-effort preparation"
    text = f"deadline as published: '{deadline['text']}'" + ("" if deadline.get("instant") else " (no offset stated)")
    if days < needed:
        return 0.0, [f"{days:.0f} days left; below {source} of {needed} days", text]
    return (1.0 if days >= 2 * needed else 0.6), [f"{days:.0f} days left ({source}: {needed} days)", text]


def _incumbency(incumbency):
    if not incumbency["incumbents"]:
        return None, [incumbency["explanation"]]
    own = [i for i in incumbency["incumbents"] if i["is_profile_supplier"]]
    if own:
        return 1.0, ["you hold or held a comparable award from this buyer: " + own[0]["awards"][0]["notice_id"], incumbency["explanation"]]
    return 0.5, [incumbency["explanation"], "another supplier is the incumbent (context, not a probability)"]


def score_item(procedure, lot_id, lot_status, assessment, facts, incumbency, award_context, *, weights, as_of_ms):
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    if set(weights) - set(DEFAULT_WEIGHTS) or any(type(v) not in (int, float) or v < 0 for v in weights.values()):
        raise RankingError("invalid_weights", "weights are nonnegative numbers for declared criteria")
    record = procedure["record"]
    view = lot_view(record, lot_id)
    effort_level, effort_reasons = _effort(record, view)
    effort_score = {"low": 1.0, "medium": 0.6, "high": 0.3}[effort_level]
    max_effort = facts.get("preferences.max_effort")
    if max_effort and ["low", "medium", "high"].index(effort_level) > ["low", "medium", "high"].index(max_effort):
        effort_score = 0.0
        effort_reasons.append(f"exceeds your maximum effort ({max_effort})")
    criteria = {
        "cpv_fit": _cpv_fit(view, facts),
        "jurisdiction": _jurisdiction(record, facts),
        "value_fit": _value_fit(view, facts),
        "submission_effort": (effort_score, effort_reasons),
        "deadline_feasibility": _deadline(lot_status, facts, effort_level, as_of_ms),
        "incumbency_context": _incumbency(incumbency),
    }
    known = {k: v[0] for k, v in criteria.items() if v[0] is not None}
    total = sum(weights[k] for k in known)
    score = round(sum(weights[k] * v for k, v in known.items()) / total, 4) if total else None
    state, verdict = lot_status["state"], assessment["verdict"]
    if verdict == "ineligible" or state in {"closed", "cancelled", "awarded"}:
        bucket = "excluded"
    elif (state == "open" and verdict == "eligible" and criteria["deadline_feasibility"][0] not in (None, 0.0)
          and criteria["cpv_fit"][0] != 0.0 and criteria["jurisdiction"][0] != 0.0):
        bucket = "apply_now"
    elif state in {"open", "unconfirmed"}:
        bucket = "consider"
    else:
        bucket = "watch"
    return {
        "item_id": f"{procedure['procedure_key']}#{lot_id or '_'}", "procedure_key": procedure["procedure_key"],
        "revision": procedure["revision"], "lot_id": lot_id, "title": record["title"], "lot_title": view["title"],
        "provider": procedure["provider"], "procedure_id": procedure["procedure_id"], "notice": procedure["cause"],
        "source_url": record["source_url"], "buyer": record["buyer"]["name"], "state": state, "state_reasons": lot_status["reasons"],
        "next_deadline": lot_status.get("next_deadline"), "verdict": verdict,
        "disqualifiers": assessment["disqualifiers"], "clarifications": assessment["clarifications"],
        "missing_facts": assessment["missing_facts"], "bucket": bucket, "match_score": score,
        "score_coverage": round(total / sum(weights.values()), 4) if sum(weights.values()) else 0,
        "criteria": {k: {"score": v[0], "reasons": v[1], "weight": weights[k], "source": SOURCES[k]} for k, v in criteria.items()},
        "unknown_criteria": sorted(k for k, v in criteria.items() if v[0] is None), "effort": effort_level,
        "estimated_value": view.get("estimated_value"),
        "award_context": award_context,
        "award_semantics": "past awards are context only; they never show that this procedure is open",
        "source_freshness": procedure.get("source_freshness"),
    }


def _order(items):
    return sorted(items, key=lambda i: (BUCKETS.index(i["bucket"]), -(i["match_score"] or 0), i["item_id"]))


class ShortlistService:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.eligibility = ProcurementEligibilityService(conn, initialize=initialize, now=self.now)
        self.identity = ProcurementIdentityService(conn, initialize=initialize, now=self.now)
        self.notices = self.eligibility.notices
        self.profiles = self.eligibility.profiles

    def _context(self, namespace, scopes, procedure, lot_id, facts, awards):
        record = procedure["record"]
        view = lot_view(record, lot_id)
        cpv = view["cpv"] or [c["code"] for c in record.get("classifications") or [] if c["scheme"] == "CPV"]
        incumbency = self.identity.incumbency(namespace, scopes=scopes, buyer=record["buyer"], cpv=cpv, rows=awards,
                                              supplier_names=[facts.get("supplier.legal_name")]) if cpv else {
            "incumbents": [], "explanation": "the notice states no CPV code; incumbency cannot be derived"}
        context = []
        for row in awards:
            same_buyer = row["buyer"]["name"].casefold() == record["buyer"]["name"].casefold()
            same_cpv = any(cpv_relation(a, b) == "covers" or cpv_relation(b, a) == "covers" for a in cpv for b in row["cpv"])
            if (same_buyer or same_cpv) and row["procedure_key"] != procedure["procedure_key"]:
                context.append({"award_key": row["award_key"], "relation": "same buyer and CPV" if same_buyer and same_cpv
                                else "same buyer" if same_buyer else "same CPV branch", "date": row["date"],
                                "buyer": row["buyer"]["name"], "suppliers": [s["name"] for s in row["suppliers"]],
                                "value": row["value"], "notice_id": row["notice_id"], "source_url": row["source_url"]})
        return incumbency, context[:5]

    def _items(self, namespace, scopes, procedures, facts, awards, *, weights, as_of_ms):
        items, assessments = [], {}
        for procedure in procedures:
            status = effective_status(procedure, [{"award_key": a["award_key"], "stage": a["stage"], "content": {"lot_ids": a["lot_ids"], "status": a["status"]}}
                                                  for a in awards if a["procedure_key"] == procedure["procedure_key"]],
                                      as_of_ms=as_of_ms, listing_state=procedure.get("listing_state", "listed"),
                                      source_stale=(procedure.get("source_freshness") or {}).get("stale", False))
            for lot_id in lot_ids(procedure["record"]):
                assessment = assess_lot(procedure, lot_id, facts)
                assessments[f"{procedure['procedure_key']}#{lot_id or '_'}"] = assessment
                incumbency, context = self._context(namespace, scopes, procedure, lot_id, facts, awards)
                items.append(score_item(procedure, lot_id, status["lots"][lot_id or "_"], assessment, facts, incumbency, context,
                                        weights=weights, as_of_ms=as_of_ms))
        return _order(items), assessments

    def _sensitivity(self, namespace, scopes, procedures, facts, awards, *, weights, as_of_ms):
        base = [i["item_id"] for i in self._items(namespace, scopes, procedures, facts, awards, weights=weights, as_of_ms=as_of_ms)[0]]
        result = {}
        for criterion in DEFAULT_WEIGHTS:
            for label, factor in (("dropped", 0), ("doubled", 2)):
                changed = {**DEFAULT_WEIGHTS, **(weights or {})}
                changed[criterion] = changed[criterion] * factor
                order = [i["item_id"] for i in self._items(namespace, scopes, procedures, facts, awards, weights=changed, as_of_ms=as_of_ms)[0]]
                result[f"{criterion}:{label}"] = {"changed_positions": sum(1 for a, b in zip(base, order) if a != b),
                                                  "top_changed": order[:1] != base[:1]}
        return result

    def _content(self, namespace, scopes, profile_id, profile_revision, facts, procedures, awards, *, weights, as_of_ms):
        items, _ = self._items(namespace, scopes, procedures, facts, awards, weights=weights, as_of_ms=as_of_ms)
        return {
            "contract": CONTRACT, "namespace": namespace, "profile_id": profile_id, "profile_revision": profile_revision,
            "as_of_ms": as_of_ms, "weights": {**DEFAULT_WEIGHTS, **(weights or {})},
            "score_semantics": "weighted mean of known criteria in [0,1]; an ordering aid, not a probability of winning",
            "eligibility_semantics": "requirements assessment per lot, not the contracting authority's decision",
            "bucket_semantics": {"apply_now": "open, eligible on stated facts, feasible deadline, inside your CPV interests and jurisdictions: prepare a bid (Noesis never submits)",
                                 "consider": "open but clarification, deadline or confirmation needed",
                                 "watch": "forthcoming or status unknown", "excluded": "ineligible, closed, cancelled or awarded"},
            "items": items, "buckets": {b: [i["item_id"] for i in items if i["bucket"] == b] for b in BUCKETS},
            "sensitivity": self._sensitivity(namespace, scopes, procedures, facts, awards, weights=weights, as_of_ms=as_of_ms) if procedures else {},
            "pins": {"procedures": {p["procedure_key"]: p["revision"] for p in procedures},
                     "listing": {p["procedure_key"]: p.get("listing_state", "listed") for p in procedures},
                     "awards": sorted(a["award_key"] for a in awards),
                     "identity_links": digest([(link["link_id"], link["status"]) for link in self.identity.audit(namespace, scopes=scopes)])},
        }

    def build(self, namespace, profile_id, *, principal_id, scopes, weights=None, providers=None, as_of_ms=None):
        as_of_ms = as_of_ms or self.now()
        as_of_date = datetime.fromtimestamp(as_of_ms / 1000, tz=UTC).date().isoformat()
        profile = self.profiles.inspect(namespace, profile_id, principal_id=principal_id, scopes=scopes)
        if profile.get("profile_kind") != "supplier":
            raise RankingError("not_a_supplier_profile", "shortlists are built for supplier profiles")
        pinned = self.profiles.pinned_facts(namespace, profile_id, profile["revision"], principal_id=principal_id, scopes=scopes, as_of=as_of_date)
        procedures = self.notices.list(namespace, scopes=scopes, providers=providers, as_of_ms=as_of_ms)
        awards = self.notices.award_history(namespace, scopes=scopes, limit=5000)
        content = self._content(namespace, scopes, profile_id, profile["revision"], pinned["facts"], procedures, awards,
                                weights=weights, as_of_ms=as_of_ms)
        content["unknown_profile_facts"] = profile["unknown_facts"]
        content["providers"] = {p: self.notices.provider_state(namespace, p) for p in sorted({o["provider"] for o in procedures})}
        shortlist_id = "procurement-shortlist:" + digest([namespace, principal_id, profile_id, profile["revision"], as_of_ms,
                                                          content["weights"], content["pins"]])[:32]
        content["shortlist_id"] = shortlist_id
        content["content_digest"] = digest({k: content[k] for k in ("items", "buckets", "weights", "pins")})
        self.conn.execute("INSERT INTO procurement_shortlists VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [shortlist_id, namespace, principal_id, profile_id, profile["revision"], canonical(content), self.now()])
        self.notices.register_view(namespace, shortlist_id, principal_id, content["pins"]["procedures"])
        return content

    def _row(self, namespace, shortlist_id, principal_id, scopes):
        if READ_SCOPE not in scopes:
            raise RankingError("unauthorized", "procurement read scope is required")
        row = self.conn.execute("SELECT owner, profile_id, content_json FROM procurement_shortlists WHERE shortlist_id=? AND namespace=?",
                                [shortlist_id, namespace]).fetchone()
        if not row or row[0] != principal_id:
            raise RankingError("shortlist_not_found", "shortlist is unavailable")
        # Re-check current profile access so revocation and withdrawal apply.
        self.profiles.inspect(namespace, row[1], principal_id=principal_id, scopes=scopes)
        return json.loads(row[2])

    def inspect(self, namespace, shortlist_id, *, principal_id, scopes):
        content = self._row(namespace, shortlist_id, principal_id, scopes)
        return {**content, "freshness": self.notices.view_status(shortlist_id)}

    def replay(self, namespace, shortlist_id, *, principal_id, scopes):
        """Recompute a stored shortlist from its pinned profile, procedure and award revisions."""
        content = self._row(namespace, shortlist_id, principal_id, scopes)
        as_of_date = datetime.fromtimestamp(content["as_of_ms"] / 1000, tz=UTC).date().isoformat()
        facts = self.profiles.pinned_facts(namespace, content["profile_id"], content["profile_revision"], principal_id=principal_id,
                                           scopes=scopes, as_of=as_of_date)["facts"]
        procedures = []
        for key, revision in sorted(content["pins"]["procedures"].items()):
            current = self.notices.get(namespace, key, scopes=scopes, revision=revision, as_of_ms=content["as_of_ms"])
            procedures.append({**current, "listing_state": content["pins"]["listing"][key],
                               "source_freshness": next((i["source_freshness"] for i in content["items"]
                                                         if i["procedure_key"] == key), current["source_freshness"])})
        pinned_awards = set(content["pins"]["awards"])
        awards = [a for a in self.notices.award_history(namespace, scopes=scopes, limit=5000) if a["award_key"] in pinned_awards]
        replayed = self._content(namespace, scopes, content["profile_id"], content["profile_revision"], facts, procedures, awards,
                                 weights=content["weights"], as_of_ms=content["as_of_ms"])
        replay_digest = digest({k: replayed[k] for k in ("items", "buckets", "weights", "pins")})
        return {"shortlist_id": shortlist_id, "replayed_digest": replay_digest, "stored_digest": content["content_digest"],
                "identical": replay_digest == content["content_digest"],
                "identity_links_changed": replayed["pins"]["identity_links"] != content["pins"]["identity_links"],
                "items": replayed["items"]}
