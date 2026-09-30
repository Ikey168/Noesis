"""Fact-check and publisher records with immutable revisions and as-of lookup (#2659, FC02).

Acquired ``noesis-fact-check-record-v1`` records (from
:mod:`src.ingestion.fact_checks_sources`) are kept per source and record key
with an append-only revision log, following the :mod:`src.kb.entity_history`
pattern of never rewriting what was recorded:

* a **fact-check** is keyed by its publisher site and review URL (canonicalised
  with the versioned ``wa-canon-v1`` rules). It keeps the publisher, review URL,
  title and date, language and every claim the page reviewed: claim text as
  quoted, claimant as named, claim date, appearance URLs and the rating text and
  any numeric rating exactly as published. A publisher's update (a changed
  review date, rating or claim) is a new revision; the same review acquired from
  Google and from Data Commons is one record key under two sources, both kept;
* a **publisher** (an IFCN signatory) keeps its status label and status date as
  published; a status change is a dated revision;
* a **removal** by the source (a record absent from a later complete release or
  listing) is an ``absent-from-*`` revision, never a deletion; an older
  observation replayed later is logged and never becomes current.

Every revision carries its source, revision number and observation time, and
every URL it cites is indexed (review, appearance and first appearance) under
the same canonicalisation for citation lookups.

**Minimisation (FC01) is enforced at write time.** A record carrying a review
author's personal name, an image, a job title or any other withheld attribute,
or any Noesis verdict or normalised rating, is refused with
``minimisation_violation`` before anything is written.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.ingestion.fact_checks_sources import (
    EXCLUSIONS,
    FEATURES,
    MINIMISATION,
    PROVIDERS,
    RECORD_CONTRACT,
    RECORD_KINDS,
    REVIEW_BOUNDARY,
    STATUSES,
    canonical_url,
    minimisation_violations,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:news:fact-checks:read"
WRITE_SCOPE = "knowledge:news:fact-checks:write"
REVIEW_SCOPE = "knowledge:news:fact-checks:review"
CLAIMANT_SCOPE = "knowledge:news:fact-checks:claimant:read"
DEFAULT_NAMESPACE = "global"
CHANGES = ("new", "revised", "unchanged", "older-observation", "absent")
# Keys that would carry a Noesis verdict or a normalised rating; no answer may contain them.
FORBIDDEN_ANSWER_KEYS = frozenset({
    "verdict", "truth", "truth_value", "veracity", "normalized_rating", "normalised_rating", "noesis_rating",
    "rating_normalised", "rating_normalized", "credibility_score", "score",
})

_DDL = """
CREATE TABLE IF NOT EXISTS fact_check_records (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL, provider TEXT NOT NULL,
  record_kind TEXT NOT NULL, unit_key TEXT NOT NULL, publisher_site TEXT, review_url_canonical TEXT,
  current_revision_id TEXT NOT NULL, revision_count INTEGER NOT NULL, first_run_id TEXT NOT NULL,
  first_observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, source_id, record_key)
);
CREATE TABLE IF NOT EXISTS fact_check_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL,
  revision_no INTEGER NOT NULL, previous_revision_id TEXT, change TEXT NOT NULL, content_hash TEXT NOT NULL,
  native_revision TEXT, revision_order TEXT NOT NULL, effective_on TEXT, status TEXT NOT NULL, vintage TEXT,
  record_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, run_id TEXT NOT NULL, receipt_id TEXT,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS fact_check_urls (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL,
  role TEXT NOT NULL, url TEXT NOT NULL, url_canonical TEXT NOT NULL,
  PRIMARY KEY(namespace, revision_id, role, url)
);
CREATE TABLE IF NOT EXISTS fact_check_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, outcome_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
_REVISION_COLUMNS = ("revision_id", "source_id", "record_key", "revision_no", "previous_revision_id", "change",
                     "content_hash", "native_revision", "revision_order", "effective_on", "status", "vintage",
                     "record_json", "evidence_origin", "run_id", "receipt_id", "observed_at_ms")
_HEAD_COLUMNS = ("provider", "record_kind", "unit_key", "publisher_site", "review_url_canonical", "revision_count",
                 "first_observed_at_ms")
_UNHASHED = ("evidence_origin", "vintage")


