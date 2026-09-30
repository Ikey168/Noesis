"""Aircraft and vessel movement records for the OSINT pack's optional ``movements`` feature (#2221).

The access, licence and volume decisions are ``docs/security/osint-movements-access.md``
(MV01); :data:`BOUNDS` enforces them. This module owns the record model and the
store (MV02) and, in the sections below, call derivation (MV08), reviewable
identity (MV09), citation links to sanctions listings (MV10) and the bounded
as-of answer (MV11).

``noesis-osint-movement-record-v1`` statements (schema under
``contracts/schemas/jsonschema/``) are kept as immutable revisions:

* **aircraft_identity** / **vessel_identity** - typed identifiers a source
  states together (an OpenSky flight's ICAO 24-bit address and call sign; a
  GFW port-visit event's vessel id and MMSI; an AIS message's MMSI and IMO);
* **registry_record** - one registry entry (N-number, G- mark, Mode S hex,
  status, type, registrant as published); every change is a dated revision and
  deregistration is a revision, never a deletion;
* **sample_window** - one bounded request window with its declared volume
  bound, licence decision and receiver coverage. Coverage is explicit: a window
  without positions is ``no_coverage_observed`` - absence of receiver coverage,
  never "did not move";
* **position_sample** - one position inside a window (a sample without a window
  is rejected);
* **call** - an airport or port call, ``source-published`` (GFW port visits,
  OpenSky estimated airports) with the source's confidence, or ``derived`` from
  samples against a geospatial facility geometry;
* **aggregate** - a published statistic (port calls per economy and year),
  never disaggregated into vessels.

Identifiers are typed (ICAO 24-bit address, registration mark with its
nationality prefix, IMO, MMSI, call sign, GFW vessel id). Names of pilots, crew
or owners who are natural persons are never movement keys: a registrant who is
a natural person is stored only as published and flagged, never indexed, and
answers withhold the name. Positions are validated through
:mod:`src.kb.geospatial` (WGS84) and facility geometry is read from it; there is
no spatial or entity store here.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

CONTRACT = "noesis-osint-movement-record-v1"
ANSWER_CONTRACT = "noesis-osint-movement-answer-v1"
LINK_CONTRACT = "noesis-osint-movement-link-v1"
ACCESS_DECISION = "docs/security/osint-movements-access.md"
SOURCE_PACK = "bounded-public-osint"
READ_SCOPE = "knowledge:read"
MOVEMENT_SCOPE = "knowledge:osint:movements"
FEATURE = "movements"
FEATURE_FLAG = "OSINT_MOVEMENTS"
RECORD_TYPES = ("aircraft_identity", "vessel_identity", "registry_record", "sample_window", "position_sample",
                "call", "aggregate")
PROVIDERS = ("faa-registry", "uk-caa-ginfo", "opensky", "gfw-port-visits", "kystdatahuset-ais",
             "unctad-port-calls")
SCHEMES = ("icao24", "registration", "serial_number", "imo", "mmsi", "call_sign", "gfw_vessel_id",
           "flight_callsign")
# Schemes a question or a match may be keyed on (a call sign or flight call sign is context, not identity).
KEY_SCHEMES = ("icao24", "registration", "imo", "mmsi", "gfw_vessel_id")
NO_COVERAGE = "no coverage observed"
COVERAGE_CAVEAT = ("positions exist only where a receiver in the source's network picked up the transponder; "
                   "an interval without positions is absence of coverage, never evidence that the aircraft or "
                   "vessel did not move or was not somewhere")
# MV01 volume bounds, per source (docs/security/osint-movements-access.md "Volume bounds").
BOUNDS: dict[str, dict[str, Any]] = {
    "faa-registry": {"max_identifiers_per_source": 50, "anchor": "#decisions"},
    "uk-caa-ginfo": {"max_identifiers_per_source": 50, "anchor": "#decisions"},
    "opensky": {"max_identifiers_per_query": 1, "max_window_hours": 48, "max_samples": 500, "max_flights": 5,
                "gap_seconds": 900, "max_selections": 10, "anchor": "#decisions"},
    "gfw-port-visits": {"max_identifiers_per_query": 1, "max_window_hours": 366 * 24, "max_samples": 0,
                        "max_events": 200, "max_selections": 10, "anchor": "#decisions"},
    "kystdatahuset-ais": {"max_identifiers_per_query": 1, "max_window_hours": 72, "max_samples": 500,
                          "gap_seconds": 1800, "max_selections": 10, "anchor": "#decisions"},
    "unctad-port-calls": {"max_economies": 10, "max_years": 5, "anchor": "#decisions"},
    "answer": {"max_window_days": 92},
    "monitor": {"max_identifiers": 25},
}
LICENCES = {
    "faa-registry": ("faa-public-record", "Source: FAA Aircraft Registry (registry.faa.gov)"),
    "uk-caa-ginfo": ("uk-caa-ginfo-terms", "Source: UK Civil Aviation Authority, G-INFO"),
    "opensky": ("opensky-research-licence", "Source: The OpenSky Network (https://opensky-network.org); Schäfer "
                                            "et al., Bringing Up OpenSky, IPSN 2014"),
    "gfw-port-visits": ("gfw-api-terms-cc-by-nc-4.0", "Source: Global Fishing Watch (https://globalfishingwatch.org/)"
                                                      ", port-visit events dataset as cited"),
    "kystdatahuset-ais": ("nlod-2.0", "Source: Kystverket / Norwegian Coastal Administration (kystdatahuset.no), "
                                      "NLOD 2.0"),
    "unctad-port-calls": ("unctadstat-terms", "Source: UNCTADstat, port call statistics"),
    "derived": ("derived-from-cited-samples", "Derived by Noesis from the cited samples and facility geometry"),
}
# Keys that must never appear in a statement or an answer: absence-of-movement, inference or verdict fields.
FORBIDDEN_KEYS = frozenset({
    "did_not_move", "no_movement", "not_moved", "not_present", "was_not_there", "absent", "stationary",
    "sanctions_evasion", "evasion", "dark_activity", "suspicious", "suspicion", "risk", "risk_score", "verdict",
    "compliance", "compliant", "is_compliant", "pattern_of_life", "predicted_route", "prediction", "behaviour",
    "behavior", "intent", "destination_prediction",
})
# Identifier kinds that key a person, never an aircraft or vessel.
PERSON_SCHEMES = frozenset({"name", "person", "owner", "owner_name", "registrant", "registrant_name", "pilot",
                            "crew", "captain", "email", "e-mail", "phone", "username", "handle"})
NEVER = (
    "report absence of positions as absence of movement or presence",
    "infer behaviour, predict routes or build a pattern of life",
    "derive a sanctions-evasion, compliance or risk verdict from movements",
    "key a movement on a natural person, or resolve a natural person",
    "track in real time, alert on positions or mirror ADS-B or AIS feeds",
)
_ICAO24 = re.compile(r"^[0-9a-f]{6}$")
_MMSI = re.compile(r"^\d{9}$")
_GFW = re.compile(r"^[0-9a-f-]{8,64}$")
_REGISTRATION = re.compile(r"^(N[0-9][0-9A-Z]{0,4}|[A-Z0-9]{1,2}-[A-Z0-9]{1,5})$")


class MovementError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def as_refusal(self) -> dict[str, Any]:
        return {"status": "refused", "code": self.code, "reason": str(self), **self.details}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decision(provider: str) -> str:
    return ACCESS_DECISION + BOUNDS.get(provider, {}).get("anchor", "#decisions")


def licence(provider: str) -> dict[str, str]:
    licence_id, attribution = LICENCES[provider]
    return {"id": licence_id, "decision": decision(provider if provider in BOUNDS else "answer"),
            "attribution": attribution}


def require(scopes: Iterable[str], *required: str) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    missing = [s for s in required if s not in scopes]
    if missing:
        raise MovementError("unauthorized", f"{missing[0]} scope is required")


# ------------------------------------------------------------------ time


def parse_time(value: Any) -> datetime:
    """An ISO date or UTC timestamp (or epoch seconds) as an aware UTC datetime."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MovementError("invalid_request", f"not an ISO date or timestamp: {value!r}") from exc
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def stamp(value: Any) -> str:
    return parse_time(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def day(value: Any) -> str:
    return parse_time(value).date().isoformat()


def hours_between(start: Any, end: Any) -> float:
    return (parse_time(end) - parse_time(start)).total_seconds() / 3600


# ------------------------------------------------------------------ identifiers


def imo_key(value: Any) -> str | None:
    from src.kb.fisheries_records import imo_key as fisheries_imo_key

    return fisheries_imo_key(value)


def identifier_key(scheme: str, value: Any) -> str | None:
    """The comparison key of one typed identifier, or None when it is malformed."""
    text = str(value or "").strip()
    if scheme == "icao24":
        text = text.lower().removeprefix("0x")
        return text if _ICAO24.fullmatch(text) else None
    if scheme == "registration":
        text = re.sub(r"\s+", "", text.upper())
        return text if _REGISTRATION.fullmatch(text) else None
    if scheme == "imo":
        return imo_key(text)
    if scheme == "mmsi":
        return text if _MMSI.fullmatch(text) else None
    if scheme == "gfw_vessel_id":
        text = text.lower()
        return text if _GFW.fullmatch(text) else None
    if scheme in {"call_sign", "flight_callsign", "serial_number"}:
        return re.sub(r"[^A-Z0-9]", "", text.upper()) or None
    return None


def nationality_prefix(mark: str) -> str:
    """The nationality prefix of a registration mark (``N`` for the US register, else the part before the dash)."""
    mark = str(mark).upper()
    return "N" if mark.startswith("N") and "-" not in mark else mark.split("-", 1)[0]


def subject_key(scheme: str, value: Any) -> str:
    key = identifier_key(scheme, value)
    if key is None or scheme not in KEY_SCHEMES:
        raise MovementError("invalid_identifier", f"{value!r} is not a well-formed {scheme}")
    kind = "aircraft" if scheme in {"icao24", "registration"} else "vessel"
    prefix = {"gfw_vessel_id": "gfw"}.get(scheme, scheme)
    return f"{kind}:{prefix}:{key}"


def parse_subject(key: str) -> tuple[str, str, str]:
    """(kind, scheme, value) of a subject key."""
    kind, prefix, value = key.split(":", 2)
    return kind, {"gfw": "gfw_vessel_id"}.get(prefix, prefix), value


def classify(identifier: Any, scheme: str | None = None) -> tuple[str, str]:
    """Resolve a question's identifier to (scheme, key); person-keyed identifiers are refused.

    A scheme may be given explicitly; otherwise ``IMO 9000027`` and 7 digits read as IMO, 9 digits as MMSI,
    ``N123AB`` or ``G-ABCD`` as a registration, 6 hex characters with a letter as an ICAO 24-bit address and a
    UUID as a GFW vessel id.
    """
    text = " ".join(str(identifier or "").split())
    folded = text.casefold()
    if scheme is not None and str(scheme).casefold() in PERSON_SCHEMES:
        raise MovementError("person_identifier_refused", "movements are keyed on aircraft and vessel identifiers, "
                                                         "never on a person")
    if "@" in text or folded.startswith(("person:", "mailto:")) or re.fullmatch(r"\+?[\d\s()-]{10,}", text):
        raise MovementError("person_identifier_refused", "e-mail, handle, phone and person ids are not movement keys")
    if scheme:
        if scheme not in KEY_SCHEMES:
            raise MovementError("invalid_identifier", f"scheme must be one of {list(KEY_SCHEMES)}")
        key = identifier_key(scheme, text)
        if key is None:
            raise MovementError("invalid_identifier", f"{text!r} is not a well-formed {scheme}")
        return scheme, key
    compact = text.replace(" ", "")
    for candidate, test in (("imo", lambda: re.fullmatch(r"(?i)(IMO)?\d{7}", compact)),
                            ("mmsi", lambda: _MMSI.fullmatch(compact)),
                            ("gfw_vessel_id", lambda: re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{4,55}", compact)),
                            ("icao24", lambda: re.fullmatch(r"(?i)[0-9a-f]{6}", compact)
                             and re.search(r"(?i)[a-f]", compact)),
                            ("registration", lambda: _REGISTRATION.fullmatch(compact.upper()))):
        if test():
            key = identifier_key(candidate, compact)
            if key is None:
                raise MovementError("invalid_identifier", f"{text!r} is not a well-formed {candidate}")
            return candidate, key
    if " " in text and len(re.findall(r"[A-Za-z]+", text)) >= 2:
        raise MovementError("person_identifier_refused", "a name is not a movement key; give an ICAO 24-bit "
                                                         "address, registration, IMO, MMSI or GFW vessel id")
    raise MovementError("invalid_identifier", "give an ICAO 24-bit address, registration mark, IMO number, MMSI "
                                               "or GFW vessel id")


# ------------------------------------------------------------------ validation


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema/noesis-osint-movement-record-v1.json"
    return json.loads(path.read_text())


def forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.add(str(key))
            found |= forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            found |= forbidden_keys(item)
    return found


def window_id(provider: str, subject: str, start: Any, end: Any, query: Mapping[str, Any]) -> str:
    return "mv-window:" + digest([provider, subject, stamp(start), stamp(end), dict(query)])[:24]


def validate_statement(statement: Mapping[str, Any]) -> dict[str, Any]:
    """Schema, window bounds, coverage semantics and the no-inference rule for one statement."""
    import jsonschema

    value = json.loads(canonical(statement))
    if value.get("contract") != CONTRACT:
        raise MovementError("invalid_record", f"statement is not a {CONTRACT} statement")
    bad = forbidden_keys(value)
    if bad:
        raise MovementError("invalid_record", f"statement carries an inference or absence verdict: {sorted(bad)}")
    try:
        jsonschema.validate(value, _schema())
    except jsonschema.ValidationError as exc:
        raise MovementError("invalid_record", f"schema: {exc.message}") from exc
    for identifier in value["identifiers"]:
        if identifier_key(identifier["scheme"], identifier["value"]) is None:
            raise MovementError("invalid_record", f"malformed {identifier['scheme']} {identifier['value']!r}")
    record_type, provider = value["record_type"], value["provider"]
    window = value.get("window")
    if window:
        if parse_time(window["end"]) < parse_time(window["start"]):
            raise MovementError("invalid_record", "a window ends before it starts")
    if record_type == "sample_window":
        bound, coverage = value["bound"], value["coverage"]
        limit = BOUNDS.get(provider, {}).get("max_window_hours")
        span = hours_between(window["start"], window["end"])
        if span > bound["max_window_hours"] or (limit is not None and span > limit):
            raise MovementError("over_bound", f"a {provider} window spans {span:.1f} h, beyond its "
                                              f"{min(bound['max_window_hours'], limit or span)} h bound")
        if coverage["samples_stored"] > bound["max_samples"]:
            raise MovementError("over_bound", "a window stores more samples than its declared bound")
        if coverage["samples_stored"] > coverage["samples_published"]:
            raise MovementError("invalid_record", "a window cannot store more samples than were published")
        position_status = "positions_observed" if coverage["samples_stored"] else "no_coverage_observed"
        if coverage["status"] in {"positions_observed", "no_coverage_observed"} and coverage["status"] != \
                position_status:
            raise MovementError("invalid_record", "coverage status must say 'no_coverage_observed' exactly when "
                                                  "the window holds no position")
        if not bound["licence_decision"].startswith(ACCESS_DECISION):
            raise MovementError("invalid_record", "a window cites its MV01 licence decision")
    if record_type == "position_sample":
        published = value["as_published"]
        from src.kb.geospatial import _point

        try:
            _point([published.get("lon"), published.get("lat")])
        except Exception as exc:
            raise MovementError("invalid_record", "a position sample is a WGS84 longitude and latitude") from exc
        at = parse_time(published.get("timestamp"))
        if not parse_time(window["start"]) <= at <= parse_time(window["end"]):
            raise MovementError("invalid_record", "a position sample lies outside its window")
    if record_type == "call":
        status = value["as_published"].get("status")
        if status not in {"source-published", "derived"}:
            raise MovementError("invalid_record", "a call is 'source-published' or 'derived'")
        if (status == "derived") != (provider == "derived"):
            raise MovementError("invalid_record", "derived calls come from the derivation, published calls from a "
                                                  "source")
    if record_type == "registry_record":
        registrant = value["as_published"].get("registrant") or {}
        if registrant.get("natural_person") and any(i["scheme"] not in {"registration", "icao24", "serial_number"}
                                                    for i in value["identifiers"]):
            raise MovementError("invalid_record", "a natural-person registrant is never an identifier")
    return value


def statement(record_type: str, provider: str, subject: str, record_key: str, as_published: Mapping[str, Any], *,
              identifiers: Sequence[Mapping[str, Any]] = (), source: Mapping[str, Any], event: str = "published",
              effective_from: Any = None, effective_to: Any = None, date_basis: str | None = None,
              window: Mapping[str, Any] | None = None, bound: Mapping[str, Any] | None = None,
              coverage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build (and validate) one statement."""
    kind = subject.split(":", 1)[0]
    value: dict[str, Any] = {
        "contract": CONTRACT, "record_type": record_type, "provider": provider,
        "subject": {"key": subject, "kind": kind}, "record_key": record_key,
        "identifiers": [{k: v for k, v in dict(i).items() if v is not None} for i in identifiers],
        "as_published": dict(as_published),
        "effective": {"from": effective_from, "to": effective_to, "event": event, "date_basis": date_basis},
        "licence": licence(provider), "source": {k: v for k, v in dict(source).items() if v is not None or k == "url"},
    }
    if window is not None:
        value["window"] = dict(window)
    if bound is not None:
        value["bound"] = dict(bound)
    if coverage is not None:
        value["coverage"] = dict(coverage)
    return validate_statement(value)


def window_bound(provider: str) -> dict[str, Any]:
    bounds = BOUNDS[provider]
    out = {"max_window_hours": bounds["max_window_hours"], "max_samples": bounds.get("max_samples", 0),
           "licence_decision": decision(provider)}
    if bounds.get("gap_seconds"):
        out["gap_seconds"] = bounds["gap_seconds"]
    return out


def coverage_gaps(timestamps: Sequence[Any], start: Any, end: Any, gap_seconds: int) -> list[dict[str, Any]]:
    """Intervals of a window longer than the gap threshold with no position (declared gaps, never 'no movement')."""
    points = [parse_time(start), *sorted(parse_time(t) for t in timestamps), parse_time(end)]
    gaps = []
    for index, (left, right) in enumerate(zip(points, points[1:])):
        seconds = int((right - left).total_seconds())
        if seconds > gap_seconds or (len(points) == 2 and seconds > 0):
            edge = index == 0 or index == len(points) - 2
            gaps.append({"from": stamp(left), "to": stamp(right), "seconds": seconds,
                         "kind": "window edge without positions" if edge and len(points) > 2
                         else "no positions received"})
    return gaps


def thin(items: Sequence[Any], limit: int) -> tuple[list[Any], bool]:
    """At most ``limit`` evenly spaced items (first and last kept); the remainder is never stored."""
    items = list(items)
    if len(items) <= limit:
        return items, False
    if limit <= 1:
        return items[:limit], True
    step = (len(items) - 1) / (limit - 1)
    return [items[round(i * step)] for i in range(limit)], True


# ------------------------------------------------------------------ store

_DDL = """
CREATE SEQUENCE IF NOT EXISTS osint_movement_seq;
CREATE TABLE IF NOT EXISTS osint_movement_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, record_type TEXT NOT NULL, provider TEXT NOT NULL,
  record_key TEXT NOT NULL, subject_key TEXT NOT NULL, subject_kind TEXT NOT NULL, window_id TEXT,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS osint_movement_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, seq BIGINT NOT NULL,
  revision_no INTEGER NOT NULL, content_sha TEXT NOT NULL, statement_json TEXT NOT NULL, effective_from TEXT,
  effective_to TEXT, event TEXT NOT NULL, supersedes TEXT, observed_at_ms BIGINT NOT NULL, run_id TEXT,
  source_id TEXT, document_id TEXT, evidence_origin TEXT, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS osint_movement_identifiers (
  namespace TEXT NOT NULL, identifier_id TEXT NOT NULL, subject_key TEXT NOT NULL, record_id TEXT NOT NULL,
  revision_id TEXT NOT NULL, provider TEXT NOT NULL, scheme TEXT NOT NULL, value_key TEXT NOT NULL,
  valid_from TEXT, valid_to TEXT, PRIMARY KEY(namespace, identifier_id)
);
CREATE TABLE IF NOT EXISTS osint_movement_privacy_refusals (
  namespace TEXT NOT NULL, scheme TEXT NOT NULL, value_key TEXT NOT NULL, programme TEXT NOT NULL,
  reason TEXT NOT NULL, source_id TEXT, recorded_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, scheme, value_key)
);
CREATE TABLE IF NOT EXISTS osint_movement_source_runs (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT, status TEXT NOT NULL,
  outcomes_json TEXT NOT NULL, cutoff_seq BIGINT NOT NULL, evidence_origin TEXT, finished_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id)
)
"""
TABLES = ("osint_movement_records", "osint_movement_revisions", "osint_movement_identifiers",
          "osint_movement_privacy_refusals", "osint_movement_source_runs")
_REQUEST_DDL = """
CREATE TABLE IF NOT EXISTS osint_movement_requests (
  request_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, principal_id TEXT NOT NULL, tool TEXT NOT NULL,
  identifier TEXT NOT NULL, window_json TEXT NOT NULL, purpose TEXT NOT NULL, scopes_json TEXT NOT NULL,
  outcome TEXT NOT NULL, requested_at_ms BIGINT NOT NULL
)
"""
_REVISION_COLUMNS = ("revision_id, record_id, seq, revision_no, content_sha, statement_json, effective_from, "
                     "effective_to, event, supersedes, observed_at_ms, run_id, source_id, document_id, evidence_origin")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def _content(value: Mapping[str, Any]) -> dict[str, Any]:
    """What makes a revision distinct: the published content, not when it was fetched."""
    return {k: value.get(k) for k in ("subject", "identifiers", "as_published", "effective", "window", "bound",
                                      "coverage")} | {"release": (value.get("source") or {}).get("release")}


class MovementStore:
    """Immutable, revisioned movement records keyed per namespace."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def ready(self) -> bool:
        return table_exists(self.conn, "osint_movement_revisions")

    def require_ready(self) -> None:
        if not self.ready():
            raise MovementError("not_ready", "no movement record has been acquired yet")

    # -------------------------------------------------------------- writes

    def _latest(self, namespace: str, record_id: str) -> tuple[str, int, str] | None:
        row = self.conn.execute(
            "SELECT content_sha, revision_no, revision_id FROM osint_movement_revisions WHERE namespace=? AND "
            "record_id=? ORDER BY revision_no DESC LIMIT 1", [namespace, record_id]).fetchone()
        return (row[0], int(row[1]), row[2]) if row else None

    def _apply(self, namespace: str, statement: Mapping[str, Any], *, run_id: str | None, source_id: str | None,
               document_id: str | None, observed_at_ms: int) -> dict[str, Any]:
        value = validate_statement(statement)
        subject = value["subject"]
        window = (value.get("window") or {}).get("window_id")
        record_id = "mv-record:" + digest([namespace, value["provider"], value["record_type"], subject["key"],
                                           value["record_key"]])[:24]
        self.conn.execute("INSERT OR IGNORE INTO osint_movement_records VALUES (?,?,?,?,?,?,?,?,?)",
                          [namespace, record_id, value["record_type"], value["provider"], value["record_key"],
                           subject["key"], subject["kind"], window, observed_at_ms])
        content_sha = digest(_content(value))
        latest = self._latest(namespace, record_id)
        if latest and latest[0] == content_sha:
            return {"record_id": record_id, "revision_id": latest[2], "status": "unchanged"}
        number = (latest[1] if latest else 0) + 1
        revision_id = "mv-revision:" + digest([record_id, content_sha, number])[:24]
        seq = self.conn.execute("SELECT nextval('osint_movement_seq')").fetchone()[0]
        effective, source = value["effective"], value["source"]
        self.conn.execute(
            "INSERT INTO osint_movement_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, record_id, seq, number, content_sha, canonical(value), effective.get("from"),
             effective.get("to"), effective["event"], latest[2] if latest else None, observed_at_ms, run_id,
             source_id, document_id, source.get("evidence_origin")])
        for identifier in value["identifiers"]:
            key = identifier_key(identifier["scheme"], identifier["value"])
            self.conn.execute(
                "INSERT OR IGNORE INTO osint_movement_identifiers VALUES (?,?,?,?,?,?,?,?,?,?)",
                [namespace, "mv-identifier:" + digest([revision_id, identifier])[:24], subject["key"], record_id,
                 revision_id, value["provider"], identifier["scheme"], key,
                 identifier.get("valid_from") or effective.get("from"), identifier.get("valid_to") or
                 effective.get("to")])
        return {"record_id": record_id, "revision_id": revision_id, "seq": seq, "revision_no": number,
                "status": "created" if number == 1 else "revised"}

    def observe(self, namespace: str, statements: Sequence[Mapping[str, Any]], *, run_id: str | None = None,
                source_id: str | None = None, document_ids: Sequence[str | None] | None = None,
                observed_at_ms: int | None = None) -> dict[str, Any]:
        """Keep a batch of statements in one transaction; replays add nothing."""
        observed = observed_at_ms if observed_at_ms is not None else self.now()
        counts = {"created": 0, "revised": 0, "unchanged": 0}
        results = []
        self.conn.execute("BEGIN")
        try:
            ids = list(document_ids or [])
            for index, item in enumerate(statements):
                result = self._apply(namespace, item, run_id=run_id, source_id=source_id,
                                     document_id=ids[index] if index < len(ids) else None, observed_at_ms=observed)
                counts[result["status"]] += 1
                results.append(result)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"counts": counts, "results": results}

    def apply(self, namespace: str, statement: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.observe(namespace, [statement], **kwargs)["results"][0]

    def record_refusal(self, namespace: str, scheme: str, value: Any, *, programme: str, reason: str,
                       source_id: str | None = None) -> dict[str, Any]:
        """Add an opted-out identifier (LADD, PIA, a withheld registry entry or an operator declaration)."""
        key = identifier_key(scheme, value)
        if key is None:
            raise MovementError("invalid_identifier", f"{value!r} is not a well-formed {scheme}")
        self.conn.execute("INSERT OR IGNORE INTO osint_movement_privacy_refusals VALUES (?,?,?,?,?,?,?)",
                          [namespace, scheme, key, programme, reason, source_id, self.now()])
        return {"scheme": scheme, "value_key": key, "programme": programme}

    def record_run(self, namespace: str, run_id: str, source_id: str, *, provider: str | None, status: str,
                   outcomes: Sequence[Mapping[str, Any]], evidence_origin: str | None) -> dict[str, Any]:
        cutoff = self.conn.execute("SELECT coalesce(max(seq), 0) FROM osint_movement_revisions WHERE namespace=?",
                                   [namespace]).fetchone()[0]
        self.conn.execute("INSERT OR REPLACE INTO osint_movement_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
                          [namespace, run_id, source_id, provider, status, canonical(list(outcomes)), cutoff,
                           evidence_origin, self.now()])
        return {"run_id": run_id, "source_id": source_id, "status": status, "cutoff_seq": int(cutoff)}

    # -------------------------------------------------------------- reads

    @staticmethod
    def _revision(row: Sequence[Any]) -> dict[str, Any]:
        return {"revision_id": row[0], "record_id": row[1], "seq": int(row[2]), "revision_no": int(row[3]),
                "content_sha": row[4], "statement": json.loads(row[5]), "effective_from": row[6],
                "effective_to": row[7], "event": row[8], "supersedes": row[9], "observed_at_ms": int(row[10]),
                "run_id": row[11], "source_id": row[12], "document_id": row[13], "evidence_origin": row[14]}

    def revisions(self, namespace: str, record_id: str, *, cutoff_seq: int | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {_REVISION_COLUMNS} FROM osint_movement_revisions WHERE namespace=? AND record_id=? "
            "AND (? IS NULL OR seq<=?) ORDER BY seq", [namespace, record_id, cutoff_seq, cutoff_seq]).fetchall()
        return [self._revision(r) for r in rows]

    def latest(self, namespace: str, record_id: str) -> dict[str, Any] | None:
        revisions = self.revisions(namespace, record_id)
        return revisions[-1] if revisions else None

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {_REVISION_COLUMNS} FROM osint_movement_revisions WHERE namespace=? AND "
                                "revision_id=?", [namespace, revision_id]).fetchone() if self.ready() else None
        if row is None:
            raise MovementError("not_found", "movement revision is not visible in this namespace")
        return self._revision(row)

    def records(self, namespace: str, *, subject_keys: Iterable[str] | None = None, record_type: str | None = None,
                provider: str | None = None, window: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id, record_type, provider, record_key, subject_key, subject_kind, window_id FROM "
            "osint_movement_records WHERE namespace=? AND (? IS NULL OR record_type=?) AND (? IS NULL OR provider=?) "
            "AND (? IS NULL OR window_id=?) ORDER BY subject_key, record_type, record_key, provider",
            [namespace, record_type, record_type, provider, provider, window, window]).fetchall()
        wanted = None if subject_keys is None else set(subject_keys)
        return [dict(zip(("record_id", "record_type", "provider", "record_key", "subject_key", "subject_kind",
                          "window_id"), r)) for r in rows if wanted is None or r[4] in wanted]

    def subjects_for(self, namespace: str, scheme: str, key: str) -> list[str]:
        """Subjects whose own records state this identifier (direct hits, no identity decision needed)."""
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT DISTINCT subject_key FROM osint_movement_identifiers WHERE namespace=? AND scheme=? AND "
            "value_key=? ORDER BY subject_key", [namespace, scheme, key]).fetchall()
        direct = {r[0] for r in rows}
        with_key = None
        try:
            with_key = subject_key(scheme, key)
        except MovementError:
            pass
        if with_key and self.records(namespace, subject_keys=[with_key]):
            direct.add(with_key)
        return sorted(direct)

    def identifiers(self, namespace: str, subject_key_: str | None = None, *,
                    cutoff_seq: int | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT i.subject_key, i.record_id, i.revision_id, i.provider, i.scheme, i.value_key, i.valid_from, "
            "i.valid_to, r.seq FROM osint_movement_identifiers i JOIN osint_movement_revisions r ON "
            "r.namespace=i.namespace AND r.revision_id=i.revision_id WHERE i.namespace=? AND (? IS NULL OR "
            "i.subject_key=?) AND (? IS NULL OR r.seq<=?) ORDER BY r.seq, i.scheme, i.value_key",
            [namespace, subject_key_, subject_key_, cutoff_seq, cutoff_seq]).fetchall()
        return [dict(zip(("subject_key", "record_id", "revision_id", "provider", "scheme", "value_key", "valid_from",
                          "valid_to", "seq"), r)) for r in rows]

    def refusal(self, namespace: str, scheme: str, key: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "osint_movement_privacy_refusals"):
            return None
        row = self.conn.execute(
            "SELECT programme, reason, source_id FROM osint_movement_privacy_refusals WHERE namespace=? AND scheme=? "
            "AND value_key=?", [namespace, scheme, key]).fetchone()
        return {"scheme": scheme, "value_key": key, "programme": row[0], "reason": row[1], "source_id": row[2]} \
            if row else None

    def runs(self, namespace: str, run_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "osint_movement_source_runs"):
            return []
        rows = self.conn.execute(
            "SELECT run_id, source_id, provider, status, outcomes_json, cutoff_seq, evidence_origin, finished_at_ms "
            "FROM osint_movement_source_runs WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY finished_at_ms, "
            "source_id", [namespace, run_id, run_id]).fetchall()
        return [{"run_id": r[0], "source_id": r[1], "provider": r[2], "status": r[3], "outcomes": json.loads(r[4]),
                 "cutoff_seq": int(r[5]), "evidence_origin": r[6], "finished_at_ms": int(r[7])} for r in rows]

    def generation(self, namespace: str) -> int:
        if not self.ready():
            return 0
        return int(self.conn.execute("SELECT coalesce(max(seq), 0) FROM osint_movement_revisions WHERE namespace=?",
                                     [namespace]).fetchone()[0])

    def registry_state(self, namespace: str, subject: str) -> dict[str, Any] | None:
        """The latest registry revision of a subject's own registry records (for projection-time privacy checks)."""
        candidates = []
        for record in self.records(namespace, record_type="registry_record"):
            revision = self.latest(namespace, record["record_id"])
            ids = {subject_key(i["scheme"], i["value"]) for i in revision["statement"]["identifiers"]
                   if i["scheme"] in KEY_SCHEMES and identifier_key(i["scheme"], i["value"])}
            if record["subject_key"] == subject or subject in ids:
                candidates.append(revision)
        return max(candidates, key=lambda r: r["seq"]) if candidates else None


