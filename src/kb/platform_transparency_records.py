"""Platform transparency records: statements of reasons, dump releases, ads, advertisers and takedown notices,
versioned as the platforms and databases published them (#2580, SP02).

Acquired ``noesis-platform-transparency-record-v1`` records (from
:mod:`src.ingestion.platform_transparency_sources`) are kept per source and
record key with an append-only revision log, following the
:mod:`src.kb.entity_history` pattern of never rewriting what was recorded:

* every record carries its **source** (source id, provider, locator), its
  **record revision** (revision id and number, content hash, the native
  revision the source published) and its **as-of time** (the observation
  time, plus the source's own data refresh time where it publishes one);
* a changed record is a new revision (``revised``); an older response observed
  later is logged as ``older-observation`` and never becomes current; a
  replayed response already on record adds nothing (``unchanged``);
* **removals and corrections by the source are revisions, never deletions**:
  when a complete listing of a declared selection (a Meta page and window, a
  Google advertiser, a DSA dump, a Lumen recipient and window) no longer
  contains a record, a ``not-returned`` revision is appended that states the
  observed absence and the run that observed it; a record listed again later
  is a new ``revised`` revision;
* :meth:`PlatformTransparencyStore.as_of` answers which revision was on record
  at a time.

**Minimisation (SP01) is enforced at write time.** A record whose fields carry
a key the decision never stores (DSA decision facts, explanations, content
ids and notifier identity; ad creative bodies, demographic or regional
distributions; Lumen bodies, works and URLs) or any access token is refused
with ``minimisation_violation`` before anything is written.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.ingestion.platform_transparency_sources import (
    FORMATS,
    MINIMISATION,
    RECORD_CONTRACT,
    RECORD_KINDS,
    REVIEW_BOUNDARY,
    minimisation_violations,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:osint:platform-transparency:read"
WRITE_SCOPE = "knowledge:osint:platform-transparency:write"
NOTICES_SCOPE = "knowledge:osint:platform-transparency:notices:read"
DEFAULT_NAMESPACE = "global"
FEATURES = {spec["provider"]: spec["feature"] for spec in FORMATS.values()}
CHANGES = ("new", "revised", "unchanged", "older-observation", "not-returned")
LISTED_KINDS = ("ad", "statement-of-reasons", "takedown-notice")
EXCLUSIONS = ("user-level profiling", "collection of private content", "inference of coordinated behaviour",
              "conversion of spend or impression ranges into point estimates")
# Keys that would carry a point estimate, a profile or a coordination reading; no answer may contain them.
FORBIDDEN_ANSWER_KEYS = frozenset({
    "midpoint", "spend_estimate", "estimated_spend", "impressions_estimate", "point_estimate", "total_spend",
    "coordination", "coordinated", "coordination_score", "inauthentic", "bot_score", "user_profile", "profile",
    "influence_score", "score", "risk_score", "verdict",
})

_DDL = """
CREATE TABLE IF NOT EXISTS platform_transparency_records (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL, provider TEXT NOT NULL,
  platform TEXT NOT NULL, record_kind TEXT NOT NULL, advertiser_key TEXT, selection_key TEXT NOT NULL,
  listing_status TEXT NOT NULL, current_revision_id TEXT NOT NULL, revision_count INTEGER NOT NULL,
  first_run_id TEXT NOT NULL, first_observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, source_id, record_key)
);
CREATE TABLE IF NOT EXISTS platform_transparency_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL,
  revision_no INTEGER NOT NULL, previous_revision_id TEXT, change TEXT NOT NULL, content_hash TEXT NOT NULL,
  native_revision TEXT, revision_order TEXT NOT NULL, effective_on TEXT, data_as_of TEXT, record_json TEXT NOT NULL,
  evidence_origin TEXT NOT NULL, run_id TEXT NOT NULL, receipt_id TEXT, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS platform_transparency_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, outcome_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
_REVISION_COLUMNS = ("revision_id", "source_id", "record_key", "revision_no", "previous_revision_id", "change",
                     "content_hash", "native_revision", "revision_order", "effective_on", "data_as_of", "record_json",
                     "evidence_origin", "run_id", "receipt_id", "observed_at_ms")
_HEAD_COLUMNS = ("provider", "platform", "record_kind", "advertiser_key", "selection_key", "listing_status",
                 "revision_count", "first_observed_at_ms")