class FactCheckError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise FactCheckError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def iso_day(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(int(ms) / 1000, tz=UTC).date().isoformat()


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the News bundle's optional ``fact-checks-*`` feature is selected (active composition plan only)."""
    try:
        if not all(table_exists(conn, t) for t in ("composition_authority", "composition_active",
                                                    "composition_generations", "composition_plans")):
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='news'").fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return feature in ((plan.get("features") or {}).get("news") or [])


def validate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Structural checks plus the FC01 minimisation guard; returns a canonical copy."""
    record = json.loads(canonical(record))
    if record.get("contract") != CONTRACT or record.get("record_kind") not in RECORD_KINDS or \
            record.get("provider") not in PROVIDERS or record.get("status") not in STATUSES:
        raise FactCheckError("invalid_record", "not a fact-check or publisher record")
    if not str(record.get("record_key") or "").startswith("fact-checks:"):
        raise FactCheckError("invalid_record", "record keys are fact-checks:* keys")
    if not str(record.get("locator") or "").startswith("https://"):
        raise FactCheckError("invalid_record", "every record cites an HTTPS locator")
    fields = record.get("fields") or {}
    if record["record_kind"] == "fact-check":
        if not str(fields.get("review_url") or "").startswith("https://") or not (fields.get("publisher") or {}).get(
                "site"):
            raise FactCheckError("invalid_record", "a fact-check names its publisher site and review URL")
        for claim in fields.get("claims") or []:
            if "rating" not in claim or "textual_rating" not in (claim.get("rating") or {}):
                raise FactCheckError("invalid_record", "every reviewed claim states its rating as published")
    violations = minimisation_violations(record)
    if violations:
        raise FactCheckError("minimisation_violation", "withheld personal attributes or a Noesis verdict may not be "
                             "stored (FC01)", paths=violations)
    return record


def cited_urls(record: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(role, url) for every URL a record cites."""
    fields = record.get("fields") or {}
    out = []
    if fields.get("review_url"):
        out.append(("review", fields["review_url"]))
    for claim in fields.get("claims") or []:
        out += [("appearance", url) for url in claim.get("appearance_urls") or []]
        if claim.get("first_appearance_url"):
            out.append(("first-appearance", claim["first_appearance_url"]))
    if fields.get("website"):
        out.append(("website", fields["website"]))
    return sorted(set(out))


def _view(row: Sequence[Any], head: Mapping[str, Any]) -> dict[str, Any]:
    revision = dict(zip(_REVISION_COLUMNS, row))
    record = json.loads(revision.pop("record_json"))
    return {
        **head, **revision, "record": record,
        "citation": {
            "source_id": revision["source_id"], "provider": head.get("provider") or record.get("provider"),
            "record_key": revision["record_key"], "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"], "native_revision": revision["native_revision"],
            "locator": record.get("locator"), "observed_at_ms": revision["observed_at_ms"],
            "observed_at": iso_day(revision["observed_at_ms"]), "evidence_origin": revision["evidence_origin"],
            "vintage": revision["vintage"], "status": revision["status"],
        },
    }


class FactChecksStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "fact_check_revisions")

    # ------------------------------------------------------------------ writes

    def project(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, source_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        """Append revisions for what changed; idempotent (re-projecting an unchanged record adds nothing).

        When the receipt says the unit is a complete release or listing, current records of the same source and
        unit that are missing from it get an ``absent-from-*`` revision (a source removal is never a deletion).
        """
        checked = [validate(r) for r in records]  # refuse the whole page before writing anything
        keys = [r["record_key"] for r in checked]
        if len(set(keys)) != len(keys):
            raise FactCheckError("invalid_record", "a page repeats a record")
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        receipt = dict(receipt or {})
        receipt_id = "fc-receipt:" + digest([namespace, source_id, run_id, receipt, keys])[:24]
        counts = dict.fromkeys(CHANGES, 0)
        self.conn.execute("BEGIN")
        try:
            for record in sorted(checked, key=lambda r: r["record_key"]):
                counts[self._observe(namespace, record, source_id, run_id, receipt_id, observed)] += 1
            if receipt.get("complete_listing") and receipt.get("unit_key"):
                counts["absent"] += self._absent(namespace, source_id, str(receipt["unit_key"]), set(keys),
                                                 receipt.get("provider"), receipt.get("vintage"), run_id,
                                                 receipt_id, observed)
            self.conn.execute(
                "INSERT OR IGNORE INTO fact_check_receipts VALUES (?,?,?,?,?,?,?)",
                [namespace, receipt_id, run_id, source_id, canonical(receipt), canonical(counts), observed])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"receipt_id": receipt_id, "counts": counts, "records": len(checked)}

    def _append(self, namespace, source_id, key, record, change, content_hash, head, run_id, receipt_id, observed):
        revision_no = 1 if head is None else int(head[1]) + 1
        revision_id = "fc-rev:" + digest([namespace, source_id, key, revision_no, content_hash])[:24]
        origin = "fixture" if record.get("evidence_origin") == "fixture" else "live"
        self.conn.execute(
            "INSERT INTO fact_check_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, source_id, key, revision_no, None if head is None else head[0], change,
             content_hash, record.get("native_revision"), str(record.get("revision_order") or ""),
             record.get("effective_on"), record["status"], record.get("vintage"), canonical(record), origin, run_id,
             receipt_id, observed])
        for role, url in cited_urls(record):
            self.conn.execute("INSERT OR IGNORE INTO fact_check_urls VALUES (?,?,?,?,?,?,?)",
                              [namespace, revision_id, source_id, key, role, url, canonical_url(url)[0]])
        return revision_id, revision_no

    def _observe(self, namespace, record, source_id, run_id, receipt_id, observed) -> str:
        key = record["record_key"]
        content_hash = digest({k: v for k, v in record.items() if k not in _UNHASHED})
        head = self.conn.execute(
            "SELECT r.current_revision_id, r.revision_count, v.content_hash, v.revision_order FROM fact_check_records r "
            "JOIN fact_check_revisions v ON v.namespace=r.namespace AND v.revision_id=r.current_revision_id "
            "WHERE r.namespace=? AND r.source_id=? AND r.record_key=?", [namespace, source_id, key]).fetchone()
        order = str(record.get("revision_order") or "")
        if head is not None:
            if head[2] == content_hash:
                return "unchanged"
            if self.conn.execute("SELECT 1 FROM fact_check_revisions WHERE namespace=? AND source_id=? AND "
                                 "record_key=? AND content_hash=? AND status='published'",
                                 [namespace, source_id, key, content_hash]).fetchone() and \
                    order and order < str(head[3] or ""):
                return "unchanged"  # a replayed older response: already on record, never re-applied
            change = "older-observation" if order and head[3] and order < str(head[3]) else "revised"
        else:
            change = "new"
        revision_id, revision_no = self._append(namespace, source_id, key, record, change, content_hash, head,
                                                run_id, receipt_id, observed)
        if head is None:
            self.conn.execute(
                "INSERT INTO fact_check_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, source_id, key, record["provider"], record["record_kind"], record["unit_key"],
                 record.get("publisher_site"), record.get("review_url_canonical"), revision_id, 1, run_id, observed])
        elif change == "older-observation":
            self.conn.execute("UPDATE fact_check_records SET revision_count=? WHERE namespace=? AND source_id=? AND "
                              "record_key=?", [revision_no, namespace, source_id, key])
        else:
            self.conn.execute(
                "UPDATE fact_check_records SET current_revision_id=?, revision_count=?, publisher_site=? "
                "WHERE namespace=? AND source_id=? AND record_key=?",
                [revision_id, revision_no, record.get("publisher_site"), namespace, source_id, key])
        return change

    def _absent(self, namespace, source_id, unit_key, present, provider, vintage, run_id, receipt_id, observed) -> int:
        rows = self.conn.execute(
            "SELECT r.record_key, r.current_revision_id, r.revision_count, v.record_json, v.status FROM "
            "fact_check_records r JOIN fact_check_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? AND r.source_id=? AND r.unit_key=? "
            "ORDER BY r.record_key", [namespace, source_id, unit_key]).fetchall()
        added = 0
        for key, current, count, body, status in rows:
            if key in present or status != "published":
                continue
            record = json.loads(body)
            record["status"] = "absent-from-listing" if record["record_kind"] == "publisher" else "absent-from-release"
            record["vintage"] = vintage
            record["absent_since_observed"] = iso_day(observed)
            content_hash = digest({k: v for k, v in record.items() if k not in _UNHASHED})
            revision_id, revision_no = self._append(namespace, source_id, key, record, "absent", content_hash,
                                                    (current, count), run_id, receipt_id, observed)
            self.conn.execute("UPDATE fact_check_records SET current_revision_id=?, revision_count=? WHERE "
                              "namespace=? AND source_id=? AND record_key=?",
                              [revision_id, revision_no, namespace, source_id, key])
            added += 1
        del provider
        return added

    # ------------------------------------------------------------------ reads

    def _heads(self, namespace: str, where: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT " + ", ".join(f"r.{c}" for c in _HEAD_COLUMNS) + ", " +
            ", ".join(f"v.{c}" for c in _REVISION_COLUMNS) +
            " FROM fact_check_records r JOIN fact_check_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? " + where +
            " ORDER BY r.record_kind, r.record_key, r.source_id", [namespace, *params]).fetchall()
        width = len(_HEAD_COLUMNS)
        return [_view(row[width:], dict(zip(_HEAD_COLUMNS, row[:width]))) for row in rows]

    def records(self, namespace: str, *, scopes: Iterable[str], kinds: Iterable[str] | None = None,
                record_keys: Iterable[str] | None = None, publisher_site: str | None = None,
                providers: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Current revision of every matching record (per source)."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        where, params = [], []
        if publisher_site is not None:
            where.append("AND r.publisher_site=?")
            params.append(publisher_site)
        for column, values in (("r.record_kind", kinds), ("r.record_key", record_keys), ("r.provider", providers)):
            if values is not None:
                values = sorted(set(values))
                if not values:
                    return []
                where.append(f"AND {column} IN (" + ",".join("?" * len(values)) + ")")
                params += values
        return self._heads(namespace, " ".join(where), params)

    def history(self, namespace: str, record_key: str, *, scopes: Iterable[str], source_id: str | None = None
                ) -> list[dict[str, Any]]:
        """Every revision of a record in arrival order, including older observations and absences."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM fact_check_revisions WHERE namespace=? AND "
            "record_key=? AND (? IS NULL OR source_id=?) ORDER BY source_id, revision_no",
            [namespace, record_key, source_id, source_id]).fetchall()
        return [_view(row, {}) for row in rows]

    def revision(self, namespace: str, revision_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute("SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM fact_check_revisions "
                                "WHERE namespace=? AND revision_id=?", [namespace, revision_id]).fetchone()
        if row is None:
            raise FactCheckError("not_found", "revision is not visible in this namespace")
        return _view(row, {})

    @staticmethod
    def effective_day(revision: Mapping[str, Any]) -> str:
        """The day a revision took effect: the date the source published (review or status date), else observation."""
        if revision["status"] != "published":
            return iso_day(revision["observed_at_ms"]) or ""
        return revision.get("effective_on") or iso_day(revision["observed_at_ms"]) or ""

    def as_of(self, namespace: str, record_key: str, as_of: str | None, *, scopes: Iterable[str],
              source_id: str | None = None) -> list[dict[str, Any]]:
        """Per source, the revision in effect on a day: published by then (source date) and observed by then.

        Older observations never become current. A record whose revisions all take effect after the day is left
        out; one whose revision in effect is an absence is returned with that status.
        """
        day = str(as_of)[:10] if as_of else None
        by_source: dict[str, list[dict[str, Any]]] = {}
        for revision in self.history(namespace, record_key, scopes=scopes, source_id=source_id):
            if revision["change"] == "older-observation":
                continue
            by_source.setdefault(revision["source_id"], []).append(revision)
        out = []
        for source, revisions in sorted(by_source.items()):
            if day is None:
                out.append(revisions[-1])
                continue
            eligible = [r for r in revisions if self.effective_day(r) <= day]
            if eligible:
                out.append(max(eligible, key=lambda r: (self.effective_day(r), r["revision_no"])))
            del source
        return out

    def by_url(self, namespace: str, url_canonical: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """(record_key, source_id, revision_id, role) for every revision citing a canonical URL."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_key, source_id, revision_id, role, url FROM fact_check_urls WHERE namespace=? AND "
            "url_canonical=? ORDER BY record_key, source_id, revision_id, role", [namespace, url_canonical]).fetchall()
        return [dict(zip(("record_key", "source_id", "revision_id", "role", "url"), r)) for r in rows]

    def receipts(self, namespace: str, run_id: str | None = None, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, run_id, source_id, receipt_json, outcome_json, recorded_at_ms FROM "
            "fact_check_receipts WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY recorded_at_ms, receipt_id",
            [namespace, run_id, run_id]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "receipt": json.loads(r[3]),
                 "counts": json.loads(r[4]), "recorded_at_ms": r[5]} for r in rows]