def natural_person_registrant(revision: Mapping[str, Any] | None) -> bool:
    if revision is None or revision["event"] == "deregistered":
        return False
    return bool(((revision["statement"]["as_published"].get("registrant") or {}).get("natural_person")))


# ------------------------------------------------------------------ runtime projector


class MovementProjector:
    """Runtime projector for ``noesis-osint-movement-record-v1`` pages, applying the MV01 privacy refusals."""

    def __init__(self, conn: Any) -> None:
        self.store = MovementStore(conn)
        self._outcomes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._origin: dict[tuple[str, str], str] = {}

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("osint_movements") or {}).get("namespace") or "osint")

    def _refused(self, namespace: str, statement: Mapping[str, Any]) -> str | None:
        subject = statement["subject"]["key"]
        keys = {(i["scheme"], identifier_key(i["scheme"], i["value"])) for i in statement.get("identifiers") or []}
        kind, scheme, value = parse_subject(subject)
        if kind != "area":
            keys.add((scheme, value))
        if any(self.store.refusal(namespace, s, k) for s, k in keys if k):
            return "privacy_opt_out"
        if statement["record_type"] != "registry_record" and kind == "aircraft":
            if natural_person_registrant(self.store.registry_state(namespace, subject)):
                return "private_aircraft_refused"
        return None

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        receipt = dict(page_receipt or {})
        for refusal in receipt.get("privacy_refusals") or []:
            self.store.record_refusal(namespace, refusal["scheme"], refusal["identifier"], programme=refusal["programme"],
                                      reason=refusal["reason"], source_id=source["source_id"])
        by_id = {str(dict(d.get("metadata") or {}).get("source_pack_record_id")): d["document_id"] for d in documents}
        kept, ids, refused = [], [], {}
        for record in records:
            item = record.get("movement_record")
            if not item:
                continue
            if item.get("contract") != CONTRACT:
                raise MovementError("invalid_record", "page record lacks a movement statement")
            reason = self._refused(namespace, item)
            if reason:
                refused[reason] = refused.get(reason, 0) + 1
                continue
            kept.append(item)
            ids.append(by_id.get(str(record.get("id"))))
        result = self.store.observe(namespace, kept, run_id=run_id, source_id=source["source_id"], document_ids=ids)
        key = (run_id, source["source_id"])
        if receipt.get("selection") is not None:
            self._outcomes.setdefault(key, []).append(
                {"selection": receipt["selection"], "outcome": receipt.get("outcome"), "statements": len(kept),
                 "refused": refused, "excluded_fields_dropped": receipt.get("excluded_fields_dropped", []),
                 "thinned": receipt.get("thinned", False)})
        if receipt.get("evidence_origin"):
            self._origin[key] = receipt["evidence_origin"]
        return result["counts"]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        key = (run_id, source["source_id"])
        return self.store.record_run(self._namespace(source), run_id, source["source_id"],
                                     provider=dict(source.get("osint_movements") or {}).get("provider"),
                                     status=status, outcomes=self._outcomes.pop(key, []),
                                     evidence_origin=self._origin.pop(key, None))


