"""Platform transparency records: statements of reasons, dump releases, ads, advertisers and takedown notices,
versioned as the platforms and the DSA Transparency Database published them (#2580, SP02).

Acquired ``noesis-platform-transparency-record-v1`` records (from
:mod:`src.ingestion.platform_transparency_sources`) are kept per source and
record key with an append-only revision log, following the
:mod:`src.kb.entity_history` pattern of never rewriting what was recorded:

* every record carries its **source** (source-pack source and provider), its
  **record revision** (``revision_id``, ``revision_no`` and the provider's own
  stamp where one exists: a dump's published SHA-1, Google's table refresh
  time) and its **as-of time** (``observed_at_ms``, when Noesis recorded the
  revision, and ``source_as_of``, the date the source states: the dump day or
  the data refresh time);
* a changed record is a new revision; a replayed response adds nothing; an
  older observation arriving late is logged as ``older-observation`` and never
  becomes current;
* **removals and corrections are revisions, never deletions.** When a
  complete ``listing`` of a declared unit no longer returns an ad that an
  earlier listing of the same unit returned, the ad receives a
  ``not-returned`` revision that cites the listing; if it returns later it
  receives a ``listed`` revision again;
* :meth:`PlatformTransparencyStore.as_of` answers which revision of a record
  was current at a given time.

**Minimisation (SP01) is enforced at write time.** A record carrying a
withheld key (a platform user identifier, the notifier identity, a free-text
explanation, targeting or demographic breakdowns, creative text, the snapshot
URL) or a point-estimate key is refused with ``minimisation_violation`` before
anything is written. Nothing here profiles users, collects private content or
infers coordinated behaviour.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.ingestion.platform_transparency_sources import (
    FORBIDDEN_KEYS,
    MINIMISATION,
    RECORD_CONTRACT,
    RECORD_KINDS,
    REVIEW_BOUNDARY,
    minimisation_violations,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:osint:platform-transparency:read"
WRITE_SCOPE = "knowledge:osint:platform-transparency:write"
DEFAULT_NAMESPACE = "osint"
FEATURES = {"dsa-transparency-db": "platform-transparency-dsa", "meta-ad-library": "platform-transparency-meta",
            "google-political-ads": "platform-transparency-google", "lumen": "platform-transparency-lumen"}
CHANGES = ("new", "revised", "not-returned", "relisted", "unchanged", "older-observation")
EXCLUSIONS = ("user-level profiling", "collection of private content", "inference of coordinated behaviour",
              "conversion of spend or impression ranges into point estimates")
_STAMPS = frozenset({"evidence_origin", "native_revision", "revision_order", "source_as_of"})
_KIND_ORDER = {kind: i for i, kind in enumerate(("dump-release", "advertiser", "statement-of-reasons", "ad",
                                                 "takedown-notice", "listing"))}

_DDL = """
CREATE TABLE IF NOT EXISTS platform_transparency_records (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL, provider TEXT NOT NULL,
  platform TEXT NOT NULL, record_kind TEXT NOT NULL, unit_key TEXT, advertiser_key TEXT, dump_key TEXT,
  current_revision_id TEXT NOT NULL, revision_count INTEGER NOT NULL, first_run_id TEXT NOT NULL,
  first_observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, source_id, record_key)
);
CREATE TABLE IF NOT EXISTS platform_transparency_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL,
  revision_no INTEGER NOT NULL, previous_revision_id TEXT, change TEXT NOT NULL, content_hash TEXT NOT NULL,
  native_revision TEXT, revision_order TEXT NOT NULL, effective_on TEXT, source_as_of TEXT, record_json TEXT NOT NULL,
  listing_state TEXT, evidence_origin TEXT NOT NULL, run_id TEXT NOT NULL, receipt_id TEXT,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS platform_transparency_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, outcome_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
_REVISION_COLUMNS = ("revision_id", "source_id", "record_key", "revision_no", "previous_revision_id", "change",
                     "content_hash", "native_revision", "revision_order", "effective_on", "source_as_of",
                     "record_json", "listing_state", "evidence_origin", "run_id", "receipt_id", "observed_at_ms")
_HEAD_COLUMNS = ("provider", "platform", "record_kind", "unit_key", "advertiser_key", "dump_key", "revision_count",
                 "first_observed_at_ms")


