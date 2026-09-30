"""Research-entity records: organisations, researchers, datasets and projects with revisions and as-of lookup (#2579,
RE02).

Acquired ``noesis-research-entity-record-v1`` records (from :mod:`src.ingestion.research_entities_sources`) are kept
per source and record key with an append-only revision log, following the :mod:`src.kb.entity_history` pattern of
never rewriting what was recorded:

* an **organisation** (ROR id) gets a revision per dump release that changes it; withdrawn and inactive records stay
  with their successor relationships; a release that no longer lists a declared id is reported by the acquisition
  receipt, never recorded as a deletion;
* a **researcher** (ORCID iD) gets a revision per changed public record, dated by the record's last-modified time; a
  deactivated or deprecated record is a withdrawn revision without personal fields;
* a **dataset** (DOI) gets a revision per metadata version; a DOI no longer served is an unavailable revision;
* a **project** (CORDIS programme and id) gets a revision per changed export row set (participants and
  contributions as published).

Every revision carries its source, native revision, the source's own as-of time and the observation time.
:meth:`ResearchEntityStore.as_of` answers the revision in force at a date (the latest revision whose source time is on
or before it). A replayed response already on record adds nothing; an older response observed later is logged as
``older-observation`` and never becomes current.

**Minimisation (RE01) is enforced at write time.** :meth:`ResearchEntityStore.project` refuses a researcher record
with a field outside the allowed set, a dataset person carrying a name, or a project participant carrying a street
address or contact form, with ``minimisation_violation``, before anything is written. Researcher records are returned
only to principals holding :data:`RESEARCHER_SCOPE`. Nothing here ranks, scores or disambiguates by name.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.ingestion.research_entities_sources import (
    FEATURES,
    MINIMISATION,
    RECORD_CONTRACT,
    RECORD_KINDS,
    REVIEW_BOUNDARY,
    canonical,
    digest,
    minimisation_violations,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:science:research-entities:read"
WRITE_SCOPE = "knowledge:science:research-entities:write"
RESEARCHER_SCOPE = "knowledge:science:research-entities:researchers:read"
DEFAULT_NAMESPACE = "global"
CHANGES = ("new", "revised", "unchanged", "older-observation")
EXCLUSIONS = ("researcher rankings or metrics", "inference of affiliation from co-authorship",
              "author disambiguation by name", "personal data beyond the RE01 minimisation decision")
# Keys that would carry a ranking, a metric or an inferred affiliation; no answer may contain them.
FORBIDDEN_ANSWER_KEYS = frozenset({
    "rank", "ranking", "score", "h_index", "hindex", "i10_index", "metric", "metrics", "impact", "impact_factor",
    "citation_count", "productivity", "inferred_affiliation", "coauthor_affiliation", "same_person", "disambiguated_as",
})

_DDL = """
CREATE TABLE IF NOT EXISTS research_entity_records (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL, provider TEXT NOT NULL,
  record_kind TEXT NOT NULL, native_id TEXT NOT NULL, current_revision_id TEXT NOT NULL, revision_count INTEGER NOT NULL,
  first_run_id TEXT NOT NULL, first_observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, source_id, record_key)
);
CREATE TABLE IF NOT EXISTS research_entity_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL,
  revision_no INTEGER NOT NULL, previous_revision_id TEXT, change TEXT NOT NULL, content_hash TEXT NOT NULL,
  native_revision TEXT, revision_order TEXT NOT NULL, source_as_of TEXT, effective_at TEXT NOT NULL,
  status TEXT NOT NULL, record_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, run_id TEXT NOT NULL,
  receipt_id TEXT, observed_at_ms BIGINT NOT NULL, redacted BOOLEAN NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS research_entity_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, outcome_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
CREATE TABLE IF NOT EXISTS research_entity_redactions (
  namespace TEXT NOT NULL, redaction_id TEXT NOT NULL, record_key TEXT NOT NULL, reason TEXT NOT NULL,
  revisions_json TEXT NOT NULL, redacted_by TEXT NOT NULL, redacted_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, redaction_id)
);
"""
_REVISION_COLUMNS = ("revision_id", "source_id", "record_key", "revision_no", "previous_revision_id", "change",
                     "content_hash", "native_revision", "revision_order", "source_as_of", "effective_at", "status",
                     "record_json", "evidence_origin", "run_id", "receipt_id", "observed_at_ms", "redacted")


class ResearchEntityError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise ResearchEntityError("unauthorized", f"{required} and namespace access are required")


def may_read_researchers(scopes: Iterable[str]) -> bool:
    scopes = set(scopes)
    return "operator" in scopes or RESEARCHER_SCOPE in scopes


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether one of the Science bundle's optional ``research-entities-*`` features is selected (defaults to off)."""
    try:
        if not all(table_exists(conn, t) for t in ("composition_authority", "composition_active",
                                                    "composition_generations", "composition_plans")):
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='science'").fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return feature in ((plan.get("features") or {}).get("science") or [])