# ------------------------------------------------------------------ request log


def log_request(conn: Any, *, namespace: str, principal_id: str, tool: str, identifier: str,
                window: Mapping[str, Any], purpose: str, scopes: Iterable[str], outcome: str,
                now: int | None = None) -> str:
    """Append one movement request with its stated purpose to the request log (a separate, writable store)."""
    conn.execute(_REQUEST_DDL)
    at = now if now is not None else int(time.time() * 1000)
    request_id = "mv-request:" + digest([namespace, principal_id, tool, identifier, dict(window), purpose, at])[:24]
    conn.execute("INSERT OR IGNORE INTO osint_movement_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                 [request_id, namespace, principal_id, tool, identifier, canonical(dict(window)), purpose,
                  canonical(sorted(set(scopes))), outcome, at])
    return request_id


def request_log(conn: Any, namespace: str | None = None) -> list[dict[str, Any]]:
    if not table_exists(conn, "osint_movement_requests"):
        return []
    rows = conn.execute("SELECT request_id, namespace, principal_id, tool, identifier, window_json, purpose, "
                        "outcome, requested_at_ms FROM osint_movement_requests WHERE (? IS NULL OR namespace=?) "
                        "ORDER BY requested_at_ms, request_id", [namespace, namespace]).fetchall()
    return [{"request_id": r[0], "namespace": r[1], "principal_id": r[2], "tool": r[3], "identifier": r[4],
             "window": json.loads(r[5]), "purpose": r[6], "outcome": r[7], "requested_at_ms": int(r[8])}
            for r in rows]


