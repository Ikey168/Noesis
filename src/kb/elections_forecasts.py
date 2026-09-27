"""Election forecasts in the existing binary forecast ledger, resolved only against certified results (#1908, L08).

A user registers a forecast through :meth:`src.kb.forecasts.ForecastStore.create`
(``create_binary_forecast``) with their own probability and evidence; this
module only adds the structured election rule it resolves by - the contest,
the certified vintage kind and one of three outcome rules (``winner``,
``votes_at_least``, ``share_at_least``, the latter only on a published share).
The pack generates no forecast and no probability.

``propose_forecast_resolution`` consults the rule: it cites the certified
result vintage record as evidence; a preliminary vintage never proposes or
resolves anything, and a contest without a certified vintage stays
``unresolved``. ``resolve_binary_forecast``, ``revise_binary_forecast`` and
``score_binary_forecasts`` run unchanged; :func:`scoring_cutoff` derives the
scoring cutoff from the certified publication time.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from src.ingestion.election_sources import day_ms
from src.kb.elections import (
    CERTIFIED_CLASS,
    READ_SCOPE,
    ElectionError,
    ElectionStore,
    authorize,
    canonical,
    table_exists,
)

RULE_KINDS = ("winner", "votes_at_least", "share_at_least")
_DDL = """
CREATE TABLE IF NOT EXISTS election_forecast_rules (
  forecast_namespace TEXT NOT NULL, forecast_id TEXT NOT NULL, namespace TEXT NOT NULL, contest_id TEXT NOT NULL,
  vintage_kind TEXT NOT NULL, rule_json TEXT NOT NULL, registered_by TEXT NOT NULL, registered_at_ms BIGINT NOT NULL,
  PRIMARY KEY(forecast_namespace, forecast_id)
);
"""


def _rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    rule = dict(rule)
    if rule.get("kind") not in RULE_KINDS or not re.fullmatch(
        r"(party|candidate):\S+", str(rule.get("entry") or "")
    ):
        raise ElectionError(
            "invalid_rule",
            f"an election rule is one of {RULE_KINDS} on a party or candidate entry",
        )
    if rule["kind"] != "winner":
        try:
            threshold = Decimal(str(rule.get("threshold")))
            if not threshold.is_finite() or threshold < 0:
                raise InvalidOperation
        except InvalidOperation:
            raise ElectionError(
                "invalid_rule", "a threshold rule states a finite threshold"
            ) from None
        rule["threshold"] = str(threshold)
    if set(rule) - {"kind", "entry", "threshold"}:
        raise ElectionError("invalid_rule", "unknown rule fields")
    return rule


def describe(rule: Mapping[str, Any]) -> str:
    if rule["kind"] == "winner":
        return f"{rule['entry']} has the most votes"
    if rule["kind"] == "votes_at_least":
        return f"{rule['entry']} has at least {rule['threshold']} votes"
    return (
        f"{rule['entry']} has a published share of at least {rule['threshold']} percent"
    )


class ElectionForecasts:
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
        self.store = ElectionStore(conn, initialize=initialize, now=self.now)
        self.forecasts = ForecastStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def register(
        self,
        namespace: str,
        forecast_namespace: str,
        request_key: str,
        *,
        contest_id: str,
        rule: Mapping[str, Any],
        probability: float,
        resolution_at_ms: int,
        evidence: list[dict[str, Any]],
        principal_id: str,
        scopes: Iterable[str],
        question: str | None = None,
    ) -> dict[str, Any]:
        """Create the user's forecast in the ledger and pin the election rule it resolves by (certified only)."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        contest = self.store.contest(namespace, contest_id)
        rule = _rule(rule)
        outcome_rule = (
            f"Election contest {contest_id} ({contest['election_id']}, {contest['unit_scheme']} "
            f"{contest['unit_native_id']}, {contest['ballot']}): resolves 1 if {describe(rule)} in the certified "
            "result vintage, 0 otherwise; preliminary results never resolve it."
        )
        created = self.forecasts.create(
            forecast_namespace,
            request_key,
            question=question
            or f"Will {describe(rule)} in {contest['election_id']} ({contest['unit_name']})?",
            outcome_rule=outcome_rule,
            resolution_at_ms=resolution_at_ms,
            probability=probability,
            evidence=evidence,
            principal_id=principal_id,
            scopes=scopes,
        )
        stored = {
            "namespace": namespace,
            "contest_id": contest_id,
            "vintage_kind": "certified",
            "rule": rule,
        }
        prior = self.conn.execute(
            "SELECT namespace, contest_id, vintage_kind, rule_json FROM election_forecast_rules WHERE "
            "forecast_namespace=? AND forecast_id=?",
            [forecast_namespace, created["forecast_id"]],
        ).fetchone()
        if prior is None:
            self.conn.execute(
                "INSERT INTO election_forecast_rules VALUES (?,?,?,?,?,?,?,?)",
                [
                    forecast_namespace,
                    created["forecast_id"],
                    namespace,
                    contest_id,
                    "certified",
                    canonical(rule),
                    principal_id,
                    self.now(),
                ],
            )
        elif canonical(
            {
                "namespace": prior[0],
                "contest_id": prior[1],
                "vintage_kind": prior[2],
                "rule": json.loads(prior[3]),
            }
        ) != canonical(stored):
            raise ElectionError(
                "idempotency_conflict",
                "the forecast is registered with a different election rule",
            )
        return {**created, "election_rule": stored}

    def rule(self, forecast_namespace: str, forecast_id: str) -> dict[str, Any] | None:
        return rule_for(self.conn, forecast_namespace, forecast_id)

    def scoring_cutoff(
        self, forecast_namespace: str, forecast_ids: list[str], *, scopes: Iterable[str]
    ) -> int:
        """The earliest certified publication time among the cohort's contests; refused before any is certified."""
        scopes = set(scopes)
        times = []
        for forecast_id in forecast_ids:
            rule = self.rule(forecast_namespace, forecast_id)
            if rule is None:
                raise ElectionError(
                    "not_an_election_forecast", f"{forecast_id} has no election rule"
                )
            authorize(rule["namespace"], scopes, READ_SCOPE)
            certified = self.store.in_force(
                rule["namespace"], rule["contest_id"], kinds=CERTIFIED_CLASS
            )
            if certified is None:
                raise ElectionError(
                    "not_certified",
                    f"{forecast_id}: no certified vintage yet; scoring waits for it",
                )
            times.append(day_ms(certified["published_on"], certified["published_at"]))
        return min(times)

    def score(
        self,
        forecast_namespace: str,
        forecast_ids: list[str],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """``score_binary_forecasts`` unchanged, with the cutoff taken from the certified publication time."""
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
            "cutoff_basis": "earliest certified result publication among the cohort's contests",
        }


