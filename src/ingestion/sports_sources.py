"""Sports source contracts and bounded acquisition for the Sports pack (#2135, SP01/SP03/SP04/SP05).

One native connector, ``sports-results``, fetches the declared publications of
one source and parses them into normalised, source-attributed *record
observations* for :mod:`src.kb.sports_store`. Six documented formats:

* ``football-data-v4-matches`` / ``football-data-v4-standings`` - the
  football-data.org v4 REST API (``X-Auth-Token`` key from the credential
  store): fixtures with their kickoff and status, scores, and the table the
  provider publishes. Every poll that sees a changed kickoff or status becomes a
  fixture-schedule revision, every changed score or status a result revision
  (the store dedupes unchanged observations). Odds and referee blocks in the
  response are dropped, never stored.
* ``statsbomb-open-data`` - competitions, matches and lineups from a pinned
  commit of the StatsBomb open-data repository (event files are out of v1
  scope). Managers, referees and player nationality are dropped.
* ``openfootball-json`` - football.json exports from a pinned commit of the
  openfootball datasets; the commit hash is the release identifier.
* ``sackmann-matches-csv`` - Jeff Sackmann's ``tennis_atp`` / ``tennis_wta``
  match files from a pinned commit, match-level fields only (no height,
  handedness, age or nationality), every record carrying CC BY-NC-SA 4.0.
* ``olympic-results-csv`` - an operator transcription of an official Olympic
  results publication (organising committee results book or IOC decision),
  one row per ranked participant, each row citing the official publication; a
  disqualification or medal reallocation is a later publication and so a
  result revision that names the deciding body.

``PROVIDER_CONTRACTS`` records the SP01 audit (the full text is
``docs/development/sports-evidence/source-audit.md``); every statement about a
provider's live terms, endpoints or layouts that could not be checked without
network access is marked ``verify``. ``LIVE_VERIFICATION`` keeps offline
fixture evidence and live evidence apart.

No format carries odds, betting markets, predictions, medical, injury,
biometric or tracking data; a person appears only as a published participant
(name as published, team, appearance).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
ACQUISITION_CONTRACT = "noesis-sports-acquisition-v1"
CONNECTOR = "sports-results"
MAX_RECORDS = 50_000
MAX_ROWS = 200_000
MAX_FILES = 64
REVIEW_BOUNDARY = (
    "Published sporting records only, as each source released them: fixtures with their reschedule history, "
    "results with every correction, forfeit, award or annulment kept as a revision, lineups and tables as "
    "published. Nothing is inferred for a match without a published result, and nothing produces odds, tips, "
    "predictions, medical or biometric data."
)
# Keys that would carry odds, a prediction, betting advice or medical/biometric data; no record or answer has them.
FORBIDDEN_KEYS = frozenset(
    {
        "odds",
        "betting",
        "bet",
        "tip",
        "tips",
        "value_bet",
        "prediction",
        "predicted",
        "predicted_winner",
        "win_probability",
        "probability_estimate",
        "expected_goals",
        "injury",
        "injuries",
        "medical",
        "height",
        "weight",
        "hand",
        "age",
        "biometric",
        "heart_rate",
        "tracking",
    }
)
# Status vocabularies of the stored records (SP02).
SCHEDULE_STATUSES = ("scheduled", "postponed", "rescheduled", "cancelled", "abandoned")
RESULT_STATUSES = (
    "provisional",
    "official",
    "corrected",
    "forfeit_awarded",
    "annulled",
)

# format id -> the provider it belongs to and the record types it can emit
FORMATS: dict[str, dict[str, Any]] = {
    "football-data-v4-matches": {
        "provider": "football-data",
        "sport": "football",
    },
    "football-data-v4-standings": {
        "provider": "football-data",
        "sport": "football",
    },
    "football-data-v4-teams": {
        "provider": "football-data",
        "sport": "football",
    },
    "statsbomb-open-data": {"provider": "statsbomb-open-data", "sport": "football"},
    "openfootball-json": {"provider": "openfootball", "sport": "football"},
    "sackmann-matches-csv": {"provider": "sackmann-tennis", "sport": "tennis"},
    "olympic-results-csv": {"provider": "olympic-results", "sport": "olympic"},
}

SACKMANN_LICENCE = {
    "id": "CC-BY-NC-SA-4.0",
    "url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "attribution": "Tennis databases, files, and algorithms by Jeff Sackmann / Tennis Abstract, licensed under "
    "CC BY-NC-SA 4.0 (https://github.com/JeffSackmann)",
    "non_commercial": True,
    "share_alike": True,
}

# SP01 access decisions. Terms, endpoints and file layouts below are recorded from the providers' published
# documentation as known without network access; every item marked ``verify`` must be checked against the live
# terms and files before a dated live run is accepted (SP13).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "football-data": {
        "publisher": "football-data.org (Daniel Freitag)",
        "delivers": ["fixtures", "results", "published-standings"],
        "access": "REST API v4: /v4/competitions/{code}/matches?season=YYYY and "
        "/v4/competitions/{code}/standings?season=YYYY (JSON)",
        "authentication": "API key sent as the X-Auth-Token header; secret ref NOESIS_FOOTBALL_DATA_API_KEY in the "
        "credential store, never committed",
        "rate_limits": "free tier: 10 requests per minute and a fixed set of competitions (verify current tier "
        "limits and the X-Requests-Available-Minute header); one request per source per run",
        "identifiers": [
            "numeric match id",
            "numeric team id",
            "competition code (PL, BL1, ...)",
            "season start year",
        ],
        "corrections": "a match's status, utcDate and score change in place; lastUpdated states when (verify that "
        "lastUpdated moves on every change); each observed change is a new revision here",
        "fixture_moves": "POSTPONED status, then a new utcDate with status SCHEDULED/TIMED (verify)",
        "cadence": "near-live on match days; standings recomputed by the provider after results",
        "terms": "football-data.org terms of use: free tier for non-commercial use with attribution "
        "'Football data provided by the Football-Data.org API' (verify wording and redistribution terms)",
        "attribution": "Football data provided by the Football-Data.org API",
        "access_decision": "implement",
        "reason": "keyed JSON API with stable ids; bounded to SP01 competitions and seasons; odds blocks are "
        "dropped at parse time",
        "v1_bounds": "Premier League (PL) and Bundesliga (BL1), seasons 2024 and 2025: <= 2 x 380 + 2 x 306 "
        "matches and 4 standings tables",
    },
    "statsbomb-open-data": {
        "publisher": "StatsBomb (Hudl) open-data repository on GitHub",
        "delivers": ["competitions", "matches", "lineups"],
        "access": "raw JSON files of github.com/statsbomb/open-data at a pinned commit: data/competitions.json, "
        "data/matches/{competition_id}/{season_id}.json, data/lineups/{match_id}.json",
        "authentication": "none",
        "rate_limits": "GitHub raw content limits (undocumented; verify); a bounded file list per run",
        "identifiers": [
            "competition_id",
            "season_id",
            "match_id",
            "team_id",
            "player_id",
        ],
        "corrections": "files are edited in later commits (last_updated per match); a new pinned commit is a new "
        "release and a changed score or lineup a new revision",
        "fixture_moves": "not applicable (historical, completed matches only)",
        "cadence": "irregular releases",
        "terms": "StatsBomb Public Data User Agreement (in the repository): use is permitted with attribution to "
        "StatsBomb and its logo on published work (verify the agreement text, and whether commercial use is "
        "restricted)",
        "attribution": "Data provided by StatsBomb (StatsBomb open data)",
        "access_decision": "implement",
        "reason": "matches and lineups only; event-level data is out of v1 scope; attribution recorded on every "
        "derived record",
        "v1_bounds": "one competition-season (e.g. competition 11 / season 90) with its matches file and at most "
        "40 lineup files",
    },
    "openfootball": {
        "publisher": "openfootball (Gerald Bauer and contributors)",
        "delivers": ["fixtures", "results"],
        "access": "football.json exports at a pinned commit of github.com/openfootball/football.json, e.g. "
        "{season}/{league}.json (the Football.TXT sources are not parsed in v1)",
        "authentication": "none",
        "rate_limits": "GitHub raw content limits (verify)",
        "identifiers": [
            "league code (en.1, de.1, ...)",
            "season label",
            "round name",
            "team names as published (no numeric ids)",
        ],
        "corrections": "files are edited in later commits; the commit hash is the release identifier",
        "fixture_moves": "a later commit changes a match's date; the fixture key (season, round, teams) stays the "
        "same, so it is a schedule revision",
        "cadence": "community-maintained, irregular",
        "terms": "dedicated to the public domain (CC0-1.0 / 'public domain' statement in the repositories; "
        "verify per repository)",
        "attribution": "openfootball / football.json (public domain)",
        "access_decision": "implement",
        "reason": "public-domain, pinned-commit JSON; results are kept beside other sources' results, never merged",
        "v1_bounds": "en.1 and de.1 for two seasons: <= 1372 matches",
    },
    "sackmann-tennis": {
        "publisher": "Jeff Sackmann / Tennis Abstract (github.com/JeffSackmann/tennis_atp, tennis_wta)",
        "delivers": ["tennis-match-results"],
        "access": "CSV files atp_matches_YYYY.csv / wta_matches_YYYY.csv at a pinned commit",
        "authentication": "none",
        "rate_limits": "GitHub raw content limits (verify)",
        "identifiers": [
            "tourney_id",
            "match_num",
            "player id (per repository)",
        ],
        "corrections": "rows are edited in later commits without a change log; each pinned commit is a release",
        "fixture_moves": "not applicable (completed matches only)",
        "cadence": "irregular",
        "terms": "CC BY-NC-SA 4.0: attribution, non-commercial use only, derived records shared under the same "
        "licence (verify that the licence file covers every file used)",
        "attribution": SACKMANN_LICENCE["attribution"],
        "licence": SACKMANN_LICENCE,
        "access_decision": "implement",
        "reason": "implemented behind the licence-gated sports_tennis feature: every derived record and export "
        "carries the licence, its attribution and the share-alike flag; commercial exports and relicensing are "
        "refused; height, handedness, age and nationality columns are never stored",
        "v1_bounds": "ATP and WTA tour-level matches for two years: <= 6000 rows",
    },
    "olympics-com": {
        "publisher": "International Olympic Committee (olympics.com results pages)",
        "delivers": ["olympic-results (link-only)"],
        "access": "HTML result pages per Games, sport and event (no documented open API; verify)",
        "authentication": "none",
        "identifiers": ["Games slug", "sport slug", "event slug"],
        "terms": "olympics.com terms of use restrict automated extraction and reuse (verify)",
        "access_decision": "link-only",
        "reason": "no scraping of sites whose terms forbid it; result pages and IOC decisions are cited as the "
        "official publication behind operator-transcribed rows",
    },
    "olympic-results": {
        "publisher": "operator transcription of an official results publication (organising committee official "
        "results book or IOC Executive Board decision)",
        "delivers": ["olympic-event-results"],
        "access": "a CSV file in the documented olympic-results-csv layout; each row cites the official "
        "publication URL it transcribes",
        "authentication": "none",
        "identifiers": [
            "Games",
            "sport",
            "event",
            "phase",
            "participant as published",
            "NOC code",
        ],
        "corrections": "a disqualification or medal reallocation is a later publication whose rows name the "
        "deciding body (IOC, CAS, the international federation) and cite the decision",
        "cadence": "per Games; reallocations years later",
        "terms": "facts transcribed with a citation to the official publication (verify the reuse terms of the "
        "cited publication)",
        "attribution": "Official results as published by the organising committee / IOC",
        "access_decision": "implement",
        "reason": "the only v1 route that needs no scraping; bounded to one Games",
        "v1_bounds": "one Games, <= 50 event phases",
    },
    "olympedia": {
        "publisher": "Olympedia (OlyMADMen)",
        "delivers": ["historical-olympic-results"],
        "terms": "no open data licence stated (verify)",
        "access_decision": "not implemented",
        "reason": "no licence that permits storing derived records",
    },
    "transfers-official": {
        "publisher": "governing bodies and clubs (official transfer announcements)",
        "delivers": ["transfer-assertions"],
        "access": "operator-recorded assertion citing the official announcement URL (record_sports_transfer)",
        "terms": "per announcement (verify); only facts stated there, a fee only when openly published",
        "access_decision": "implement",
        "reason": "openly published announcements only; a transfer reported only by news stays a cited news claim",
    },
    "wikidata-transfers": {
        "publisher": "Wikidata (member of sports team, P54, with start/end qualifiers)",
        "delivers": ["team-membership statements"],
        "terms": "CC0-1.0 (verify)",
        "access_decision": "not implemented",
        "reason": "v1 bound: memberships are not transfers (no from/to or date semantics); QIDs a source states "
        "are used only as identity cross-references",
    },
    "commercial-transfer-databases": {
        "publisher": "commercial transfer and market-value databases (e.g. Transfermarkt)",
        "delivers": ["transfers", "market values"],
        "terms": "terms forbid automated extraction and redistribution (verify)",
        "access_decision": "not implemented",
        "reason": "excluded: commercial data and scraping are non-goals; market values are not published records",
    },
    "bookmakers": {
        "publisher": "bookmakers and odds aggregators",
        "delivers": ["odds"],
        "access_decision": "not implemented",
        "reason": "excluded by the pack's non-goals: no odds, betting advice or value bets are acquired or stored",
    },
}

# Live evidence is reported separately from offline fixtures; no dated live run has happened (SP13).
LIVE_VERIFICATION: dict[str, dict[str, Any]] = {
    provider: {
        "status": "outstanding",
        "note": "offline fixture evidence only; a dated bounded live run is recorded under "
        "docs/development/sports-evidence/",
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
    if contract["access_decision"] == "implement"
}
LIVE_VERIFICATION["football-data"]["credential"] = "NOESIS_FOOTBALL_DATA_API_KEY"
for _provider, _contract in PROVIDER_CONTRACTS.items():
    if _contract["access_decision"] != "implement":
        LIVE_VERIFICATION[_provider] = {
            "status": _contract["access_decision"],
            "note": _contract["reason"],
        }


class SportsFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ helpers


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def compact(value: Any) -> Any:
    """Drop absent values recursively: a missing value is absent, never ``None`` or ``"None"``."""
    if isinstance(value, Mapping):
        return {
            str(k): compact(v)
            for k, v in value.items()
            if v is not None and v != "" and v != [] and v != {}
        }
    if isinstance(value, (list, tuple)):
        return [compact(v) for v in value if v is not None]
    return value


def _clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def slug(value: Any) -> str:
    """A stable key part for a published label (letters of any script kept, punctuation dropped)."""
    return "-".join(re.sub(r"[\W_]+", " ", str(value or "").casefold()).split())


def record_key(provider: str, kind: str, *parts: Any) -> str:
    """Record keys name the provider and every distinguishing field; they never depend on arrival order."""
    cleaned = [slug(p) for p in parts]
    if not all(cleaned):
        raise SportsFormatError("schema_drift", f"{kind} key needs every part")
    return f"sports:{provider}:{kind}:" + ":".join(cleaned)


def iso_datetime(value: Any) -> str | None:
    """An ISO UTC date-time (``YYYY-MM-DDTHH:MM:SSZ``) or date (``YYYY-MM-DD``) as the source states it."""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text).isoformat()
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SportsFormatError(
            "schema_drift", f"date {value!r} is not ISO 8601"
        ) from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_ms(value: str) -> int:
    """Epoch milliseconds of an ISO date or UTC date-time (a date is its start, UTC)."""
    if len(value) == 10:
        stamp = datetime.combine(date.fromisoformat(value), datetime.min.time())
        return int(stamp.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise SportsFormatError("schema_drift", f"{value!r} is not an integer") from exc


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SportsFormatError("schema_drift", "response is not JSON") from exc


def _csv(raw: bytes, required: Sequence[str]) -> list[dict[str, str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SportsFormatError("schema_drift", "CSV is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in reader.fieldnames or []]
    missing = [c for c in required if c not in header]
    if missing:
        raise SportsFormatError("schema_drift", f"missing columns {missing}")
    rows = []
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items() if k})
        if len(rows) > MAX_ROWS:
            raise SportsFormatError(
                "input_limit", "file has more rows than the parser accepts"
            )
    return rows


def _observation(
    record_type: str,
    key: str,
    body: Mapping[str, Any],
    *,
    source_record_id: str,
    locator: str,
    published_at: str | None = None,
) -> dict[str, Any]:
    return compact(
        {
            "record_type": record_type,
            "record_key": key,
            "source_record_id": str(source_record_id),
            "locator": locator,
            "published_at": published_at,
            "body": compact(dict(body)),
        }
    )


def _score(home: Any, away: Any) -> dict[str, int] | None:
    home, away = _int(home), _int(away)
    if home is None and away is None:
        return None
    if home is None or away is None:
        raise SportsFormatError("schema_drift", "a score states both sides")
    return {"home": home, "away": away}


# ------------------------------------------------------------------ football-data.org v4

FD_SCHEDULE = {
    "SCHEDULED": "scheduled",
    "TIMED": "scheduled",
    "IN_PLAY": "scheduled",
    "PAUSED": "scheduled",
    "LIVE": "scheduled",
    "FINISHED": "scheduled",
    "AWARDED": "scheduled",
    "POSTPONED": "postponed",
    "SUSPENDED": "abandoned",
    "CANCELLED": "cancelled",
}
FD_RESULT = {
    "IN_PLAY": "provisional",
    "PAUSED": "provisional",
    "LIVE": "provisional",
    "FINISHED": "official",
    "AWARDED": "forfeit_awarded",
}


def _fd_team(team: Mapping[str, Any], locator: str) -> tuple[str, dict[str, Any]]:
    native = team.get("id")
    if native is None:
        raise SportsFormatError("schema_drift", "a team has no id")
    key = record_key("football-data", "team", native)
    return key, _observation(
        "team",
        key,
        {
            "provider": "football-data",
            "native_id": str(native),
            "name": _clean(team.get("name")),
            "short_name": _clean(team.get("shortName")),
            "code": _clean(team.get("tla")),
        },
        source_record_id=str(native),
        locator=locator,
    )


def _fd_competition(
    data: Mapping[str, Any], declared: Mapping[str, Any], locator: str
) -> tuple[str, str, list[dict[str, Any]]]:
    competition = dict(data.get("competition") or {})
    stated = dict(declared.get("competition") or {}).get("code")
    if stated and competition.get("code") and competition["code"] != stated:
        raise SportsFormatError(
            "schema_drift",
            f"response is for {competition['code']}, the source declares {stated}",
        )
    code = competition.get("code") or stated
    season = str(declared.get("season") or "")
    if not code or not season:
        raise SportsFormatError(
            "schema_drift", "response or source names the competition and season"
        )
    comp_key = record_key("football-data", "competition", code)
    season_key = record_key("football-data", "season", code, season)
    rules = dict(declared.get("competition") or {})
    out = [
        _observation(
            "competition",
            comp_key,
            {
                "provider": "football-data",
                "native_id": str(code),
                "name": _clean(competition.get("name")) or rules.get("name"),
                "sport": "football",
                "format": _clean(competition.get("type")),
                "governing_body": rules.get("governing_body"),
                "area": rules.get("area"),
            },
            source_record_id=str(competition.get("id") or code),
            locator=locator,
        ),
        _observation(
            "season",
            season_key,
            {"competition_key": comp_key, "label": season},
            source_record_id=f"{code}:{season}",
            locator=locator,
        ),
    ]
    return comp_key, season_key, out


def parse_football_data_matches(
    raw: bytes, *, declared: Mapping[str, Any], url: str
) -> dict[str, Any]:
    data = _json(raw)
    matches = data.get("matches")
    if not isinstance(matches, list):
        raise SportsFormatError("schema_drift", "response has no matches array")
    _, season_key, out = _fd_competition(data, declared, url)
    for index, match in enumerate(matches):
        locator = f"{url}#/matches/{index}"
        native = match.get("id")
        status = str(match.get("status") or "")
        if native is None or status not in FD_SCHEDULE:
            raise SportsFormatError(
                "schema_drift", f"match {native!r} has unknown status {status!r}"
            )
        published = iso_datetime(match.get("lastUpdated"))
        sides, names = {}, {}
        for side, field in (("home", "homeTeam"), ("away", "awayTeam")):
            team = dict(match.get(field) or {})
            if team.get("id") is None:
                continue  # an undecided knockout slot names no team yet
            # The teams endpoint owns team records; a match states only the side and its name as published.
            sides[side] = record_key("football-data", "team", team["id"])
            names[side] = _clean(team.get("name"))
        fixture_key = record_key("football-data", "fixture", native)
        stage = _clean(match.get("stage"))
        stage_key = None
        if stage:
            stage_key = record_key(
                "football-data", "stage", *season_key.split(":")[-2:], stage
            )
            out.append(
                _observation(
                    "stage",
                    stage_key,
                    {
                        "season_key": season_key,
                        "name": stage,
                        "kind": "league" if stage == "REGULAR_SEASON" else "knockout",
                        "group": _clean(match.get("group")),
                    },
                    source_record_id=stage,
                    locator=locator,
                )
            )
        out.append(
            _observation(
                "fixture",
                fixture_key,
                {
                    "sport": "football",
                    "season_key": season_key,
                    "stage_key": stage_key,
                    "round": None
                    if match.get("matchday") is None
                    else f"Matchday {match['matchday']}",
                    "sides": sides,
                    "side_names": names,
                },
                source_record_id=str(native),
                locator=locator,
            )
        )
        out.append(
            _observation(
                "fixture_schedule_revision",
                fixture_key,
                {
                    "status": FD_SCHEDULE[status],
                    "kickoff": iso_datetime(match.get("utcDate")),
                },
                source_record_id=str(native),
                locator=locator,
                published_at=published,
            )
        )
        if status in FD_RESULT:
            score = dict(match.get("score") or {})
            full = dict(score.get("fullTime") or {})
            half = dict(score.get("halfTime") or {})
            result = _score(full.get("home"), full.get("away"))
            if result is None:
                raise SportsFormatError(
                    "schema_drift", f"match {native} is {status} without a score"
                )
            out.append(
                _observation(
                    "match_result_revision",
                    fixture_key,
                    {
                        "status": FD_RESULT[status],
                        "score": result,
                        "periods": compact(
                            {"half_time": _score(half.get("home"), half.get("away"))}
                        ),
                        "duration": _clean(score.get("duration")),
                        "source_status": status,
                    },
                    source_record_id=str(native),
                    locator=locator,
                    published_at=published,
                )
            )
    return {"records": out, "published_at": None}


def parse_football_data_standings(
    raw: bytes,
    *,
    declared: Mapping[str, Any],
    url: str,
    headers: Mapping[str, Any],
) -> dict[str, Any]:
    data = _json(raw)
    tables = data.get("standings")
    if not isinstance(tables, list):
        raise SportsFormatError("schema_drift", "response has no standings array")
    _, season_key, out = _fd_competition(data, declared, url)
    # The response states no as-of time; the provider's HTTP Date header is the publication time (verify).
    stamp = headers.get("date") or headers.get("last-modified")
    as_of = None
    if stamp:
        try:
            as_of = iso_datetime(parsedate_to_datetime(str(stamp)).isoformat())
        except (TypeError, ValueError):
            as_of = None
    if as_of is None:
        raise SportsFormatError(
            "schema_drift", "standings response states no date (HTTP Date header)"
        )
    for index, table in enumerate(tables):
        if table.get("type") != "TOTAL":
            continue  # HOME and AWAY splits are derived views, not the table
        stage = _clean(table.get("stage")) or "REGULAR_SEASON"
        group = _clean(table.get("group"))
        rows = []
        for row in table.get("table") or []:
            team = dict(row.get("team") or {})
            if team.get("id") is None:
                raise SportsFormatError("schema_drift", "a table row names no team")
            key = record_key("football-data", "team", team["id"])
            rows.append(
                compact(
                    {
                        "position": _int(row.get("position")),
                        "team_key": key,
                        "team_name": _clean(team.get("name")),
                        "played": _int(row.get("playedGames")),
                        "won": _int(row.get("won")),
                        "drawn": _int(row.get("draw")),
                        "lost": _int(row.get("lost")),
                        "goals_for": _int(row.get("goalsFor")),
                        "goals_against": _int(row.get("goalsAgainst")),
                        "goal_difference": _int(row.get("goalDifference")),
                        "points": _int(row.get("points")),
                    }
                )
            )
        out.append(
            _observation(
                "standing_snapshot",
                f"{season_key}|standings|{slug(stage)}"
                + (f"|{slug(group)}" if group else ""),
                {
                    "season_key": season_key,
                    "stage": stage,
                    "group": group,
                    "as_of": as_of,
                    "rows": rows,
                },
                source_record_id=f"{season_key}:{stage}:{group or ''}",
                locator=f"{url}#/standings/{index}",
                published_at=as_of,
            )
        )
    return {"records": out, "published_at": as_of}


def parse_football_data_teams(
    raw: bytes, *, declared: Mapping[str, Any], url: str
) -> dict[str, Any]:
    """Teams of a competition season with founding year, country and venue name; squads as published participants.

    Only name and a published date of birth are kept for a squad member (identity evidence); position,
    nationality and the coach block are dropped.
    """
    data = _json(raw)
    teams = data.get("teams")
    if not isinstance(teams, list):
        raise SportsFormatError("schema_drift", "response has no teams array")
    _, _, out = _fd_competition(data, declared, url)
    for index, item in enumerate(teams):
        locator = f"{url}#/teams/{index}"
        key, observation = _fd_team(item, locator)
        venue_name = _clean(item.get("venue"))
        country = _clean(dict(item.get("area") or {}).get("name"))
        venue_key = None
        if venue_name:
            venue_key = record_key("football-data", "venue", item["id"], venue_name)
            out.append(
                _observation(
                    "venue",
                    venue_key,
                    {
                        "provider": "football-data",
                        "name": venue_name,
                        "country": country,
                    },
                    source_record_id=f"{item['id']}:{venue_name}",
                    locator=locator,
                )
            )
        observation["body"] = compact(
            {
                **observation["body"],
                "founded": _int(item.get("founded")),
                "country": country,
                "venue_key": venue_key,
            }
        )
        out.append(observation)
        for number, member in enumerate(item.get("squad") or []):
            if member.get("id") is None:
                raise SportsFormatError("schema_drift", "a squad member has no id")
            player_key = record_key("football-data", "player", member["id"])
            out.append(
                _observation(
                    "player",
                    player_key,
                    {
                        "provider": "football-data",
                        "native_id": str(member["id"]),
                        "name": _clean(member.get("name")),
                        "birth_date": iso_datetime(member.get("dateOfBirth")),
                    },
                    source_record_id=str(member["id"]),
                    locator=f"{locator}/squad/{number}",
                )
            )
    return {"records": out}


# ------------------------------------------------------------------ StatsBomb open data


def parse_statsbomb(
    raw: bytes, *, path: str, declared: Mapping[str, Any], url: str
) -> dict[str, Any]:
    data = _json(raw)
    if not isinstance(data, list):
        raise SportsFormatError("schema_drift", "StatsBomb files are JSON arrays")
    provider = "statsbomb-open-data"
    out: list[dict[str, Any]] = []
    if path.endswith("competitions.json"):
        for index, item in enumerate(data):
            comp, season = item.get("competition_id"), item.get("season_id")
            if comp is None or season is None:
                raise SportsFormatError("schema_drift", "competition row without ids")
            comp_key = record_key(provider, "competition", comp)
            locator = f"{url}#/{index}"
            out.append(
                _observation(
                    "competition",
                    comp_key,
                    {
                        "provider": provider,
                        "native_id": str(comp),
                        "name": _clean(item.get("competition_name")),
                        "sport": "football",
                        "area": _clean(item.get("country_name")),
                        "gender": _clean(item.get("competition_gender")),
                    },
                    source_record_id=str(comp),
                    locator=locator,
                )
            )
            out.append(
                _observation(
                    "season",
                    record_key(provider, "season", comp, season),
                    {
                        "competition_key": comp_key,
                        "label": _clean(item.get("season_name")),
                    },
                    source_record_id=f"{comp}:{season}",
                    locator=locator,
                )
            )
        return {"records": out}
    if "/matches/" in f"/{path}":
        for index, item in enumerate(data):
            locator = f"{url}#/{index}"
            native = item.get("match_id")
            comp = dict(item.get("competition") or {}).get("competition_id")
            season = dict(item.get("season") or {}).get("season_id")
            if native is None or comp is None or season is None:
                raise SportsFormatError("schema_drift", "match row without ids")
            season_key = record_key(provider, "season", comp, season)
            sides = {}
            for side in ("home", "away"):
                team = dict(item.get(f"{side}_team") or {})
                team_id = team.get(f"{side}_team_id")
                if team_id is None:
                    raise SportsFormatError(
                        "schema_drift", "match side without a team id"
                    )
                key = record_key(provider, "team", team_id)
                sides[side] = key
                out.append(
                    _observation(
                        "team",
                        key,
                        {
                            "provider": provider,
                            "native_id": str(team_id),
                            "name": _clean(team.get(f"{side}_team_name")),
                            "country": _clean(
                                dict(team.get("country") or {}).get("name")
                            ),
                        },
                        source_record_id=str(team_id),
                        locator=locator,
                    )
                )
            stadium = dict(item.get("stadium") or {})
            venue_key = None
            if stadium.get("id") is not None:
                venue_key = record_key(provider, "venue", stadium["id"])
                out.append(
                    _observation(
                        "venue",
                        venue_key,
                        {
                            "provider": provider,
                            "native_id": str(stadium["id"]),
                            "name": _clean(stadium.get("name")),
                            "country": _clean(
                                dict(stadium.get("country") or {}).get("name")
                            ),
                        },
                        source_record_id=str(stadium["id"]),
                        locator=locator,
                    )
                )
            stage = _clean(dict(item.get("competition_stage") or {}).get("name"))
            fixture_key = record_key(provider, "fixture", native)
            published = iso_datetime(item.get("last_updated"))
            kickoff = iso_datetime(item.get("match_date"))
            out.append(
                _observation(
                    "fixture",
                    fixture_key,
                    {
                        "sport": "football",
                        "season_key": season_key,
                        "round": None
                        if item.get("match_week") is None
                        else f"Matchday {item['match_week']}",
                        "stage": stage,
                        "sides": sides,
                        "venue_key": venue_key,
                    },
                    source_record_id=str(native),
                    locator=locator,
                )
            )
            out.append(
                _observation(
                    "fixture_schedule_revision",
                    fixture_key,
                    {"status": "scheduled", "kickoff": kickoff},
                    source_record_id=str(native),
                    locator=locator,
                    published_at=published,
                )
            )
            score = _score(item.get("home_score"), item.get("away_score"))
            if score is not None:
                out.append(
                    _observation(
                        "match_result_revision",
                        fixture_key,
                        {"status": "official", "score": score},
                        source_record_id=str(native),
                        locator=locator,
                        published_at=published,
                    )
                )
        return {"records": out}
    if "/lineups/" in f"/{path}":
        match = re.search(r"lineups/(\d+)\.json$", path)
        if not match:
            raise SportsFormatError("schema_drift", "lineup file names its match id")
        fixture_key = record_key(provider, "fixture", match.group(1))
        for index, team in enumerate(data):
            team_id = team.get("team_id")
            if team_id is None:
                raise SportsFormatError("schema_drift", "lineup team without an id")
            team_key = record_key(provider, "team", team_id)
            locator = f"{url}#/{index}"
            starters, substitutes = [], []
            for player in team.get("lineup") or []:
                player_id = player.get("player_id")
                if player_id is None:
                    raise SportsFormatError(
                        "schema_drift", "lineup player without an id"
                    )
                player_key = record_key(provider, "player", player_id)
                out.append(
                    _observation(
                        "player",
                        player_key,
                        {
                            "provider": provider,
                            "native_id": str(player_id),
                            "name": _clean(player.get("player_name")),
                            "known_as": _clean(player.get("player_nickname")),
                        },
                        source_record_id=str(player_id),
                        locator=locator,
                    )
                )
                positions = list(player.get("positions") or [])
                first = dict(positions[0]) if positions else {}
                entry = compact(
                    {
                        "player_key": player_key,
                        "name": _clean(player.get("player_name")),
                        "shirt_number": _int(player.get("jersey_number")),
                        "position": _clean(first.get("position")),
                    }
                )
                if first.get("start_reason") == "Starting XI":
                    starters.append(entry)
                else:
                    substitutes.append(entry)
            out.append(
                _observation(
                    "lineup",
                    f"{fixture_key}|lineup|{team_key}",
                    {
                        "fixture_key": fixture_key,
                        "team_key": team_key,
                        "starters": starters,
                        "substitutes": substitutes,
                    },
                    source_record_id=f"{match.group(1)}:{team_id}",
                    locator=locator,
                )
            )
        return {"records": out}
    raise SportsFormatError("schema_drift", f"unsupported StatsBomb file {path!r}")


# ------------------------------------------------------------------ openfootball


def parse_openfootball(
    raw: bytes, *, declared: Mapping[str, Any], url: str
) -> dict[str, Any]:
    data = _json(raw)
    provider = "openfootball"
    league = str(dict(declared.get("competition") or {}).get("code") or "")
    season = str(declared.get("season") or "")
    if not league or not season:
        raise SportsFormatError(
            "schema_drift", "the source names the league and season"
        )
    comp_key = record_key(provider, "competition", league)
    season_key = record_key(provider, "season", league, season)
    out = [
        _observation(
            "competition",
            comp_key,
            {
                "provider": provider,
                "native_id": league,
                "name": _clean(dict(declared.get("competition") or {}).get("name")),
                "sport": "football",
                "area": dict(declared.get("competition") or {}).get("area"),
            },
            source_record_id=league,
            locator=url,
        ),
        _observation(
            "season",
            season_key,
            {
                "competition_key": comp_key,
                "label": season,
                "name": _clean(data.get("name")),
            },
            source_record_id=f"{league}:{season}",
            locator=url,
        ),
    ]
    items: list[tuple[str, Mapping[str, Any], str | None]] = []
    if isinstance(data.get("matches"), list):
        items = [
            (f"{url}#/matches/{i}", m, None) for i, m in enumerate(data["matches"])
        ]
    elif isinstance(data.get("rounds"), list):
        for r, block in enumerate(data["rounds"]):
            for i, m in enumerate(block.get("matches") or []):
                # The enclosing round names this match's round; nothing carries over between matches.
                items.append(
                    (f"{url}#/rounds/{r}/matches/{i}", m, _clean(block.get("name")))
                )
    else:
        raise SportsFormatError(
            "schema_drift", "football.json has no matches or rounds"
        )
    for locator, match, enclosing_round in items:
        round_name = _clean(match.get("round")) or enclosing_round
        home, away = _clean(match.get("team1")), _clean(match.get("team2"))
        if not round_name or not home or not away:
            raise SportsFormatError(
                "schema_drift", "a football.json match states its round and both teams"
            )
        sides = {}
        for side, name in (("home", home), ("away", away)):
            key = record_key(provider, "team", league, name)
            sides[side] = key
            out.append(
                _observation(
                    "team",
                    key,
                    {"provider": provider, "name": name},
                    source_record_id=name,
                    locator=locator,
                )
            )
        # Season, round and both teams identify the match; a moved date is a schedule revision, not a new key.
        fixture_key = record_key(
            provider, "fixture", league, season, round_name, home, away
        )
        out.append(
            _observation(
                "fixture",
                fixture_key,
                {
                    "sport": "football",
                    "season_key": season_key,
                    "round": round_name,
                    "sides": sides,
                },
                source_record_id=f"{season}:{round_name}:{home}-{away}",
                locator=locator,
            )
        )
        day = iso_datetime(match.get("date"))
        time_text = _clean(match.get("time"))
        kickoff = day
        if day and time_text and re.fullmatch(r"\d{1,2}:\d{2}", time_text):
            # football.json states local kickoff times without a zone; kept as published, never converted.
            kickoff = f"{day}T{time_text.zfill(5)}"
        out.append(
            _observation(
                "fixture_schedule_revision",
                fixture_key,
                {
                    "status": "scheduled",
                    "kickoff": kickoff,
                    "kickoff_zone": "local (as published)"
                    if kickoff and "T" in kickoff
                    else None,
                },
                source_record_id=f"{season}:{round_name}:{home}-{away}",
                locator=locator,
            )
        )
        score = dict(match.get("score") or {})
        full = score.get("ft")
        if full is not None:
            if not isinstance(full, list) or len(full) != 2:
                raise SportsFormatError("schema_drift", "a score is a pair")
            half = score.get("ht")
            out.append(
                _observation(
                    "match_result_revision",
                    fixture_key,
                    {
                        "status": "official",
                        "score": _score(full[0], full[1]),
                        "periods": compact(
                            {
                                "half_time": _score(half[0], half[1])
                                if isinstance(half, list) and len(half) == 2
                                else None
                            }
                        ),
                    },
                    source_record_id=f"{season}:{round_name}:{home}-{away}",
                    locator=locator,
                )
            )
    return {"records": out}


# ------------------------------------------------------------------ Sackmann tennis

_SACKMANN_REQUIRED = (
    "tourney_id",
    "tourney_name",
    "surface",
    "tourney_level",
    "tourney_date",
    "match_num",
    "winner_id",
    "winner_name",
    "loser_id",
    "loser_name",
    "score",
    "best_of",
    "round",
)


def parse_sackmann(
    raw: bytes, *, declared: Mapping[str, Any], url: str
) -> dict[str, Any]:
    provider = "sackmann-tennis"
    tour = str(declared.get("tour") or "").lower()
    if tour not in {"atp", "wta"}:
        raise SportsFormatError(
            "schema_drift", "a tennis source names its tour (atp or wta)"
        )
    out: list[dict[str, Any]] = []
    for index, row in enumerate(_csv(raw, _SACKMANN_REQUIRED)):
        locator = f"{url}#row={index + 2}"
        tourney = row["tourney_id"]
        year = (iso_datetime(row["tourney_date"]) or "")[:4]
        if not tourney or not year or not row["match_num"]:
            raise SportsFormatError(
                "schema_drift", "a match row names its tournament and match"
            )
        comp_key = record_key(provider, "competition", tour, tourney)
        season_key = record_key(provider, "season", tour, tourney, year)
        out.append(
            _observation(
                "competition",
                comp_key,
                {
                    "provider": provider,
                    "native_id": tourney,
                    "name": _clean(row["tourney_name"]),
                    "sport": "tennis",
                    "format": "knockout",
                    "tour": tour,
                    "level": _clean(row["tourney_level"]),
                    "surface": _clean(row["surface"]),
                },
                source_record_id=tourney,
                locator=locator,
            )
        )
        out.append(
            _observation(
                "season",
                season_key,
                {
                    "competition_key": comp_key,
                    "label": year,
                    "start_date": iso_datetime(row["tourney_date"]),
                },
                source_record_id=f"{tourney}:{year}",
                locator=locator,
            )
        )
        sides = {}
        for side, prefix in (("p1", "winner"), ("p2", "loser")):
            native = row[f"{prefix}_id"]
            if not native:
                raise SportsFormatError("schema_drift", "a match side has no player id")
            key = record_key(provider, "player", tour, native)
            sides[side] = key
            # Match-level fields only: height, handedness, age and nationality columns are never read.
            out.append(
                _observation(
                    "player",
                    key,
                    {
                        "provider": provider,
                        "native_id": native,
                        "name": _clean(row[f"{prefix}_name"]),
                        "tour": tour,
                    },
                    source_record_id=native,
                    locator=locator,
                )
            )
        fixture_key = record_key(provider, "fixture", tour, tourney, row["match_num"])
        out.append(
            _observation(
                "fixture",
                fixture_key,
                {
                    "sport": "tennis",
                    "season_key": season_key,
                    "round": _clean(row["round"]),
                    "sides": sides,
                    "best_of": _int(row["best_of"]),
                    "surface": _clean(row["surface"]),
                },
                source_record_id=f"{tourney}:{row['match_num']}",
                locator=locator,
            )
        )
        score = _clean(row["score"])
        if score:
            out.append(
                _observation(
                    "match_result_revision",
                    fixture_key,
                    {
                        "status": "official",
                        "score_text": score,
                        "winner": "p1",
                        "walkover": True if score.upper() == "W/O" else None,
                        "retired": True if "RET" in score.upper() else None,
                    },
                    source_record_id=f"{tourney}:{row['match_num']}",
                    locator=locator,
                )
            )
    return {"records": out}


# ------------------------------------------------------------------ Olympic results

_OLYMPIC_REQUIRED = (
    "games",
    "sport",
    "event",
    "phase",
    "rank",
    "participant",
    "noc",
    "result",
    "status",
    "decision_body",
    "decision_url",
    "published_on",
)
OLYMPIC_ROW_STATUSES = ("official", "provisional", "disqualified", "reallocated")


def parse_olympic(raw: bytes, *, url: str) -> dict[str, Any]:
    provider = "olympic-results"
    rows = _csv(raw, _OLYMPIC_REQUIRED)
    phases: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        locator = f"{url}#row={index + 2}"
        for column in (
            "games",
            "sport",
            "event",
            "phase",
            "participant",
            "published_on",
        ):
            if not row[column]:
                raise SportsFormatError(
                    "schema_drift", f"row {index + 2} has no {column}"
                )
        if row["status"] not in OLYMPIC_ROW_STATUSES:
            raise SportsFormatError(
                "schema_drift", f"unknown row status {row['status']!r}"
            )
        published = iso_datetime(row["published_on"])
        comp_key = record_key(
            provider, "competition", row["games"], row["sport"], row["event"]
        )
        season_key = record_key(
            provider, "season", row["games"], row["sport"], row["event"]
        )
        fixture_key = record_key(
            provider, "fixture", row["games"], row["sport"], row["event"], row["phase"]
        )
        phase = phases.setdefault(
            fixture_key,
            {
                "published": published,
                "locators": [],
                "ranking": [],
                "bodies": set(),
                "urls": set(),
                "games": row["games"],
                "sport": row["sport"],
                "event": row["event"],
                "phase": row["phase"],
                "comp_key": comp_key,
                "season_key": season_key,
            },
        )
        if phase["published"] != published:
            raise SportsFormatError(
                "schema_drift",
                "one file is one publication: every row of a phase states the same date",
            )
        phase["locators"].append(locator)
        participant_key = record_key(
            provider, "participant", row["noc"] or "none", row["participant"]
        )
        out.append(
            _observation(
                "player",
                participant_key,
                {
                    "provider": provider,
                    "name": _clean(row["participant"]),
                    "noc": _clean(row["noc"]),
                },
                source_record_id=f"{row['noc']}:{row['participant']}",
                locator=locator,
            )
        )
        phase["ranking"].append(
            compact(
                {
                    "rank": _int(row["rank"]),
                    "participant_key": participant_key,
                    "participant": _clean(row["participant"]),
                    "noc": _clean(row["noc"]),
                    "result": _clean(row["result"]),
                    "status": row["status"],
                }
            )
        )
        if row["decision_body"]:
            phase["bodies"].add(row["decision_body"])
        if row["decision_url"]:
            if not row["decision_url"].startswith("https://"):
                raise SportsFormatError(
                    "schema_drift", "a decision is cited by an https URL"
                )
            phase["urls"].add(row["decision_url"])
    for fixture_key, phase in sorted(phases.items()):
        locator = phase["locators"][0]
        out.append(
            _observation(
                "competition",
                phase["comp_key"],
                {
                    "provider": provider,
                    "name": f"{phase['games']} {phase['sport']} - {phase['event']}",
                    "sport": phase["sport"],
                    "governing_body": "International Olympic Committee",
                    "format": "event",
                },
                source_record_id=f"{phase['games']}:{phase['sport']}:{phase['event']}",
                locator=locator,
            )
        )
        out.append(
            _observation(
                "season",
                phase["season_key"],
                {"competition_key": phase["comp_key"], "label": phase["games"]},
                source_record_id=phase["games"],
                locator=locator,
            )
        )
        out.append(
            _observation(
                "fixture",
                fixture_key,
                {
                    "sport": "olympic",
                    "season_key": phase["season_key"],
                    "round": phase["phase"],
                    "sides": {},
                },
                source_record_id=f"{phase['games']}:{phase['sport']}:{phase['event']}:{phase['phase']}",
                locator=locator,
            )
        )
        statuses = {r["status"] for r in phase["ranking"]}
        if len(phase["bodies"]) > 1 or len(phase["urls"]) > 1:
            raise SportsFormatError(
                "schema_drift", "one publication cites one deciding body and decision"
            )
        decided = bool(statuses & {"disqualified", "reallocated"})
        if decided and not (phase["bodies"] and phase["urls"]):
            raise SportsFormatError(
                "schema_drift",
                "a disqualification or reallocation names the deciding body and cites its decision",
            )
        body = next(iter(phase["bodies"]), None)
        out.append(
            _observation(
                "match_result_revision",
                fixture_key,
                {
                    "status": "provisional"
                    if statuses == {"provisional"}
                    else "official",
                    "ranking": sorted(
                        phase["ranking"],
                        key=lambda r: (
                            r.get("rank") is None,
                            r.get("rank") or 0,
                            r["participant"],
                        ),
                    ),
                    "deciding_body": body,
                    "decision": compact(
                        {"url": next(iter(phase["urls"]), None), "body": body}
                    ),
                },
                source_record_id=fixture_key.rsplit(":", 1)[-1],
                locator=locator,
                published_at=phase["published"],
            )
        )
    return {"records": out}


def olympic_acquisition(
    csv_text: str, *, source_url: str, evidence_origin: str = "operator"
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """An operator transcription of an official Olympic results publication, cited to that publication."""
    if not str(source_url or "").startswith("https://"):
        raise SportsFormatError(
            "schema_drift", "the transcription cites its official publication (https)"
        )
    raw = str(csv_text or "").encode()
    parsed = parse_olympic(raw, url=source_url)
    header = compact(
        {
            "contract": ACQUISITION_CONTRACT,
            "provider": "olympic-results",
            "format": "olympic-results-csv",
            "sport": "olympic",
            "url": source_url,
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "record_count": len(parsed["records"]),
            "attribution": PROVIDER_CONTRACTS["olympic-results"]["attribution"],
            "licence": {"id": "facts-with-citation", "terms_url": source_url},
            "evidence_origin": evidence_origin,
        }
    )
    return header, parsed["records"]


# ------------------------------------------------------------------ declarations and the adapter


def sports_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("sports") or {})
    format_id = declared.get("format")
    if format_id not in FORMATS or FORMATS[format_id]["provider"] != declared.get(
        "provider"
    ):
        raise SourcePackError(
            "invalid_manifest", "sports sources declare a matching provider and format"
        )
    if not str(declared.get("attribution") or "").strip():
        raise SourcePackError(
            "invalid_manifest", "sports sources record their attribution text"
        )
    files = declared.get("files")
    if files is not None:
        if (
            not isinstance(files, list)
            or not files
            or len(files) > MAX_FILES
            or any(
                not isinstance(f, str) or f.startswith("/") or ".." in f or "://" in f
                for f in files
            )
        ):
            raise SourcePackError(
                "invalid_manifest", "a file list is a bounded list of relative paths"
            )
    if format_id in {
        "statsbomb-open-data",
        "openfootball-json",
        "sackmann-matches-csv",
    }:
        release = dict(declared.get("release") or {})
        if not re.fullmatch(
            r"[0-9a-f]{40}", str(release.get("commit") or "")
        ) or not release.get("committed_at"):
            raise SourcePackError(
                "invalid_manifest",
                "a repository source pins a commit and states its date",
            )
        if release["commit"] not in str(source.get("endpoint") or ""):
            raise SourcePackError(
                "invalid_manifest",
                "the endpoint reads the pinned commit, never a branch",
            )
    rules = declared.get("rules")
    if rules is not None and not str(dict(rules).get("source_url") or "").startswith(
        "https://"
    ):
        raise SourcePackError(
            "invalid_manifest", "competition rules cite their source (https)"
        )
    if format_id == "sackmann-matches-csv":
        licence = dict(declared.get("licence") or {})
        if licence.get("id") != SACKMANN_LICENCE["id"] or not licence.get(
            "share_alike"
        ):
            raise SourcePackError(
                "invalid_manifest",
                "tennis archive sources carry CC BY-NC-SA 4.0 with share-alike",
            )
    return declared


def unverified(provider: str) -> bool:
    return LIVE_VERIFICATION.get(provider, {}).get("status") != "verified-live"


class SportsResultsAdapter:
    """Fetch the declared publications of one sports source on the runtime transport; one page per file."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = sports_declaration(self.source)
        self.secret = secret
        if transport is None:
            from functools import partial

            transport = partial(
                HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
            )
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "sports": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        if dict(request.get("parameters") or {}):
            raise SourcePackError(
                "parameter_forbidden",
                "sports runs fetch the declared publications, not ad-hoc queries",
            )

    def _files(self) -> list[str | None]:
        return list(self.declared.get("files") or [None])

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        self._check(request)
        files = self._files()
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(files):
            raise SourcePackError(
                "cursor_drift", "cursor does not name a declared file"
            )
        path = files[index]
        base = self.source["endpoint"]
        url = base if path is None else base.rstrip("/") + "/" + path
        host = (urlsplit(base).hostname or "").casefold()
        headers = {"Accept": "application/json, text/csv, text/plain"}
        auth = dict(self.source.get("auth") or {})
        if auth.get("kind") == "required-secret":
            if not self.secret:
                raise SourcePackError(
                    "credential_missing",
                    f"{auth.get('secret_ref')} is not configured in the credential store",
                )
            headers["X-Auth-Token"] = self.secret
        response = self.transport(
            url=url,
            params={},
            headers=headers,
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "publication was served from another host"
            )
        status = int(response.get("status", 200))
        response_headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "publication exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(
                    response_headers.get("retry-after")
                    or response_headers.get("x-requestcounter-reset")
                ),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"request refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"provider returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        try:
            parsed = self.parse(raw, url=url, path=path, headers=response_headers)
        except SportsFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift",
                f"{exc.code}: {exc}",
            ) from exc
        records = parsed["records"]
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > min(limit, MAX_RECORDS):
            # Never a truncated publication: a missing match would read as a match without a result.
            raise SourcePackError(
                "budget_exhausted", "publication has more records than the run's budget"
            )
        release = dict(self.declared.get("release") or {})
        header = compact(
            {
                "contract": ACQUISITION_CONTRACT,
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "sport": FORMATS[self.declared["format"]]["sport"],
                "path": path,
                "url": url,
                "file_sha256": hashlib.sha256(raw).hexdigest(),
                "release": release.get("commit"),
                "release_date": iso_datetime(release.get("committed_at")),
                "published_at": parsed.get("published_at")
                or iso_datetime(release.get("committed_at")),
                "record_count": len(records),
                "attribution": self.declared["attribution"],
                "licence": compact(
                    {
                        **dict(self.declared.get("licence") or {}),
                        "id": dict(self.declared.get("licence") or {}).get("id")
                        or self.source["license"]["id"],
                        "terms_url": self.source["license"]["terms_url"],
                        "redistribution": self.source["license"]["redistribution"],
                    }
                ),
                "competition": self.declared.get("competition"),
                "rules": self.declared.get("rules"),
                "evidence_origin": "fixture"
                if response.get("origin") == "fixture"
                else "live",
                "rate_limit": compact(
                    {
                        "available_minute": response_headers.get(
                            "x-requests-available-minute"
                        ),
                        "reset_s": response_headers.get("x-requestcounter-reset"),
                    }
                ),
            }
        )
        page_records = []
        for position, item in enumerate(records):
            page_records.append(
                {
                    "id": f"{item['record_type']}|{item['record_key']}|{position}",
                    "title": f"{item['record_type']} {item['record_key']}",
                    "url": url,
                    "language": "en",
                    "published_at": item.get("published_at")
                    or header.get("published_at"),
                    "content": json.dumps(item, sort_keys=True, ensure_ascii=False),
                    "sports_acquisition": header,
                    "sports_record": item,
                }
            )
        receipt = {
            "status": status,
            "provider": header["provider"],
            "file": path,
            "file_sha256": header["file_sha256"],
            "records": len(page_records),
            "evidence_origin": header["evidence_origin"],
            "rate_limit": header.get("rate_limit"),
            "final_page": index + 1 >= len(files),
        }
        next_cursor = None if index + 1 >= len(files) else str(index + 1)
        return RuntimePage(tuple(page_records), next_cursor, len(raw), receipt=receipt)

    def parse(
        self, raw: bytes, *, url: str, path: str | None, headers: Mapping[str, Any]
    ) -> dict[str, Any]:
        parsed = self._parse(raw, url=url, path=path, headers=headers)
        rules = dict(self.declared.get("rules") or {})
        if rules:
            # The competition's stated scoring rule and tie-breakers, cited to the regulation they come from.
            seasons = sorted(
                {
                    r["record_key"]
                    for r in parsed["records"]
                    if r["record_type"] == "season"
                }
            )
            for season_key in seasons:
                parsed["records"].append(
                    _observation(
                        "table_rule",
                        f"{season_key}|rule|scoring",
                        {
                            "season_key": season_key,
                            "kind": "scoring",
                            "points": rules.get("points"),
                            "tiebreakers": rules.get("tiebreakers"),
                            "valid_from": iso_datetime(rules.get("valid_from")),
                            "source_url": rules.get("source_url"),
                        },
                        source_record_id=f"{season_key}:rules",
                        locator=str(rules.get("source_url") or url),
                    )
                )
        return parsed

    def _parse(
        self, raw: bytes, *, url: str, path: str | None, headers: Mapping[str, Any]
    ) -> dict[str, Any]:
        format_id = self.declared["format"]
        if format_id == "football-data-v4-matches":
            return parse_football_data_matches(raw, declared=self.declared, url=url)
        if format_id == "football-data-v4-standings":
            return parse_football_data_standings(
                raw, declared=self.declared, url=url, headers=headers
            )
        if format_id == "football-data-v4-teams":
            return parse_football_data_teams(raw, declared=self.declared, url=url)
        if format_id == "statsbomb-open-data":
            return parse_statsbomb(
                raw, path=path or "", declared=self.declared, url=url
            )
        if format_id == "openfootball-json":
            return parse_openfootball(raw, declared=self.declared, url=url)
        if format_id == "sackmann-matches-csv":
            return parse_sackmann(raw, declared=self.declared, url=url)
        return parse_olympic(raw, url=url)


FIXTURE_SECRET = "fixture-credential"
ADAPTERS = {CONNECTOR: SportsResultsAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored publications keyed by URL path (+ query); responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del params, headers, timeout
        parts = urlsplit(url)
        key = parts.path + ("?" + parts.query if parts.query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = (
            body.encode()
            if isinstance(body, str)
            else b""
            if body is None
            else json.dumps(body).encode()
        )
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content,
            "origin": "fixture",
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = SportsResultsAdapter(
        source,
        transport=fixture_transport(list(fixture["native_pages"])),
        secret=FIXTURE_SECRET,
    )
    out, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {
                "operation": min(source["operations"]),
                "parameters": {},
                "limit": int(source["budgets"]["max_results"]),
            },
            cursor=cursor,
        )
        out += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return out
