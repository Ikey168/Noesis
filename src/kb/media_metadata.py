"""Books, music and authority metadata beside Cultural Collections (#2225).

Works, editions (manifestations), recordings, releases, creators and authority
links from Open Library, MusicBrainz (CC0 core data), Wikidata, the Deutsche
Nationalbibliothek and the Library of Congress. Records sit next to the
cultural object records of :mod:`src.kb.cultural`. They connect to them, and to
news entities in ``canonical_entities``, only through evidence.

* **Records and revisions (MM02).** There is one record per source and native
  ID. Revisions are immutable and keyed by the provider's own revision marker:
  the Open Library ``revision``, the Wikidata revision ID, MARC 005, or the
  digest of the CC0 core payload for MusicBrainz (its web service exposes no
  edit marker). An older revision that arrives late is history, and replaying
  a marker adds nothing. The same marker with different content is recorded
  as a conflict and never overwrites the stored revision. A record's level
  (work, edition, recording, release, creator, authority link) is fixed at
  its first revision, so an edition never becomes a work.
* **Identifiers.** Identifiers are typed as ISBN-10/13 (checksums; an ISBN-10
  meets its ISBN-13 form), ISRC, ISWC, MBID, Wikidata QID, GND, DNB IDN,
  LCCN/LCNAF, OLID, VIAF, ISNI, OCLC and barcode. Each is kept as published
  beside a normalized key. An invalid identifier stays visible as invalid.
* **Redirects.** A provider redirect, merge, deletion or deprecation is a
  revision of the old record. Redirects and merges are also ``redirect``
  decisions in :class:`src.kb.entity_history.EntityHistoryStore`.

Identity (MM07), cultural and news links (MM08), as-of answers (MM09) and
monitors (:mod:`src.kb.media_metadata_monitoring`, MM10) build on this store.
The store never holds content: no full text, audio, images, popularity data
or rights determinations.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

CONTRACT = "noesis-media-metadata-record-v1"
MATCH_CONTRACT = "noesis-media-metadata-match-v1"
LINK_CONTRACT = "noesis-media-metadata-link-v1"
ANSWER_CONTRACT = "noesis-media-metadata-answer-v1"
READ_SCOPE = "knowledge:cultural:read"
WRITE_SCOPE = "knowledge:cultural:write"
REVIEW_SCOPE = "knowledge:cultural:review"
SOURCE_PACK = "primary-scientific-evidence"
DEFAULT_NAMESPACE = "global"
FEATURE = "media-metadata"
NEWS_FEATURE = "media-metadata-news"
SOURCES = ("open-library", "musicbrainz", "wikidata", "dnb", "loc")
RECORD_TYPES = ("work", "edition", "recording", "release", "creator", "authority-link")
STATUSES = ("active", "redirected", "deprecated", "deleted")
SCHEMES = ("isbn10", "isbn13", "isrc", "iswc", "mbid", "wikidata", "gnd", "idn", "lccn", "lcnaf", "olid", "viaf",
           "isni", "oclc", "barcode")
# Normalized identifier families: ISBN-10 and ISBN-13 meet as one ISBN; an LCNAF heading is an LCCN.
FAMILY = {"isbn10": "isbn", "isbn13": "isbn", "lcnaf": "lccn"}
# The scheme that names a record of each source (its own key).
SELF_SCHEMES = {"open-library": ("olid",), "musicbrainz": ("mbid",), "wikidata": ("wikidata",),
                "dnb": ("gnd", "idn"), "loc": ("lcnaf", "lccn")}
BOUNDARY = ("Metadata as published by each provider: no full text, audio, cover images, popularity rankings or "
            "rights clearance determinations.")
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:read", "knowledge:entity-history:write",
                          "knowledge:entity-history:review", "knowledge:entity-history:execute"}
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema/noesis-media-metadata-record-v1.json"

_DDL = """
CREATE SEQUENCE IF NOT EXISTS media_metadata_seq START 1;
CREATE TABLE IF NOT EXISTS media_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, source TEXT NOT NULL, native_id TEXT NOT NULL,
  record_type TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS media_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, seq BIGINT NOT NULL,
  marker TEXT NOT NULL, basis TEXT NOT NULL, order_key TEXT, revision_date DATE, content_digest TEXT NOT NULL,
  status TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL, run_id TEXT, source_id TEXT, document_id TEXT,
  statement_json TEXT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS media_current (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS media_identifiers (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, position INTEGER NOT NULL,
  scheme TEXT NOT NULL, value TEXT NOT NULL, key TEXT, valid BOOLEAN NOT NULL, role TEXT NOT NULL,
  property TEXT, rank TEXT, PRIMARY KEY(namespace, revision_id, position)
);
CREATE TABLE IF NOT EXISTS media_revision_conflicts (
  namespace TEXT NOT NULL, conflict_id TEXT NOT NULL, record_id TEXT NOT NULL, marker TEXT NOT NULL,
  stored_revision_id TEXT NOT NULL, offered_digest TEXT NOT NULL, offered_json TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, conflict_id)
);
CREATE TABLE IF NOT EXISTS media_redirects (
  namespace TEXT NOT NULL, source TEXT NOT NULL, from_id TEXT NOT NULL, to_id TEXT, kind TEXT NOT NULL,
  revision_id TEXT NOT NULL, decision_id TEXT, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, source, from_id, revision_id)
);
CREATE TABLE IF NOT EXISTS media_selection (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, selection_index INTEGER NOT NULL,
  selector_json TEXT NOT NULL, outcome TEXT NOT NULL, statements INTEGER NOT NULL, response_sha256 TEXT,
  excluded_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, run_id, source_id, selection_index)
);
CREATE TABLE IF NOT EXISTS media_dump_imports (
  namespace TEXT NOT NULL, import_id TEXT NOT NULL, dump_name TEXT NOT NULL, dump_date TEXT NOT NULL,
  slice_sha256 TEXT NOT NULL, receipt_json TEXT NOT NULL, imported_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, import_id)
);
"""


class MediaMetadataError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_name=?", [name]).fetchone()[0])


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise MediaMetadataError("unauthorized", f"{required} and namespace access are required")


def iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def as_of_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise MediaMetadataError("invalid_as_of", "as_of is an ISO date (YYYY-MM-DD)") from exc


# ------------------------------------------------------------------ identifiers


def _isbn10_valid(text: str) -> bool:
    if not re.fullmatch(r"\d{9}[\dX]", text):
        return False
    total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(text))
    return total % 11 == 0


def _isbn13_valid(text: str) -> bool:
    if not re.fullmatch(r"97[89]\d{10}", text):
        return False
    return sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(text)) % 10 == 0


def isbn13_from_isbn10(text: str) -> str:
    core = "978" + text[:9]
    check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
    return core + str(check)


def _iswc_valid(text: str) -> bool:
    if not re.fullmatch(r"T\d{10}", text):
        return False
    digits = [int(c) for c in text[1:10]]
    check = (10 - (1 + sum((i + 1) * d for i, d in enumerate(digits))) % 10) % 10
    return check == int(text[10])


def normalize_lccn(value: str) -> str:
    """Library of Congress LCCN normalization (whitespace, trailing slash part, hyphenated serial padding)."""
    text = re.sub(r"\s+", "", str(value)).casefold()
    text = text.split("/", 1)[0]
    if "-" in text:
        prefix, serial = text.split("-", 1)
        if serial.isdigit():
            text = prefix + serial.zfill(6)
    return text


_GND = re.compile(r"(?:1[0123]?\d{7}[0-9X]|[47]\d{6}-\d|[1-9]\d{0,7}-[0-9X]|3\d{7}[0-9X])")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def normalize_identifier(scheme: str, value: Any) -> dict[str, Any]:
    """``{scheme, value, key, valid, reason}``: the value as published beside a normalized key."""
    if scheme not in SCHEMES:
        raise MediaMetadataError("unknown_scheme", f"identifier scheme {scheme!r} is not audited")
    raw = str(value if value is not None else "").strip()
    text, valid, reason = raw, False, None
    if scheme in {"isbn10", "isbn13"}:
        text = re.sub(r"[\s\-]", "", raw).upper()
        if len(text) == 10:
            valid = _isbn10_valid(text)
            text = isbn13_from_isbn10(text) if valid else text
        elif len(text) == 13:
            valid = _isbn13_valid(text)
        reason = None if valid else "ISBN checksum or length is invalid"
    elif scheme == "isrc":
        text = re.sub(r"[\s\-]", "", raw).upper()
        valid = bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", text))
        reason = None if valid else "ISRC is CC-XXX-YY-NNNNN (country, registrant, year, designation)"
    elif scheme == "iswc":
        text = re.sub(r"[\s\-.]", "", raw).upper()
        valid = _iswc_valid(text)
        reason = None if valid else "ISWC format or check digit is invalid"
    elif scheme == "mbid":
        text = raw.casefold()
        valid = bool(_UUID.fullmatch(text))
        reason = None if valid else "MBID is a UUID"
    elif scheme == "wikidata":
        text = raw.upper().rsplit("/", 1)[-1]
        valid = bool(re.fullmatch(r"Q[1-9]\d*", text))
        reason = None if valid else "Wikidata item IDs are Q followed by digits"
    elif scheme == "gnd":
        text = raw.upper().removeprefix("(DE-588)").rsplit("/", 1)[-1]
        valid = bool(_GND.fullmatch(text))
        reason = None if valid else "not a GND identifier"
    elif scheme == "idn":
        text = raw.upper().removeprefix("(DE-101)")
        valid = bool(re.fullmatch(r"\d{8,9}[\dX]", text))
        reason = None if valid else "DNB IDN is 9 or 10 characters"
    elif scheme in {"lccn", "lcnaf"}:
        text = normalize_lccn(raw.rsplit("/", 1)[-1] if raw.startswith("http") else raw)
        valid = bool(re.fullmatch(r"[a-z]{0,3}\d{8}|[a-z]{0,2}\d{10}", text))
        reason = None if valid else "LCCN is an optional prefix and 8 or 10 digits"
    elif scheme == "olid":
        text = raw.rsplit("/", 1)[-1].upper()
        valid = bool(re.fullmatch(r"OL[1-9]\d*[AWM]", text))
        reason = None if valid else "OLID is OL, digits and A, W or M"
    elif scheme == "viaf":
        text = raw.rsplit("/", 1)[-1]
        valid = bool(re.fullmatch(r"[1-9]\d{0,21}", text))
        reason = None if valid else "VIAF IDs are digits"
    elif scheme == "isni":
        text = re.sub(r"\s", "", raw).upper()
        valid = bool(re.fullmatch(r"\d{15}[\dX]", text))
        reason = None if valid else "ISNI is 16 characters"
    elif scheme == "oclc":
        text = re.sub(r"^\(OCOLC\)|^OCM|^OCN|^ON", "", raw.upper())
        valid = text.isdigit()
        reason = None if valid else "OCLC numbers are digits"
    elif scheme == "barcode":
        text = re.sub(r"\D", "", raw)
        valid = len(text) in {8, 12, 13, 14}
        reason = None if valid else "barcodes are 8, 12, 13 or 14 digits"
    family = FAMILY.get(scheme, scheme)
    return {"scheme": scheme, "value": raw, "key": f"{family}:{text}" if valid else None, "valid": valid,
            "reason": reason}


def detect_scheme(value: str) -> str | None:
    """Best-effort scheme of a bare identifier (a declared scheme always wins)."""
    text = str(value or "").strip()
    compact = re.sub(r"[\s\-]", "", text).upper()
    if _UUID.fullmatch(text.casefold()):
        return "mbid"
    if re.fullmatch(r"Q[1-9]\d*", compact):
        return "wikidata"
    if re.fullmatch(r"OL[1-9]\d*[AWM]", compact):
        return "olid"
    if re.fullmatch(r"97[89]\d{10}", compact):
        return "isbn13"
    if re.fullmatch(r"\d{9}[\dX]", compact) and _isbn10_valid(compact):
        return "isbn10"
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", compact):
        return "isrc"
    if re.fullmatch(r"T\d{10}", re.sub(r"[.\-]", "", compact)):
        return "iswc"
    if re.fullmatch(r"[a-z]{1,3}\d{8,10}", text.casefold().replace(" ", "")):
        return "lccn"
    if _GND.fullmatch(text.upper()):
        return "gnd"
    return None


def native_key(source: str, native_id: str) -> str:
    """Native IDs are compared in their normalized form (QIDs upper case, MBIDs lower case, LCCNs normalized)."""
    text = str(native_id).strip()
    if source == "musicbrainz":
        return text.casefold()
    if source == "loc":
        return normalize_lccn(text)
    return text.upper() if source in {"wikidata", "open-library", "dnb"} else text


# ------------------------------------------------------------------ validation


_VALIDATOR = None


def _validator():
    global _VALIDATOR
    if _VALIDATOR is None:
        import jsonschema

        _VALIDATOR = jsonschema.Draft7Validator(json.loads(SCHEMA_PATH.read_text()))
    return _VALIDATOR


def validate_statement(statement: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(canonical(statement))
    errors = sorted(_validator().iter_errors(value), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        raise MediaMetadataError("invalid_record", f"{'/'.join(map(str, first.path)) or 'record'}: {first.message}")
    if not any(i["role"] == "self" for i in value["identifiers"]):
        raise MediaMetadataError("invalid_record", "a record names its own key as a self identifier")
    for item in value["identifiers"]:
        if item["role"] == "self" and item["scheme"] not in SELF_SCHEMES[value["source"]]:
            raise MediaMetadataError("invalid_record", f"{value['source']} records are keyed by "
                                                       f"{SELF_SCHEMES[value['source']]}")
    if value["record_type"] == "edition" and any(r["type"] == "has_edition" for r in value["relations"]):
        raise MediaMetadataError("invalid_record", "an edition never lists editions; it is not a work")
    return value


def record_id_for(namespace: str, source: str, native_id: str) -> str:
    return "media-record:" + digest([namespace, source, native_key(source, native_id)])[:24]


def _content(statement: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in statement.items() if k != "retrieved"}


class MediaMetadataStore:
    """Immutable, revision-addressable media metadata records per namespace."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "media_revisions")

    def require_ready(self) -> None:
        if not self.ready():
            raise MediaMetadataError(
                "not_ready", f"no media metadata is stored yet; run the {SOURCE_PACK} media-metadata sources")

    # ------------------------------------------------------------------ writes

    def observe_page(self, run_id: str, source: Mapping[str, Any], namespace: str,
                     records: Sequence[Mapping[str, Any]], *, documents: Mapping[str, str] | None = None,
                     page_receipt: Mapping[str, Any] | None = None) -> dict[str, int]:
        """Project one runtime page in one transaction; replays add nothing."""
        statements = []
        for item in records:
            statement = dict(item.get("media_metadata") or {})
            if statement.get("contract") != CONTRACT:
                raise MediaMetadataError("invalid_record", "page record lacks a media metadata statement")
            statements.append((statement, (documents or {}).get(str(item.get("id")))))
        counts = {"created": 0, "revised": 0, "history": 0, "unchanged": 0, "conflict": 0}
        now = self.now()
        receipt = dict(page_receipt or {})
        self.conn.execute("BEGIN")
        try:
            for statement, document_id in statements:
                result = self._apply(namespace, statement, run_id=run_id, source_id=source.get("source_id"),
                                     document_id=document_id, retrieved_at_ms=now)
                counts[result["status"]] += 1
            if receipt.get("selection_index") is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO media_selection VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [namespace, run_id, source.get("source_id"), int(receipt["selection_index"]),
                     canonical(receipt.get("selector") or {}), str(receipt.get("selector_outcome") or "returned"),
                     int(receipt.get("statements") or 0), receipt.get("response_sha256"),
                     canonical(receipt.get("excluded_fields_dropped") or []), now])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def apply(self, namespace: str, statement: Mapping[str, Any], *, run_id: str | None = None,
              source_id: str | None = None, document_id: str | None = None) -> dict[str, Any]:
        """Record one statement outside a runtime page (imports, refresh, tests); same rules as a page."""
        self.conn.execute("BEGIN")
        try:
            result = self._apply(namespace, dict(statement), run_id=run_id, source_id=source_id,
                                 document_id=document_id, retrieved_at_ms=self.now())
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def _apply(self, namespace: str, statement: dict[str, Any], *, run_id, source_id, document_id,
               retrieved_at_ms: int) -> dict[str, Any]:
        statement = validate_statement(statement)
        source, native = statement["source"], native_key(statement["source"], statement["native_id"])
        record_id = record_id_for(namespace, source, native)
        revision = statement["revision"]
        content_digest = digest(_content(statement))
        head = self.conn.execute("SELECT record_type FROM media_records WHERE namespace=? AND record_id=?",
                                 [namespace, record_id]).fetchone()
        if head and head[0] != statement["record_type"]:
            raise MediaMetadataError("level_change", f"{source}:{native} is a {head[0]}; a revision never changes "
                                                     f"its level to {statement['record_type']}")
        stored = self.conn.execute(
            "SELECT revision_id, content_digest FROM media_revisions WHERE namespace=? AND record_id=? AND marker=? "
            "AND basis=?", [namespace, record_id, revision["marker"], revision["basis"]]).fetchone()
        if stored:
            if stored[1] == content_digest:
                return {"status": "unchanged", "record_id": record_id, "revision_id": stored[0]}
            conflict_id = "media-conflict:" + digest([namespace, record_id, revision["marker"], content_digest])[:24]
            self.conn.execute("INSERT OR IGNORE INTO media_revision_conflicts VALUES (?,?,?,?,?,?,?,?)",
                              [namespace, conflict_id, record_id, revision["marker"], stored[0], content_digest,
                               canonical(statement), retrieved_at_ms])
            return {"status": "conflict", "record_id": record_id, "revision_id": stored[0],
                    "conflict_id": conflict_id}
        if head is None:
            self.conn.execute("INSERT INTO media_records VALUES (?,?,?,?,?,?)",
                              [namespace, record_id, source, native, statement["record_type"], retrieved_at_ms])
        seq = int(self.conn.execute("SELECT nextval('media_metadata_seq')").fetchone()[0])
        order_key = revision["order"]
        current = self.conn.execute(
            "SELECT r.revision_id, r.order_key FROM media_current c JOIN media_revisions r ON "
            "r.namespace=c.namespace AND r.revision_id=c.revision_id WHERE c.namespace=? AND c.record_id=?",
            [namespace, record_id]).fetchone()
        # Ordered markers (OL revision, Wikidata revid, MARC 005) decide by the provider's order; unordered markers
        # (MusicBrainz core digests) by acquisition.
        newer = current is None or order_key is None or current[1] is None or order_key >= current[1]
        revision_id = "media-revision:" + digest([namespace, record_id, revision["basis"], revision["marker"]])[:24]
        self.conn.execute(
            "INSERT INTO media_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, record_id, seq, revision["marker"], revision["basis"], order_key,
             revision.get("date"), content_digest, statement["status"], retrieved_at_ms, run_id, source_id,
             document_id, canonical(statement)])
        for position, item in enumerate(statement["identifiers"]):
            norm = normalize_identifier(item["scheme"], item["value"])
            self.conn.execute("INSERT INTO media_identifiers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, revision_id, record_id, position, item["scheme"], item["value"],
                               norm["key"], norm["valid"], item["role"], item.get("property"), item.get("rank")])
        if newer:
            self.conn.execute("INSERT OR REPLACE INTO media_current VALUES (?,?,?,?)",
                              [namespace, record_id, revision_id, retrieved_at_ms])
        if statement["status"] in {"redirected", "deleted", "deprecated"}:
            self._redirect(namespace, statement, revision_id, retrieved_at_ms)
        return {"status": ("created" if current is None else "revised") if newer else "history",
                "record_id": record_id, "revision_id": revision_id}

    def _redirect(self, namespace: str, statement: Mapping[str, Any], revision_id: str, observed: int) -> None:
        from src.kb.entity_history import EntityHistoryStore

        source = statement["source"]
        old = native_key(source, statement["native_id"])
        target = statement.get("redirect_to")
        target = native_key(source, target) if target else None
        kind = {"redirected": "redirect", "deleted": "deleted", "deprecated": "deprecated"}[statement["status"]]
        decision_id = None
        if target:
            history = EntityHistoryStore(self.conn, now=self.now)
            left, right = f"media:{source}:{old}", f"media:{source}:{target}"
            for entity in (left, right):
                history.register_entity(namespace, entity, [f"{source}:{entity.rsplit(':', 1)[1]}"],
                                        principal_id=f"provider:{source}", scopes=_ENTITY_HISTORY_SCOPES)
            decided = history.decide(
                namespace, "redirect", [left, right],
                {"source": source, "from": old, "to": target, "revision_id": revision_id,
                 "revision_marker": statement["revision"]["marker"], "status": statement["status"],
                 "provenance": {"producer": "science.media-metadata", "asserted_by": source},
                 "policy": {"merge": False, "note": "the provider's redirect, recorded as identity history"}},
                reviewer_id=f"provider:{source}", principal_id=f"provider:{source}", scopes=_ENTITY_HISTORY_SCOPES,
                event_key=f"media-redirect:{namespace}:{source}:{old}:{target}")
            decision_id = decided["decision_id"]
        self.conn.execute("INSERT OR IGNORE INTO media_redirects VALUES (?,?,?,?,?,?,?,?)",
                          [namespace, source, old, target, kind, revision_id, decision_id, observed])

    def import_dump_slice(self, namespace: str, text: str, *, dump_name: str, dump_date: str, keys: Sequence[str],
                          scopes: Iterable[str], max_lines: int = 500) -> dict[str, Any]:
        """Import an operator-cut Open Library dump slice (TSV: type, key, revision, last_modified, JSON).

        Only the named keys are accepted, at most ``max_lines`` lines; the full dump is never read here. The receipt
        records the dump name and date, the slice digest, the requested keys and which were found."""
        from src.ingestion.media_metadata_sources import parse_open_library

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not re.fullmatch(r"ol_dump_(works|editions|authors)_\d{4}-\d{2}-\d{2}", str(dump_name)) or \
                not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(dump_date)) or dump_date not in dump_name:
            raise MediaMetadataError("invalid_slice", "name the pinned dump (ol_dump_<kind>_YYYY-MM-DD) and its date")
        wanted = {str(k).strip() for k in keys if str(k).strip()}
        if not 1 <= len(wanted) <= max_lines:
            raise MediaMetadataError("unbounded_slice", f"a dump slice names 1-{max_lines} keys")
        lines = [line for line in str(text).splitlines() if line.strip()]
        if len(lines) > max_lines:
            raise MediaMetadataError("unbounded_slice", f"a dump slice has at most {max_lines} lines")
        slice_sha = hashlib.sha256(str(text).encode()).hexdigest()
        found, counts = [], {"created": 0, "revised": 0, "history": 0, "unchanged": 0, "conflict": 0}
        parsed = []
        for number, line in enumerate(lines, 1):
            parts = line.split("\t")
            if len(parts) != 5:
                raise MediaMetadataError("invalid_slice", f"line {number} is not a five-column dump row")
            kind, key, revision, _modified, payload = parts
            if key not in wanted:
                raise MediaMetadataError("unbounded_slice", f"line {number} ({key}) is not a requested key")
            try:
                body = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise MediaMetadataError("invalid_slice", f"line {number} has no JSON record") from exc
            if body.get("key") != key or str(body.get("revision")) != revision or \
                    dict(body.get("type") or {}).get("key") != kind:
                raise MediaMetadataError("invalid_slice", f"line {number}: columns disagree with the record")
            parsed.append((key, body))
        run_id = f"dump-slice:{dump_name}:{slice_sha[:16]}"
        self.conn.execute("BEGIN")
        try:
            for key, body in parsed:
                statements, _ = parse_open_library(body, f"https://openlibrary.org{key}.json")
                for statement in statements:
                    counts[self._apply(namespace, statement, run_id=run_id, source_id=f"dump:{dump_name}",
                                       document_id=None, retrieved_at_ms=self.now())["status"]] += 1
                found.append(key)
            receipt = {"dump_name": dump_name, "dump_date": dump_date, "slice_sha256": slice_sha,
                       "requested_keys": sorted(wanted), "found_keys": sorted(set(found)),
                       "missing_keys": sorted(wanted - set(found)), "lines": len(lines), "counts": counts,
                       "note": "operator-cut slice of one pinned dump; the full dump is never mirrored"}
            import_id = "media-dump-import:" + digest([namespace, receipt])[:24]
            self.conn.execute("INSERT OR IGNORE INTO media_dump_imports VALUES (?,?,?,?,?,?,?)",
                              [namespace, import_id, dump_name, dump_date, slice_sha, canonical(receipt),
                               self.now()])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"import_id": import_id, **receipt}

    # ------------------------------------------------------------------ reads

    def record(self, namespace: str, record_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT record_id, source, native_id, record_type FROM media_records WHERE namespace=? AND record_id=?",
            [namespace, record_id]).fetchone()
        if row is None:
            raise MediaMetadataError("not_found", "media record is not visible in this namespace")
        return dict(zip(("record_id", "source", "native_id", "record_type"), row))

    def records(self, namespace: str, *, source: str | None = None,
                record_type: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT record_id, source, native_id, record_type FROM media_records WHERE namespace=? "
            "AND (? IS NULL OR source=?) AND (? IS NULL OR record_type=?) ORDER BY source, native_id",
            [namespace, source, source, record_type, record_type]).fetchall()
        return [dict(zip(("record_id", "source", "native_id", "record_type"), r)) for r in rows]

    def find(self, namespace: str, source: str, native_id: str) -> str | None:
        record_id = record_id_for(namespace, source, native_id)
        row = self.conn.execute("SELECT 1 FROM media_records WHERE namespace=? AND record_id=?",
                                [namespace, record_id]).fetchone()
        return record_id if row else None

    def resolve_record(self, namespace: str, reference: str) -> str:
        """A record ID or ``source:native_id``."""
        text = str(reference or "").strip()
        if text.startswith("media-record:"):
            self.record(namespace, text)
            return text
        source, _, native = text.partition(":")
        if source not in SOURCES or not native:
            raise MediaMetadataError("invalid_reference", "name a record ID or source:native_id")
        found = self.find(namespace, source, native)
        if found is None:
            raise MediaMetadataError("not_found", f"{text} is not acquired in this namespace")
        return found

    _REVISION_KEYS = ("revision_id", "record_id", "seq", "marker", "basis", "order_key", "revision_date",
                      "status", "retrieved_at_ms", "run_id", "source_id", "document_id")

    def _revision_row(self, row) -> dict[str, Any]:
        value = dict(zip(self._REVISION_KEYS, row))
        value["revision_date"] = value["revision_date"].isoformat() if value["revision_date"] else None
        value["retrieved_at"] = iso_from_ms(value["retrieved_at_ms"])
        return value

    def revisions(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {', '.join(self._REVISION_KEYS)} FROM media_revisions WHERE namespace=? AND record_id=? "
            "ORDER BY seq", [namespace, record_id]).fetchall()
        return [self._revision_row(r) for r in rows]

    def current_revision(self, namespace: str, record_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT revision_id FROM media_current WHERE namespace=? AND record_id=?",
                                [namespace, record_id]).fetchone()
        if row is None:
            return None
        return next(r for r in self.revisions(namespace, record_id) if r["revision_id"] == row[0])

    @staticmethod
    def _effective(revision: Mapping[str, Any]) -> str:
        """The date a revision speaks for: the provider's revision date, else its retrieval date."""
        return revision["revision_date"] or (revision["retrieved_at"] or "")[:10]

    def revision_as_of(self, namespace: str, record_id: str, as_of: date | None) -> tuple[dict | None, list[dict]]:
        """(revision current at the date, revisions known at the date oldest first)."""
        chain = self.revisions(namespace, record_id)
        if as_of is None:
            return self.current_revision(namespace, record_id), chain
        known = [r for r in chain if self._effective(r) <= as_of.isoformat()]
        best = None
        for revision in known:
            if best is None or revision["order_key"] is None or best["order_key"] is None \
                    or revision["order_key"] >= best["order_key"]:
                best = revision
        return best, known

    def statement(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT statement_json FROM media_revisions WHERE namespace=? AND revision_id=?",
                                [namespace, revision_id]).fetchone()
        if row is None:
            raise MediaMetadataError("not_found", "revision is not visible in this namespace")
        return _load(row[0], {})

    def identifiers(self, namespace: str, revision_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT scheme, value, key, valid, role, property, rank FROM media_identifiers WHERE namespace=? "
            "AND revision_id=? ORDER BY position", [namespace, revision_id]).fetchall()
        return [dict(zip(("scheme", "value", "key", "valid", "role", "property", "rank"), r)) for r in rows]

    def conflicts(self, namespace: str, record_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "media_revision_conflicts"):
            return []
        rows = self.conn.execute(
            "SELECT conflict_id, record_id, marker, stored_revision_id, offered_digest, observed_at_ms FROM "
            "media_revision_conflicts WHERE namespace=? AND (? IS NULL OR record_id=?) ORDER BY conflict_id",
            [namespace, record_id, record_id]).fetchall()
        return [dict(zip(("conflict_id", "record_id", "marker", "stored_revision_id", "offered_digest",
                          "observed_at_ms"), r)) for r in rows]

    def redirects(self, namespace: str, source: str | None = None, native_id: str | None = None,
                  *, as_of: date | None = None) -> list[dict[str, Any]]:
        key = native_key(source, native_id) if source and native_id else None
        rows = self.conn.execute(
            "SELECT d.source, d.from_id, d.to_id, d.kind, d.revision_id, d.decision_id, d.observed_at_ms, "
            "r.revision_date, r.marker FROM media_redirects d JOIN media_revisions r ON r.namespace=d.namespace "
            "AND r.revision_id=d.revision_id WHERE d.namespace=? AND (? IS NULL OR d.source=?) "
            "AND (? IS NULL OR d.from_id=? OR d.to_id=?) ORDER BY d.observed_at_ms, d.from_id",
            [namespace, source, source, key, key, key]).fetchall()
        result = []
        for r in rows:
            item = dict(zip(("source", "from_id", "to_id", "kind", "revision_id", "decision_id", "observed_at_ms"),
                            r[:7]))
            item["revision_date"] = r[7].isoformat() if r[7] else None
            item["revision_marker"] = r[8]
            if as_of is not None and (item["revision_date"] or iso_from_ms(item["observed_at_ms"])[:10]) \
                    > as_of.isoformat():
                continue
            result.append(item)
        return result

    def follow(self, namespace: str, source: str, native_id: str, *, as_of: date | None = None) -> list[str]:
        """The redirect chain from a native ID to the surviving one (the ID itself when not redirected)."""
        chain, key = [native_key(source, native_id)], native_key(source, native_id)
        for _ in range(10):
            step = [r for r in self.redirects(namespace, source, key, as_of=as_of)
                    if r["from_id"] == key and r["to_id"]]
            if not step or step[-1]["to_id"] in chain:
                break
            key = step[-1]["to_id"]
            chain.append(key)
        return chain

    def citation(self, namespace: str, record_id: str, revision: Mapping[str, Any]) -> dict[str, Any]:
        head = self.record(namespace, record_id)
        statement = self.statement(namespace, revision["revision_id"])
        return {"record_id": record_id, "source": head["source"], "native_id": head["native_id"],
                "record_type": head["record_type"], "revision_id": revision["revision_id"],
                "revision_marker": revision["marker"], "revision_basis": revision["basis"],
                "revision_date": revision["revision_date"], "retrieved_at": revision["retrieved_at"],
                "status": revision["status"], "url": statement.get("url"), "licence": statement.get("licence")}


class MediaMetadataProjector:
    """Source-pack runtime projector for ``noesis-media-metadata-record-v1``."""

    def __init__(self, conn: Any) -> None:
        self.store = MediaMetadataStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("media_metadata") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        document_ids = {str(dict(item.get("metadata") or {}).get("source_pack_record_id")): str(item["document_id"])
                        for item in documents or []}
        return self.store.observe_page(run_id, source, self._namespace(source), records, documents=document_ids,
                                       page_receipt=page_receipt)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        return {"source_id": source["source_id"], "status": status}


__all__ = [
    "ANSWER_CONTRACT", "BOUNDARY", "CONTRACT", "FEATURE", "LINK_CONTRACT", "MATCH_CONTRACT", "MediaMetadataError",
    "MediaMetadataProjector", "MediaMetadataStore", "NEWS_FEATURE", "READ_SCOPE", "RECORD_TYPES", "REVIEW_SCOPE",
    "SCHEMES", "SOURCES", "WRITE_SCOPE", "detect_scheme", "isbn13_from_isbn10", "native_key", "normalize_identifier",
    "normalize_lccn", "record_id_for", "validate_statement",
]
