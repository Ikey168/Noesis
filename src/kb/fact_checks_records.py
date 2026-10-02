"""Fact-check and publisher records with immutable revisions and as-of lookup (#2659, FC02).

Acquired ``noesis-fact-check-record-v2`` records (from
:mod:`src.ingestion.fact_checks_sources`) are kept per source and record key
with an append-only revision log, following the
:mod:`src.kb.entity_history` pattern of never rewriting what was recorded:

* a **fact-check** is one publisher's review of one claim, keyed by the
  publisher, the review URL and the reviewed claim. It keeps the publisher, the
  review URL, title, date and language, the claim text as quoted, the claimant as
  named, the claim date and appearance URLs where published, and the rating text
  and any numeric rating with its scale exactly as published. A publisher's update
  (a new review date, a changed rating, a corrected claim) is a new revision;
* a **publisher** is an IFCN signatory keyed by its website domain; each status
  (verified, expired, under review) with its published date is a dated revision,
  so the status in force on any day can be read back;
* a record that a complete release or listing no longer carries gets an
  ``absent-from-release`` / ``absent-from-listing`` revision. Nothing is deleted:
  removals and corrections by the source are revisions.

The same review acquired from two sources (the Fact Check Tools API and a Data
Commons release) has the same record key and keeps both provenance chains; the
queries (:mod:`src.kb.fact_checks_queries`) resolve the duplicate by review URL
and cite both.

Every revision carries its source, record revision and as-of times (the
publisher's own date as ``effective_on``, the acquisition time as
``observed_at_ms``). **The FC01 minimisation decision is enforced at write
time**: :meth:`FactCheckStore.project` refuses a record carrying a claimant's
image, job title, contact details, birth date or social profile, a review's
individual author, or an unredacted social-platform appearance URL, with
``minimisation_violation``, before anything is written. No record carries a truth
verdict or a normalised rating.
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
    MINIMISATION,
    RECORD_CONTRACT,
    RECORD_KINDS,
    REVIEW_BOUNDARY,
    STATUSES,
    minimisation_violations,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:news:fact-checks:read"
WRITE_SCOPE = "knowledge:news:fact-checks:write"
REVIEW_SCOPE = "knowledge:news:fact-checks:review"
DEFAULT_NAMESPACE = "global"
BUNDLE = "news"
FEATURES = ("fact-checks-google", "fact-checks-datacommons", "fact-checks-ifcn")
CHANGES = ("new", "revised", "unchanged", "older-observation", "absent")
# Keys that would carry a Noesis verdict or a rating re-expressed on a Noesis scale; no record or answer may hold them.
FORBIDDEN_KEYS = frozenset({
    "truth_verdict", "noesis_verdict", "verdict", "normalized_rating", "normalised_rating", "rating_normalized",
    "rating_normalised", "truth_score", "veracity", "veracity_score", "credibility_score", "is_true", "is_false",
})

_DDL = """
CREATE TABLE IF NOT EXISTS fact_check_records (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL, provider TEXT NOT NULL,
  record_kind TEXT NOT NULL, publisher_key TEXT, review_key TEXT, scope_key TEXT,
  current_revision_id TEXT NOT NULL, revision_count INTEGER NOT NULL, first_run_id TEXT NOT NULL,
  first_observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, source_id, record_key)
);
CREATE TABLE IF NOT EXISTS fact_check_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL,
  revision_no INTEGER NOT NULL, previous_revision_id TEXT, change TEXT NOT NULL, content_hash TEXT NOT NULL,
  native_revision TEXT, revision_order TEXT NOT NULL, effective_on TEXT, status TEXT NOT NULL,
  record_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, run_id TEXT NOT NULL, receipt_id TEXT,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS fact_check_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, outcome_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
_REVISION_COLUMNS = ("revision_id", "source_id", "record_key", "revision_no", "previous_revision_id", "change",
                     "content_hash", "native_revision", "revision_order", "effective_on", "status", "record_json",
                     "evidence_origin", "run_id", "receipt_id", "observed_at_ms")


class FactCheckError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def day_ms(day: str, *, end: bool = True) -> int:
    """Milliseconds at the end (or start) of an ISO day, UTC."""
    moment = datetime.fromisoformat(str(day)[:10]).replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000) + (86_399_999 if end else 0)


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


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the News bundle's optional fact-checks feature is selected in the active composition plan.

    Reads the active plan only; features default to off.
    """
    try:
        if not all(table_exists(conn, t) for t in ("composition_authority", "composition_active",
                                                    "composition_generations", "composition_plans")):
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return feature in ((plan.get("features") or {}).get(BUNDLE) or [])


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a record or answer that would carry a Noesis verdict or a normalised rating."""
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


