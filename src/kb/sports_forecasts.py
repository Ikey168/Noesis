"""Sports forecasts in the existing binary forecast ledger, resolved only against official results (#2143, SP08).

A user registers their own forecast through :meth:`src.kb.forecasts.ForecastStore.create`
with their own probability and evidence; this module only pins the structured
rule it resolves by. Noesis produces no forecast, probability, odds or tip: a
registration without the user's probability is refused.

Rules:

* ``match_outcome`` - a fixture ends ``home_win``, ``draw`` or ``away_win``;
* ``advancement`` - a team wins a (knockout) fixture; a drawn published score
  does not decide it;
* ``final_position`` - a team finishes at or above a position in a season's
  table, replayed from official results under the stated rule once every
  fixture has one.

``propose_forecast_resolution`` consults the rule: only ``official``,
``corrected`` or ``forfeit_awarded`` revisions propose an outcome; a
provisional result never does; a postponed, cancelled, abandoned or annulled
match leaves the forecast ``unresolved`` with the reason. A correction
published after the forecast was resolved is surfaced as a reviewable
re-resolution, never an automatic flip. Scoring reuses
``score_binary_forecasts`` with the cutoff stated.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.sports_records import (
    OFFICIAL_CLASS,
    READ_SCOPE,
    SportsError,
    authorize,
    canonical,
    table_exists,
)
from src.kb.sports_store import SportsStore

RULE_KINDS = ("match_outcome", "advancement", "final_position")
OUTCOMES = ("home_win", "draw", "away_win")
_DDL = """
CREATE TABLE IF NOT EXISTS sports_forecast_rules (
  forecast_namespace TEXT NOT NULL, forecast_id TEXT NOT NULL, namespace TEXT NOT NULL, rule_json TEXT NOT NULL,
  registered_by TEXT NOT NULL, registered_at_ms BIGINT NOT NULL, PRIMARY KEY(forecast_namespace, forecast_id)
);
"""


def _rule(
    store: SportsStore, namespace: str, rule: Mapping[str, Any]
) -> dict[str, Any]:
    rule = dict(rule)
    kind = rule.get("kind")
    allowed = {
        "match_outcome": {"kind", "fixture_key", "outcome"},
        "advancement": {"kind", "fixture_key", "team_key"},
        "final_position": {"kind", "season_key", "team_key", "position_at_most"},
    }
    if kind not in RULE_KINDS or set(rule) != allowed[kind]:
        raise SportsError(
            "invalid_rule",
            f"a sports rule is one of {RULE_KINDS} with exactly its fields",
        )
    if kind == "match_outcome" and rule["outcome"] not in OUTCOMES:
        raise SportsError("invalid_rule", f"a match outcome is one of {OUTCOMES}")
    if "fixture_key" in rule:
        fixture = store.body(namespace, "fixture", rule["fixture_key"])
        if fixture is None:
            raise SportsError(
                "not_found",
                "the rule names a fixture that is not visible in this namespace",
            )
        if set(dict(fixture.get("sides") or {})) != {"home", "away"}:
            raise SportsError("invalid_rule", "match rules name a home/away fixture")
        if kind == "advancement" and rule["team_key"] not in fixture["sides"].values():
            raise SportsError("invalid_rule", "the team plays in the fixture")
    if kind == "final_position":
        if store.current(namespace, "season", rule["season_key"]) is None:
            raise SportsError(
                "not_found",
                "the rule names a season that is not visible in this namespace",
            )
        if type(rule["position_at_most"]) is not int or rule["position_at_most"] < 1:
            raise SportsError("invalid_rule", "a position is a positive integer")
    return rule


def describe(rule: Mapping[str, Any]) -> str:
    if rule["kind"] == "match_outcome":
        return f"fixture {rule['fixture_key']} ends in a {rule['outcome'].replace('_', ' ')}"
    if rule["kind"] == "advancement":
        return f"{rule['team_key']} wins fixture {rule['fixture_key']}"
    return f"{rule['team_key']} finishes {rule['season_key']} in position {rule['position_at_most']} or higher"


class SportsForecasts:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        from src.kb.forecasts import ForecastStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = SportsStore(conn, initialize=initialize, now=self.now)
        self.forecasts = ForecastStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def register(
        self,
        namespace: str,
        forecast_namespace: str,
        request_key: str,
        *,
        rule: Mapping[str, Any],
        probability: float | None,
        resolution_at_ms: int,
        evidence: list[dict[str, Any]],
        principal_id: str,
        scopes: Iterable[str],
        question: str | None = None,
    ) -> dict[str, Any]:
        """Create the user's own forecast in the ledger and pin the sports rule it resolves by (official only)."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if probability is None:
            raise SportsError(
                "forecast_refused",
                "Noesis produces no forecast, probability, odds or tip; register your own probability",
            )
        self.store.require_ready(namespace)
        rule = _rule(self.store, namespace, rule)
        outcome_rule = (
            f"Sports rule ({namespace}): resolves 1 if {describe(rule)} in the official result "
            "(official, corrected or forfeit_awarded revision), 0 otherwise; provisional results never resolve it; "
            "a postponed, cancelled, abandoned or annulled match leaves it unresolved."
        )
        created = self.forecasts.create(
            forecast_namespace,
            request_key,
            question=question or f"Will {describe(rule)}?",
            outcome_rule=outcome_rule,
            resolution_at_ms=resolution_at_ms,
            probability=probability,
            evidence=evidence,
            principal_id=principal_id,
            scopes=scopes,
        )
        stored = {"namespace": namespace, "rule": rule}
        prior = rule_for(self.conn, forecast_namespace, created["forecast_id"])
        if prior is None:
            self.conn.execute(
                "INSERT INTO sports_forecast_rules VALUES (?,?,?,?,?,?)",
                [
                    forecast_namespace,
                    created["forecast_id"],
                    namespace,
                    canonical(rule),
                    principal_id,
                    self.now(),
                ],
            )
        elif canonical(prior) != canonical(stored):
            raise SportsError(
                "idempotency_conflict",
                "the forecast is registered with a different sports rule",
            )
        return {**created, "sports_rule": stored}

    def scoring_cutoff(
        self, forecast_namespace: str, forecast_ids: list[str], *, scopes: Iterable[str]
    ) -> int:
        """The earliest official-result publication among the cohort; refused before every rule has one."""
        scopes = set(scopes)
        times = []
        for forecast_id in forecast_ids:
            pinned = rule_for(self.conn, forecast_namespace, forecast_id)
            if pinned is None:
                raise SportsError(
                    "not_a_sports_forecast", f"{forecast_id} has no sports rule"
                )
            authorize(pinned["namespace"], scopes, READ_SCOPE)
            decided = evaluate(self.store, pinned["namespace"], pinned["rule"])
            if decided["outcome"] is None:
                raise SportsError(
                    "not_official", f"{forecast_id}: {decided['reason']}; scoring waits"
                )
            times.append(max(r["published_ms"] for r in decided["revisions"]))
        return min(times)

    def score(
        self, forecast_namespace, forecast_ids, *, principal_id, scopes
    ) -> dict[str, Any]:
        cutoff = self.scoring_cutoff(forecast_namespace, forecast_ids, scopes=scopes)
        scored = self.forecasts.score(
            forecast_namespace,
            forecast_ids,
            cutoff_ms=cutoff,
            principal_id=principal_id,
            scopes=set(scopes),
        )
        return {
            **scored,
            "cutoff_basis": "earliest official result publication among the cohort's rules",
        }


