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
from datetime import date, datetime, timedelta, timezone
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
    if re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", text):
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


def _today() -> str:
    return date.today().isoformat()