def check_window(start: Any, end: Any, *, max_days: int | None = None) -> tuple[datetime, datetime]:
    """A question's window: ordered, bounded by the MV01 answer bound; over-bound windows are refused."""
    begin, finish = parse_time(start), parse_time(end)
    if finish < begin:
        raise MovementError("invalid_request", "the window ends before it starts")
    limit = max_days if max_days is not None else BOUNDS["answer"]["max_window_days"]
    if finish - begin > timedelta(days=limit):
        raise MovementError("over_bound", f"a movement window is at most {limit} days "
                                          f"({ACCESS_DECISION}#volume-bounds)", max_window_days=limit)
    return begin, finish


# ================================================================== MV08: derived calls


DWELL_SECONDS = {"airport": 300, "port": 7200}
DERIVATION = {"name": "dwell-in-facility-geometry", "version": "1.0.0",
              "note": "consecutive samples inside a facility geometry from src.kb.geospatial for at least the dwell "
                      "threshold; no behaviour inference, route prediction or pattern-of-life analysis"}
FACILITY_TYPES = {"aircraft": "airport", "vessel": "port"}


def _facilities(conn: Any, namespace: str, kind: str) -> list[dict[str, Any]]:
    """Facility places of one type (airport or port) with their polygon geometries, from the geospatial store."""
    from src.kb.geospatial import READ_SCOPE as GEO_READ
    from src.kb.geospatial import GeospatialStore

    if not namespace or not table_exists(conn, "geospatial_places"):
        return []
    rows = conn.execute(
        "SELECT p.place_id, p.place_key, r.canonical_name, r.source_ids_json FROM geospatial_places p JOIN "
        "geospatial_place_current c USING(place_id) JOIN geospatial_place_revisions r ON r.revision_id=c.revision_id "
        "WHERE p.namespace=? AND r.place_type=? ORDER BY p.place_id", [namespace, kind]).fetchall()
    store = GeospatialStore(conn, initialize=False)
    out = []
    for place_id, place_key, name, source_ids in rows:
        for geometry in store.geometries(namespace, place_id, scopes={GEO_READ}):
            shape = geometry["geometry"]
            if shape["type"] not in {"Polygon", "MultiPolygon"}:
                continue
            polygons = [shape["coordinates"]] if shape["type"] == "Polygon" else shape["coordinates"]
            points = [p for polygon in polygons for ring in polygon for p in ring]
            out.append({"place_id": place_id, "place_key": place_key, "name": name, "kind": kind,
                        "code": (json.loads(source_ids) or {}).get("code"), "geometry_id": geometry["geometry_id"],
                        "bbox": (min(p[0] for p in points), min(p[1] for p in points),
                                 max(p[0] for p in points), max(p[1] for p in points))})
            break
    return out


def resolve_facility(conn: Any, namespace: str | None, kind: str, code: Any) -> dict[str, Any]:
    """A published facility code resolved to a geospatial place by its key; unresolved facilities stay unresolved."""
    if not namespace or not code:
        return {"resolution": "unresolved"}
    for facility in _facilities(conn, namespace, kind):
        if facility["place_key"] == f"{kind}:{code}" or facility["code"] == code:
            return {"resolution": "resolved", "place_id": facility["place_id"], "name": facility["name"],
                    "geometry_id": facility["geometry_id"], "namespace": namespace}
    return {"resolution": "unresolved"}