class FactChecksProjector:
    """Source-pack runtime projector for ``noesis-fact-check-record-v1`` pages (one selection unit per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = FactChecksStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("fact_checks") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        items = []
        for item in records:
            record = item.get("fact_check_record")
            if not isinstance(record, Mapping):
                raise FactCheckError("invalid_record", "page record is not a fact-check record")
            items.append(dict(record))
        return [self.store.project(self._namespace(source), items, run_id=run_id, source_id=source["source_id"],
                                   receipt=dict(page_receipt or {}))]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        rows = self.store.conn.execute(
            "SELECT count(*) FROM fact_check_receipts WHERE namespace=? AND run_id=? AND source_id=?",
            [self._namespace(source), run_id, source["source_id"]]).fetchone()
        return {"status": status, "units": int(rows[0])}


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.fact_checks_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS

    store = FactChecksStore(conn, initialize=False)
    counts: dict[str, int] = {}
    if store.ready():
        counts = dict(conn.execute("SELECT provider, count(*) FROM fact_check_records GROUP BY provider").fetchall())
    return {
        "provider": "news.fact-checks",
        "features": {feature: feature_enabled(conn, feature) for feature in FEATURES.values()},
        "store_ready": store.ready(),
        "providers": {p: {"access_decision": c["access_decision"], "live": LIVE_VERIFICATION[p]["status"],
                          "records": int(counts.get(p, 0))} for p, c in PROVIDER_CONTRACTS.items()},
        "minimisation": {"policy": MINIMISATION["policy"], "query_scope": MINIMISATION["query_scope"]},
        "review_boundary": REVIEW_BOUNDARY,
        "exclusions": list(EXCLUSIONS),
    }


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in an answer that would carry a Noesis verdict or a normalised rating."""
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