def validate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Structural checks, the no-verdict rule and the FC01 minimisation guard; returns a canonical copy."""
    record = json.loads(canonical(record))
    if record.get("contract") != CONTRACT or record.get("record_kind") not in RECORD_KINDS:
        raise FactCheckError("invalid_record", "not a fact-check record")
    if not str(record.get("record_key") or "").startswith("fact-check:"):
        raise FactCheckError("invalid_record", "record keys are fact-check:* keys")
    if not str(record.get("locator") or "").startswith("https://"):
        raise FactCheckError("invalid_record", "every record cites an HTTPS locator")
    if forbidden_keys(record):
        raise FactCheckError("assessment_forbidden", "records hold what publishers published; no truth verdict or "
                             "normalised rating is stored", paths=forbidden_keys(record))
    fields = record.get("fields") or {}
    if fields.get("status", "published") not in STATUSES:
        raise FactCheckError("invalid_record", "status is published or an absence as observed")
    if record["record_kind"] == "fact-check":
        claims = fields.get("claims")
        if not isinstance(claims, list) or len(claims) != 1 or not claims[0].get("claim_text"):
            raise FactCheckError("invalid_record", "a fact-check reviews one claim, quoted as published")
        if not isinstance(claims[0].get("rating"), Mapping):
            raise FactCheckError("invalid_record", "a fact-check keeps its rating as published (or none)")
        if not (fields.get("publisher") or {}).get("domain") or not fields.get("review_url"):
            raise FactCheckError("invalid_record", "a fact-check names its publisher and review URL")
    elif not fields.get("domain") or not fields.get("name_as_published"):
        raise FactCheckError("invalid_record", "a publisher names its website domain and name as published")
    violations = minimisation_violations(record)
    if violations:
        raise FactCheckError("minimisation_violation", "personal fields may not be stored (FC01)", paths=violations)
    return record


def content_hash_of(record: Mapping[str, Any]) -> str:
    """What a revision says: the record without its acquisition origin and vintage labels.

    A later release or search that republishes the same content under a new vintage label is ``unchanged``; the
    receipt of that run records the vintage it was confirmed in.
    """
    return digest({k: v for k, v in record.items() if k not in {"evidence_origin", "native_revision",
                                                                  "revision_order"}})


def _view(row: Sequence[Any], head: Mapping[str, Any]) -> dict[str, Any]:
    revision = dict(zip(_REVISION_COLUMNS, row))
    record = json.loads(revision.pop("record_json"))
    return {
        **head, **revision, "provider": head.get("provider") or record.get("provider"),
        "record_kind": record.get("record_kind"), "publisher_key": record.get("publisher_key"),
        "review_key": record.get("review_key"), "record": record,
        "citation": {
            "source_id": revision["source_id"], "provider": record.get("provider"),
            "record_key": revision["record_key"], "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"], "native_revision": revision["native_revision"],
            "locator": record.get("locator"), "effective_on": revision["effective_on"],
            "observed_at_ms": revision["observed_at_ms"], "observed_at": iso(revision["observed_at_ms"]),
            "evidence_origin": revision["evidence_origin"], "status": revision["status"],
        },
    }


class FactCheckStore:
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

        When the receipt declares a complete scope (a Data Commons release, the IFCN listing), a current record
        of that scope the page no longer carries gets an absence revision; nothing is deleted.
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
            scope = dict(receipt.get("scope") or {})
            if scope.get("complete") and scope.get("scope_key"):
                counts["absent"] += self._absences(namespace, source_id, scope, set(keys), run_id, receipt_id,
                                                   observed)
            self.conn.execute("INSERT OR IGNORE INTO fact_check_receipts VALUES (?,?,?,?,?,?,?)",
                              [namespace, receipt_id, run_id, source_id, canonical(receipt), canonical(counts),
                               observed])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"receipt_id": receipt_id, "counts": counts, "records": len(checked)}

    def _head(self, namespace: str, source_id: str, key: str):
        return self.conn.execute(
            "SELECT r.current_revision_id, r.revision_count, v.content_hash, v.revision_order, v.status, "
            "v.record_json FROM fact_check_records r JOIN fact_check_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? AND r.source_id=? AND r.record_key=?",
            [namespace, source_id, key]).fetchone()

    def _insert(self, namespace, source_id, record, head, change, content_hash, run_id, receipt_id, observed,
                origin) -> None:
        key = record["record_key"]
        revision_no = 1 if head is None else int(head[1]) + 1
        revision_id = "fc-rev:" + digest([namespace, source_id, key, revision_no, content_hash])[:24]
        status = (record.get("fields") or {}).get("status") or "published"
        self.conn.execute(
            "INSERT INTO fact_check_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, source_id, key, revision_no, None if head is None else head[0], change,
             content_hash, record.get("native_revision"), str(record.get("revision_order") or ""),
             record.get("effective_on"), status, canonical(record), origin, run_id, receipt_id, observed])
        if head is None:
            self.conn.execute(
                "INSERT INTO fact_check_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, source_id, key, record["provider"], record["record_kind"], record.get("publisher_key"),
                 record.get("review_key"), record.get("scope_key"), revision_id, 1, run_id, observed])
        elif change == "older-observation":
            self.conn.execute("UPDATE fact_check_records SET revision_count=? WHERE namespace=? AND source_id=? AND "
                              "record_key=?", [revision_no, namespace, source_id, key])
        else:
            self.conn.execute(
                "UPDATE fact_check_records SET current_revision_id=?, revision_count=?, scope_key=? WHERE "
                "namespace=? AND source_id=? AND record_key=?",
                [revision_id, revision_no, record.get("scope_key"), namespace, source_id, key])

    def _observe(self, namespace, record, source_id, run_id, receipt_id, observed) -> str:
        key = record["record_key"]
        origin = "fixture" if record.get("evidence_origin") == "fixture" else "live"
        content_hash = content_hash_of(record)
        head = self._head(namespace, source_id, key)
        order = str(record.get("revision_order") or "")
        if head is not None:
            if head[2] == content_hash:
                return "unchanged"
            if head[4] == "published" and self.conn.execute(
                    "SELECT 1 FROM fact_check_revisions WHERE namespace=? AND source_id=? AND record_key=? AND "
                    "content_hash=?", [namespace, source_id, key, content_hash]).fetchone():
                return "unchanged"  # a replayed older response: already on record, never re-applied
            change = "older-observation" if order < str(head[3] or "") else "revised"
        else:
            change = "new"
        self._insert(namespace, source_id, record, head, change, content_hash, run_id, receipt_id, observed, origin)
        return change

    def _absences(self, namespace, source_id, scope, present, run_id, receipt_id, observed) -> int:
        rows = self.conn.execute(
            "SELECT record_key FROM fact_check_records WHERE namespace=? AND source_id=? AND scope_key=? AND "
            "record_kind=? ORDER BY record_key",
            [namespace, source_id, scope["scope_key"], scope.get("record_kind") or "fact-check"]).fetchall()
        absent = 0
        for (key,) in rows:
            if key in present:
                continue
            head = self._head(namespace, source_id, key)
            if head is None or head[4] != "published":
                continue
            previous = json.loads(head[5])
            record = {**previous, "fields": {**previous["fields"], "status": scope.get("absence") or
                                             "absent-from-release"},
                      "native_revision": scope.get("vintage") or previous.get("native_revision"),
                      "revision_order": max(str(head[3] or ""), str(scope.get("vintage_order") or "")),
                      "effective_on": datetime.fromtimestamp(observed / 1000, tz=UTC).date().isoformat()}
            origin = previous.get("evidence_origin") or "live"
            self._insert(namespace, source_id, record, head, "absent", content_hash_of(record), run_id, receipt_id,
                         observed,
                         "fixture" if origin == "fixture" else "live")
            absent += 1
        return absent

    # ------------------------------------------------------------------ reads

    def _all_revisions(self, namespace: str, where: str = "", params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT r.provider, r.first_observed_at_ms, " + ", ".join(f"v.{c}" for c in _REVISION_COLUMNS) +
            " FROM fact_check_revisions v JOIN fact_check_records r ON r.namespace=v.namespace AND "
            "r.source_id=v.source_id AND r.record_key=v.record_key WHERE v.namespace=? " + where +
            " ORDER BY v.record_key, v.source_id, v.revision_no", [namespace, *params]).fetchall()
        return [_view(row[2:], {"provider": row[0], "first_observed_at_ms": row[1]}) for row in rows]

    def records(self, namespace: str, *, scopes: Iterable[str], kinds: Iterable[str] | None = None,
                record_keys: Iterable[str] | None = None, publisher_key: str | None = None,
                known_at_ms: int | None = None, include_absent: bool = True) -> list[dict[str, Any]]:
        """The current revision of every matching record per source, or the one known at ``known_at_ms``."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        where, params = [], []
        if publisher_key is not None:
            where.append("AND r.publisher_key=?")
            params.append(publisher_key)
        for column, values in (("r.record_kind", kinds), ("v.record_key", record_keys)):
            if values is not None:
                values = sorted(set(values))
                if not values:
                    return []
                where.append(f"AND {column} IN (" + ",".join("?" * len(values)) + ")")
                params += values
        if known_at_ms is None:
            where.append("AND v.revision_id=r.current_revision_id")
        revisions = self._all_revisions(namespace, " ".join(where), params)
        if known_at_ms is not None:
            chosen: dict[tuple[str, str], dict[str, Any]] = {}
            for view in revisions:
                if view["observed_at_ms"] > known_at_ms or view["change"] == "older-observation":
                    continue
                chosen[(view["source_id"], view["record_key"])] = view  # revision order ascending
            revisions = [chosen[k] for k in sorted(chosen)]
        if not include_absent:
            revisions = [v for v in revisions if v["status"] == "published"]
        return revisions

    def history(self, namespace: str, record_key: str, *, scopes: Iterable[str], source_id: str | None = None
                ) -> list[dict[str, Any]]:
        """Every revision of a record in arrival order, including older observations and absences."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        return [v for v in self._all_revisions(namespace, "AND v.record_key=?", [record_key])
                if source_id is None or v["source_id"] == source_id]

    def revision(self, namespace: str, revision_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        found = self._all_revisions(namespace, "AND v.revision_id=?", [revision_id]) if self.ready() else []
        if not found:
            raise FactCheckError("not_found", "revision is not visible in this namespace")
        return found[0]

    def as_of(self, namespace: str, record_key: str, *, scopes: Iterable[str], day: str | None = None,
              known_at_ms: int | None = None) -> list[dict[str, Any]]:
        """Per source, the revision in force on ``day`` by the publisher's own dates, as known at ``known_at_ms``.

        A revision is in force from its ``effective_on`` date (the review date, the status date or the day an
        absence was observed); revisions without a published date count from their observation day.
        """
        out: dict[str, dict[str, Any]] = {}
        for view in self.history(namespace, record_key, scopes=scopes):
            if known_at_ms is not None and view["observed_at_ms"] > known_at_ms:
                continue
            since = view["effective_on"] or iso(view["observed_at_ms"])[:10]
            if day is not None and since > str(day)[:10]:
                continue
            best = out.get(view["source_id"])
            rank = (since, view["revision_order"], view["revision_no"])
            if best is None or rank >= best["_rank"]:
                out[view["source_id"]] = {**view, "_rank": rank, "in_force_since": since}
        return [{k: v for k, v in item.items() if k != "_rank"} for _, item in sorted(out.items())]

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


class FactCheckProjector:
    """Source-pack runtime projector for ``noesis-fact-check-record-v2`` pages (one selection unit per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = FactCheckStore(conn)

    @staticmethod
    def namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("fact_checks") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id,
                     observed_at_ms: int | None = None):
        del manifest, principal_id
        items = []
        for item in records:
            record = item.get("fact_check_record")
            if not isinstance(record, Mapping):
                raise FactCheckError("invalid_record", "page record is not a fact-check record")
            items.append(dict(record))
        if observed_at_ms is None:
            observed_at_ms = max((int(d["ingested_at"]) for d in documents or [] if d.get("ingested_at") is not None),
                                 default=None)
        return [self.store.project(self.namespace(source), items, run_id=run_id, source_id=source["source_id"],
                                   receipt=dict(page_receipt or {}), observed_at_ms=observed_at_ms)]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        rows = self.store.conn.execute(
            "SELECT count(*) FROM fact_check_receipts WHERE namespace=? AND run_id=? AND source_id=?",
            [self.namespace(source), run_id, source["source_id"]]).fetchone()
        return {"status": status, "units": int(rows[0])}


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.fact_checks_sources import (
        FORMATS,
        LIVE_VERIFICATION,
        PROVIDER_CONTRACTS,
    )

    store = FactCheckStore(conn, initialize=False)
    counts: dict[str, int] = {}
    if store.ready():
        counts = dict(conn.execute("SELECT provider, count(*) FROM fact_check_records GROUP BY provider").fetchall())
    feature = {spec["provider"]: spec["feature"] for spec in FORMATS.values()}
    return {
        "provider": "news.fact-checks", "bundle": BUNDLE,
        "enabled": {f: feature_enabled(conn, f) for f in FEATURES},
        "store_ready": store.ready(),
        "providers": {p: {"feature": feature[p], "access_decision": c["access_decision"],
                          "live": LIVE_VERIFICATION[p]["status"], "records": int(counts.get(p, 0))}
                      for p, c in PROVIDER_CONTRACTS.items()},
        "minimisation": MINIMISATION["policy"], "review_boundary": REVIEW_BOUNDARY, "exclusions": list(EXCLUSIONS),
        "notice": "unverified-live providers have fixture evidence only; a dated live run is outstanding (#2722)",
    }