def parse_moment(value: Any, *, end_of_day: bool = True) -> datetime | None:
    """A date (its end, by default) or an ISO timestamp as an aware UTC datetime."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if len(raw) == 10:
        raw += "T23:59:59.999999+00:00" if end_of_day else "T00:00:00+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ResearchEntityError("invalid_request", f"not a date or timestamp: {value!r}") from exc
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def validate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Structural checks plus the RE01 minimisation guard; returns a canonical copy."""
    record = json.loads(canonical(record))
    if record.get("contract") != CONTRACT or record.get("record_kind") not in RECORD_KINDS:
        raise ResearchEntityError("invalid_record", "not a research-entity record")
    if not str(record.get("record_key") or "").startswith("research-entities:"):
        raise ResearchEntityError("invalid_record", "record keys are research-entities:* keys")
    if not str(record.get("locator") or "").startswith("https://"):
        raise ResearchEntityError("invalid_record", "every record cites an HTTPS locator")
    if (record.get("minimisation") or {}).get("policy") != MINIMISATION["policy"]:
        raise ResearchEntityError("invalid_record", "every record states the RE01 minimisation policy")
    if set(record) & FORBIDDEN_ANSWER_KEYS or set(record.get("fields") or {}) & FORBIDDEN_ANSWER_KEYS:
        raise ResearchEntityError("invalid_record", "records carry no ranking, metric or inferred affiliation")
    violations = minimisation_violations(record)
    if violations:
        raise ResearchEntityError("minimisation_violation", "personal fields outside the RE01 decision may not be "
                                  "stored", paths=violations)
    return record


def citation(revision: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_id": revision["source_id"], "provider": record.get("provider"), "record_key": revision["record_key"],
        "revision_id": revision["revision_id"], "revision_no": revision["revision_no"],
        "native_revision": revision["native_revision"], "source_as_of": revision["source_as_of"],
        "release": record.get("release"), "locator": record.get("locator"),
        "observed_at_ms": revision["observed_at_ms"], "evidence_origin": revision["evidence_origin"],
    }


def _view(row: Sequence[Any], head: Mapping[str, Any]) -> dict[str, Any]:
    revision = dict(zip(_REVISION_COLUMNS, row, strict=True))
    record = json.loads(revision.pop("record_json"))
    revision["redacted"] = bool(revision["redacted"])
    return {**head, **revision, "record_kind": record["record_kind"], "provider": record["provider"],
            "record": record, "citation": citation(revision, record)}


class ResearchEntityStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "research_entity_revisions")

    # ------------------------------------------------------------------ writes

    def project(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, source_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        """Append revisions for what changed; idempotent (re-projecting an unchanged record adds nothing)."""
        checked = [validate(r) for r in records]  # refuse the whole page before writing anything
        keys = [r["record_key"] for r in checked]
        if len(set(keys)) != len(keys):
            raise ResearchEntityError("invalid_record", "a page repeats a record")
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        receipt = dict(receipt or {})
        receipt_id = "re-receipt:" + digest([namespace, source_id, run_id, receipt, keys])[:24]
        counts = dict.fromkeys(CHANGES, 0)
        changes: dict[str, str] = {}
        self.conn.execute("BEGIN")
        try:
            for record in sorted(checked, key=lambda r: r["record_key"]):
                change = self._observe(namespace, record, source_id, run_id, receipt_id, observed)
                counts[change] += 1
                changes[record["record_key"]] = change
            self.conn.execute(
                "INSERT OR IGNORE INTO research_entity_receipts VALUES (?,?,?,?,?,?,?)",
                [namespace, receipt_id, run_id, source_id, canonical(receipt), canonical(counts), observed])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"receipt_id": receipt_id, "counts": counts, "records": len(checked), "changes": changes}

    def _observe(self, namespace, record, source_id, run_id, receipt_id, observed) -> str:
        key = record["record_key"]
        origin = "fixture" if record.get("evidence_origin") == "fixture" else "live"
        body = {k: v for k, v in record.items() if k != "evidence_origin"}
        content_hash = digest(body)
        head = self.conn.execute(
            "SELECT r.current_revision_id, r.revision_count, v.content_hash, v.revision_order "
            "FROM research_entity_records r JOIN research_entity_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? AND r.source_id=? AND r.record_key=?",
            [namespace, source_id, key]).fetchone()
        order = str(record.get("revision_order") or "")
        if head is not None:
            if head[2] == content_hash:
                return "unchanged"
            if self.conn.execute(
                    "SELECT 1 FROM research_entity_revisions WHERE namespace=? AND source_id=? AND record_key=? AND "
                    "content_hash=?", [namespace, source_id, key, content_hash]).fetchone():
                return "unchanged"  # a replayed older response: already on record, never re-applied
            change = "older-observation" if order and head[3] and order < str(head[3]) else "revised"
        else:
            change = "new"
        observed_iso = datetime.fromtimestamp(observed / 1000, tz=UTC).isoformat()
        effective = record.get("as_of") or observed_iso
        revision_no = 1 if head is None else int(head[1]) + 1
        revision_id = "re-rev:" + digest([namespace, source_id, key, revision_no, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO research_entity_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, source_id, key, revision_no, None if head is None else head[0], change,
             content_hash, record.get("native_revision"), order, record.get("as_of"), effective,
             record.get("status") or "unknown", canonical(record), origin, run_id, receipt_id, observed, False])
        if head is None:
            self.conn.execute(
                "INSERT INTO research_entity_records VALUES (?,?,?,?,?,?,?,?,?,?)",
                [namespace, source_id, key, record["provider"], record["record_kind"], record["native_id"],
                 revision_id, 1, run_id, observed])
        elif change == "older-observation":
            self.conn.execute("UPDATE research_entity_records SET revision_count=? WHERE namespace=? AND "
                              "source_id=? AND record_key=?", [revision_no, namespace, source_id, key])
        else:
            self.conn.execute(
                "UPDATE research_entity_records SET current_revision_id=?, revision_count=? WHERE namespace=? AND "
                "source_id=? AND record_key=?", [revision_id, revision_no, namespace, source_id, key])
        return change

    def redact_researcher(self, namespace: str, orcid_key: str, reason: str, *, principal_id: str,
                          scopes: Iterable[str]) -> dict[str, Any]:
        """Remove the public name from every stored revision of one researcher (RE01 retention) and log it."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not may_read_researchers(scopes):
            raise ResearchEntityError("unauthorized", f"{RESEARCHER_SCOPE} is required to redact a researcher")
        if not orcid_key.startswith("research-entities:orcid:") or not str(reason or "").strip():
            raise ResearchEntityError("invalid_request", "redact a researcher record key with a reason")
        rows = self.conn.execute("SELECT revision_id, record_json FROM research_entity_revisions WHERE namespace=? "
                                 "AND record_key=? ORDER BY revision_no", [namespace, orcid_key]).fetchall()
        if not rows:
            raise ResearchEntityError("not_found", "no researcher record with that key")
        touched = []
        for revision_id, raw in rows:
            record = json.loads(raw)
            record["fields"]["name"] = None
            record["fields"]["name_status"] = "redacted"
            record["title"] = record["fields"]["orcid"]
            self.conn.execute("UPDATE research_entity_revisions SET record_json=?, redacted=TRUE WHERE namespace=? "
                              "AND revision_id=?", [canonical(record), namespace, revision_id])
            touched.append(revision_id)
        now = self.now()
        redaction_id = "re-redaction:" + digest([namespace, orcid_key, now])[:24]
        self.conn.execute("INSERT INTO research_entity_redactions VALUES (?,?,?,?,?,?,?)",
                          [namespace, redaction_id, orcid_key, reason.strip(), canonical(touched), principal_id, now])
        return {"redaction_id": redaction_id, "record_key": orcid_key, "revisions": touched,
                "note": "the public name is removed from every stored revision; the content hashes of the "
                        "revisions still identify what was acquired"}

    # ------------------------------------------------------------------ reads

    def _visible(self, rows: list[dict[str, Any]], scopes: set[str]) -> list[dict[str, Any]]:
        if may_read_researchers(scopes):
            return rows
        return [r for r in rows if r["record_kind"] != "researcher"]

    def records(self, namespace: str, *, scopes: Iterable[str], kinds: Iterable[str] | None = None,
                record_keys: Iterable[str] | None = None, provider: str | None = None) -> list[dict[str, Any]]:
        """Current revision of every matching record (per source); researchers only with the researcher scope."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        where, params = [], []
        if provider is not None:
            where.append("AND r.provider=?")
            params.append(provider)
        for column, values in (("r.record_kind", kinds), ("r.record_key", record_keys)):
            if values is not None:
                values = sorted(set(values))
                if not values:
                    return []
                where.append(f"AND {column} IN (" + ",".join("?" * len(values)) + ")")
                params += values
        rows = self.conn.execute(
            "SELECT r.revision_count, r.first_observed_at_ms, " + ", ".join(f"v.{c}" for c in _REVISION_COLUMNS) +
            " FROM research_entity_records r JOIN research_entity_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? " + " ".join(where) +
            " ORDER BY r.record_kind, r.record_key, r.source_id", [namespace, *params]).fetchall()
        out = [_view(row[2:], {"revision_count": row[0], "first_observed_at_ms": row[1]}) for row in rows]
        return self._visible(out, scopes)

    def withheld_researchers(self, namespace: str, *, scopes: Iterable[str]) -> int:
        """How many researcher records a principal without the researcher scope does not see."""
        if may_read_researchers(scopes) or not self.ready():
            return 0
        return int(self.conn.execute("SELECT count(*) FROM research_entity_records WHERE namespace=? AND "
                                     "record_kind='researcher'", [namespace]).fetchone()[0])

    def history(self, namespace: str, record_key: str, *, scopes: Iterable[str], source_id: str | None = None
                ) -> list[dict[str, Any]]:
        """Every revision of a record in arrival order, including older observations that never became current."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM research_entity_revisions WHERE namespace=? AND "
            "record_key=? AND (? IS NULL OR source_id=?) ORDER BY source_id, revision_no",
            [namespace, record_key, source_id, source_id]).fetchall()
        return self._visible([_view(row, {}) for row in rows], scopes)

    def revision(self, namespace: str, revision_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        row = self.conn.execute("SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM research_entity_revisions "
                                "WHERE namespace=? AND revision_id=?", [namespace, revision_id]).fetchone()
        if row is None:
            raise ResearchEntityError("not_found", "revision is not visible in this namespace")
        view = _view(row, {})
        if not self._visible([view], scopes):
            raise ResearchEntityError("unauthorized", f"{RESEARCHER_SCOPE} is required to read researcher records")
        return view

    def as_of(self, namespace: str, record_key: str, when: Any, *, scopes: Iterable[str]) -> dict[str, Any] | None:
        """The revision in force at ``when`` (a date means its end): the latest revision whose source time - or
        observation time when the source states none, as for a removal - is on or before it.

        ``None`` when the record has no revision in force then (not yet published, or not on record)."""
        cutoff = parse_moment(when) if when is not None else None
        candidates = [v for v in self.history(namespace, record_key, scopes=scopes)
                      if cutoff is None or parse_moment(v["effective_at"]) <= cutoff]
        if not candidates:
            return None
        current = {r["revision_id"] for r in self.records(namespace, scopes=scopes, record_keys=[record_key])}
        if cutoff is None:
            chosen = [v for v in candidates if v["revision_id"] in current]
            return chosen[0] if chosen else None
        candidates.sort(key=lambda v: (parse_moment(v["effective_at"]), v["revision_order"] or "", v["revision_no"]))
        return candidates[-1]

    def receipts(self, namespace: str, run_id: str | None = None, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, run_id, source_id, receipt_json, outcome_json, recorded_at_ms FROM "
            "research_entity_receipts WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY recorded_at_ms, "
            "receipt_id", [namespace, run_id, run_id]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "receipt": json.loads(r[3]),
                 "counts": json.loads(r[4]), "recorded_at_ms": r[5]} for r in rows]


class ResearchEntityProjector:
    """Source-pack runtime projector for ``noesis-research-entity-record-v1`` pages (one selection unit per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = ResearchEntityStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("research_entities") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        items = []
        for item in records:
            record = item.get("research_entity_record")
            if not isinstance(record, Mapping):
                raise ResearchEntityError("invalid_record", "page record is not a research-entity record")
            items.append(dict(record))
        return [self.store.project(self._namespace(source), items, run_id=run_id, source_id=source["source_id"],
                                   receipt=dict(page_receipt or {}))]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        rows = self.store.conn.execute(
            "SELECT count(*) FROM research_entity_receipts WHERE namespace=? AND run_id=? AND source_id=?",
            [self._namespace(source), run_id, source["source_id"]]).fetchone()
        return {"status": status, "units": int(rows[0])}


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.research_entities_sources import (
        LIVE_VERIFICATION,
        PROVIDER_CONTRACTS,
    )

    store = ResearchEntityStore(conn, initialize=False)
    counts: dict[str, int] = {}
    if store.ready():
        counts = dict(conn.execute("SELECT provider, count(*) FROM research_entity_records GROUP BY provider"
                                   ).fetchall())
    return {
        "provider": "science.research-entities",
        "features": {provider: {"feature": feature, "selected": feature_enabled(conn, feature)}
                     for provider, feature in FEATURES.items()},
        "store_ready": store.ready(),
        "providers": {p: {"access_decision": c["access_decision"], "live_verification": LIVE_VERIFICATION[p]["status"],
                          "records": int(counts.get(p, 0)),
                          "degraded": ("needs the NOESIS_ORCID_PUBLIC_TOKEN client token; without it acquisition "
                                       "fails with authentication_failed") if p == "orcid" else None}
                      for p, c in PROVIDER_CONTRACTS.items()},
        "minimisation": {"policy": MINIMISATION["policy"], "researcher_scope": RESEARCHER_SCOPE},
        "review_boundary": REVIEW_BOUNDARY,
    }


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in an answer that would carry a ranking, a metric or an inferred affiliation."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_ANSWER_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found
