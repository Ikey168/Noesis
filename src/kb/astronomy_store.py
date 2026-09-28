"""Revisioned store for Astronomy and Space records (#2149, AS02).

The one record owner of ``noesis-astronomy-record-v1``
(:mod:`src.kb.astronomy_records`), declared by the ``astronomy.small-bodies``
provider and read by the exoplanet, launch and space-weather providers.

Store rules:

* a stable ``record_id`` per namespace, kind, provider and source record ID
  (keys carry every distinguishing field and never change when older data
  arrives later);
* re-acquiring unchanged content adds nothing. Content is compared as a
  normalised, source-independent representation (no locators, documents or
  retrieval times), and only against the **current** revision, so a
  reversion is a new revision (a correction);
* "current" follows the record's own date (orbit ``computed_at``, a
  disposition's ``asserted_at``, a product's ``issue_time``, else the source's
  ``published_at``), then the date the source states for the document, then
  observation order. A late-arriving older state is kept as ``history`` and
  never becomes current or raises a correction event;
* a record that leaves a *complete* bounded listing gets a listing observation
  (``no_longer_listed``) with the date observed; nothing is deleted.

Point-in-time reads take a public cutoff (a stated date counts as the end of
that day, UTC; a missing date falls back to the first observation) and an
optional acquisition cutoff: only revisions acquired by then exist for that
read, and a later revision is never visible before it was observed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.astronomy_records import (
    DEFAULT_NAMESPACE,
    KINDS,
    PROVIDERS,
    AstronomyError,
    canonical,
    clock_ms,
    digest,
    iso_day,
    object_keys,
    observed_day,
    record_id,
    semantic,
    validate_record,
)

_DDL = """
CREATE TABLE IF NOT EXISTS astronomy_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, kind TEXT NOT NULL, provider TEXT NOT NULL,
  source_record_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS astronomy_record_revisions (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, revision BIGINT NOT NULL, revision_id TEXT NOT NULL,
  record_hash TEXT NOT NULL, payload_json TEXT NOT NULL, change TEXT NOT NULL, stated_at TEXT,
  source_as_of TEXT, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id, revision)
);
CREATE TABLE IF NOT EXISTS astronomy_record_keys (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, key TEXT NOT NULL,
  PRIMARY KEY(namespace, record_id, key)
);
CREATE TABLE IF NOT EXISTS astronomy_listing_observations (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, state TEXT NOT NULL, observed_on TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, run_id TEXT NOT NULL, scope_json TEXT NOT NULL,
  PRIMARY KEY(namespace, record_id, observed_at_ms, state)
);
CREATE TABLE IF NOT EXISTS astronomy_page_receipts (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, provider TEXT NOT NULL, document TEXT NOT NULL,
  receipt_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, provider, document)
);
"""
TABLES = (
    "astronomy_records",
    "astronomy_record_revisions",
    "astronomy_record_keys",
    "astronomy_listing_observations",
    "astronomy_page_receipts",
)
_REVISION_FIELDS = (
    "revision",
    "revision_id",
    "record_hash",
    "change",
    "stated_at",
    "source_as_of",
    "run_id",
    "observed_at_ms",
)
# The record's own date field per kind (else source.published_at).
_OWN_DATE = {
    "orbit_solution": ("computed_at",),
    "impact_risk_listing": ("removed_at", "listing_date"),
    "exoplanet_status_assertion": ("asserted_at",),
    "space_weather_product": ("issue_time",),
    "identification": ("announced_on",),
}


def own_date(record: Mapping[str, Any]) -> str | None:
    """The date the source states for this record's content (orders revisions); ``None`` when it states none."""
    for field in _OWN_DATE.get(record["kind"], ()):
        if record.get(field):
            return str(record[field])
    return (record.get("source") or {}).get("published_at")


def _order_key(row: Mapping[str, Any]) -> tuple:
    stated = (
        row.get("stated_at")
        or row.get("source_as_of")
        or observed_day(int(row["observed_at_ms"]))
    )
    return (str(stated), int(row["observed_at_ms"]), int(row["revision"]))