def _runs(inside: Sequence[tuple[bool, str, str, Any]]) -> list[list[tuple[bool, str, str, Any]]]:
    runs, current = [], []
    for item in inside:
        if item[0]:
            current.append(item)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def derive_calls(conn: Any, namespace: str, *, facilities_namespace: str, principal_id: str, scopes: Iterable[str],
                 window_ids: Iterable[str] | None = None, now=None) -> dict[str, Any]:
    """Derive airport and port calls from stored sample windows against geospatial facility geometry.

    Every derived call carries ``derived`` status, the method and thresholds, the samples and the spatial receipts it
    rests on, and is ``uncertain`` when a coverage gap lies inside it or touches it (the gap could hide or fake a
    call). A stay shorter than the threshold that touches a gap is recorded as an uncertain call; otherwise nothing is
    asserted, and the absence of a call is never asserted.
    """
    from src.kb.geospatial import CALCULATE_SCOPE, GeospatialStore

    scopes = set(scopes)
    require(scopes, READ_SCOPE, CALCULATE_SCOPE)
    store = MovementStore(conn, now=now)
    geo = GeospatialStore(conn, initialize=False, **({"now": now} if now else {}))
    wanted = None if window_ids is None else set(window_ids)
    derived, notes = [], []
    for record in store.records(namespace, record_type="sample_window"):
        if wanted is not None and record["window_id"] not in wanted:
            continue
        window = store.latest(namespace, record["record_id"])["statement"]
        kind = window["subject"]["kind"]
        if window["coverage"]["status"] != "positions_observed":
            notes.append({"window_id": record["window_id"],
                          "note": f"{window['coverage']['status']}: no call is asserted or denied for this window"})
            continue
        samples = sorted(((store.latest(namespace, r["record_id"]), r) for r in
                          store.records(namespace, record_type="position_sample", window=record["window_id"])),
                         key=lambda pair: pair[0]["statement"]["as_published"]["timestamp"])
        gaps = window["coverage"]["gaps"]
        for facility in _facilities(conn, facilities_namespace, FACILITY_TYPES[kind]):
            west, south, east, north = facility["bbox"]
            inside = []
            for revision, sample_record in samples:
                published = revision["statement"]["as_published"]
                point = [published["lon"], published["lat"]]
                receipt = None
                if west <= point[0] <= east and south <= point[1] <= north:
                    receipt = geo.relation(facilities_namespace, "contains", facility["geometry_id"], point,
                                           scopes=scopes, principal_id=principal_id)
                inside.append((bool(receipt and receipt["result"]["contains"]), published["timestamp"],
                               sample_record["record_id"], receipt))
            threshold = DWELL_SECONDS[facility["kind"]]
            for run in _runs(inside):
                arrival, departure = run[0][1], run[-1][1]
                dwell = int((parse_time(departure) - parse_time(arrival)).total_seconds())
                uncertainty = [{"gap": g, "relation": "inside the call" if arrival < g["from"] < departure
                                else "adjacent to the call"} for g in gaps
                               if g["to"] == arrival or g["from"] == departure or arrival < g["from"] < departure]
                if dwell < threshold and not uncertainty:
                    continue  # a pass below the dwell threshold is no call; nothing is asserted either way
                if dwell < threshold:
                    uncertainty.append({"reason": "observed dwell below the threshold; the adjacent gap could hide "
                                                  "the rest of the stay"})
                published = {
                    "status": "derived", "event": "call", "arrival": arrival, "departure": departure,
                    "dwell_seconds": dwell,
                    "facility": {"kind": facility["kind"], "place_id": facility["place_id"], "name": facility["name"],
                                 "code": facility["code"], "geometry_id": facility["geometry_id"],
                                 "namespace": facilities_namespace, "resolution": "resolved"},
                    "method": {**DERIVATION, "dwell_threshold_seconds": threshold,
                               "gap_threshold_seconds": window["bound"].get("gap_seconds")},
                    "sample_record_ids": [item[2] for item in run],
                    "spatial_receipts": [item[3]["receipt_id"] for item in run if item[3]],
                    "source_window": {"window_id": record["window_id"], "provider": record["provider"]},
                    "uncertain": bool(uncertainty), "uncertainty": uncertainty,
                    "caveat": "derived from the cited samples and facility geometry; a gap in coverage can hide a "
                              "call or make a pass look like a stay"}
                derived.append(statement(
                    "call", "derived", window["subject"]["key"],
                    f"derived:{record['window_id']}:{facility['place_id']}:{arrival}", published,
                    identifiers=window["identifiers"],
                    source={"url": None, "locator": f"derived:{record['window_id']}", "evidence_origin": "derived"},
                    event="derived", effective_from=arrival, effective_to=departure,
                    date_basis="first and last sample inside the facility", window=window["window"]))
    result = store.observe(namespace, derived, run_id=None, source_id="derived-calls")
    return {"derived": len(derived), "counts": result["counts"],
            "records": [r["record_id"] for r in result["results"]], "notes": notes, "method": DERIVATION}


# ================================================================== MV09: reviewable identity


IDENTITY_PREFIX = "movements:"
ORG_PREFIX = "movements:org:"
ORGANISATION_TYPES = ("organization", "organisation", "org", "company", "corporation")


def movement_key(subject: str) -> str:
    return IDENTITY_PREFIX + subject


def org_key(name: str) -> str:
    return ORG_PREFIX + " ".join(re.sub(r"[^\w\s]", " ", str(name).casefold()).split())


def _overlaps(a_from: Any, a_to: Any, b_from: Any, b_to: Any) -> bool:
    lo = max(str(a_from or "0000-00-00")[:10], str(b_from or "0000-00-00")[:10])
    hi = min(str(a_to or "9999-12-31")[:10], str(b_to or "9999-12-31")[:10])
    return lo <= hi


def _pairs(items: Sequence[Any]) -> list[tuple[Any, Any]]:
    return [(items[i], items[j]) for i in range(len(items)) for j in range(i + 1, len(items))]


def _similar(a: str, b: str) -> bool:
    from difflib import SequenceMatcher

    return a != b and SequenceMatcher(None, a, b).ratio() >= 0.85


