"""Sports records: the ``noesis-sports-record-v1`` contract, validation and schema registration (#2135, SP02).

Record types (every one revisioned; a later publication appends a revision and
never overwrites):

* ``competition`` - governing body, sport and format as the source states them;
* ``season`` and ``stage`` (league, group, knockout round);
* ``team``, ``player`` and ``venue`` - participants and places as published.
  A person appears only as a published participant: name as published, the
  source's id, the team, and a date of birth only where the source itself
  publishes it (used solely as identity evidence). There is no field for
  odds, medical, injury, biometric or tracking data;
* ``fixture`` - the sides (home/away, or the source's own side labels), the
  season and stage, the venue reference;
* ``fixture_schedule_revision`` - status ``scheduled`` | ``postponed`` |
  ``rescheduled`` | ``cancelled`` | ``abandoned``, the kickoff and a stated
  reason; ``rescheduled`` is derived when a later publication moves the kickoff;
* ``match_result_revision`` - the score as published (periods, a tennis score
  text or an Olympic ranking), status ``provisional`` | ``official`` |
  ``corrected`` | ``forfeit_awarded`` | ``annulled``, and the deciding body
  with its decision citation; ``corrected`` is derived when an official score
  differs from an earlier official one in the source's own date order;
* ``lineup`` - starters and substitutes with positions and shirt numbers as
  published;
* ``table_rule`` - points, tie-breakers and deductions as published (a
  deduction names the deciding body and cites its decision);
* ``standing_snapshot`` - the table a source publishes, with its as-of time;
* ``transfer_assertion`` - from, to, date, a fee only if openly published, and
  the source.

Every stored revision carries its source (provider, source id, source record
id, locator, acquisition, licence and attribution), a content-addressed
revision id, published-at and retrieved-at (:mod:`src.kb.sports_store`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.sports_sources import (
    FORBIDDEN_KEYS,
    RESULT_STATUSES,
    SCHEDULE_STATUSES,
)

CONTRACT = "noesis-sports-record-v1"
ANSWER_CONTRACT = "noesis-sports-answer-v1"
READ_SCOPE = "knowledge:sports:read"
WRITE_SCOPE = "knowledge:sports:write"
REVIEW_SCOPE = "knowledge:sports:review"
DEFAULT_NAMESPACE = "global"
RECORD_TYPES = (
    "competition",
    "season",
    "stage",
    "team",
    "player",
    "venue",
    "fixture",
    "fixture_schedule_revision",
    "match_result_revision",
    "lineup",
    "table_rule",
    "standing_snapshot",
    "transfer_assertion",
)
OFFICIAL_CLASS = ("official", "corrected", "forfeit_awarded")
# Every field a person record may carry: a published participant, nothing more.
PLAYER_FIELDS = frozenset(
    {
        "provider",
        "native_id",
        "name",
        "known_as",
        "birth_date",
        "noc",
        "tour",
        "cross_ids",
    }
)
REQUIRED = {
    "competition": ("provider", "sport", "name"),
    "season": ("competition_key", "label"),
    "stage": ("season_key", "name"),
    "team": ("provider", "name"),
    "player": ("provider", "name"),
    "venue": ("provider", "name"),
    "fixture": ("sport", "season_key"),
    "fixture_schedule_revision": ("status",),
    "match_result_revision": ("status",),
    "lineup": ("fixture_key", "team_key"),
    "table_rule": ("season_key", "kind"),
    "standing_snapshot": ("season_key", "as_of", "rows"),
    "transfer_assertion": ("player_key", "to_team_key", "date", "source"),
}
TIEBREAKERS = (
    "points",
    "goal_difference",
    "goals_for",
    "goals_against",
    "wins",
    "head_to_head_points",
    "head_to_head_goal_difference",
    "head_to_head_goals_for",
)
SCHEMA_FILES = {
    "noesis-sports-record": "contracts/schemas/jsonschema/noesis-sports-record-v1.json",
}


class SportsError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(
    namespace: str, scopes: Iterable[str], required: str, *, write: bool = False
) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise SportsError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require_scope(scopes: Iterable[str], required: str) -> None:
    """A scope needed only for an optional part of an answer, checked when that part is requested."""
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise SportsError(
            "unauthorized", f"{required} is required for this part of the answer"
        )


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a record or answer that would carry odds, a prediction or medical/biometric data."""
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def _has_none(value: Any) -> bool:
    if value is None or value == "None":
        return True
    if isinstance(value, Mapping):
        return any(_has_none(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_none(v) for v in value)
    return False


def validate_body(record_type: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Check one record body against the contract; returns the body unchanged."""
    if record_type not in RECORD_TYPES:
        raise SportsError("invalid_record", f"unknown record type {record_type!r}")
    body = json.loads(canonical(dict(body)))
    missing = [f for f in REQUIRED[record_type] if f not in body]
    if missing:
        raise SportsError("invalid_record", f"{record_type} needs {missing}")
    if _has_none(body):
        raise SportsError("invalid_record", "a missing value is absent, never None")
    bad = forbidden_keys(body)
    if bad:
        raise SportsError(
            "invalid_record",
            f"records carry no odds, predictions or medical data: {bad}",
        )
    if record_type == "player" and set(body) - PLAYER_FIELDS:
        raise SportsError(
            "invalid_record",
            f"a person appears only as a published participant; refused {sorted(set(body) - PLAYER_FIELDS)}",
        )
    if (
        record_type == "fixture_schedule_revision"
        and body["status"] not in SCHEDULE_STATUSES
    ):
        raise SportsError(
            "invalid_record", f"unknown schedule status {body['status']!r}"
        )
    if record_type == "match_result_revision":
        if body["status"] not in RESULT_STATUSES:
            raise SportsError(
                "invalid_record", f"unknown result status {body['status']!r}"
            )
        if body["status"] != "annulled" and not {
            "score",
            "score_text",
            "ranking",
        } & set(body):
            raise SportsError(
                "invalid_record", "a result states its score as published"
            )
        if (
            body["status"] in {"forfeit_awarded", "annulled"}
            and body.get("source_status") is None
        ):
            if not body.get("deciding_body") or not dict(
                body.get("decision") or {}
            ).get("url"):
                raise SportsError(
                    "invalid_record",
                    "a forfeit, award or annulment names the deciding body and cites its decision",
                )
    if record_type == "table_rule":
        if body["kind"] == "scoring":
            points = dict(body.get("points") or {})
            if set(points) != {"win", "draw", "loss"}:
                raise SportsError(
                    "invalid_record", "a scoring rule states win, draw and loss points"
                )
            unknown = [t for t in body.get("tiebreakers") or [] if t not in TIEBREAKERS]
            if unknown:
                raise SportsError(
                    "invalid_record", f"unsupported tie-breakers {unknown}"
                )
        elif body["kind"] == "deduction":
            for field in (
                "team_key",
                "points_deducted",
                "effective_on",
                "deciding_body",
                "decision",
            ):
                if field not in body:
                    raise SportsError(
                        "invalid_record", f"a points deduction states {field}"
                    )
        else:
            raise SportsError(
                "invalid_record", "a table rule is a scoring rule or a deduction"
            )
    if record_type == "transfer_assertion":
        source = dict(body["source"])
        if not str(source.get("url") or "").startswith("https://"):
            raise SportsError(
                "invalid_record", "a transfer cites its published source (https)"
            )
    return body


def parent_key(
    record_type: str, record_key: str, body: Mapping[str, Any]
) -> str | None:
    """The record a record belongs to (season of a fixture, fixture of a lineup, player of a transfer)."""
    if record_type in {"fixture_schedule_revision", "match_result_revision"}:
        return record_key
    for field in ("fixture_key", "season_key", "competition_key", "player_key"):
        if body.get(field):
            return str(body[field])
    return None


def schema_definitions(root=None) -> dict[str, Any]:
    from pathlib import Path

    root = Path(root) if root else Path(__file__).resolve().parents[2]
    return {
        name: json.loads((root / path).read_text())
        for name, path in SCHEMA_FILES.items()
    }


def register_schemas(conn, *, principal_id, scopes, root=None) -> list[dict[str, Any]]:
    """Register the pack's record schema in the existing schema registry (idempotent per version)."""
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1",
            "name": name,
            "kind": "schema",
            "semantic_version": "1.0.0",
            "content": content,
            "owner": "sports.football",
            "dependencies": [],
            "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/sports"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(
            registry.register(
                definition,
                f"sports-schema:{name}:1.0.0:{digest(content)[:16]}",
                principal_id=principal_id,
                scopes=scopes,
            )
        )
    return results


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
    )


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the Sports bundle's optional feature is selected in the active composition plan."""
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return False
        managed = conn.execute(
            "SELECT authority FROM composition_authority WHERE bundle='sports'"
        ).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return feature in ((plan.get("features") or {}).get("sports") or [])