class AstronomyStore:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # -------------------------------------------------------------- readiness

    def ready(self) -> bool:
        rows = self.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN ("
            + ",".join("?" * len(TABLES))
            + ")",
            list(TABLES),
        ).fetchall()
        return len({r[0] for r in rows}) == len(TABLES)

    def require_ready(self) -> None:
        if not self.ready():
            raise AstronomyError(
                "not_ready",
                "no astronomy source has run yet; run the astronomy-and-space source "
                "pack first",
            )

    def has_kind(self, namespace: str, kinds: Sequence[str]) -> bool:
        if not self.ready():
            return False
        return bool(
            self.conn.execute(
                "SELECT 1 FROM astronomy_records WHERE namespace=? AND kind IN ("
                + ",".join("?" * len(kinds))
                + ") "
                "LIMIT 1",
                [namespace, *kinds],
            ).fetchone()
        )

    # -------------------------------------------------------------- writes

    def _revisions(self, namespace: str, rid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT "
            + ", ".join(_REVISION_FIELDS)
            + " FROM astronomy_record_revisions "
            "WHERE namespace=? AND record_id=? ORDER BY revision",
            [namespace, rid],
        ).fetchall()
        return [dict(zip(_REVISION_FIELDS, r)) for r in rows]

    def apply(
        self,
        namespace: str,
        records: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        observed_at_ms: int,
        source_as_of: str | None = None,
    ) -> dict[str, Any]:
        """Validate and append records; unchanged content adds nothing.

        ``source_as_of`` is the date the source states for the document the records came from (``None`` when it
        states none; the record's own date, then observation order, decide what is current).
        """
        counts = {"inserted": 0, "revised": 0, "unchanged": 0, "history": 0}
        changed: list[str] = []
        source_as_of = iso_day(source_as_of) if source_as_of else None
        validated = [validate_record(r) for r in records]
        self.conn.execute("BEGIN")
        try:
            for record in validated:
                provider, source_record_id = (
                    record["source"]["provider"],
                    record["source"]["source_record_id"],
                )
                rid = record_id(namespace, record["kind"], provider, source_record_id)
                record_hash = digest(semantic(record))
                revisions = self._revisions(namespace, rid)
                incoming = {
                    "stated_at": own_date(record),
                    "source_as_of": source_as_of,
                    "observed_at_ms": int(observed_at_ms),
                    "revision": (revisions[-1]["revision"] + 1) if revisions else 1,
                }
                if revisions:
                    current = max(revisions, key=_order_key)
                    if current["record_hash"] == record_hash:
                        counts["unchanged"] += 1
                        continue
                    older = _order_key(incoming) < _order_key(current)
                    if older and any(
                        r["record_hash"] == record_hash for r in revisions
                    ):
                        counts["unchanged"] += (
                            1  # a late copy of an older state already on record
                        )
                        continue
                    change = "history" if older else "revised"
                else:
                    change = "new"
                revision = incoming["revision"]
                revision_id = "astro-rev:" + digest([rid, revision, record_hash])[:24]
                self.conn.execute(
                    "INSERT INTO astronomy_record_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        rid,
                        revision,
                        revision_id,
                        record_hash,
                        canonical(record),
                        change,
                        incoming["stated_at"],
                        source_as_of,
                        run_id,
                        int(observed_at_ms),
                    ],
                )
                for key in object_keys(record):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO astronomy_record_keys VALUES (?,?,?)",
                        [namespace, rid, key],
                    )
                if not revisions:
                    self.conn.execute(
                        "INSERT INTO astronomy_records VALUES (?,?,?,?,?,?)",
                        [
                            namespace,
                            rid,
                            record["kind"],
                            provider,
                            source_record_id,
                            int(observed_at_ms),
                        ],
                    )
                    counts["inserted"] += 1
                else:
                    counts["history" if change == "history" else "revised"] += 1
                if change != "history":
                    changed.append(rid)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**counts, "changed": changed}

    def observe_listing(
        self,
        namespace: str,
        *,
        provider: str,
        kinds: Sequence[str],
        scope_keys: Iterable[str],
        seen: Iterable[str],
        observed_at_ms: int,
        run_id: str,
        id_prefix: str = "",
    ) -> dict[str, list[str]]:
        """Record what a *complete* bounded listing no longer (or again) shows.

        In scope are the provider's records of ``kinds`` whose source record ID starts with ``id_prefix`` (the
        listed table) and that carry any of ``scope_keys`` (e.g. ``host:<name>`` for a TAP query bounded by
        hosts); ``seen`` holds the source record IDs the listing showed.
        """
        if provider not in PROVIDERS:
            raise AstronomyError("invalid_request", "unknown provider")
        seen, scope_keys = set(seen), sorted(set(scope_keys))
        if not scope_keys or not kinds:
            return {"no_longer_listed": [], "listed_again": []}
        rows = self.conn.execute(
            "SELECT DISTINCT r.record_id, r.source_record_id FROM astronomy_records r JOIN astronomy_record_keys k "
            "ON k.namespace=r.namespace AND k.record_id=r.record_id WHERE r.namespace=? AND r.provider=? "
            "AND r.kind IN ("
            + ",".join("?" * len(kinds))
            + ") AND k.key IN ("
            + ",".join("?" * len(scope_keys))
            + ") AND starts_with(r.source_record_id, ?) ORDER BY r.record_id",
            [namespace, provider, *kinds, *scope_keys, id_prefix],
        ).fetchall()
        marks: dict[str, list[str]] = {"no_longer_listed": [], "listed_again": []}
        self.conn.execute("BEGIN")
        try:
            for rid, source_record_id in rows:
                state = self._listing_state(namespace, rid)
                present = source_record_id in seen
                if not present and state != "no_longer_listed":
                    new_state = "no_longer_listed"
                elif present and state == "no_longer_listed":
                    new_state = "listed_again"
                else:
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO astronomy_listing_observations VALUES (?,?,?,?,?,?,?)",
                    [
                        namespace,
                        rid,
                        new_state,
                        observed_day(observed_at_ms),
                        int(observed_at_ms),
                        run_id,
                        canonical(
                            {
                                "kinds": sorted(kinds),
                                "keys": scope_keys,
                                "id_prefix": id_prefix,
                            }
                        ),
                    ],
                )
                marks[new_state].append(rid)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return marks

    def record_receipt(
        self,
        namespace: str,
        *,
        run_id: str,
        provider: str,
        document: str,
        receipt: Mapping[str, Any],
        observed_at_ms: int,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO astronomy_page_receipts VALUES (?,?,?,?,?,?)",
            [
                namespace,
                run_id,
                provider,
                document,
                canonical(dict(receipt)),
                int(observed_at_ms),
            ],
        )

    # -------------------------------------------------------------- reads

    def _listing_state(
        self, namespace: str, rid: str, *, until_ms: int | None = None
    ) -> str | None:
        row = self.conn.execute(
            "SELECT state FROM astronomy_listing_observations WHERE namespace=? AND record_id=? "
            "AND (? IS NULL OR observed_at_ms<=?) ORDER BY observed_at_ms DESC, state LIMIT 1",
            [namespace, rid, until_ms, until_ms],
        ).fetchone()
        return row[0] if row else None

    def _listing(
        self, namespace: str, rid: str, until_ms: int | None
    ) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT state, observed_on, observed_at_ms, run_id FROM astronomy_listing_observations "
            "WHERE namespace=? AND record_id=? AND (? IS NULL OR observed_at_ms<=?) ORDER BY observed_at_ms, state",
            [namespace, rid, until_ms, until_ms],
        ).fetchall()
        history = [
            dict(zip(("state", "observed_on", "observed_at_ms", "run_id"), r))
            for r in rows
        ]
        latest = history[-1] if history else None
        listed = latest is None or latest["state"] == "listed_again"
        result: dict[str, Any] = {
            "state": "listed" if listed else "no_longer_listed",
            "history": history,
        }
        if not listed:
            result["observed_on"] = latest["observed_on"]
        return result

    @staticmethod
    def _clock(
        row: Mapping[str, Any], first_observed: int, index: int, first_clock: int
    ) -> tuple[int, str]:
        """A revision's publication clock: its stated date, else the document date, else its observation.

        The first revision in source order without a stated date counts from the record's first observation; a
        later revision is never visible before the record's first publication.
        """
        stated = row.get("stated_at") or row.get("source_as_of")
        own = (
            clock_ms(stated)
            if stated
            else (first_observed if index == 0 else int(row["observed_at_ms"]))
        )
        return (own if index == 0 else max(first_clock, own)), (
            "stated" if stated else "first-observed"
        )

    def visible(
        self,
        namespace: str,
        *,
        kinds: Sequence[str] | None = None,
        keys: Iterable[str] | None = None,
        public_cutoff_ms: int | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Each record's revision visible at the cutoffs, the records not yet published by then, and unreadable rows.

        ``keys`` selects records carrying any of the given object keys (the shared equivalence of
        :func:`src.kb.astronomy_records.object_keys`). ``pending`` lists records acquired by the acquisition
        cutoff whose first publication is after the public cutoff, so an answer can say "not yet published"
        instead of absence. Each view's ``later`` lists revisions published after the cutoff.
        """
        self.require_ready()
        params: list[Any] = [namespace]
        query = (
            "SELECT r.record_id, r.kind, v.revision, v.revision_id, v.record_hash, v.change, v.stated_at, "
            "v.source_as_of, v.run_id, v.observed_at_ms, v.payload_json FROM astronomy_records r JOIN "
            "astronomy_record_revisions v ON v.namespace=r.namespace AND v.record_id=r.record_id "
            "WHERE r.namespace=?"
        )
        if kinds:
            unknown = set(kinds) - set(KINDS)
            if unknown:
                raise AstronomyError(
                    "invalid_request", f"unknown kinds {sorted(unknown)}"
                )
            query += " AND r.kind IN (" + ",".join("?" * len(kinds)) + ")"
            params.extend(kinds)
        if keys is not None:
            keys = sorted(set(keys))
            if not keys:
                return {"records": [], "pending": [], "unreadable": []}
            query += (
                " AND r.record_id IN (SELECT record_id FROM astronomy_record_keys WHERE namespace=? AND key IN ("
                + ",".join("?" * len(keys))
                + "))"
            )
            params.extend([namespace, *keys])
        rows = self.conn.execute(
            query + " ORDER BY r.record_id, v.revision", params
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        fields = ("record_id", "kind", *_REVISION_FIELDS, "payload_json")
        for r in rows:
            grouped.setdefault(r[0], []).append(dict(zip(fields, r)))
        until = (
            None
            if public_cutoff_ms is None and acquired_by_ms is None
            else min(x for x in (public_cutoff_ms, acquired_by_ms) if x is not None)
        )
        views, pending, unreadable = [], [], []
        for rid, revisions in sorted(grouped.items()):
            try:
                if acquired_by_ms is not None:
                    revisions = [
                        r
                        for r in revisions
                        if int(r["observed_at_ms"]) <= acquired_by_ms
                    ]
                if not revisions:
                    continue
                ordered = sorted(revisions, key=_order_key)
                first_observed = min(int(r["observed_at_ms"]) for r in revisions)
                clocks = []
                first_clock = 0
                for index, row in enumerate(ordered):
                    clock, basis = self._clock(row, first_observed, index, first_clock)
                    if index == 0:
                        first_clock = clock
                    clocks.append((row, clock, basis))
                chosen = None
                later = []
                for row, clock, basis in clocks:
                    if public_cutoff_ms is not None and clock > public_cutoff_ms:
                        later.append(
                            {"revision_id": row["revision_id"], "public_at_ms": clock}
                        )
                        continue
                    chosen = (row, clock, basis)
                if chosen is None:
                    pending.append(
                        {
                            "record_id": rid,
                            "kind": revisions[0]["kind"],
                            "first_public_at_ms": min(c for _, c, _ in clocks),
                            "record": json.loads(ordered[0]["payload_json"]),
                        }
                    )
                    continue
                row, clock, basis = chosen
                views.append(
                    {
                        "record_id": rid,
                        "kind": row["kind"],
                        **{
                            k: row[k]
                            for k in _REVISION_FIELDS
                            if row.get(k) is not None
                        },
                        "public_at_ms": int(clock),
                        "publication_basis": basis,
                        "listing": self._listing(namespace, rid, until),
                        "later": later,
                        "record": json.loads(row["payload_json"]),
                    }
                )
            except (ValueError, KeyError, TypeError) as exc:
                unreadable.append(
                    {
                        "record_id": rid,
                        "reason": f"{type(exc).__name__}: {str(exc)[:120]}",
                    }
                )
        return {"records": views, "pending": pending, "unreadable": unreadable}

    def history(self, namespace: str, rid: str) -> list[dict[str, Any]]:
        """Every revision of one record in revision order, with its change class."""
        self.require_ready()
        rows = self.conn.execute(
            "SELECT "
            + ", ".join(_REVISION_FIELDS)
            + ", payload_json FROM astronomy_record_revisions "
            "WHERE namespace=? AND record_id=? ORDER BY revision",
            [namespace, rid],
        ).fetchall()
        if not rows:
            raise AstronomyError(
                "not_found", "record is not on record in this namespace"
            )
        return [
            {
                **{k: v for k, v in zip(_REVISION_FIELDS, r[:-1]) if v is not None},
                "record": json.loads(r[-1]),
            }
            for r in rows
        ]

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        """One revision by id; caller-supplied references are checked against the store."""
        self.require_ready()
        row = self.conn.execute(
            "SELECT record_id, revision, record_hash FROM astronomy_record_revisions WHERE namespace=? "
            "AND revision_id=?",
            [namespace, str(revision_id or "")],
        ).fetchone()
        if row is None:
            raise AstronomyError(
                "not_found", "revision is not on record in this namespace"
            )
        return {
            "record_id": row[0],
            "revision": int(row[1]),
            "revision_id": revision_id,
            "record_hash": row[2],
        }

    def receipts(
        self, namespace: str, *, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.require_ready()
        rows = self.conn.execute(
            "SELECT run_id, provider, document, receipt_json, observed_at_ms FROM astronomy_page_receipts "
            "WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY observed_at_ms, provider, document",
            [namespace, run_id, run_id],
        ).fetchall()
        return [
            {
                "run_id": r[0],
                "provider": r[1],
                "document": r[2],
                "receipt": json.loads(r[3]),
                "observed_at_ms": int(r[4]),
            }
            for r in rows
        ]

    def generation(self, namespace: str) -> str:
        """Changes whenever any revision, listing observation, citation link or identity decision changes."""
        if not self.ready():
            return "astronomy-generation:empty"
        revisions = self.conn.execute(
            "SELECT count(*), coalesce(max(observed_at_ms), 0), coalesce(string_agg(revision_id, ',' ORDER BY "
            "revision_id), '') FROM astronomy_record_revisions WHERE namespace=?",
            [namespace],
        ).fetchone()
        listings = self.conn.execute(
            "SELECT count(*), coalesce(max(observed_at_ms), 0) FROM astronomy_listing_observations "
            "WHERE namespace=?",
            [namespace],
        ).fetchone()
        links: list[Any] = []
        if self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='astronomy_citation_links'"
        ).fetchone():
            links = list(
                self.conn.execute(
                    "SELECT count(*), coalesce(string_agg(link_id || ':' || state, ',' ORDER BY link_id), '') "
                    "FROM astronomy_citation_links WHERE namespace=?",
                    [namespace],
                ).fetchone()
            )
        candidates: list[Any] = []
        if self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='astronomy_identity_candidates'"
        ).fetchone():
            # Reviewed identity shapes answers (provider and site links), so a decision is a new generation.
            candidates = list(
                self.conn.execute(
                    "SELECT count(*), coalesce(string_agg(candidate_id || ':' || state || ':' || "
                    "coalesce(decision_id, ''), ',' ORDER BY candidate_id), '') "
                    "FROM astronomy_identity_candidates WHERE namespace=?",
                    [namespace],
                ).fetchone()
            )
        return (
            "astronomy-generation:"
            + digest([list(revisions), list(listings), links, candidates])[:24]
        )


# ------------------------------------------------------------------ runtime projector


class AstronomyProjector:
    """Source-pack runtime projector for ``noesis-astronomy-record-v1`` pages."""

    def __init__(self, conn: Any) -> None:
        self.store = AstronomyStore(conn)

    def project_page(
        self,
        *,
        run_id,
        manifest,
        source,
        records,
        documents,
        page_receipt,
        principal_id,
    ):
        del manifest, principal_id
        declared = dict(source.get("astronomy") or {})
        namespace = str(declared.get("namespace") or DEFAULT_NAMESPACE)
        observed = max(
            (
                int(d["ingested_at"])
                for d in documents or []
                if d.get("ingested_at") is not None
            ),
            default=self.store.now(),
        )
        items = [
            dict(item["astronomy_record"])
            for item in records
            if item.get("astronomy_record")
        ]
        for item in items:
            item["source"] = {**item["source"], "retrieved_at_ms": observed}
        receipt = dict(page_receipt or {})
        try:
            counts = self.store.apply(
                namespace,
                items,
                run_id=run_id,
                observed_at_ms=observed,
                source_as_of=receipt.get("source_as_of"),
            )
        except AstronomyError as exc:
            from src.ingestion.source_packs import SourcePackError

            raise SourcePackError("mapping_failed", str(exc)) from exc
        listing = receipt.get("listing") or {}
        marks: dict[str, list[str]] = {}
        if listing.get("complete"):
            marks = self.store.observe_listing(
                namespace,
                provider=declared["provider"],
                kinds=listing.get("kinds") or [],
                scope_keys=listing.get("scope_keys") or [],
                seen=listing.get("seen") or [],
                observed_at_ms=observed,
                run_id=run_id,
                id_prefix=str(listing.get("id_prefix") or ""),
            )
        self.store.record_receipt(
            namespace,
            run_id=run_id,
            provider=declared["provider"],
            document=str(receipt.get("document") or ""),
            receipt={
                **{k: v for k, v in receipt.items() if k != "listing"},
                "listing": {k: v for k, v in listing.items() if k != "seen"},
                "stored": {k: v for k, v in counts.items() if k != "changed"},
                "marks": marks,
            },
            observed_at_ms=observed,
        )
        return {k: v for k, v in counts.items() if k != "changed"}

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}


# ------------------------------------------------------------------ feature selection

FEATURES = ("astronomy-launches", "astronomy-space-weather")


def active_plan(conn: Any) -> dict[str, Any] | None:
    """The active composition plan when the astronomy bundle is composition-managed (reads only)."""
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return None
        managed = conn.execute(
            "SELECT authority FROM composition_authority WHERE bundle='astronomy'"
        ).fetchone()
        if not managed or managed[0] != "composition":
            return None
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        return json.loads(row[0]) if row else None
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return None


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the Astronomy bundle's optional feature is selected (default off; reads only)."""
    if feature not in FEATURES:
        raise AstronomyError("invalid_request", f"feature is one of {FEATURES}")
    plan = active_plan(conn) or {}
    return feature in ((plan.get("features") or {}).get("astronomy") or [])