class PlatformTransparencyError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def to_ms(value: Any) -> int | None:
    """An as-of time as epoch milliseconds: an int, or an ISO date/time (a bare date means its end, UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
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
    """Whether the OSINT bundle's optional platform-transparency feature is selected (active composition plan only;
    defaults to off)."""
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


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in an answer that would carry a point estimate, a user profile or a coordination reading."""
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


def validate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Structural checks plus the SP01 minimisation guard; returns a canonical copy."""
    record = json.loads(canonical(record))
    if record.get("contract") != CONTRACT or record.get("record_kind") not in RECORD_KINDS:
        raise PlatformTransparencyError("invalid_record", "not a platform-transparency record")
    if not str(record.get("record_key") or "").startswith("platform-transparency:"):
        raise PlatformTransparencyError("invalid_record", "record keys are platform-transparency:* keys")
    locator = str(record.get("locator") or "")
    if not locator.startswith("https://") or "access_token" in locator:
        raise PlatformTransparencyError("invalid_record", "every record cites a clean HTTPS locator")
    fields = record.get("fields") or {}
    if record["record_kind"] == "statement-of-reasons" and not record.get("dump_key"):
        raise PlatformTransparencyError("invalid_record", "a statement of reasons names the dump it was read from")
    if record["record_kind"] == "ad":
        for axis in ("spend", "impressions"):
            value = fields.get(axis)
            if value is not None and not isinstance(value, Mapping):
                raise PlatformTransparencyError("invalid_record", f"{axis} is stored as the published range")
    violations = minimisation_violations(record)
    if violations:
        raise PlatformTransparencyError("minimisation_violation", "withheld or point-estimate fields may not be "
                                        "stored (SP01)", paths=violations)
    return record


def cite(view: Mapping[str, Any]) -> dict[str, Any]:
    citation = dict(view["citation"])
    citation["observed_at"] = iso(citation.get("observed_at_ms"))
    return citation


def _view(row: Sequence[Any], head: Mapping[str, Any]) -> dict[str, Any]:
    revision = dict(zip(_REVISION_COLUMNS, row))
    record = json.loads(revision.pop("record_json"))
    return {
        **head, **revision, "record": record,
        "citation": {
            "source_id": revision["source_id"], "provider": head.get("provider") or record.get("provider"),
            "platform": record.get("platform"), "record_key": revision["record_key"],
            "revision_id": revision["revision_id"], "revision_no": revision["revision_no"],
            "native_revision": revision["native_revision"], "source_as_of": revision["source_as_of"],
            "locator": record.get("locator"), "observed_at_ms": revision["observed_at_ms"],
            "evidence_origin": revision["evidence_origin"], "dump_key": record.get("dump_key"),
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
        """Append revisions for what changed; idempotent (re-projecting an unchanged record adds nothing).

        Dump releases and advertisers are stored before the statements and ads that cite them, and a unit's
        listing last, so that ads it no longer returns receive their ``not-returned`` revision in the same run.
        """
        checked = [validate(r) for r in records]  # refuse the whole page before writing anything
        keys = [(r["record_key"], r.get("native_revision"), r.get("revision_order")) for r in checked]
        if len(set(keys)) != len(keys):
            raise PlatformTransparencyError("invalid_record", "a page repeats a record revision")
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        receipt = dict(receipt or {})
        receipt_id = "pt-receipt:" + digest([namespace, source_id, run_id, receipt, [k[0] for k in keys]])[:24]
        counts = dict.fromkeys(CHANGES, 0)
        self.conn.execute("BEGIN")
        # A unit whose complete listing was already on record (and is not the current one) is a replayed older
        # response: its ads are matched against every earlier revision and never relisted by it.
        replayed = {r["unit_key"] for r in checked if r["record_kind"] == "listing"
                    and self._replayed(namespace, source_id, r)}
        try:
            for record in sorted(checked, key=lambda r: (_KIND_ORDER[r["record_kind"]], r["record_key"])):
                change = self._observe(namespace, record, source_id, run_id, receipt_id, observed,
                                       replay=record.get("unit_key") in replayed)
                counts[change] += 1
                if record["record_kind"] == "listing" and change not in {"unchanged", "older-observation"}:
                    for change in self._reconcile_listing(namespace, record, source_id, run_id, receipt_id, observed):
                        counts[change] += 1
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
            "v.listing_state FROM platform_transparency_records r JOIN platform_transparency_revisions v ON "
            "v.namespace=r.namespace AND v.revision_id=r.current_revision_id WHERE r.namespace=? AND r.source_id=? "
            "AND r.record_key=?", [namespace, source_id, key]).fetchone()

    def _replayed(self, namespace: str, source_id: str, listing: Mapping[str, Any]) -> bool:
        content_hash = digest({k: v for k, v in listing.items() if k not in _STAMPS})
        head = self._head(namespace, source_id, listing["record_key"])
        return bool(head is not None and head[2] != content_hash and self.conn.execute(
            "SELECT 1 FROM platform_transparency_revisions WHERE namespace=? AND source_id=? AND record_key=? AND "
            "content_hash=?", [namespace, source_id, listing["record_key"], content_hash]).fetchone())

    def _observe(self, namespace, record, source_id, run_id, receipt_id, observed, *, change: str | None = None,
                 replay: bool = False) -> str:
        key = record["record_key"]
        origin = "fixture" if record.get("evidence_origin") == "fixture" else "live"
        # the published content: a refresh or dump stamp alone (with unchanged values) is not a new revision
        content_hash = digest({k: v for k, v in record.items() if k not in _STAMPS})
        head = self._head(namespace, source_id, key)
        order = str(record.get("revision_order") or "")
        if head is not None:
            if head[2] == content_hash:
                return "unchanged"
            if change is None and self.conn.execute(
                    "SELECT 1 FROM platform_transparency_revisions WHERE namespace=? AND source_id=? AND "
                    "record_key=? AND content_hash=? AND change NOT IN ('not-returned')",
                    [namespace, source_id, key, content_hash]).fetchone() and (replay or head[5] != "not-returned"):
                return "unchanged"  # a replayed older response: already on record, never re-applied
            if change is None:
                if order < str(head[3] or ""):
                    change = "older-observation"
                elif head[5] == "not-returned" and (record.get("fields") or {}).get("listing_state") == "listed":
                    change = "relisted"
                else:
                    change = "revised"
        else:
            change = change or "new"
        revision_no = 1 if head is None else int(head[1]) + 1
        revision_id = "pt-rev:" + digest([namespace, source_id, key, revision_no, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO platform_transparency_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, source_id, key, revision_no, None if head is None else head[0], change,
             content_hash, record.get("native_revision"), order, record.get("effective_on"),
             record.get("source_as_of"), canonical(record), (record.get("fields") or {}).get("listing_state"),
             origin, run_id, receipt_id, observed])
        if head is None:
            self.conn.execute(
                "INSERT INTO platform_transparency_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, source_id, key, record["provider"], record["platform"], record["record_kind"],
                 record.get("unit_key"), record.get("advertiser_key"), record.get("dump_key"), revision_id, 1,
                 run_id, observed])
        elif change == "older-observation":
            self.conn.execute("UPDATE platform_transparency_records SET revision_count=? WHERE namespace=? AND "
                              "source_id=? AND record_key=?", [revision_no, namespace, source_id, key])
        else:
            self.conn.execute(
                "UPDATE platform_transparency_records SET current_revision_id=?, revision_count=?, unit_key=?, "
                "advertiser_key=?, dump_key=? WHERE namespace=? AND source_id=? AND record_key=?",
                [revision_id, revision_no, record.get("unit_key"), record.get("advertiser_key"),
                 record.get("dump_key"), namespace, source_id, key])
        return change

    def _reconcile_listing(self, namespace, listing, source_id, run_id, receipt_id, observed) -> list[str]:
        """Ads of the same unit that a complete listing no longer returns receive a ``not-returned`` revision."""
        returned = set((listing.get("fields") or {}).get("ad_keys") or [])
        rows = self.conn.execute(
            "SELECT r.record_key, v.record_json FROM platform_transparency_records r JOIN "
            "platform_transparency_revisions v ON v.namespace=r.namespace AND v.revision_id=r.current_revision_id "
            "WHERE r.namespace=? AND r.source_id=? AND r.unit_key=? AND r.record_kind='ad' ORDER BY r.record_key",
            [namespace, source_id, listing["unit_key"]]).fetchall()
        head = self._head(namespace, source_id, listing["record_key"])
        changes = []
        for key, body in rows:
            record = json.loads(body)
            if key in returned or record["fields"].get("listing_state") == "not-returned":
                continue
            absent = copy.deepcopy(record)
            absent.pop("evidence_origin", None)
            absent["fields"]["listing_state"] = "not-returned"
            absent["fields"]["not_returned_basis"] = {
                "listing_record_key": listing["record_key"], "listing_revision_id": head[0] if head else None,
                "source_as_of": listing.get("source_as_of"),
                "note": "a complete listing of the same declared unit no longer returned this ad; the source did "
                        "not state why (a platform removal, an end of delivery or a reclassification)"}
            absent["revision_order"] = max(str(absent.get("revision_order") or ""),
                                           str(listing.get("revision_order") or ""))
            absent["source_as_of"] = listing.get("source_as_of") or absent.get("source_as_of")
            absent["evidence_origin"] = listing.get("evidence_origin")
            changes.append(self._observe(namespace, absent, source_id, run_id, receipt_id, observed,
                                         change="not-returned"))
        return changes

    # ------------------------------------------------------------------ reads

    def _heads(self, namespace: str, where: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT " + ", ".join(f"r.{c}" for c in _HEAD_COLUMNS) + ", " +
            ", ".join(f"v.{c}" for c in _REVISION_COLUMNS) +
            " FROM platform_transparency_records r JOIN platform_transparency_revisions v ON "
            "v.namespace=r.namespace AND v.revision_id=r.current_revision_id WHERE r.namespace=? " + where +
            " ORDER BY r.record_kind, r.record_key, r.source_id", [namespace, *params]).fetchall()
        return [_view(row[len(_HEAD_COLUMNS):], dict(zip(_HEAD_COLUMNS, row[:len(_HEAD_COLUMNS)]))) for row in rows]

    def records(self, namespace: str, *, scopes: Iterable[str], kinds: Iterable[str] | None = None,
                provider: str | None = None, platform: str | None = None, advertiser_key: str | None = None,
                unit_key: str | None = None, dump_key: str | None = None, record_keys: Iterable[str] | None = None,
                as_of: Any = None) -> list[dict[str, Any]]:
        """Current revision of every matching record (per source); with ``as_of``, the revision current then."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        where, params = [], []
        for column, value in (("r.provider", provider), ("r.platform", platform),
                              ("r.advertiser_key", advertiser_key), ("r.unit_key", unit_key),
                              ("r.dump_key", dump_key)):
            if value is not None:
                where.append(f"AND {column}=?")
                params.append(value)
        for column, values in (("r.record_kind", kinds), ("r.record_key", record_keys)):
            if values is not None:
                values = sorted(set(values))
                if not values:
                    return []
                where.append(f"AND {column} IN (" + ",".join("?" * len(values)) + ")")
                params += values
        heads = self._heads(namespace, " ".join(where), params)
        at = to_ms(as_of)
        if at is None:
            return heads
        out = []
        for head in heads:
            found = self.as_of(namespace, head["record_key"], at, scopes=scopes, source_id=head["source_id"])
            if found is not None:
                out.append({**{k: head[k] for k in _HEAD_COLUMNS}, **found})
        return out

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

    def as_of(self, namespace: str, record_key: str, at: Any, *, scopes: Iterable[str], source_id: str | None = None
              ) -> dict[str, Any] | None:
        """The revision that was current at ``at`` (recorded on or before it; older observations never current)."""
        at_ms = to_ms(at)
        current = None
        for revision in self.history(namespace, record_key, scopes=scopes, source_id=source_id):
            if at_ms is not None and revision["observed_at_ms"] > at_ms:
                continue
            if revision["change"] == "older-observation":
                continue
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

    def __init__(self, conn: Any) -> None:
        self.store = PlatformTransparencyStore(conn)

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
    return {
        "feature": "osint.platform-transparency",
        "enabled": {provider: feature_enabled(conn, feature) for provider, feature in FEATURES.items()},
        "store_ready": store.ready(),
        "providers": {p: {"access_decision": c["access_decision"], "live": LIVE_VERIFICATION[p]["status"],
                          "records": int(counts.get(p, 0))} for p, c in PROVIDER_CONTRACTS.items()},
        "minimisation": {"policy": MINIMISATION["policy"], "never_stored": MINIMISATION["never_stored"]},
        "exclusions": list(EXCLUSIONS),
        "review_boundary": REVIEW_BOUNDARY,
    }
