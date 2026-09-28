"""Vintage-aware sports queries: tables as of a date, result and reschedule histories, schedules (#2142, SP07).

Two time axes are explicit in every answer:

* the **match date** - which matches had been played (kickoff on or before it);
* the **knowledge cutoff** - which revisions had been *published* by then
  (the source's own publication time). It defaults to the end of the match
  date, stated in the answer. An optional ``acquired_by_ms`` also restricts an
  answer to what had been acquired by then.

A table is recomputed only from recorded results and the competition's stated
rule (points, tie-breakers) and deductions in force, and is always compared
with the nearest table the source published; a disagreement lists the rows that
differ and is never resolved. Forfeits, awarded results, annulments and
deductions change the table only from the revision that published them, with
the deciding body's citation. A match with no published result stays
unresolved; nothing is inferred, and nothing predicts or prices anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from src.ingestion.sports_sources import REVIEW_BOUNDARY, iso_datetime, to_ms
from src.kb.sports_records import (
    ANSWER_CONTRACT,
    OFFICIAL_CLASS,
    READ_SCOPE,
    SportsError,
    authorize,
)
from src.kb.sports_store import SportsStore

DAY_MS = 86_400_000
NOT_PLAYED = ("postponed", "cancelled", "abandoned")


def _day(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise SportsError("invalid_date", f"{value!r} is not an ISO date") from exc


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _cutoff(day: str, knowledge_cutoff: str | None) -> int:
    if knowledge_cutoff is None:
        return to_ms(day) + DAY_MS - 1
    text = iso_datetime(knowledge_cutoff)
    return to_ms(text) + (DAY_MS - 1 if len(text) == 10 else 0)


def drop_none(value: Any) -> Any:
    """Missing values are absent in every answer, never ``None``."""
    if isinstance(value, Mapping):
        return {k: drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_none(v) for v in value if v is not None]
    return value


def _cite(revision: Mapping[str, Any]) -> dict[str, Any]:
    body = revision["body"]
    return {
        k: v
        for k, v in {
            "revision_id": revision["revision_id"],
            "status": revision.get("status") or body.get("status"),
            "published_at": revision.get("published_at"),
            "deciding_body": body.get("deciding_body"),
            "decision": body.get("decision"),
            "provider": revision["source"]["provider"],
            "locator": revision["source"].get("locator"),
            "url": revision["source"].get("url"),
            "attribution": revision["source"].get("attribution"),
            "licence": revision["source"].get("licence"),
        }.items()
        if v is not None
    }


class SportsQueries:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = SportsStore(conn, initialize=False)

    def _ready(self, namespace: str, scopes: Iterable[str]) -> None:
        authorize(namespace, set(scopes), READ_SCOPE)
        self.store.require_ready(namespace)

    def _labelled(
        self, namespace, record_type, key, *, cutoff_ms=None, acquired_by_ms=None
    ):
        return self.store.labelled_history(
            namespace,
            record_type,
            key,
            cutoff_ms=cutoff_ms,
            acquired_by_ms=acquired_by_ms,
        )

    def _current_labelled(
        self, namespace, record_type, key, *, cutoff_ms=None, acquired_by_ms=None
    ):
        current = self.store.current(
            namespace,
            record_type,
            key,
            cutoff_ms=cutoff_ms,
            acquired_by_ms=acquired_by_ms,
        )
        if current is None:
            return None
        for item in self._labelled(
            namespace,
            record_type,
            key,
            cutoff_ms=cutoff_ms,
            acquired_by_ms=acquired_by_ms,
        ):
            if item["revision_id"] == current["revision_id"]:
                return item
        return None

    # ------------------------------------------------------------------ tables

    def standings_as_of(
        self,
        namespace: str,
        season_key: str,
        on: str,
        *,
        scopes: Iterable[str],
        knowledge_cutoff: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """The competition table on a date: replayed from recorded results under the stated rule, beside the
        nearest source-published table, every counted result and every correction cited."""
        self._ready(namespace, scopes)
        day = _day(on)
        cutoff = _cutoff(day, knowledge_cutoff)
        end_of_day = to_ms(day) + DAY_MS - 1
        season = self.store.current(
            namespace, "season", season_key, acquired_by_ms=acquired_by_ms
        )
        if season is None:
            raise SportsError("not_found", "season is not visible in this namespace")
        rule, deductions = self._rules(
            namespace, season_key, day, cutoff, acquired_by_ms
        )
        teams: dict[str, dict[str, Any]] = {}
        counted, unresolved, corrections, matches = [], [], [], []

        def team(key: str, name: str | None) -> dict[str, Any]:
            return teams.setdefault(
                key,
                {
                    "team_key": key,
                    "team_name": name,
                    "played": 0,
                    "won": 0,
                    "drawn": 0,
                    "lost": 0,
                    "goals_for": 0,
                    "goals_against": 0,
                    "points_from_results": 0,
                    "deductions": 0,
                },
            )

        for fixture_key in self.store.records(namespace, "fixture", parent=season_key):
            fixture = self.store.body(
                namespace, "fixture", fixture_key, acquired_by_ms=acquired_by_ms
            )
            sides = dict((fixture or {}).get("sides") or {})
            if set(sides) != {"home", "away"}:
                continue  # not a home/away league fixture
            names = dict(fixture.get("side_names") or {})
            for side in ("home", "away"):
                team(
                    sides[side],
                    names.get(side) or self._team_name(namespace, sides[side]),
                )
            schedule = self._current_labelled(
                namespace,
                "fixture_schedule_revision",
                fixture_key,
                cutoff_ms=cutoff,
                acquired_by_ms=acquired_by_ms,
            )
            result = self._current_labelled(
                namespace,
                "match_result_revision",
                fixture_key,
                cutoff_ms=cutoff,
                acquired_by_ms=acquired_by_ms,
            )
            kickoff = (schedule or {}).get("body", {}).get("kickoff")
            if kickoff is None or to_ms(kickoff[:10]) > end_of_day:
                continue  # not scheduled to have been played by the match date
            for item in self._labelled(
                namespace,
                "match_result_revision",
                fixture_key,
                cutoff_ms=cutoff,
                acquired_by_ms=acquired_by_ms,
            ):
                if item["status"] in {"corrected", "forfeit_awarded", "annulled"}:
                    corrections.append(
                        {
                            "fixture_key": fixture_key,
                            **_cite(item),
                            "score": item["body"].get("score"),
                        }
                    )
            if result is None:
                reason = (
                    (schedule or {}).get("status")
                    if (schedule or {}).get("status") in NOT_PLAYED
                    else None
                )
                unresolved.append(
                    {
                        "fixture_key": fixture_key,
                        "reason": reason or "no-published-result",
                        "schedule": None if schedule is None else _cite(schedule),
                    }
                )
                continue
            if result["status"] == "annulled":
                unresolved.append(
                    {
                        "fixture_key": fixture_key,
                        "reason": "result-annulled",
                        "result": _cite(result),
                    }
                )
                continue
            if result["status"] not in OFFICIAL_CLASS or "score" not in result["body"]:
                unresolved.append(
                    {
                        "fixture_key": fixture_key,
                        "reason": "provisional-result",
                        "result": _cite(result),
                    }
                )
                continue
            score = result["body"]["score"]
            home, away = team(sides["home"], None), team(sides["away"], None)
            for row, scored, conceded in (
                (home, score["home"], score["away"]),
                (away, score["away"], score["home"]),
            ):
                row["played"] += 1
                row["goals_for"] += scored
                row["goals_against"] += conceded
                outcome = (
                    "won"
                    if scored > conceded
                    else "lost"
                    if scored < conceded
                    else "drawn"
                )
                row[outcome] += 1
                row["points_from_results"] += (
                    rule["points"][
                        "win"
                        if outcome == "won"
                        else "loss"
                        if outcome == "lost"
                        else "draw"
                    ]
                    if rule
                    else 0
                )
            matches.append((sides["home"], sides["away"], score))
            counted.append(
                {
                    "fixture_key": fixture_key,
                    "kickoff": kickoff,
                    "score": score,
                    "result": _cite(result),
                }
            )
        applied = []
        for deduction in deductions:
            key = deduction["body"]["team_key"]
            team(key, self._team_name(namespace, key))["deductions"] += int(
                deduction["body"]["points_deducted"]
            )
            applied.append(
                {
                    "team_key": key,
                    "points_deducted": deduction["body"]["points_deducted"],
                    "effective_on": deduction["body"]["effective_on"],
                    **_cite(deduction),
                }
            )
        rows = []
        for row in teams.values():
            rows.append(
                {
                    **row,
                    "goal_difference": row["goals_for"] - row["goals_against"],
                    "points": row["points_from_results"] - row["deductions"],
                }
            )
        table = (
            self._order(
                rows, matches, (rule or {}).get("tiebreakers") or ["points"], rule
            )
            if rule
            else []
        )
        published = self._snapshot(
            namespace, season_key, cutoff, end_of_day, acquired_by_ms
        )
        comparison = self._compare(table, published)
        return drop_none(
            {
                "contract": ANSWER_CONTRACT,
                "answer": "standings_as_of",
                "namespace": namespace,
                "season_key": season_key,
                "season": season["body"],
                "time_axes": {
                    "match_date": day,
                    "knowledge_cutoff": _iso_ms(cutoff),
                    "acquired_by_ms": acquired_by_ms,
                    "default": "the knowledge cutoff defaults to the end of the match date; only revisions published "
                    "by the cutoff count",
                },
                "status": "replayed" if rule else "no-stated-rule",
                "rule": None
                if rule is None
                else {
                    k: rule.get(k)
                    for k in ("points", "tiebreakers", "valid_from", "source_url")
                    if rule.get(k) is not None
                },
                "table": table,
                "deductions": applied,
                "counted_results": counted,
                "corrections": corrections,
                "unresolved": unresolved,
                "published_table": published,
                "comparison": comparison,
                "coverage": {
                    "n": len(counted),
                    "unresolved": len(unresolved),
                    "teams": len(table),
                },
                "generation": self.store.generation(namespace),
                "review_boundary": REVIEW_BOUNDARY,
            }
        )

    def _team_name(self, namespace: str, key: str) -> str | None:
        body = self.store.body(namespace, "team", key)
        return None if body is None else body.get("name")

    def _rules(self, namespace, season_key, day, cutoff, acquired_by_ms):
        rule, deductions = None, []
        for key in self.store.records(namespace, "table_rule", parent=season_key):
            current = self.store.current(
                namespace,
                "table_rule",
                key,
                cutoff_ms=cutoff,
                acquired_by_ms=acquired_by_ms,
            )
            if current is None:
                continue
            body = current["body"]
            if body["kind"] == "scoring":
                if body.get("valid_from") and body["valid_from"][:10] > day:
                    continue
                if rule is None or (body.get("valid_from") or "") > (
                    rule.get("valid_from") or ""
                ):
                    rule = body
            elif body["effective_on"][:10] <= day and int(body["points_deducted"]) != 0:
                deductions.append(current)
        return rule, sorted(
            deductions, key=lambda d: (d["body"]["effective_on"], d["revision_id"])
        )

    @staticmethod
    def _order(rows, matches, tiebreakers, rule) -> list[dict[str, Any]]:
        criteria = list(tiebreakers)
        if not criteria or criteria[0] != "points":
            criteria.insert(0, "points")

        def value(row, criterion, group_keys):
            if criterion.startswith("head_to_head"):
                points = gd = gf = 0
                for home, away, score in matches:
                    if (
                        home in group_keys
                        and away in group_keys
                        and row["team_key"] in (home, away)
                    ):
                        scored, conceded = (
                            (score["home"], score["away"])
                            if home == row["team_key"]
                            else (score["away"], score["home"])
                        )
                        gf += scored
                        gd += scored - conceded
                        points += rule["points"][
                            "win"
                            if scored > conceded
                            else "loss"
                            if scored < conceded
                            else "draw"
                        ]
                return {
                    "head_to_head_points": points,
                    "head_to_head_goal_difference": gd,
                    "head_to_head_goals_for": gf,
                }[criterion]
            if criterion == "goals_against":
                return -row["goals_against"]
            if criterion == "wins":
                return row["won"]
            return row[criterion]

        def split(group, remaining):
            if len(group) <= 1 or not remaining:
                return [(group, len(group) > 1)]
            criterion, keys = remaining[0], {r["team_key"] for r in group}
            buckets: dict[Any, list] = {}
            for row in group:
                buckets.setdefault(value(row, criterion, keys), []).append(row)
            out = []
            for _, bucket in sorted(buckets.items(), key=lambda item: -item[0]):
                out += split(bucket, remaining[1:])
            return out

        table, position = [], 1
        for group, tied in split(sorted(rows, key=lambda r: r["team_key"]), criteria):
            for row in sorted(group, key=lambda r: r["team_key"]):
                table.append(
                    {
                        "position": position,
                        **row,
                        **({"tie_unresolved": True} if tied else {}),
                    }
                )
            position += len(group)
        return table

    def _snapshot(self, namespace, season_key, cutoff, end_of_day, acquired_by_ms):
        best = None
        for key in self.store.records(
            namespace, "standing_snapshot", parent=season_key
        ):
            for revision in self.store.history(
                namespace,
                "standing_snapshot",
                key,
                cutoff_ms=cutoff,
                acquired_by_ms=acquired_by_ms,
            ):
                if to_ms(revision["body"]["as_of"]) <= end_of_day and (
                    best is None or revision["body"]["as_of"] > best["body"]["as_of"]
                ):
                    best = revision
        if best is None:
            return None
        return {
            "as_of": best["body"]["as_of"],
            "stage": best["body"].get("stage"),
            "rows": best["body"]["rows"],
            **_cite(best),
        }

    @staticmethod
    def _compare(table, published) -> dict[str, Any]:
        if published is None:
            return {
                "status": "no-published-table",
                "n": 0,
                "differences": [],
                "note": "no source-published table by the cutoff; the replay stands alone",
            }
        theirs = {r["team_key"]: r for r in published["rows"]}
        ours = {r["team_key"]: r for r in table}
        differences = []
        for key in sorted(set(theirs) | set(ours)):
            a, b = ours.get(key, {}), theirs.get(key, {})
            fields = {
                f: {"replayed": a.get(f), "published": b.get(f)}
                for f in ("position", "played", "points", "goal_difference")
                if a.get(f) != b.get(f)
            }
            if fields:
                differences.append(
                    {
                        "team_key": key,
                        "team_name": a.get("team_name") or b.get("team_name"),
                        "fields": fields,
                    }
                )
        return {
            "status": "agrees" if not differences else "disagrees",
            "n": len(differences),
            "compared_with": {
                "as_of": published["as_of"],
                "revision_id": published["revision_id"],
            },
            "differences": differences,
            "note": "a disagreement is shown with both values and never resolved",
        }

    # ------------------------------------------------------------------ histories

    def match_history(
        self, namespace, fixture_key, *, scopes, knowledge_cutoff=None, identity=None
    ):
        """Every result and schedule revision of a match with derived labels, lineups, and other sources' results
        for the same match (joined by reviewed team identity and date) side by side, never merged."""
        self._ready(namespace, scopes)
        cutoff = (
            None
            if knowledge_cutoff is None
            else _cutoff(_day(knowledge_cutoff), knowledge_cutoff)
        )
        fixture = self.store.current(namespace, "fixture", fixture_key)
        if fixture is None:
            raise SportsError("not_found", "fixture is not visible in this namespace")
        results = self._labelled(
            namespace, "match_result_revision", fixture_key, cutoff_ms=cutoff
        )
        lineups = [
            self.store.current(namespace, "lineup", key)
            for key in self.store.records(namespace, "lineup", parent=fixture_key)
        ]
        return drop_none(
            {
                "contract": ANSWER_CONTRACT,
                "answer": "match_history",
                "fixture": fixture,
                "status": "no-published-result"
                if not results
                else results[-1]["status"],
                "results": results,
                "schedule": self._labelled(
                    namespace,
                    "fixture_schedule_revision",
                    fixture_key,
                    cutoff_ms=cutoff,
                ),
                "lineups": [lineup for lineup in lineups if lineup],
                "other_sources": self._other_sources(namespace, fixture, identity)
                if identity
                else [],
                "coverage": {"n": len(results)},
                "review_boundary": REVIEW_BOUNDARY,
            }
        )

    def fixture_history(self, namespace, fixture_key, *, scopes):
        self._ready(namespace, scopes)
        schedule = self._labelled(namespace, "fixture_schedule_revision", fixture_key)
        if (
            not schedule
            and self.store.current(namespace, "fixture", fixture_key) is None
        ):
            raise SportsError("not_found", "fixture is not visible in this namespace")
        return drop_none(
            {
                "contract": ANSWER_CONTRACT,
                "answer": "fixture_history",
                "fixture_key": fixture_key,
                "status": schedule[-1]["status"]
                if schedule
                else "no-published-schedule",
                "revisions": schedule,
                "moves": [
                    {
                        "from": prev["body"].get("kickoff"),
                        "to": item["body"].get("kickoff"),
                        "status": item["status"],
                        "reason": item["body"].get("reason"),
                        "old": _cite(prev),
                        "new": _cite(item),
                    }
                    for prev, item in zip(schedule, schedule[1:])
                    if prev["body"].get("kickoff") != item["body"].get("kickoff")
                    or prev["body"].get("status") != item["body"].get("status")
                ],
                "coverage": {"n": len(schedule)},
            }
        )

    def _other_sources(self, namespace, fixture, identity):
        body = fixture["body"]
        sides = dict(body.get("sides") or {})
        schedule = self.store.current(
            namespace, "fixture_schedule_revision", fixture["record_key"]
        )
        kickoff = (schedule or {}).get("body", {}).get("kickoff")
        if set(sides) != {"home", "away"} or not kickoff:
            return []
        home, away = (
            set(identity.linked(namespace, sides["home"])),
            set(identity.linked(namespace, sides["away"])),
        )
        out = []
        for key in self.store.records(namespace, "fixture"):
            if key == fixture["record_key"]:
                continue
            other = self.store.body(namespace, "fixture", key) or {}
            other_sides = dict(other.get("sides") or {})
            if (
                other_sides.get("home") not in home
                or other_sides.get("away") not in away
            ):
                continue
            other_schedule = self.store.current(
                namespace, "fixture_schedule_revision", key
            )
            other_kickoff = (other_schedule or {}).get("body", {}).get("kickoff")
            if (
                not other_kickoff
                or abs(to_ms(other_kickoff[:10]) - to_ms(kickoff[:10])) > 3 * DAY_MS
            ):
                continue
            ours = self.store.current(
                namespace, "match_result_revision", fixture["record_key"]
            )
            theirs = self.store.current(namespace, "match_result_revision", key)
            out.append(
                {
                    "fixture_key": key,
                    "result": None
                    if theirs is None
                    else {"score": theirs["body"].get("score"), **_cite(theirs)},
                    "agrees": None
                    if not ours or not theirs
                    else ours["body"].get("score") == theirs["body"].get("score"),
                    "note": "another source's record of the same match, joined by reviewed team identity; never merged",
                }
            )
        return out

    def team_schedule(
        self, namespace, team_key, *, scopes, date_from, date_to, identity=None
    ):
        """A team's fixtures and results in a window, from every source its reviewed identity joins, per source."""
        self._ready(namespace, scopes)
        start, end = to_ms(_day(date_from)), to_ms(_day(date_to)) + DAY_MS - 1
        keys = identity.linked(namespace, team_key) if identity else [team_key]
        items = []
        for key in self.store.records(namespace, "fixture"):
            body = self.store.body(namespace, "fixture", key) or {}
            sides = dict(body.get("sides") or {})
            side = next((s for s, k in sides.items() if k in keys), None)
            if side is None:
                continue
            schedule = self._current_labelled(
                namespace, "fixture_schedule_revision", key
            )
            kickoff = (schedule or {}).get("body", {}).get("kickoff")
            if not kickoff or not start <= to_ms(kickoff[:10]) <= end:
                continue
            result = self._current_labelled(namespace, "match_result_revision", key)
            items.append(
                {
                    "fixture_key": key,
                    "side": side,
                    "team_key": sides[side],
                    "kickoff": kickoff,
                    "schedule_status": schedule["status"],
                    "result": None
                    if result is None
                    else {
                        "score": result["body"].get("score"),
                        "score_text": result["body"].get("score_text"),
                        **_cite(result),
                    },
                    "provider": (schedule or result)["source"]["provider"],
                }
            )
        items.sort(key=lambda i: (i["kickoff"], i["provider"], i["fixture_key"]))
        return drop_none(
            {
                "contract": ANSWER_CONTRACT,
                "answer": "team_schedule",
                "team_key": team_key,
                "joined_records": keys,
                "window": {"from": _day(date_from), "to": _day(date_to)},
                "fixtures": items,
                "coverage": {"n": len(items)},
            }
        )

    def player_appearances(self, namespace, player_key, *, scopes, identity=None):
        """Lineups that list a player (as published) from every source its reviewed identity joins."""
        self._ready(namespace, scopes)
        keys = set(identity.linked(namespace, player_key) if identity else [player_key])
        out = []
        for key in self.store.records(namespace, "lineup"):
            lineup = self.store.current(namespace, "lineup", key)
            for role in ("starters", "substitutes"):
                for entry in lineup["body"].get(role) or []:
                    if entry["player_key"] in keys:
                        out.append(
                            {
                                "fixture_key": lineup["body"]["fixture_key"],
                                "team_key": lineup["body"]["team_key"],
                                "role": "starter"
                                if role == "starters"
                                else "substitute",
                                **entry,
                                "lineup": _cite(lineup),
                            }
                        )
        return {
            "contract": ANSWER_CONTRACT,
            "answer": "player_appearances",
            "player_key": player_key,
            "joined_records": sorted(keys),
            "appearances": out,
            "coverage": {"n": len(out)},
            "note": "appearances as published in lineups; no medical, biometric or tracking data exists here",
        }

    def transfers(self, namespace, player_key, *, scopes, identity=None):
        self._ready(namespace, scopes)
        keys = identity.linked(namespace, player_key) if identity else [player_key]
        out = []
        for key in keys:
            for record in self.store.records(
                namespace, "transfer_assertion", parent=key
            ):
                current = self.store.current(namespace, "transfer_assertion", record)
                out.append({**current["body"], "record_key": record, **_cite(current)})
        return {
            "contract": ANSWER_CONTRACT,
            "answer": "transfers",
            "player_key": player_key,
            "transfers": out,
            "coverage": {"n": len(out)},
            "note": "openly published transfers only; a transfer reported only by news stays a cited news claim",
        }

    # ------------------------------------------------------------------ licence-aware export

    def export_records(
        self,
        namespace: str,
        refs: Sequence[Mapping[str, str]],
        *,
        scopes: Iterable[str],
        purpose: str,
        relicense_as: str | None = None,
    ) -> dict[str, Any]:
        """Export current revisions with their licence; refused when the purpose or relicensing breaks one."""
        self._ready(namespace, scopes)
        if purpose not in {"non-commercial", "commercial"}:
            raise SportsError(
                "invalid_export",
                "state the export purpose: non-commercial or commercial",
            )
        records = []
        for ref in refs:
            current = self.store.current(
                namespace, ref["record_type"], ref["record_key"]
            )
            if current is None:
                raise SportsError(
                    "not_found", f"{ref['record_key']} is not visible in this namespace"
                )
            records.append(current)
        licences = {
            r["source"]["licence"]["id"]: r["source"]["licence"] for r in records
        }
        for licence in licences.values():
            if licence.get("non_commercial") and purpose == "commercial":
                raise SportsError(
                    "licence_refused",
                    f"{licence['id']} forbids commercial use; export refused",
                )
            if (
                licence.get("share_alike")
                and relicense_as
                and relicense_as != licence["id"]
            ):
                raise SportsError(
                    "licence_refused",
                    f"{licence['id']} is share-alike; derived records keep it",
                )
        return {
            "contract": ANSWER_CONTRACT,
            "answer": "export",
            "purpose": purpose,
            "records": records,
            "licences": sorted(licences.values(), key=lambda item: item["id"]),
            "attributions": sorted(
                {
                    r["source"]["attribution"]
                    for r in records
                    if r["source"].get("attribution")
                }
            ),
            "coverage": {"n": len(records)},
        }