class MovementIdentity:
    """Exact identifier matches and organisation matches as reviewable candidates; records are never merged.

    Candidates live in the shared reviewable state machine
    (:class:`src.kb.ownership_identity.OwnershipIdentityService`) under ``movements:`` record keys, so review and
    revert are recorded as entity identity decisions (:mod:`src.kb.entity_history`) and are reversible.
    """

    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.now = now
        self.store = MovementStore(conn, initialize=False, now=now)

    # -------------------------------------------------------------- statements of identifier pairs

    def _registry_facts(self, namespace: str) -> list[dict[str, Any]]:
        facts = []
        for record in self.store.records(namespace, record_type="registry_record"):
            revisions = self.store.revisions(namespace, record["record_id"])
            latest = revisions[-1]
            valid_from = next((r["effective_from"] for r in revisions if r["event"] == "registered"), None)
            valid_to = latest["effective_from"] if latest["event"] == "deregistered" else None
            ids = [(i["scheme"], identifier_key(i["scheme"], i["value"])) for i in latest["statement"]["identifiers"]
                   if i["scheme"] in KEY_SCHEMES]
            cites = [{"store": "osint_movement_revisions", "record_id": record["record_id"],
                      "revision_id": r["revision_id"], "provider": record["provider"], "event": r["event"],
                      "effective_from": r["effective_from"]} for r in revisions]
            for (s1, k1), (s2, k2) in _pairs(ids):
                facts.append({"left": subject_key(s1, k1), "right": subject_key(s2, k2), "valid_from": valid_from,
                              "valid_to": valid_to, "stated_by": cites, "kind": "registry entry"})
        return facts

    def _identity_facts(self, namespace: str) -> list[dict[str, Any]]:
        facts = []
        for record in self.store.records(namespace):
            if record["record_type"] not in {"vessel_identity", "aircraft_identity"}:
                continue
            revision = self.store.latest(namespace, record["record_id"])
            value = revision["statement"]
            ids = [i for i in value["identifiers"] if i["scheme"] in KEY_SCHEMES]
            for left, right in _pairs(ids):
                start = right.get("valid_from") or left.get("valid_from") or value["effective"].get("from")
                finish = right.get("valid_to") or left.get("valid_to") or value["effective"].get("to")
                facts.append({"left": subject_key(left["scheme"], left["value"]),
                              "right": subject_key(right["scheme"], right["value"]),
                              "valid_from": str(start)[:10] if start else None,
                              "valid_to": str(finish)[:10] if finish else None,
                              "stated_by": [{"store": "osint_movement_revisions", "record_id": record["record_id"],
                                             "revision_id": revision["revision_id"], "provider": record["provider"]}],
                              "kind": value["record_type"]})
        return facts

    def _fisheries_facts(self, fisheries_namespace: str | None, scopes: set[str]) -> list[dict[str, Any]]:
        """GFW self-reported identity segments from the Fisheries store, cited, never copied (#2258)."""
        if not fisheries_namespace or not table_exists(self.conn, "fisheries_records"):
            return []
        require(scopes, "knowledge:fisheries:read")
        from src.kb.fisheries_store import FisheriesStore

        fisheries = FisheriesStore(self.conn, initialize=False)
        facts = []
        for record in fisheries.records(fisheries_namespace, record_type="vessel", provider="gfw"):
            for revision in fisheries.revisions(fisheries_namespace, record["record_id"]):
                published = revision["statement"]["as_published"]
                ids = [(scheme, identifier_key(scheme, published.get(field))) for scheme, field in
                       (("gfw_vessel_id", "gfw_vessel_id"), ("imo", "imo"), ("mmsi", "mmsi"))]
                ids = [(s, k) for s, k in ids if k]
                cite = {"store": "fisheries_revisions", "namespace": fisheries_namespace,
                        "record_id": record["record_id"], "revision_id": revision["revision_id"], "provider": "gfw",
                        "shared_with": "Fisheries pack (#2222)"}
                for (s1, k1), (s2, k2) in _pairs(ids):
                    facts.append({"left": subject_key(s1, k1), "right": subject_key(s2, k2),
                                  "valid_from": published.get("transmission_from"),
                                  "valid_to": published.get("transmission_to"), "stated_by": [cite],
                                  "kind": "GFW self-reported identity segment"})
        return facts

    def facts(self, namespace: str, *, scopes: Iterable[str], fisheries_namespace: str | None = None
              ) -> list[dict[str, Any]]:
        """Every stated identifier pairing with its period and citations; overlapping disagreements marked."""
        scopes = set(scopes)
        require(scopes, READ_SCOPE)
        items = self._registry_facts(namespace) + self._identity_facts(namespace) + \
            self._fisheries_facts(fisheries_namespace, scopes)
        by_side: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for fact in items:
            for mine, other in (("left", "right"), ("right", "left")):
                by_side.setdefault((fact[mine], parse_subject(fact[other])[1]), []).append(
                    {"fact": fact, "other": fact[other]})
        for (subject, _scheme), entries in sorted(by_side.items()):
            for a in entries:
                competing = sorted({b["other"] for b in entries if b["other"] != a["other"] and _overlaps(
                    a["fact"]["valid_from"], a["fact"]["valid_to"], b["fact"]["valid_from"], b["fact"]["valid_to"])})
                if competing:
                    a["fact"].setdefault("competing", {})[subject] = competing
        return items

    # -------------------------------------------------------------- proposals

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                fisheries_namespace: str | None = None) -> dict[str, Any]:
        """Offer exact identifier matches (time-bounded) and organisation-to-entity candidates; idempotent."""
        from src.kb.ownership_identity import OwnershipIdentityService
        from src.kb.ownership_store import canonical_entity_id

        scopes = set(scopes)
        service = OwnershipIdentityService(self.conn, **({"now": self.now} if self.now else {}))
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for fact in self.facts(namespace, scopes=scopes, fisheries_namespace=fisheries_namespace):
            a, b = sorted((fact["left"], fact["right"]))
            grouped.setdefault((a, b), []).append(fact)
        offered = []
        for (a, b), facts in sorted(grouped.items()):
            evidence = [{"method": "exact-identifier", "left": a, "right": b, "valid_from": f["valid_from"],
                         "valid_to": f["valid_to"], "stated_by": f["stated_by"], "kind": f["kind"],
                         **({"competing": f["competing"]} if f.get("competing") else {}),
                         "policy": "a time-bounded identifier pairing as stated; records are never merged"}
                        for f in facts]
            offered.append(service.offer(namespace, left_key=movement_key(a), right_key=movement_key(b),
                                         left_entity=canonical_entity_id(movement_key(a)),
                                         right_entity=canonical_entity_id(movement_key(b)), basis="exact-identifier",
                                         evidence=evidence, principal_id=principal_id, scopes=scopes))
        offered += self._propose_organisations(namespace, service, principal_id, scopes)
        return {"proposed": sorted({o["candidate_id"] for o in offered if o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes),
                "notice": "identifier pairings as the sources state them, time-bounded; organisations only, never "
                          "natural persons; a reviewer decides and records are never merged"}

    def organisations(self, namespace: str) -> list[dict[str, Any]]:
        """Registrants that are organisations, as published, with the registry records naming them."""
        found: dict[str, dict[str, Any]] = {}
        for record in self.store.records(namespace, record_type="registry_record"):
            revision = self.store.latest(namespace, record["record_id"])
            registrant = revision["statement"]["as_published"].get("registrant") or {}
            if registrant.get("natural_person") or not registrant.get("name"):
                continue  # natural persons are never matched or resolved
            for name in str(registrant["name"]).split(";"):
                if not name.strip():
                    continue
                item = found.setdefault(org_key(name), {"key": org_key(name), "names": set(), "records": []})
                item["names"].add(name.strip())
                item["records"].append({"record_id": record["record_id"], "revision_id": revision["revision_id"],
                                        "subject": record["subject_key"], "role": "registrant",
                                        "kind": registrant.get("kind")})
        return [{**v, "names": sorted(v["names"])} for _, v in sorted(found.items())]

    def _propose_organisations(self, namespace, service, principal_id, scopes) -> list[dict[str, Any]]:
        from src.kb.ownership_store import canonical_entity_id

        if not table_exists(self.conn, "canonical_entities"):
            return []
        entities = self.conn.execute(
            "SELECT canonical_id, preferred_name FROM canonical_entities WHERE lower(coalesce(entity_type, '')) IN ("
            + ",".join("?" * len(ORGANISATION_TYPES)) + ") ORDER BY canonical_id", list(ORGANISATION_TYPES)).fetchall()
        offered = []
        for organisation in self.organisations(namespace):
            mine = organisation["key"].removeprefix(ORG_PREFIX)
            for canonical_id, preferred in entities:
                theirs = org_key(preferred).removeprefix(ORG_PREFIX)
                basis = "name-jurisdiction" if theirs == mine else "similar-name" if _similar(mine, theirs) else None
                if not basis:
                    continue
                offered.append(service.offer(
                    namespace, left_key=organisation["key"], right_key=f"canonical:{canonical_id}",
                    left_entity=canonical_entity_id(organisation["key"]), right_entity=canonical_id, basis=basis,
                    evidence=[{"method": basis, "names_as_published": organisation["names"],
                               "records": organisation["records"],
                               "right": {"canonical_id": canonical_id, "preferred_name": preferred},
                               "policy": "an organisation registrant as published; a reviewer decides"}],
                    principal_id=principal_id, scopes=scopes))
        return offered

    # -------------------------------------------------------------- review

    def candidates(self, namespace: str, *, scopes: Iterable[str], subject: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        from src.kb.ownership_identity import OwnershipIdentityService

        require(scopes, READ_SCOPE)
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return []
        rows = OwnershipIdentityService(self.conn, initialize=False).candidates(
            namespace, scopes=set(scopes) | {"knowledge:ownership:read"}, state=state,
            record_key=movement_key(subject) if subject else None)
        return [c for c in rows if c["left_key"].startswith(IDENTITY_PREFIX) or
                c["right_key"].startswith(IDENTITY_PREFIX)]

    def _own(self, namespace: str, candidate_id: str, scopes: Iterable[str]) -> None:
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise MovementError("not_found", "no movement identity candidate with that id")

    def review(self, namespace: str, candidate_id: str, decision_: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_identity import OwnershipIdentityService

        self._own(namespace, candidate_id, scopes)
        return OwnershipIdentityService(self.conn, **({"now": self.now} if self.now else {})).review(
            namespace, candidate_id, decision_, reason, principal_id=principal_id, scopes=scopes)

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_identity import OwnershipIdentityService

        self._own(namespace, candidate_id, scopes)
        return OwnershipIdentityService(self.conn, **({"now": self.now} if self.now else {})).revert(
            namespace, candidate_id, reason, principal_id=principal_id, scopes=scopes)

    # -------------------------------------------------------------- resolution

    def connected(self, namespace: str, subject: str, start: Any = None, end: Any = None) -> dict[str, Any]:
        """Subjects joined to one subject by accepted exact-identifier matches whose stated period overlaps the window.

        Proposed and competing candidates never join records; they are reported for review.
        """
        rows = []
        if table_exists(self.conn, "ownership_identity_candidates"):
            rows = self.conn.execute(
                "SELECT candidate_id, left_key, right_key, state, evidence_json, decision_id FROM "
                "ownership_identity_candidates WHERE namespace=? AND basis='exact-identifier' AND (left_key LIKE "
                "'movements:%' OR right_key LIKE 'movements:%') ORDER BY candidate_id", [namespace]).fetchall()
        lo, hi = (day(start) if start else None), (day(end) if end else None)
        seen, frontier, used, pending = {subject}, [subject], [], []
        while frontier:
            current = frontier.pop()
            for candidate_id, left, right, state, evidence_json, decision_id in rows:
                keys = {left.removeprefix(IDENTITY_PREFIX), right.removeprefix(IDENTITY_PREFIX)}
                if current not in keys:
                    continue
                other = (keys - {current}).pop()
                evidence = json.loads(evidence_json)
                periods = [{"valid_from": e.get("valid_from"), "valid_to": e.get("valid_to")} for e in evidence]
                if not any(_overlaps(p["valid_from"], p["valid_to"], lo, hi) for p in periods):
                    continue
                entry = {"candidate_id": candidate_id, "from": current, "to": other, "state": state,
                         "decision_id": decision_id, "periods": periods,
                         "competing": [e["competing"] for e in evidence if e.get("competing")],
                         "stated_by": [c for e in evidence for c in e.get("stated_by") or []]}
                if state == "accepted" and other not in seen:
                    seen.add(other)
                    frontier.append(other)
                    used.append(entry)
                elif state == "proposed" and all(p["candidate_id"] != candidate_id for p in pending):
                    pending.append(entry)
        return {"subjects": sorted(seen), "accepted": used, "proposed": pending}


# ================================================================== MV10: citation links to sanctions and other records


_LINK_DDL = """
CREATE TABLE IF NOT EXISTS osint_movement_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, subject_key TEXT NOT NULL, scheme TEXT NOT NULL,
  identifier TEXT NOT NULL, target TEXT NOT NULL, target_namespace TEXT NOT NULL, target_id TEXT NOT NULL,
  basis TEXT NOT NULL, evidence_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
)
"""
LINK_SCHEMES = ("imo", "registration", "mmsi")
# What a list-stated identifier must say (kind, or its source label) to count as this scheme; never a guess.
_STATED_AS = {
    "imo": lambda i: i.get("kind") == "imo",
    "registration": lambda i: i.get("kind") in {"registration_number", "other", "call_sign"} and re.search(
        r"(?i)aircraft|tail|registration|mark", str(i.get("source_type") or "")),
    "mmsi": lambda i: re.search(r"(?i)mmsi", str(i.get("source_type") or "")) is not None,
}
SANCTIONS_BOUNDARY = ("listing statements as published, cited to the list revision and snapshot; no sanctions-evasion, "
                      "screening or compliance verdict is derived from registry records or movements")


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


class MovementLinks:
    """Links from aircraft and vessels to sanctions listings and other pack records, only on a stated identifier."""

    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = MovementStore(conn, initialize=False)

    def _identifiers(self, namespace: str) -> list[tuple[str, str, str]]:
        """(subject, scheme, key) every stored record states for the linkable schemes."""
        found = set()
        for item in self.store.identifiers(namespace):
            if item["scheme"] in LINK_SCHEMES:
                found.add((item["subject_key"], item["scheme"], item["value_key"]))
        for record in self.store.records(namespace):
            kind, scheme, value = parse_subject(record["subject_key"])
            if scheme in LINK_SCHEMES:
                found.add((record["subject_key"], scheme, value))
        return sorted(found)

    def _names(self, namespace: str, fisheries_namespace: str | None) -> list[tuple[str, str, str]]:
        """(subject, name, cited record) of vessel names as published (GFW events, Fisheries GFW segments)."""
        names = set()
        for record in self.store.records(namespace, record_type="call", provider="gfw-port-visits"):
            vessel = self.store.latest(namespace, record["record_id"])["statement"]["as_published"].get(
                "vessel_as_published") or {}
            if vessel.get("name"):
                names.add((record["subject_key"], vessel["name"], record["record_id"]))
        if fisheries_namespace and table_exists(self.conn, "fisheries_records"):
            from src.kb.fisheries_store import FisheriesStore

            fisheries = FisheriesStore(self.conn, initialize=False)
            for record in fisheries.records(fisheries_namespace, record_type="vessel", provider="gfw"):
                for revision in fisheries.revisions(fisheries_namespace, record["record_id"]):
                    published = revision["statement"]["as_published"]
                    gfw = identifier_key("gfw_vessel_id", published.get("gfw_vessel_id"))
                    if gfw and published.get("shipname") and published["shipname"] != "not stated":
                        names.add((subject_key("gfw_vessel_id", gfw), published["shipname"], revision["revision_id"]))
        return sorted(names)

    def link(self, namespace: str, *, principal_id: str, scopes: Iterable[str], sanctions_namespace: str | None = None,
             fisheries_namespace: str | None = None) -> dict[str, Any]:
        """Create identifier links (idempotent) and name-only review candidates; never a link on a name."""
        scopes = set(scopes)
        require(scopes, READ_SCOPE)
        self.conn.execute(_LINK_DDL)
        created, candidates = [], []
        identifiers = self._identifiers(namespace)
        if sanctions_namespace:
            from src.kb.sanctions import SanctionsStore, authorize
            from src.kb.sanctions_queries import SanctionsQueries

            authorize(sanctions_namespace, scopes, "knowledge:sanctions:read")
            queries = SanctionsQueries(self.conn)
            sanctions = SanctionsStore(self.conn, initialize=False)
            linked_designations: dict[str, set[str]] = {}
            for subject, scheme, key in identifiers:
                found = queries.find(sanctions_namespace, identifier=key)
                if scheme == "imo":  # lists write IMO numbers with or without the prefix
                    found += [d for d in queries.find(sanctions_namespace, identifier=f"IMO{key}") if d not in found]
                for designation_id in found:
                    history = [r for r in sanctions.history(sanctions_namespace, designation_id) if r["statement"]]
                    stated = next((i for r in reversed(history) for i in r["statement"].get("identifiers") or []
                                   if _norm(i.get("value")).removeprefix("IMO" if scheme == "imo" else "") == _norm(key)
                                   and _STATED_AS[scheme](i)), None)
                    if stated is None:
                        continue  # the value appears, but not as this kind of identifier: no link
                    designation = sanctions.designation(sanctions_namespace, designation_id)
                    revision = history[-1]
                    evidence = {"designation_id": designation_id, "list_id": designation["list_id"],
                                "list_entry_id": designation["list_entry_id"], "record_key": designation["record_key"],
                                "party_kind": designation["party_kind"], "revision_id": revision["revision_id"],
                                "source_revision": revision["source_revision"], "stated_identifier": stated}
                    created.append(self._insert(namespace, subject, scheme, key, "sanctions", sanctions_namespace,
                                                designation_id, "listed-identifier", evidence, principal_id))
                    linked_designations.setdefault(subject, set()).add(designation_id)
            candidates = self._name_candidates(namespace, sanctions_namespace, fisheries_namespace, queries,
                                               sanctions, linked_designations, principal_id, scopes)
        if fisheries_namespace and table_exists(self.conn, "fisheries_records"):
            require(scopes, "knowledge:fisheries:read")
            from src.kb.fisheries_store import FisheriesStore

            fisheries = FisheriesStore(self.conn, initialize=False)
            for subject, scheme, key in identifiers:
                if scheme != "imo":
                    continue
                for fisheries_subject in fisheries.find_subjects(fisheries_namespace, "imo", key):
                    for record in fisheries.records(fisheries_namespace, subject_keys=[fisheries_subject]):
                        if record["record_type"] not in {"authorisation", "listing"}:
                            continue
                        revision = fisheries.revisions(fisheries_namespace, record["record_id"])[-1]
                        evidence = {"record_id": record["record_id"], "revision_id": revision["revision_id"],
                                    "record_type": record["record_type"], "provider": record["provider"],
                                    "list_key": record["list_key"], "stated_imo": key}
                        created.append(self._insert(namespace, subject, scheme, key, "fisheries",
                                                    fisheries_namespace, record["record_id"], "listed-identifier",
                                                    evidence, principal_id))
        return {"links": self.links(namespace), "created": sorted({c for c in created if c}),
                "name_candidates": candidates, "boundary": SANCTIONS_BOUNDARY}

    def _name_candidates(self, namespace, sanctions_namespace, fisheries_namespace, queries, sanctions, linked,
                         principal_id, scopes) -> list[dict[str, Any]]:
        from src.kb.ownership_identity import OwnershipIdentityService
        from src.kb.ownership_store import canonical_entity_id

        if not {"knowledge:ownership:write", "operator"} & scopes:
            return []  # name-only candidates enter the reviewable state machine, which needs its write scope
        service = OwnershipIdentityService(self.conn, now=self.now)
        out, seen = [], set()
        for subject, name, cited in self._names(namespace, fisheries_namespace):
            for designation_id in queries.find(sanctions_namespace, name=name):
                if any(designation_id in ids for ids in linked.values()) or (subject, designation_id) in seen:
                    continue  # already linked on a stated identifier, or already offered
                seen.add((subject, designation_id))
                designation = sanctions.designation(sanctions_namespace, designation_id)
                offered = service.offer(
                    namespace, left_key=movement_key(subject), right_key=designation["record_key"],
                    left_entity=canonical_entity_id(movement_key(subject)),
                    right_entity=designation["canonical_entity_id"] or canonical_entity_id(designation["record_key"]),
                    basis="similar-name",
                    evidence=[{"method": "name-only", "name_as_published": name, "cited": cited,
                               "designation_id": designation_id, "list_id": designation["list_id"],
                               "policy": "a name alone is never a link; a reviewer sees it as a candidate that "
                                         "cannot be accepted without identifier evidence"}],
                    principal_id=principal_id, scopes=scopes)
                out.append({"subject": subject, "designation_id": designation_id, "name": name,
                            "candidate_id": offered["candidate_id"], "basis": "similar-name"})
        return out

    def _insert(self, namespace, subject, scheme, key, target, target_namespace, target_id, basis, evidence,
                principal_id) -> str | None:
        link_id = "mv-link:" + digest([namespace, subject, scheme, key, target, target_namespace, target_id])[:24]
        exists = self.conn.execute("SELECT 1 FROM osint_movement_links WHERE namespace=? AND link_id=?",
                                   [namespace, link_id]).fetchone()
        if exists:
            return None
        self.conn.execute("INSERT INTO osint_movement_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, subject, scheme, key, target, target_namespace, target_id, basis,
                           canonical(evidence), principal_id, self.now()])
        return link_id

    def links(self, namespace: str, subjects: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "osint_movement_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id, subject_key, scheme, identifier, target, target_namespace, target_id, basis, "
            "evidence_json, created_at_ms FROM osint_movement_links WHERE namespace=? ORDER BY link_id",
            [namespace]).fetchall()
        wanted = None if subjects is None else set(subjects)

        def keyed(scheme: str, value: str) -> str | None:
            try:
                return subject_key(scheme, value)
            except MovementError:
                return None

        return [{"contract": LINK_CONTRACT, "link_id": r[0], "subject_key": r[1], "scheme": r[2], "identifier": r[3],
                 "target": r[4], "target_namespace": r[5], "target_id": r[6], "basis": r[7],
                 "evidence": json.loads(r[8]), "created_at_ms": int(r[9])}
                for r in rows if wanted is None or r[1] in wanted or keyed(r[2], r[3]) in wanted]

    def statements(self, namespace: str, subjects: Iterable[str], as_of: Any, *, scopes: Iterable[str]
                   ) -> list[dict[str, Any]]:
        """What each linked list stated as of a date, as published (listed, not listed in the snapshot, unknown)."""
        out = []
        for link in self.links(namespace, subjects):
            if link["target"] != "sanctions":
                out.append({"link": link, "status": "cited record", "statement": link["evidence"]})
                continue
            from src.kb.sanctions import authorize
            from src.kb.sanctions_queries import SanctionsQueries

            authorize(link["target_namespace"], set(scopes), "knowledge:sanctions:read")
            stated = SanctionsQueries(self.conn).statement_as_of(link["target_namespace"], link["target_id"],
                                                                 day(as_of))
            out.append({"link": link, "status": stated["status"], "as_of": stated["as_of"],
                        "coverage": stated.get("coverage"), "delisting": stated.get("delisting"),
                        "reason": stated.get("reason"),
                        "statement": {k: (stated.get("statement") or {}).get(k) for k in
                                      ("revision_id", "source_revision", "listed_on", "programmes")}
                        if stated.get("statement") else None,
                        "boundary": SANCTIONS_BOUNDARY})
        return out


# ================================================================== MV11: bounded as-of answers


def _cite(record: Mapping[str, Any], revision: Mapping[str, Any]) -> dict[str, Any]:
    value = revision["statement"]
    return {"record_id": record["record_id"], "revision_id": revision["revision_id"], "provider": record["provider"],
            "url": value["source"].get("url"), "locator": value["source"].get("locator"),
            "licence": value["licence"], "evidence_origin": revision["evidence_origin"] or
            value["source"].get("evidence_origin"), "retrieved_at_ms": revision["observed_at_ms"]}


def _withhold(published: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(published)
    registrant = dict(value.get("registrant") or {})
    if registrant.get("natural_person"):
        registrant["name"] = "withheld: a natural person"
    if registrant:
        value["registrant"] = registrant
    return value


def _in(value: Any, start: datetime, end: datetime) -> bool:
    return value is not None and start <= parse_time(value) <= end


class MovementQueries:
    """Registry state and sampled movements for one aircraft or vessel as of a bounded window."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = MovementStore(conn, initialize=False)
        self.identity = MovementIdentity(conn)

    # -------------------------------------------------------------- resolution

    def _resolve(self, namespace: str, identifier: Any, scheme: str | None, start: Any, end: Any) -> dict[str, Any]:
        scheme, key = classify(identifier, scheme)
        subject = subject_key(scheme, key)
        refused = self.store.refusal(namespace, scheme, key)
        if refused:
            raise MovementError("privacy_opt_out", f"{scheme} {key} is on the privacy refusal list "
                                                   f"({refused['programme']}); no movement or registry answer is "
                                                   "given", programme=refused["programme"])
        connected = self.identity.connected(namespace, subject, start, end)
        subjects = set(connected["subjects"])  # movement records join only through accepted matches
        for other in sorted(subjects - {subject}):
            _, other_scheme, other_key = parse_subject(other)
            if self.store.refusal(namespace, other_scheme, other_key):
                raise MovementError("privacy_opt_out", "a connected identifier is on the privacy refusal list")
        return {"scheme": scheme, "key": key, "subject": subject, "subjects": sorted(subjects),
                "connected": connected}

    def _registry(self, namespace: str, subjects: Iterable[str], start: datetime, end: datetime
                  ) -> list[dict[str, Any]]:
        subjects = set(subjects)
        out = []
        for record in self.store.records(namespace, record_type="registry_record"):
            revisions = self.store.revisions(namespace, record["record_id"])
            stated = {subject_key(i["scheme"], i["value"]) for r in revisions for i in r["statement"]["identifiers"]
                      if i["scheme"] in KEY_SCHEMES}
            if record["subject_key"] not in subjects and not stated & subjects:
                continue
            for index, revision in enumerate(revisions):
                begins = revision["effective_from"] or revision["statement"]["effective"].get("from")
                following = revisions[index + 1]["effective_from"] if index + 1 < len(revisions) else None
                valid = (begins is None or parse_time(begins) <= end) and (following is None or
                                                                            parse_time(following) >= start)
                if not valid:
                    continue
                value = revision["statement"]
                out.append({"record_key": value["record_key"], "subject": record["subject_key"],
                            "event": revision["event"], "valid_from": begins, "superseded_on": following,
                            "as_published": _withhold(value["as_published"]),
                            "identifiers": value["identifiers"], "citation": _cite(record, revision)})
        return sorted(out, key=lambda r: (r["record_key"], r["citation"]["revision_id"]))

    def _fisheries_identity(self, namespace: str, fisheries_namespace: str | None, subjects: set[str],
                            start: datetime, end: datetime, scopes: set[str]) -> list[dict[str, Any]]:
        out = []
        for fact in self.identity._fisheries_facts(fisheries_namespace, scopes):
            if not {fact["left"], fact["right"]} & subjects:
                continue
            if not _overlaps(fact["valid_from"], fact["valid_to"], start.date().isoformat(), end.date().isoformat()):
                continue
            out.append(fact)
        return out

    def _windows(self, namespace: str, subjects: set[str], start: datetime, end: datetime) -> list[dict[str, Any]]:
        out = []
        for record in self.store.records(namespace, record_type="sample_window"):
            if record["subject_key"] not in subjects:
                continue
            revision = self.store.latest(namespace, record["record_id"])
            value = revision["statement"]
            if parse_time(value["window"]["end"]) < start or parse_time(value["window"]["start"]) > end:
                continue
            samples = []
            for sample in self.store.records(namespace, record_type="position_sample", window=record["window_id"]):
                latest = self.store.latest(namespace, sample["record_id"])
                published = latest["statement"]["as_published"]
                if _in(published["timestamp"], start, end):
                    samples.append({**{k: published.get(k) for k in ("timestamp", "lat", "lon", "altitude_m",
                                                                     "on_ground", "speed_over_ground_kn")
                                       if published.get(k) is not None},
                                    "receiver_category": published.get("receiver_category"),
                                    "revision_id": latest["revision_id"], "record_id": sample["record_id"]})
            coverage = dict(value["coverage"])
            out.append({"window_id": record["window_id"], "provider": record["provider"], "subject": record[
                "subject_key"], "start": value["window"]["start"], "end": value["window"]["end"],
                "coverage": {**coverage, "statement": NO_COVERAGE if coverage["status"] == "no_coverage_observed"
                             else coverage["status"].replace("_", " ")},
                "bound": value["bound"], "samples": sorted(samples, key=lambda s: s["timestamp"]),
                "citation": _cite(record, revision)})
        return sorted(out, key=lambda w: (w["start"], w["provider"]))

    def _calls(self, namespace: str, subjects: set[str], start: datetime, end: datetime,
               facilities_namespace: str | None) -> dict[str, list[dict[str, Any]]]:
        published, derived = [], []
        for record in self.store.records(namespace, record_type="call"):
            if record["subject_key"] not in subjects:
                continue
            revision = self.store.latest(namespace, record["record_id"])
            value = revision["statement"]
            call = value["as_published"]
            first = call.get("arrival") or call.get("time")
            last = call.get("departure") or call.get("time")
            if parse_time(last) < start or parse_time(first) > end:
                continue
            item = {"subject": record["subject_key"], "window_id": record["window_id"], **call,
                    "citation": _cite(record, revision)}
            if record["provider"] == "derived":
                derived.append(item)
            else:
                facility = dict(call.get("facility") or {})
                if facility.get("resolution") == "unresolved":
                    facility.update(resolve_facility(self.conn, facilities_namespace, facility.get("kind"),
                                                     facility.get("code") or facility.get("anchorage_id")))
                item["facility"] = facility
                published.append(item)
        key = lambda c: (c.get("arrival") or c.get("time") or "", c["citation"]["record_id"])  # noqa: E731
        return {"source_published": sorted(published, key=key), "derived": sorted(derived, key=key)}

    # -------------------------------------------------------------- answers

    def _private(self, namespace: str, subjects: Iterable[str], end: datetime) -> bool:
        for subject in subjects:
            if subject.startswith("aircraft:"):
                revision = self.store.registry_state(namespace, subject)
                if natural_person_registrant(revision):
                    return True
        return False

    def registry(self, namespace: str, identifier: Any, *, scopes: Iterable[str], as_of: Any = None,
                 scheme: str | None = None) -> dict[str, Any]:
        """Registry state (revisions valid on the date) for one aircraft or vessel; no positions."""
        scopes = set(scopes)
        require(scopes, READ_SCOPE)
        when = parse_time(as_of or datetime.now(timezone.utc).date().isoformat())
        resolved = self._resolve(namespace, identifier, scheme, when, when)
        registry = self._registry(namespace, resolved["subjects"], when, when + timedelta(days=1) - timedelta(
            seconds=1))
        answer = {"contract": ANSWER_CONTRACT, "query": "registry", "namespace": namespace,
                  "identifier": {"scheme": resolved["scheme"], "value": resolved["key"]}, "as_of": day(when),
                  "registry": registry,
                  "status": "found" if registry else "none on record in the acquired registries",
                  "identity_matches": {"accepted": resolved["connected"]["accepted"],
                                       "proposed": resolved["connected"]["proposed"]},
                  "notice": "registry entries as published; a natural-person registrant's name is withheld and "
                            "never a key; absence from the acquired registries is not absence of registration",
                  "never": list(NEVER)}
        answer["answer_hash"] = digest(answer)
        return answer

    def window(self, namespace: str, identifier: Any, start: Any, end: Any, *, scopes: Iterable[str],
               scheme: str | None = None, facilities_namespace: str | None = None,
               fisheries_namespace: str | None = None, sanctions_namespace: str | None = None,
               as_of: Any = None) -> dict[str, Any]:
        """Registry state, sample windows with gaps, published and derived calls, identity matches and sanctions
        citations for one identifier and a window of at most 92 days."""
        scopes = set(scopes)
        require(scopes, READ_SCOPE, MOVEMENT_SCOPE)
        begin, finish = check_window(start, end)
        resolved = self._resolve(namespace, identifier, scheme, begin, finish)
        subjects = set(resolved["subjects"])
        if self._private(namespace, subjects, finish):
            raise MovementError("private_aircraft_refused", "the aircraft's registry record names a natural person; "
                                                            "its movements are an individual's movements and are "
                                                            "not answered")
        windows = self._windows(namespace, subjects, begin, finish)
        calls = self._calls(namespace, subjects, begin, finish, facilities_namespace)
        observed = [w for w in windows if w["coverage"]["status"] == "positions_observed"]
        if not windows:
            coverage = {"status": "no_coverage_observed", "statement": NO_COVERAGE,
                        "detail": "no sample window was acquired for this identifier and window"}
        elif not observed and not any(w["coverage"]["status"] == "events_published" for w in windows):
            coverage = {"status": "no_coverage_observed", "statement": NO_COVERAGE,
                        "detail": "the acquired windows hold no positions or events"}
        else:
            coverage = {"status": "partial", "statement": "positions and events only where the sources had receiver "
                                                          "coverage; see each window's gaps",
                        "windows": len(windows), "windows_with_positions": len(observed),
                        "gaps": sum(len(w["coverage"]["gaps"]) for w in windows)}
        coverage["caveat"] = COVERAGE_CAVEAT
        sanctions = []
        if sanctions_namespace:
            links = MovementLinks(self.conn)
            sanctions = links.statements(namespace, subjects, as_of or finish, scopes=scopes)
        answer = {
            "contract": ANSWER_CONTRACT, "query": "window", "namespace": namespace,
            "identifier": {"scheme": resolved["scheme"], "value": resolved["key"]},
            "window": {"start": stamp(begin), "end": stamp(finish)}, "as_of": day(as_of or finish),
            "subjects": sorted(subjects),
            "registry": self._registry(namespace, subjects, begin, finish),
            "vessel_identity": self._fisheries_identity(namespace, fisheries_namespace, subjects, begin, finish,
                                                        scopes),
            "sample_windows": windows, "calls": calls, "coverage": coverage,
            "identity_matches": {"accepted": resolved["connected"]["accepted"],
                                 "proposed": resolved["connected"]["proposed"]},
            "sanctions": sanctions,
            "boundary": SANCTIONS_BOUNDARY,
            "never": list(NEVER),
        }
        bad = forbidden_keys(answer)
        if bad:  # pragma: no cover - a guard: nothing above writes an inference key
            raise MovementError("invalid_answer", f"answer carries {sorted(bad)}")
        answer["answer_hash"] = digest(answer)
        return answer

    # -------------------------------------------------------------- evidence bundles

    def export_bundle(self, answer: Mapping[str, Any]) -> dict[str, Any]:
        """An evidence bundle with source, revision and as-of time for every registry revision, window, sample,
        call, identity match and listing statement the answer used."""
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        if answer.get("contract") != ANSWER_CONTRACT:
            raise MovementError("invalid_request", "export a movement answer")
        builder = EvidenceBundleBuilder("receipt", {"operation": f"movement-{answer['query']}",
                                                    "namespace": answer["namespace"],
                                                    "answer_hash": answer.get("answer_hash")}, created_at_ms=0)
        refs = []

        def add(kind: str, payload: Mapping[str, Any], object_id: str) -> None:
            refs.append(builder.add_object("evidence", {"kind": kind, "as_of": answer["as_of"], **payload},
                                           object_id=object_id))
            url = (payload.get("citation") or {}).get("url")
            if url:
                builder.add_external_reference(f"source:{object_id}", url, required=False)

        for item in answer.get("registry") or []:
            add("movement-registry-revision", item, item["citation"]["revision_id"])
        for window in answer.get("sample_windows") or []:
            add("movement-sample-window", {k: v for k, v in window.items() if k != "samples"},
                window["citation"]["revision_id"])
            for sample in window["samples"]:
                add("movement-position-sample", {**sample, "window_id": window["window_id"],
                                                 "provider": window["provider"]}, sample["revision_id"])
        for status in ("source_published", "derived"):
            for call in (answer.get("calls") or {}).get(status, []):
                add(f"movement-call-{status}", call, call["citation"]["revision_id"])
        for match in (answer.get("identity_matches") or {}).get("accepted", []):
            add("movement-identity-match", match, match["candidate_id"])
        for fact in answer.get("vessel_identity") or []:
            add("fisheries-vessel-identity", fact, f"{fact['stated_by'][0]['revision_id']}:{fact['left']}:{fact['right']}")
        for statement_ in answer.get("sanctions") or []:
            add("movement-listing-statement", statement_, statement_["link"]["link_id"])
        builder.add_object("receipt", {"kind": "movement-answer", **{k: v for k, v in answer.items()
                                                                     if k not in {"sample_windows"}}},
                           object_id=f"movement:{answer.get('answer_hash')}", references=refs, root=True)
        if answer.get("coverage", {}).get("status") == "no_coverage_observed":
            builder.add_omission(NO_COVERAGE + ": not evidence that the aircraft or vessel did not move")
        return {"bundle": builder.build(), "boundary": [COVERAGE_CAVEAT, SANCTIONS_BOUNDARY]}