def rule_for(
    conn: Any, forecast_namespace: str, forecast_id: str
) -> dict[str, Any] | None:
    if not table_exists(conn, "sports_forecast_rules"):
        return None
    row = conn.execute(
        "SELECT namespace, rule_json FROM sports_forecast_rules WHERE forecast_namespace=? AND forecast_id=?",
        [forecast_namespace, forecast_id],
    ).fetchone()
    return None if row is None else {"namespace": row[0], "rule": json.loads(row[1])}


def _match(store: SportsStore, namespace: str, fixture_key: str) -> dict[str, Any]:
    fixture = store.body(namespace, "fixture", fixture_key) or {}
    result = store.current(namespace, "match_result_revision", fixture_key)
    schedule = store.current(namespace, "fixture_schedule_revision", fixture_key)
    status = (schedule or {}).get("body", {}).get("status")
    if result is None:
        reason = (
            f"fixture-{status}"
            if status in {"postponed", "cancelled", "abandoned"}
            else "no-published-result"
        )
        return {"reason": reason, "revision": None, "sides": fixture.get("sides") or {}}
    declared = result["body"]["status"]
    if declared == "annulled":
        return {
            "reason": "result-annulled",
            "revision": result,
            "sides": fixture["sides"],
        }
    if declared not in OFFICIAL_CLASS or "score" not in result["body"]:
        return {
            "reason": "no-official-result",
            "revision": result,
            "provisional_available": True,
            "sides": fixture["sides"],
        }
    return {"reason": None, "revision": result, "sides": fixture["sides"]}