def rule_for(
    conn: Any, forecast_namespace: str, forecast_id: str
) -> dict[str, Any] | None:
    if not table_exists(conn, "election_forecast_rules"):
        return None
    row = conn.execute(
        "SELECT namespace, contest_id, vintage_kind, rule_json FROM election_forecast_rules WHERE "
        "forecast_namespace=? AND forecast_id=?",
        [forecast_namespace, forecast_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "namespace": row[0],
        "contest_id": row[1],
        "vintage_kind": row[2],
        "rule": json.loads(row[3]),
    }


def _evaluate(
    rule: Mapping[str, Any], figures: Mapping[str, Any]
) -> tuple[int | None, str]:
    entries = figures.get("entries") or []
    modes = {e.get("mode") for e in entries}
    if len(modes) > 1 and None not in modes and "TOTAL" not in modes:
        return None, "figures-split-by-mode-are-never-summed"
    entries = [e for e in entries if e.get("mode") in (None, "TOTAL")]
    wanted = [e for e in entries if e["key"] == rule["entry"]]
    if rule["kind"] == "winner":
        counts = [e.get("votes") for e in entries]
        if not wanted or any(c is None for c in counts):
            return None, "entry-or-count-not-published"
        top = max(counts)
        leaders = [e["key"] for e in entries if e["votes"] == top]
        if len(leaders) > 1 and rule["entry"] in leaders:
            return None, "tie-in-published-counts"
        return int(leaders == [rule["entry"]]), "certified-vintage-rule-matched"
    if not wanted:
        return 0, "entry-absent-from-certified-result"
    value = wanted[0].get(
        "votes" if rule["kind"] == "votes_at_least" else "share_published"
    )
    if value is None:
        return None, "figure-not-published"
    return int(
        Decimal(str(value)) >= Decimal(rule["threshold"])
    ), "certified-vintage-rule-matched"


def propose_for_forecast(
    conn: Any,
    state: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    now: int,
    scopes: Iterable[str],
) -> dict[str, Any] | None:
    """The ledger's resolution proposal for an election forecast (None when the forecast has no election rule)."""
    rule = rule_for(conn, state["namespace"], state["forecast_id"])
    if rule is None:
        return None
    from src.kb.forecasts import ForecastError

    try:
        authorize(rule["namespace"], set(scopes), READ_SCOPE)
    except ElectionError as exc:
        raise ForecastError("unauthorized", str(exc)) from exc
    base = {**dict(result), "election_rule": rule}
    if now < state["resolution_at_ms"]:
        return {**base, "reason": "resolution-not-due"}
    store = ElectionStore(conn, initialize=False)
    certified = store.in_force(
        rule["namespace"], rule["contest_id"], kinds=CERTIFIED_CLASS
    )
    if certified is None:
        preliminary = store.in_force(
            rule["namespace"], rule["contest_id"], kinds=("preliminary",)
        )
        return {
            **base,
            "status": "unresolved",
            "reason": "no-certified-vintage",
            "preliminary_available": preliminary is not None,
            "note": "a preliminary result never proposes or resolves an election forecast",
        }
    outcome, reason = _evaluate(rule["rule"], certified["figures"])
    citation = {
        "vintage_id": certified["vintage_id"],
        "kind": certified["kind"],
        "published_on": certified["published_on"],
        "source_revision": certified["source_revision"],
    }
    evidence = [
        {
            "kind": "source",
            "id": certified["vintage_id"],
            "revision": certified["release_id"],
            "namespace": rule["namespace"],
        }
    ]
    if outcome is None:
        return {
            **base,
            "status": "unresolved",
            "reason": reason,
            "certified_vintage": citation,
            "evidence": evidence,
        }
    return {
        **base,
        "status": "proposed",
        "proposed_outcome": outcome,
        "reason": reason,
        "evidence": evidence,
        "certified_vintage": citation,
    }


__all__ = ["ElectionForecasts", "RULE_KINDS", "propose_for_forecast", "rule_for"]