class PlatformTransparencyError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def observed_at(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def to_ms(value: Any) -> int:
    """An ISO date or date-time (UTC when no offset is given) as epoch milliseconds; an int passes through."""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if len(text) == 10:
        text += "T23:59:59.999+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise PlatformTransparencyError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the Osint bundle's optional ``platform-transparency-*`` feature is selected (active plan only)."""
    try:
        if not all(table_exists(conn, t) for t in ("composition_authority", "composition_active",
                                                    "composition_generations", "composition_plans")):
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='osint'").fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return feature in ((plan.get("features") or {}).get("osint") or [])


def validate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Structural checks plus the SP01 minimisation guard; returns a canonical copy."""
    record = json.loads(canonical(record))
    if record.get("contract") != CONTRACT or record.get("record_kind") not in RECORD_KINDS:
        raise PlatformTransparencyError("invalid_record", "not a platform-transparency record")
    if not str(record.get("record_key") or "").startswith("platform-transparency:"):
        raise PlatformTransparencyError("invalid_record", "record keys are platform-transparency:* keys")
    if not str(record.get("locator") or "").startswith("https://"):
        raise PlatformTransparencyError("invalid_record", "every record cites an HTTPS locator")
    if not record.get("provider") or not record.get("platform") or not record.get("selection_key"):
        raise PlatformTransparencyError("invalid_record", "a record names its provider, platform and selection")
    fields = record.get("fields") or {}
    if record["record_kind"] == "ad" and (not fields.get("ad_id") or "advertiser_as_declared" not in fields
                                          or "funding_entity_as_declared" not in fields):
        raise PlatformTransparencyError("invalid_record", "an ad keeps its id, advertiser and funding entity as "
                                                          "declared")
    if record["record_kind"] == "statement-of-reasons":
        for key in ("uuid", "platform_uid", "decision_ground", "automated_detection", "automated_decision"):
            if key not in fields:
                raise PlatformTransparencyError("invalid_record", f"a statement of reasons keeps {key} as published")
    violations = minimisation_violations(record)
    if violations:
        raise PlatformTransparencyError("minimisation_violation", "a field the SP01 decision never stores is "
                                        "present", paths=violations)
    return record


def cite(row: Mapping[str, Any]) -> dict[str, Any]:
    citation = dict(row["citation"])
    citation["observed_at"] = observed_at(citation.get("observed_at_ms"))
    return citation


def _view(row: Sequence[Any], head: Mapping[str, Any]) -> dict[str, Any]:
    revision = dict(zip(_REVISION_COLUMNS, row))
    record = json.loads(revision.pop("record_json"))
    return {
        **head, **revision, "record": record,
        "record_kind": head.get("record_kind") or record.get("record_kind"),
        "citation": {
            "source_id": revision["source_id"], "provider": head.get("provider") or record.get("provider"),
            "platform": record.get("platform"), "record_key": revision["record_key"],
            "revision_id": revision["revision_id"], "revision_no": revision["revision_no"],
            "change": revision["change"], "native_revision": revision["native_revision"],
            "locator": record.get("locator"), "observed_at_ms": revision["observed_at_ms"],
            "data_as_of": revision["data_as_of"], "evidence_origin": revision["evidence_origin"],
        },
    }


class PlatformTransparencyStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "platform_transparency_revisions")

    # ------------------------------------------------------------------ writes

    def project(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, source_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        """Append revisions for what changed; idempotent. A complete listing that no longer contains a record of
        its selection appends a ``not-returned`` revision (never a deletion)."""
        checked = [validate(r) for r in records]  # refuse the whole page before writing anything
        keys = [r["record_key"] for r in checked]
        if len(set(keys)) != len(keys):
            raise PlatformTransparencyError("invalid_record", "a page repeats a record")
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        receipt = dict(receipt or {})
        receipt_id = "pt-receipt:" + digest([namespace, source_id, run_id, receipt, keys])[:24]
        counts = dict.fromkeys(CHANGES, 0)
        order = {kind: i for i, kind in enumerate(RECORD_KINDS)}
        self.conn.execute("BEGIN")
        try:
            for record in sorted(checked, key=lambda r: (order[r["record_kind"]], r["record_key"])):
                counts[self._observe(namespace, record, source_id, run_id, receipt_id, observed)] += 1
            present = set(keys)
            for listing in receipt.get("complete_listings") or []:
                counts["not-returned"] += self._not_returned(namespace, source_id, listing, present, run_id,
                                                             receipt_id, observed)
            self.conn.execute(
                "INSERT OR IGNORE INTO platform_transparency_receipts VALUES (?,?,?,?,?,?,?)",
                [namespace, receipt_id, run_id, source_id, canonical(receipt), canonical(counts), observed])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"receipt_id": receipt_id, "counts": counts, "records": len(checked)}

    def _head(self, namespace: str, source_id: str, key: str):
        return self.conn.execute(
            "SELECT r.current_revision_id, r.revision_count, v.content_hash, v.revision_order, v.record_json, "
            "v.evidence_origin "
            "FROM platform_transparency_records r JOIN platform_transparency_revisions v ON v.namespace=r.namespace "
            "AND v.revision_id=r.current_revision_id WHERE r.namespace=? AND r.source_id=? AND r.record_key=?",
            [namespace, source_id, key]).fetchone()

    def _append(self, namespace, source_id, record, head, change, content_hash, origin, run_id, receipt_id,
                observed) -> str:
        key = record["record_key"]
        revision_no = 1 if head is None else int(head[1]) + 1
        revision_id = "pt-rev:" + digest([namespace, source_id, key, revision_no, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO platform_transparency_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, source_id, key, revision_no, None if head is None else head[0], change,
             content_hash, record.get("native_revision"), str(record.get("revision_order") or ""),
             record.get("effective_on"), record.get("data_as_of"), canonical(record), origin, run_id, receipt_id,
             observed])
        if head is None:
            self.conn.execute(
                "INSERT INTO platform_transparency_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, source_id, key, record["provider"], record["platform"], record["record_kind"],
                 record.get("advertiser_key"), record["selection_key"], record.get("listing_status") or "listed",
                 revision_id, 1, run_id, observed])
        elif change == "older-observation":
            self.conn.execute("UPDATE platform_transparency_records SET revision_count=? WHERE namespace=? AND "
                              "source_id=? AND record_key=?", [revision_no, namespace, source_id, key])
        else:
            self.conn.execute(
                "UPDATE platform_transparency_records SET current_revision_id=?, revision_count=?, platform=?, "
                "advertiser_key=?, selection_key=?, listing_status=? WHERE namespace=? AND source_id=? AND "
                "record_key=?", [revision_id, revision_no, record["platform"], record.get("advertiser_key"),
                                 record["selection_key"], record.get("listing_status") or "listed", namespace,
                                 source_id, key])
        return revision_id

    def _observe(self, namespace, record, source_id, run_id, receipt_id, observed) -> str:
        key = record["record_key"]
        origin = "fixture" if record.get("evidence_origin") == "fixture" else "live"
        body = {k: v for k, v in record.items() if k != "evidence_origin"}
        content_hash = digest(body)
        head = self._head(namespace, source_id, key)
        order = str(record.get("revision_order") or "")
        if head is not None:
            if head[2] == content_hash:
                return "unchanged"
            relisted = json.loads(head[4]).get("listing_status") == "not-returned"
            if not relisted and self.conn.execute(
                    "SELECT 1 FROM platform_transparency_revisions WHERE namespace=? AND source_id=? AND record_key=? "
                    "AND content_hash=?", [namespace, source_id, key, content_hash]).fetchone():
                return "unchanged"  # a replayed older response: already on record, never re-applied
            previous_order = str(head[3] or "")
            change = "older-observation" if order and previous_order and order < previous_order else "revised"
        else:
            change = "new"
        self._append(namespace, source_id, body, head, change, content_hash, origin, run_id, receipt_id, observed)
        return change

    def _not_returned(self, namespace, source_id, listing, present, run_id, receipt_id, observed) -> int:
        kind, selection = listing.get("record_kind"), listing.get("selection_key")
        if kind not in LISTED_KINDS or not selection:
            return 0
        rows = self.conn.execute(
            "SELECT record_key FROM platform_transparency_records WHERE namespace=? AND source_id=? AND "
            "selection_key=? AND record_kind=? AND listing_status<>'not-returned' ORDER BY record_key",
            [namespace, source_id, selection, kind]).fetchall()
        appended = 0
        for (key,) in rows:
            if key in present:
                continue
            head = self._head(namespace, source_id, key)
            record = json.loads(head[4])
            record["listing_status"] = "not-returned"
            record["not_returned"] = {"run_id": run_id, "observed_at": observed_at(observed),
                                      "statement": "the source no longer returned this record for the same declared "
                                                   "selection; an observed absence, not a stated deletion"}
            origin = head[5]
            self._append(namespace, source_id, record, head, "not-returned", digest(record), origin, run_id,
                         receipt_id, observed)
            appended += 1
        return appended

    # ------------------------------------------------------------------ reads

    def _heads(self, namespace: str, where: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT " + ", ".join(f"r.{c}" for c in _HEAD_COLUMNS) + ", " + ", ".join(f"v.{c}" for c in
                                                                                     _REVISION_COLUMNS) +
            " FROM platform_transparency_records r JOIN platform_transparency_revisions v ON v.namespace=r.namespace "
            "AND v.revision_id=r.current_revision_id WHERE r.namespace=? " + where +
            " ORDER BY r.record_kind, r.record_key, r.source_id", [namespace, *params]).fetchall()
        return [_view(row[len(_HEAD_COLUMNS):], dict(zip(_HEAD_COLUMNS, row[:len(_HEAD_COLUMNS)]))) for row in rows]

    def records(self, namespace: str, *, scopes: Iterable[str], kinds: Iterable[str] | None = None,
                platform: str | None = None, advertiser_key: str | None = None, selection_key: str | None = None,
                record_keys: Iterable[str] | None = None, providers: Iterable[str] | None = None
                ) -> list[dict[str, Any]]:
        """Current revision of every matching record (per source)."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        where, params = [], []
        for column, value in (("r.platform", platform), ("r.advertiser_key", advertiser_key),
                              ("r.selection_key", selection_key)):
            if value is not None:
                where.append(f"AND {column}=?")
                params.append(value)
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
        """Every revision of a record in arrival order, including older observations that never became current."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM platform_transparency_revisions WHERE namespace=? AND "
            "record_key=? AND (? IS NULL OR source_id=?) ORDER BY source_id, revision_no",
            [namespace, record_key, source_id, source_id]).fetchall()
        return [_view(row, {}) for row in rows]

    def as_of(self, namespace: str, record_key: str, as_of: Any, *, scopes: Iterable[str],
              source_id: str | None = None) -> dict[str, Any] | None:
        """The revision that was current on record at a time (observation time); ``None`` before the first."""
        at = to_ms(as_of)
        current = None
        for revision in self.history(namespace, record_key, scopes=scopes, source_id=source_id):
            if revision["observed_at_ms"] <= at and revision["change"] != "older-observation":
                current = revision
        return current

    def revision(self, namespace: str, revision_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute("SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM platform_transparency_revisions "
                                "WHERE namespace=? AND revision_id=?", [namespace, revision_id]).fetchone()
        if row is None:
            raise PlatformTransparencyError("not_found", "revision is not visible in this namespace")
        return _view(row, {})

    def receipts(self, namespace: str, run_id: str | None = None, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, run_id, source_id, receipt_json, outcome_json, recorded_at_ms FROM "
            "platform_transparency_receipts WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY recorded_at_ms, "
            "receipt_id", [namespace, run_id, run_id]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "receipt": json.loads(r[3]),
                 "counts": json.loads(r[4]), "recorded_at_ms": r[5]} for r in rows]


class PlatformTransparencyProjector:
    """Source-pack runtime projector for ``noesis-platform-transparency-record-v1`` pages (one unit per page)."""

    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.store = PlatformTransparencyStore(conn, now=now)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("platform_transparency") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        items = []
        for item in records:
            record = item.get("platform_transparency_record")
            if not isinstance(record, Mapping):
                raise PlatformTransparencyError("invalid_record", "page record is not a platform-transparency record")
            items.append(dict(record))
        return [self.store.project(self._namespace(source), items, run_id=run_id, source_id=source["source_id"],
                                   receipt=dict(page_receipt or {}))]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        rows = self.store.conn.execute(
            "SELECT count(*) FROM platform_transparency_receipts WHERE namespace=? AND run_id=? AND source_id=?",
            [self._namespace(source), run_id, source["source_id"]]).fetchone()
        return {"status": status, "units": int(rows[0])}


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.platform_transparency_sources import (
        LIVE_VERIFICATION,
        PROVIDER_CONTRACTS,
    )

    store = PlatformTransparencyStore(conn, initialize=False)
    counts: dict[str, int] = {}
    if store.ready():
        counts = dict(conn.execute("SELECT provider, count(*) FROM platform_transparency_records GROUP BY provider"
                                   ).fetchall())
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        state = "available" if contract["access_decision"] != "gated-not-granted" else "unavailable"
        providers[provider] = {"feature": FEATURES[provider], "enabled": feature_enabled(conn, FEATURES[provider]),
                               "access_decision": contract["access_decision"], "live": LIVE_VERIFICATION[provider][
                                   "status"], "records": int(counts.get(provider, 0)), "state": state,
                               **({"reason": contract["reason"]} if state == "unavailable" else {})}
    return {"feature": "osint.platform-transparency", "store_ready": store.ready(), "providers": providers,
            "minimisation": MINIMISATION["policy"], "review_boundary": REVIEW_BOUNDARY,
            "degraded": sorted(p for p, v in providers.items() if v["state"] != "available")}


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in an answer that would carry a point estimate, a profile or a coordination reading."""
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