def evaluate(
    store: SportsStore, namespace: str, rule: Mapping[str, Any]
) -> dict[str, Any]:
    """The outcome a rule has under the current official revisions (``None`` with a reason when undecided)."""
    if rule["kind"] in {"match_outcome", "advancement"}:
        match = _match(store, namespace, rule["fixture_key"])
        base = {"revisions": [] if match["revision"] is None else [match["revision"]]}
        if match.get("provisional_available"):
            base["provisional_available"] = True
        if match["reason"]:
            return {**base, "outcome": None, "reason": match["reason"]}
        score = match["revision"]["body"]["score"]
        result = (
            "home_win"
            if score["home"] > score["away"]
            else "away_win"
            if score["home"] < score["away"]
            else "draw"
        )
        if rule["kind"] == "match_outcome":
            return {
                **base,
                "outcome": int(result == rule["outcome"]),
                "reason": "official-result-rule-matched",
            }
        if result == "draw":
            return {
                **base,
                "outcome": None,
                "reason": "draw-not-decided-by-published-score",
            }
        winner = match["sides"]["home" if result == "home_win" else "away"]
        return {
            **base,
            "outcome": int(winner == rule["team_key"]),
            "reason": "official-result-rule-matched",
        }
    from src.kb.sports_queries import SportsQueries

    revisions, last = [], None
    for fixture_key in store.records(namespace, "fixture", parent=rule["season_key"]):
        match = _match(store, namespace, fixture_key)
        if match["reason"]:
            return {
                "outcome": None,
                "reason": f"season-incomplete:{match['reason']}",
                "revisions": revisions,
            }
        revisions.append(match["revision"])
        schedule = store.current(namespace, "fixture_schedule_revision", fixture_key)
        kickoff = (schedule or {}).get("body", {}).get("kickoff") or ""
        last = max(last or "", kickoff[:10], match["revision"]["published_at"][:10])
    if last is None:
        return {"outcome": None, "reason": "no-fixtures", "revisions": []}
    table = SportsQueries(store.conn).standings_as_of(
        namespace, rule["season_key"], last, scopes={"operator"}, knowledge_cutoff=None
    )
    rows = {row["team_key"]: row for row in table["table"]}
    if rule["team_key"] not in rows:
        return {"outcome": None, "reason": "team-not-in-table", "revisions": revisions}
    if (
        table["comparison"]["status"] == "disagrees"
        and table["comparison"]["compared_with"]["as_of"][:10] >= last
    ):
        return {
            "outcome": None,
            "reason": "replayed-and-published-final-tables-disagree",
            "revisions": revisions,
        }
    row = rows[rule["team_key"]]
    if row.get("tie_unresolved"):
        return {
            "outcome": None,
            "reason": "position-tied-under-the-stated-rule",
            "revisions": revisions,
        }
    return {
        "outcome": int(row["position"] <= rule["position_at_most"]),
        "reason": "official-final-table-rule-matched",
        "revisions": revisions,
        "position": row["position"],
    }


def propose_for_forecast(
    conn, state, result, *, now: int, scopes: Iterable[str]
) -> dict[str, Any] | None:
    """The ledger's resolution proposal for a sports forecast (None when the forecast has no sports rule)."""
    pinned = rule_for(conn, state["namespace"], state["forecast_id"])
    if pinned is None:
        return None
    from src.kb.forecasts import ForecastError

    try:
        authorize(pinned["namespace"], set(scopes), READ_SCOPE)
    except SportsError as exc:
        raise ForecastError("unauthorized", str(exc)) from exc
    base = {**dict(result), "sports_rule": pinned}
    if now < state["resolution_at_ms"]:
        return {**base, "reason": "resolution-not-due"}
    store = SportsStore(conn, initialize=False)
    decided = evaluate(store, pinned["namespace"], pinned["rule"])
    evidence = [
        {
            "kind": "source",
            "id": r["revision_id"],
            "revision": r["source"]["acquisition_id"],
            "namespace": pinned["namespace"],
        }
        for r in decided["revisions"][-50:]
    ]
    cited = [
        {
            k: v
            for k, v in {
                "revision_id": r["revision_id"],
                "status": r["body"]["status"],
                "published_at": r.get("published_at"),
                "provider": r["source"]["provider"],
                "deciding_body": r["body"].get("deciding_body"),
            }.items()
            if v is not None
        }
        for r in decided["revisions"][-50:]
    ]
    proposal = {**base, "evidence": evidence, "official_revisions": cited}
    if decided.get("provisional_available"):
        proposal["note"] = (
            "a provisional result never proposes or resolves a sports forecast"
        )
    if decided["outcome"] is None:
        proposal.update(status="unresolved", reason=decided["reason"])
    else:
        proposal.update(
            status="proposed",
            proposed_outcome=decided["outcome"],
            reason=decided["reason"],
        )
    row = (
        conn.execute(
            "SELECT content_json FROM research_forecast_outcomes WHERE forecast_id=? ORDER BY revision DESC LIMIT 1",
            [state["forecast_id"]],
        ).fetchone()
        if table_exists(conn, "research_forecast_outcomes")
        else None
    )
    if row is not None:
        recorded = json.loads(row[0])
        before = sorted(e["id"] for e in recorded.get("evidence") or [])
        if recorded.get("status") == "resolved" and before != sorted(
            e["id"] for e in evidence
        ):
            proposal["re_resolution"] = {
                "resolved_outcome_revision": recorded["revision"],
                "resolved_outcome": recorded.get("outcome"),
                "resolved_on_evidence": before,
                "note": "a revision published after resolution; a reviewer decides through resolve_binary_forecast, "
                "the recorded outcome never flips automatically",
            }
    return proposal


__all__ = [
    "RULE_KINDS",
    "SportsForecasts",
    "evaluate",
    "propose_for_forecast",
    "rule_for",
]
