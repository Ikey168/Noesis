"""Research-entity records with immutable revisions and as-of lookup: the ``science.research-entities`` owner.

Records (contract ``noesis-research-entity-record-v1``, #2579 RE02 #2589) follow the revision pattern of
:mod:`src.kb.entity_history` and :mod:`src.kb.media_metadata` in namespace-scoped ``rentity_*`` tables:

* **release** - one acquired document (a ROR data-dump release, one ORCID record fetch, one DataCite DOI or query
  page, one CORDIS bulk file) with its provider, label, date and basis, file digest, evidence origin and the
  declared identifiers it did not contain. Re-acquiring an unchanged document adds nothing.
* **record** - an organisation (ROR ID), a researcher (ORCID iD), a dataset (DOI) or a project (CORDIS programme and
  project ID), keyed by provider and native id.
* **revision** - each distinct statement of a record, immutable, with the provider's revision marker (ROR release
  version, ORCID last-modified time, DataCite ``metadataVersion`` and ``updated``, CORDIS ``contentUpdateDate``), the
  effective time used for as-of answers (with its basis) and the release it came from. Corrections are new revisions;
  removals by the source (``not_in_release``, ``not_found``, ``deactivated``, ``locked``) are revisions too, never
  deletions. A statement whose content equals the revision in force is recorded as release membership only.
* **release member** - which records each release contained and whether it revised them, so every ROR release is
  a vintage of the records it holds.

Personal data follows the RE01 data-minimisation decision (:data:`src.ingestion.research_entities_sources.MINIMISATION`)
and is enforced here at write time: a researcher statement carrying any field outside ``ORCID_KEPT``, or a DataCite
creator carrying a personal name, is refused (``minimisation_violation``). Researcher records are read only with
:data:`RESEARCHER_SCOPE`. No ranking, metric, citation or usage count is ever stored (``forbidden_keys``).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.ingestion.research_entities_sources import (
    DATACITE_CREATOR_KEPT,
    EXCLUSIONS,
    FORMATS,
    LIVE_VERIFICATION,
    NEVER_SENTENCE,
    ORCID_KEPT,
    ORCID_WORK_IDS,
    PROVIDER_CONTRACTS,
    RECORD_CONTRACT,
    REMOVAL_STATUSES,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:research-entities:read"
WRITE_SCOPE = "knowledge:research-entities:write"
REVIEW_SCOPE = "knowledge:research-entities:review"
RESEARCHER_SCOPE = "knowledge:research-entities:researchers"
DEFAULT_NAMESPACE = "global"
BUNDLE = "science"
SOURCE_PACK = "research-discovery"
FEATURES = {
    "ror": "research-entities-ror",
    "orcid": "research-entities-orcid",
    "datacite": "research-entities-datacite",
    "cordis": "research-entities-cordis",
}
KIND_PROVIDER = {"organisation": "ror", "researcher": "orcid", "dataset": "datacite", "project": "cordis"}
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema/noesis-research-entity-record-v1.json"
# Keys that would carry a ranking, a metric, an inferred relation or co-authorship.
FORBIDDEN_KEYS = frozenset({
    "rank", "ranking", "score", "h_index", "hindex", "i10_index", "impact_factor", "altmetric", "metrics",
    "citation_count", "citationcount", "citations_count", "view_count", "viewcount", "download_count",
    "downloadcount", "reference_count", "referencecount", "influence", "coauthors", "co_authors", "collaborators",
    "inferred_affiliation", "inferred_affiliations",
})
_DDL = """
CREATE TABLE IF NOT EXISTS rentity_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_json TEXT NOT NULL, label TEXT NOT NULL, published_on TEXT, release_basis TEXT NOT NULL,
  http_status INTEGER NOT NULL, file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, item_count INTEGER NOT NULL,
  missing_json TEXT NOT NULL, excluded_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, url TEXT, run_id TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, sequence INTEGER NOT NULL, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS rentity_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, record_kind TEXT NOT NULL, provider TEXT NOT NULL,
  native_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS rentity_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
  marker TEXT NOT NULL, basis TEXT NOT NULL, effective_at_ms BIGINT NOT NULL, effective_basis TEXT NOT NULL,
  status TEXT NOT NULL, content_hash TEXT NOT NULL, statement_json TEXT NOT NULL, release_id TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, previous_revision_id TEXT, seq INTEGER NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS rentity_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  outcome TEXT NOT NULL, PRIMARY KEY(namespace, release_id, record_id)
);
"""


class ResearchEntitiesError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise ResearchEntitiesError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise ResearchEntitiesError("unauthorized", f"{required} is required for this part of the answer")


def may_read_researchers(scopes: Iterable[str]) -> bool:
    scopes = set(scopes)
    return "operator" in scopes or RESEARCHER_SCOPE in scopes


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a value that would carry a ranking, a metric, an inferred relation or co-authorship."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def iso_from_ms(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()


def instant_ms(value: Any) -> int | None:
    if value in (None, ""):
        return None
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.timestamp() * 1000)


def as_of_ms(value: Any) -> int | None:
    """An as-of cutoff through :func:`src.kb.temporal.parse_source_time`: epoch milliseconds or ISO-8601; a date,
    month or year means the end of that period (UTC)."""
    from src.kb.temporal import TemporalError, parse_source_time

    if value is None or value == "":
        return None
    raw = int(value) if isinstance(value, str) and value.strip().isdigit() and len(value.strip()) > 4 else value
    try:
        millis, meta = parse_source_time(raw, field="as_of")
    except TemporalError as exc:
        raise ResearchEntitiesError("invalid_request", "as_of is epoch milliseconds or an ISO-8601 date") from exc
    if meta["precision"] in {"year", "month", "day"}:
        start = datetime.fromtimestamp(millis / 1000, tz=UTC)
        if meta["precision"] == "day":
            following = start + timedelta(days=1)
        elif meta["precision"] == "month":
            following = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
        else:
            following = start.replace(year=start.year + 1)
        millis = int(following.timestamp() * 1000) - 1
    return millis


def selected_features(conn: Any) -> list[str]:
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
        ).fetchall()}
        if len(tables) < 4:
            return []
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get(BUNDLE) or [])


def feature_enabled(conn: Any, feature: str | None = None) -> bool:
    """Whether a Science ``research-entities-*`` feature (or any of them) is selected in the active plan."""
    chosen = set(selected_features(conn))
    return feature in chosen if feature else bool(chosen & set(FEATURES.values()))


# ------------------------------------------------------------------ validation and minimisation

_VALIDATOR = None


def _validator():
    global _VALIDATOR
    if _VALIDATOR is None:
        import jsonschema

        _VALIDATOR = jsonschema.Draft7Validator(json.loads(SCHEMA_PATH.read_text()))
    return _VALIDATOR


def minimisation_violations(statement: Mapping[str, Any]) -> list[str]:
    """Fields a statement carries that the RE01 data-minimisation decision does not allow."""
    body = dict(statement.get("body") or {})
    kind = statement.get("record_kind")
    out = []
    if statement.get("status") in REMOVAL_STATUSES and body:
        out.append("$.body (a removal carries no personal or record fields)")
    if kind == "researcher":
        out += [f"$.body.{k}" for k in body if k not in ORCID_KEPT["record"]]
        for index, employment in enumerate(body.get("employments") or []):
            employment = dict(employment)
            out += [f"$.body.employments[{index}].{k}" for k in employment if k not in ORCID_KEPT["employment"]]
            organisation = dict(employment.get("organisation") or {})
            out += [f"$.body.employments[{index}].organisation.{k}" for k in organisation
                    if k not in ORCID_KEPT["organisation"]]
            disambiguated = dict(organisation.get("disambiguated") or {})
            out += [f"$.body.employments[{index}].organisation.disambiguated.{k}" for k in disambiguated
                    if k not in {"source", "identifier"}]
        for index, work in enumerate(body.get("works") or []):
            work = dict(work)
            out += [f"$.body.works[{index}].{k}" for k in work if k not in ORCID_KEPT["work"]]
            for position, identifier in enumerate(work.get("identifiers") or []):
                if set(dict(identifier)) - {"type", "value"} or dict(identifier).get("type") not in ORCID_WORK_IDS:
                    out.append(f"$.body.works[{index}].identifiers[{position}]")
    elif kind == "dataset":
        for index, creator in enumerate(body.get("creators") or []):
            creator = dict(creator)
            out += [f"$.body.creators[{index}].{k}" for k in creator if k not in DATACITE_CREATOR_KEPT]
            if creator.get("name") and creator.get("name_type") != "Organizational":
                out.append(f"$.body.creators[{index}].name (a personal creator name)")
    return out


def validate_statement(statement: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(canonical(statement))
    forbidden = forbidden_keys(value)
    if forbidden:
        raise ResearchEntitiesError("forbidden_field", "research-entity records carry no ranking, metric, inferred "
                                    "relation or co-authorship: " + ", ".join(forbidden[:5]))
    violations = minimisation_violations(value)
    if violations:
        raise ResearchEntitiesError("minimisation_violation", "fields outside the RE01 data-minimisation decision: "
                                    + ", ".join(violations[:5]), fields=violations)
    errors = sorted(_validator().iter_errors(value), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        raise ResearchEntitiesError("invalid_record", f"{'/'.join(map(str, first.path)) or 'record'}: {first.message}")
    if KIND_PROVIDER[value["record_kind"]] != value["provider"]:
        raise ResearchEntitiesError("invalid_record", f"{value['record_kind']} records come from "
                                    f"{KIND_PROVIDER[value['record_kind']]}")
    return value


def record_id_for(namespace: str, provider: str, native_id: str) -> str:
    return "rentity:" + digest([namespace, provider, str(native_id)])[:24]


def native_for(kind: str, value: Any, programme: str | None = None) -> str:
    """The native id a user names a record by (ROR ID, ORCID iD, DOI or CORDIS programme and project id)."""
    from src.ingestion.research_entities_sources import (
        ResearchEntitiesFormatError,
        doi,
        orcid_id,
        ror_id,
    )

    try:
        if kind == "organisation":
            return ror_id(value)
        if kind == "researcher":
            return orcid_id(value)
        if kind == "dataset":
            return doi(value)
    except ResearchEntitiesFormatError as exc:
        raise ResearchEntitiesError("invalid_request", str(exc)) from exc
    if kind == "project":
        raw = str(value or "").strip()
        if ":" in raw:
            return raw
        if not programme:
            raise ResearchEntitiesError("invalid_request", "a CORDIS project is named by programme and project id")
        return f"{programme}:{raw}"
    raise ResearchEntitiesError("invalid_request", f"record kind is one of {sorted(KIND_PROVIDER)}")


def _content(statement: Mapping[str, Any]) -> dict[str, Any]:
    return {"status": statement["status"], "body": statement["body"]}


class ResearchEntitiesStore:
    """Immutable, revision-addressable research-entity records per namespace."""

    _REVISION_KEYS = ("revision_id", "record_id", "revision", "marker", "basis", "effective_at_ms", "effective_basis",
                      "status", "content_hash", "release_id", "retrieved_at_ms", "previous_revision_id", "seq")

    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "rentity_revisions")

    # ------------------------------------------------------------------ writes

    def _release(self, namespace, header, *, source_id, run_id, retrieved):
        provider, fmt = str(header.get("provider") or ""), header.get("format")
        if fmt not in FORMATS or FORMATS[fmt]["provider"] != provider:
            raise ResearchEntitiesError("invalid_release", "release names a known provider and format")
        document = dict(header.get("document") or {})
        release_id = "rentity-release:" + digest([namespace, provider, source_id, header["file_sha256"], document,
                                                 header.get("http_status", 200)])[:24]
        if self.conn.execute("SELECT 1 FROM rentity_releases WHERE namespace=? AND release_id=?",
                             [namespace, release_id]).fetchone():
            return release_id, False
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM rentity_releases WHERE namespace=? AND provider=?",
            [namespace, provider]).fetchone()[0]
        origin = header.get("evidence_origin")
        origin = origin if origin in {"fixture", "operator"} else "live"
        self.conn.execute(
            "INSERT INTO rentity_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, release_id, provider, source_id, fmt, canonical(document), str(header.get("label") or ""),
             header.get("published_on"), str(header.get("release_basis") or "retrieval_time"),
             int(header.get("http_status") or 200), header["file_sha256"], header.get("content_sha256") or "",
             int(header.get("item_count") or 0), canonical(header.get("missing") or []),
             canonical(header.get("excluded_fields") or []), origin, header.get("url"), run_id, retrieved,
             int(sequence) + 1])
        return release_id, True

    def apply_release(self, namespace: str, header: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                      run_id: str, source_id: str | None, retrieved_at_ms: int | None = None) -> dict[str, Any]:
        """Record one acquired document: a revision per changed record, membership for every record it holds;
        idempotent by file and document. Every statement is validated and minimised before anything is written."""
        if int(header.get("item_count", -1)) != len(items):
            raise ResearchEntitiesError("incomplete_release", "a release carries every item it states")
        statements = [validate_statement(item) for item in items]
        provider = str(header.get("provider") or "")
        if any(s["provider"] != provider for s in statements):
            raise ResearchEntitiesError("invalid_release", "a release carries records of its own provider only")
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        counts = {"created": 0, "revised": 0, "unchanged": 0, "removed": 0, "not_held": 0}
        self.conn.execute("BEGIN")
        try:
            release_id, created = self._release(namespace, header, source_id=source_id, run_id=run_id,
                                                retrieved=retrieved)
            if not created:
                self.conn.execute("COMMIT")
                return {"release_id": release_id, "status": "unchanged", **counts}
            revision_ids = []
            for statement in statements:
                outcome, revision_id = self._apply(namespace, statement, release_id, retrieved)
                counts[outcome] += 1
                if revision_id:
                    revision_ids.append(revision_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"release_id": release_id, "status": "applied", "published_on": header.get("published_on"),
                "revision_ids": revision_ids, **counts}

    def _apply(self, namespace: str, statement: dict[str, Any], release_id: str, retrieved: int
               ) -> tuple[str, str | None]:
        provider, native = statement["provider"], statement["native_id"]
        record_id = record_id_for(namespace, provider, native)
        head = self.conn.execute("SELECT record_kind FROM rentity_records WHERE namespace=? AND record_id=?",
                                 [namespace, record_id]).fetchone()
        removal = statement["status"] in REMOVAL_STATUSES
        if head is None and removal:
            return "not_held", None
        effective = instant_ms(statement["revision"].get("effective_at"))
        effective_basis = statement["revision"]["basis"] if effective is not None else "retrieval_time"
        effective = retrieved if effective is None else effective
        content_hash = digest(_content(statement))
        in_force = None
        if head is not None:
            in_force = self.conn.execute(
                "SELECT revision_id, content_hash FROM rentity_revisions WHERE namespace=? AND record_id=? AND "
                "effective_at_ms<=? ORDER BY effective_at_ms DESC, seq DESC LIMIT 1",
                [namespace, record_id, effective]).fetchone()
            if in_force and in_force[1] == content_hash:
                self.conn.execute("INSERT OR IGNORE INTO rentity_release_members VALUES (?,?,?,?,?)",
                                  [namespace, release_id, record_id, in_force[0], "unchanged"])
                return "unchanged", None
        else:
            self.conn.execute("INSERT INTO rentity_records VALUES (?,?,?,?,?,?)",
                              [namespace, record_id, statement["record_kind"], provider, native, retrieved])
        seq = self.conn.execute("SELECT coalesce(max(seq), 0) + 1 FROM rentity_revisions WHERE namespace=?",
                                [namespace]).fetchone()[0]
        number = int(self.conn.execute("SELECT count(*) + 1 FROM rentity_revisions WHERE namespace=? AND record_id=?",
                                       [namespace, record_id]).fetchone()[0])
        revision_id = "rentity-revision:" + digest([namespace, record_id, statement["revision"], content_hash])[:24]
        self.conn.execute(
            "INSERT INTO rentity_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, record_id, number, statement["revision"]["marker"], statement["revision"]["basis"],
             effective, effective_basis, statement["status"], content_hash, canonical(statement), release_id, retrieved,
             in_force[0] if in_force else None, int(seq)])
        self.conn.execute("INSERT OR IGNORE INTO rentity_release_members VALUES (?,?,?,?,?)",
                          [namespace, release_id, record_id, revision_id, "removed" if removal else "revised"])
        return ("removed" if removal else "created" if head is None else "revised"), revision_id

    # ------------------------------------------------------------------ reads

    def record(self, namespace: str, record_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT record_id, record_kind, provider, native_id, created_at_ms FROM rentity_records WHERE namespace=? "
            "AND record_id=?", [namespace, record_id]).fetchone() if self.ready() else None
        if row is None:
            raise ResearchEntitiesError("not_found", "record is not held in this namespace")
        return dict(zip(("record_id", "record_kind", "provider", "native_id", "created_at_ms"), row))

    def find(self, namespace: str, kind: str, native_id: str) -> str | None:
        if not self.ready():
            return None
        record_id = record_id_for(namespace, KIND_PROVIDER[kind], native_id)
        row = self.conn.execute("SELECT 1 FROM rentity_records WHERE namespace=? AND record_id=?",
                                [namespace, record_id]).fetchone()
        return record_id if row else None

    def records(self, namespace: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id, record_kind, provider, native_id, created_at_ms FROM rentity_records WHERE namespace=? "
            "AND (? IS NULL OR record_kind=?) ORDER BY record_kind, native_id", [namespace, kind, kind]).fetchall()
        return [dict(zip(("record_id", "record_kind", "provider", "native_id", "created_at_ms"), r)) for r in rows]

    def _revision_row(self, row: Sequence[Any]) -> dict[str, Any]:
        value = dict(zip(self._REVISION_KEYS, row))
        value["effective_at"] = iso_from_ms(value["effective_at_ms"])
        value["retrieved_at"] = iso_from_ms(value["retrieved_at_ms"])
        return value

    def revisions(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        """Every revision of a record, oldest first by effective time."""
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {', '.join(self._REVISION_KEYS)} FROM rentity_revisions WHERE namespace=? AND record_id=? "
            "ORDER BY effective_at_ms, seq", [namespace, record_id]).fetchall()
        return [self._revision_row(r) for r in rows]

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            f"SELECT {', '.join(self._REVISION_KEYS)} FROM rentity_revisions WHERE namespace=? AND revision_id=?",
            [namespace, revision_id]).fetchone()
        if row is None:
            raise ResearchEntitiesError("not_found", "revision is not visible in this namespace")
        return self._revision_row(row)

    def revision_as_of(self, namespace: str, record_id: str, cutoff: int | None
                       ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """(the revision in force at the cutoff, every revision effective by then oldest first)."""
        chain = self.revisions(namespace, record_id)
        known = chain if cutoff is None else [r for r in chain if r["effective_at_ms"] <= cutoff]
        return (known[-1] if known else None), known

    def statement(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT statement_json FROM rentity_revisions WHERE namespace=? AND revision_id=?",
                                [namespace, revision_id]).fetchone()
        if row is None:
            raise ResearchEntitiesError("not_found", "revision is not visible in this namespace")
        return json.loads(row[0])

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, document_json, label, published_on, release_basis, "
            "http_status, file_sha256, missing_json, excluded_json, evidence_origin, url, retrieved_at_ms, run_id "
            "FROM rentity_releases WHERE namespace=? AND release_id=?", [namespace, release_id]).fetchone()
        if row is None:
            raise ResearchEntitiesError("not_found", "release is not visible in this namespace")
        keys = ("release_id", "provider", "source_id", "format", "document", "label", "published_on",
                "release_basis", "http_status", "file_sha256", "missing", "excluded_fields", "evidence_origin", "url",
                "retrieved_at_ms", "run_id")
        value = dict(zip(keys, row))
        for key in ("document", "missing", "excluded_fields"):
            value[key] = json.loads(value[key])
        value["retrieved_at"] = iso_from_ms(value["retrieved_at_ms"])
        value["attribution"] = PROVIDER_CONTRACTS[value["provider"]]["attribution"]
        value["live_verification"] = LIVE_VERIFICATION[value["provider"]]["status"]
        return value

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "rentity_releases"):
            return []
        rows = self.conn.execute("SELECT release_id FROM rentity_releases WHERE namespace=? AND (? IS NULL OR "
                                 "provider=?) ORDER BY provider, sequence", [namespace, provider, provider]).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def memberships(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        """The releases that contained a record, with the revision in force in each (the ROR vintages)."""
        rows = self.conn.execute(
            "SELECT m.release_id, m.revision_id, m.outcome, r.label, r.published_on, r.sequence FROM "
            "rentity_release_members m JOIN rentity_releases r ON r.namespace=m.namespace AND "
            "r.release_id=m.release_id WHERE m.namespace=? AND m.record_id=? ORDER BY r.published_on, r.sequence",
            [namespace, record_id]).fetchall()
        return [dict(zip(("release_id", "revision_id", "outcome", "release_label", "published_on"), r[:5]))
                for r in rows]

    def citation(self, namespace: str, revision: Mapping[str, Any], *, cutoff: int | None = None) -> dict[str, Any]:
        """Source, record revision and as-of time of one revision, as every answer cites it."""
        head = self.record(namespace, revision["record_id"])
        statement = self.statement(namespace, revision["revision_id"])
        release = self.release(namespace, revision["release_id"])
        return {
            "provider": head["provider"], "record_kind": head["record_kind"], "native_id": head["native_id"],
            "record_id": head["record_id"], "revision_id": revision["revision_id"], "revision": revision["revision"],
            "revision_marker": revision["marker"], "revision_basis": revision["basis"],
            "effective_at": revision["effective_at"], "effective_basis": revision["effective_basis"],
            "retrieved_at": revision["retrieved_at"], "status": revision["status"],
            "as_of": iso_from_ms(cutoff) if cutoff is not None else "latest",
            "release_id": release["release_id"], "release_label": release["label"],
            "published_on": release["published_on"], "url": statement["citation"].get("url"),
            "record_url": statement["citation"].get("record_url"), "licence": statement["citation"].get("licence"),
            "attribution": release["attribution"], "evidence_origin": release["evidence_origin"],
            "live_verification": release["live_verification"],
        }

    def latest_retrieval_ms(self, namespace: str) -> int | None:
        if not table_exists(self.conn, "rentity_releases"):
            return None
        row = self.conn.execute("SELECT max(retrieved_at_ms) FROM rentity_releases WHERE namespace=?",
                                [namespace]).fetchone()
        return None if row is None or row[0] is None else int(row[0])


def researcher_view(statement: Mapping[str, Any], *, withhold_name: bool) -> dict[str, Any]:
    """A researcher revision as answers show it: only minimisation-allowed fields, every assertion labelled as
    ORCID-asserted and the display name withheld once ORCID reports the record gone."""
    body = dict(statement.get("body") or {})
    return {
        "orcid": body.get("orcid") or statement.get("native_id"),
        "display_name": None if withhold_name else body.get("display_name"),
        "display_name_withheld": bool(withhold_name and body.get("display_name")),
        "employments": [{**e, "assertion": "orcid-asserted", "verified": False} for e in body.get("employments") or []],
        "works": [{**w, "assertion": "orcid-asserted", "authorship_verified": False} for w in body.get("works") or []],
    }


class ResearchEntitiesProjector:
    """Source-pack runtime projector for ``noesis-research-entity-record-v1`` pages (one document per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = ResearchEntitiesStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("research_entities") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = dict(item.get("research_entities_release") or {}), item.get("research_entity")
            if not header or not isinstance(body, Mapping):
                raise ResearchEntitiesError("invalid_record", "page record is not a research-entity release item")
            groups.setdefault(header["file_sha256"] + canonical(header.get("document")), (header, []))[1].append(
                dict(body))
        namespace = self._namespace(source)
        return [self.store.apply_release(namespace, header, items, run_id=run_id, source_id=source["source_id"])
                for header, items in groups.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, label, published_on FROM rentity_releases WHERE namespace=? AND source_id=? "
            "ORDER BY sequence DESC LIMIT 1", [self._namespace(source), source["source_id"]]).fetchone()
        return {"status": status, "latest_release_id": row[0] if row else None,
                "latest_label": row[1] if row else None, "latest_published_on": row[2] if row else None}


def readiness(conn: Any) -> dict[str, Any]:
    store = ResearchEntitiesStore(conn, initialize=False)
    ready = store.ready() and table_exists(conn, "rentity_releases")
    selected = set(selected_features(conn))
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = records = 0
        if ready:
            releases = int(conn.execute("SELECT count(*) FROM rentity_releases WHERE provider=?",
                                        [provider]).fetchone()[0])
            records = int(conn.execute("SELECT count(*) FROM rentity_records WHERE provider=?",
                                       [provider]).fetchone()[0])
        providers[provider] = {"feature": FEATURES[provider], "selected": FEATURES[provider] in selected,
                               "delivers": contract["delivers"], "access_decision": contract["access_decision"],
                               "live_verification": LIVE_VERIFICATION[provider]["status"], "releases": releases,
                               "records": records}
    return {
        "features": sorted(FEATURES.values()),
        "selected": sorted(selected & set(FEATURES.values())),
        "stores_ready": ready,
        "providers": providers,
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
        "is live until a dated run verifies it",
    }


__all__ = [
    "CONTRACT",
    "FEATURES",
    "READ_SCOPE",
    "RESEARCHER_SCOPE",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "ResearchEntitiesError",
    "ResearchEntitiesProjector",
    "ResearchEntitiesStore",
    "as_of_ms",
    "authorize",
    "feature_enabled",
    "forbidden_keys",
    "minimisation_violations",
    "readiness",
    "record_id_for",
    "researcher_view",
    "validate_statement",
]
