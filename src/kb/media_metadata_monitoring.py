"""Monitor authority revisions, merges, redirects and deprecations through subscriptions (#2225, MM10).

A media metadata monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), following
:mod:`src.kb.food_monitoring`. There is no watcher table and no scheduler.
Targets are records (``source:native_id`` or a record ID) and identifiers
(ISBN, ISRC, MBID, QID, GND, LCCN and so on, bare or ``scheme:value``). An
identifier watches every record keyed by it or asserting it, plus the targets
its redirects lead to.

Each run evaluates one committed watermark against a cumulative snapshot. The
subscription store turns new items into events, which are delivered by poll
or outbox. Every item is dated and cites both revisions:

* ``authority_revision``: a revision became current. The first one names no
  previous revision.
* ``redirect`` and ``merge``: the provider redirected the record, or merged
  it (a MusicBrainz MBID), to another native ID.
* ``deprecation``: the provider deleted or deprecated the record.
* ``identifier_assertion``: a record revision newly carries a watched
  identifier.

:meth:`MediaMetadataMonitor.refresh` re-acquires a source's pinned selection
through its runtime adapter. It stays within the page budget, is paced by the
provider's minimum interval, records one receipt per run, stops at the first
rate-limit answer and waits for the provider's Retry-After. A replayed
payload adds no revision, so it emits no event.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.media_metadata import (
    READ_SCOPE,
    SCHEMES,
    SOURCE_PACK,
    SOURCES,
    WRITE_SCOPE,
    MediaMetadataError,
    MediaMetadataProjector,
    MediaMetadataStore,
    authorize,
    canonical,
    detect_scheme,
    digest,
    iso_from_ms,
    normalize_identifier,
    table_exists,
)

CONTRACT = "noesis-media-metadata-notification-v1"
EVENT_KINDS = ("authority_revision", "redirect", "merge", "deprecation", "identifier_assertion")
WATCH_KEYS = ("records", "identifiers")
MESSAGES = {
    "authority_revision": "A watched authority record has a new revision",
    "redirect": "A watched authority record was redirected to another record",
    "merge": "A watched record was merged into another record by its provider",
    "deprecation": "A watched authority record was deprecated or deleted by its provider",
    "identifier_assertion": "A record now carries a watched identifier",
}
_DDL = """
CREATE TABLE IF NOT EXISTS media_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def identifier_key(value: str) -> str:
    """``scheme:value`` or a bare identifier -> its normalized key (refused when invalid or unrecognised)."""
    text = str(value or "").strip()
    scheme, _, rest = text.partition(":")
    if scheme.casefold() in SCHEMES and rest:
        scheme, text = scheme.casefold(), rest
    else:
        scheme = detect_scheme(text)
    if scheme is None:
        raise MediaMetadataError("invalid_watch", f"{value!r} is not a recognised identifier; prefix its scheme")
    normalized = normalize_identifier(scheme, text)
    if not normalized["valid"]:
        raise MediaMetadataError("invalid_watch", f"{value!r}: {normalized['reason']}")
    return normalized["key"]


class MediaMetadataMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = MediaMetadataStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ create

    def _watch(self, namespace: str, watch: Mapping[str, Any]) -> dict[str, list[str]]:
        raw = dict(watch or {})
        if set(raw) - set(WATCH_KEYS) or not any(raw.get(k) for k in WATCH_KEYS):
            raise MediaMetadataError("invalid_watch", f"watch at least one of {WATCH_KEYS}")
        records = sorted({self.store.resolve_record(namespace, str(r)) for r in raw.get("records") or []})
        identifiers = sorted({identifier_key(str(i)) for i in raw.get("identifiers") or []})
        return {"records": records, "identifiers": identifiers}

    def create(self, namespace: str, request_key: str, *, watch: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        normalized = self._watch(namespace, watch)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "science",
             "query": {"operation": "search", "kind": "media-metadata-monitor", "watch": normalized},
             "filters": {"watch": sorted(k for k, v in normalized.items() if v)},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "media-metadata-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": f"the {SOURCE_PACK} media-metadata sources' schedule (or refresh()) acquires "
                                      "revisions and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "media-metadata-monitor":
            raise MediaMetadataError("monitor_not_found", "subscription is not a media metadata monitor")
        return subscription

    # ------------------------------------------------------------------ snapshot

    def _chain(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        """Revisions that became current when they arrived (by the provider's own order)."""
        chain, best = [], None
        for revision in self.store.revisions(namespace, record_id):
            if best is None or revision["order_key"] is None or best["order_key"] is None \
                    or revision["order_key"] >= best["order_key"]:
                chain.append(revision)
                best = revision
        return chain

    def _cite(self, namespace: str, record_id: str, revision: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if revision is None:
            return None
        cite = self.store.citation(namespace, record_id, revision)
        return {k: cite[k] for k in ("record_id", "source", "native_id", "record_type", "revision_id",
                                     "revision_marker", "revision_basis", "revision_date", "retrieved_at", "status")}

    def _record_items(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        head = self.store.record(namespace, record_id)
        items, previous = [], None
        for revision in self._chain(namespace, record_id):
            cite = self._cite(namespace, record_id, revision)
            before = self._cite(namespace, record_id, previous)
            dated = revision["revision_date"] or (revision["retrieved_at"] or "")[:10]
            items.append({"id": f"revision:{revision['revision_id']}", "kind": "authority_revision", "date": dated,
                          "cites": {"revision": cite, "previous_revision": before}})
            if revision["status"] != (previous or {}).get("status", "active"):
                statement = self.store.statement(namespace, revision["revision_id"])
                if revision["status"] == "redirected":
                    kind = "merge" if head["source"] == "musicbrainz" else "redirect"
                    target_id = self.store.find(namespace, head["source"], statement["redirect_to"])
                    target = self.store.current_revision(namespace, target_id) if target_id else None
                    items.append({"id": f"{kind}:{revision['revision_id']}", "kind": kind, "date": dated,
                                  "cites": {"revision": cite, "previous_revision": before,
                                            "target": {"source": head["source"], "native_id": statement["redirect_to"],
                                                       "revision": self._cite(namespace, target_id, target)
                                                       if target_id else None}}})
                elif revision["status"] in {"deprecated", "deleted"}:
                    items.append({"id": f"deprecation:{revision['revision_id']}", "kind": "deprecation",
                                  "date": dated, "status": revision["status"],
                                  "cites": {"revision": cite, "previous_revision": before}})
            previous = revision
        return items

    def _targets(self, namespace: str, watch: Mapping[str, Any]) -> tuple[set[str], list[dict[str, Any]]]:
        records, items = set(watch["records"]), []
        for key in watch["identifiers"]:
            rows = self.conn.execute(
                "SELECT i.record_id, i.revision_id, r.revision_date, r.retrieved_at_ms, i.role FROM media_identifiers "
                "i JOIN media_revisions r ON r.namespace=i.namespace AND r.revision_id=i.revision_id WHERE "
                "i.namespace=? AND i.key=? ORDER BY r.seq", [namespace, key]).fetchall()
            first: dict[str, tuple] = {}
            for row in rows:
                first.setdefault(row[0], row)
            for record_id, (_, revision_id, revision_date, retrieved_at_ms, role) in sorted(first.items()):
                records.add(record_id)
                revision = next(r for r in self.store.revisions(namespace, record_id)
                                if r["revision_id"] == revision_id)
                items.append({"id": f"holder:{record_id}:{key}", "kind": "identifier_assertion",
                              "date": revision_date.isoformat() if revision_date else iso_from_ms(retrieved_at_ms)[:10],
                              "identifier": key, "role": role,
                              "cites": {"revision": self._cite(namespace, record_id, revision),
                                        "previous_revision": None}})
        for record_id in sorted(records):
            head = self.store.record(namespace, record_id)
            for native in self.store.follow(namespace, head["source"], head["native_id"])[1:]:
                target = self.store.find(namespace, head["source"], native)
                if target:
                    records.add(target)
        return records, items

    def snapshot(self, subscription: Mapping[str, Any]) -> dict[str, Any]:
        namespace, watch = subscription["namespace"], subscription["query"]["watch"]
        if not self.store.ready():
            return {"items": [], "coverage": {"complete": True}}
        records, items = self._targets(namespace, watch)
        for record_id in sorted(records):
            try:
                items += self._record_items(namespace, record_id)
            except MediaMetadataError:
                continue  # a watched record not (yet) visible watches nothing
        return {"items": items, "coverage": {"complete": True}}

    # ------------------------------------------------------------------ run

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        """The namespace's current media state as a watermark: reused when already committed, else a new one."""
        latest = self.conn.execute("SELECT max(retrieved_at_ms) FROM media_revisions WHERE namespace=?",
                                   [namespace]).fetchone()[0] if self.store.ready() else None
        if latest is None:
            raise MediaMetadataError("not_ready", "no media metadata revision yet; acquire first")
        revisions = self.conn.execute("SELECT revision_id FROM media_revisions WHERE namespace=? ORDER BY revision_id",
                                      [namespace]).fetchall()
        generation = digest([r[0] for r in revisions])[:24]
        rows = (self.conn.execute(
            "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark",
            [namespace]).fetchall() if table_exists(self.conn, "knowledge_subscription_watermarks") else [])
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("media_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(int(latest), highest + 1), {"media_generation": generation, "observed_at": iso_from_ms(latest)}

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            watermark, detail = self.default_watermark(namespace)
            self.subscriptions.commit_watermark(namespace, watermark, kind="ingestion", detail=detail)
        evaluated = self.subscriptions.evaluate(subscription_id, int(watermark), self.snapshot(subscription),
                                                principal_id=principal_id, scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            event_type, key, after = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id]).fetchone()
            if event_type != "added" or not after:
                continue  # items are cumulative; only additions are news
            item = json.loads(after)
            notifications.append({
                "contract": CONTRACT, "event_id": event_id, "notification_id": f"{event_id}:{item['kind']}",
                "object": key, "kind": item["kind"], "date": item["date"], "message": MESSAGES[item["kind"]] + ".",
                "cites": item["cites"], **{k: item[k] for k in ("identifier", "role", "status") if k in item},
                "note": "a published authority change, reported as published; no interpretation or ranking"})
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": int(watermark),
                "notifications": notifications, "delivery": subscription["delivery"]}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, secret: str | None = None,
                max_pages: int | None = None, sleep: Callable[[float], None] | None = None) -> dict[str, Any]:
        """Re-acquire a source's pinned selection within its page budget and the provider's pace; idempotent, one
        receipt per run, stopped at the first rate-limit answer and refused before its Retry-After has passed."""
        from src.ingestion.media_metadata_sources import MediaMetadataAdapter
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        provider = dict(source.get("media_metadata") or {}).get("provider")
        if source.get("connector") != "media-metadata" or provider not in SOURCES:
            raise MediaMetadataError("invalid_source", "refresh a media-metadata source of the scientific pack")
        now = self.now()
        waiting = self.conn.execute(
            "SELECT retry_at_ms FROM media_refresh_receipts WHERE namespace=? AND source_id=? "
            "ORDER BY started_at_ms DESC LIMIT 1", [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = MediaMetadataAdapter(source, transport=transport, secret=secret, sleep=sleep)
        limit = min(len(adapter.entries), int(max_pages or source["budgets"]["max_pages"]))
        projector = MediaMetadataProjector(self.conn)
        projector.store.now = self.now
        pages, stopped, retry_at, cursor = [], None, None, None
        for _ in range(limit):
            try:
                page = adapter.fetch_page({"operation": "media", "parameters": {},
                                           "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            except SourcePackError as exc:
                stopped = {"code": exc.code, "message": str(exc)}
                if exc.code == "rate_limited":
                    retry_at = now + int(getattr(exc, "details", {}).get("retry_after_ms") or 60_000)
                break
            counts = projector.project_page(run_id=f"refresh:{source['source_id']}:{now}", manifest=None,
                                            source=source, records=page.records, documents=None,
                                            page_receipt=page.receipt, principal_id=principal_id)
            pages.append({"selection_index": page.receipt.get("selection_index"),
                          "outcome": page.receipt.get("selector_outcome"), "counts": counts,
                          "response_sha256": page.receipt.get("response_sha256")})
            cursor = page.next_cursor
            if cursor is None:
                break
        status = "stopped" if stopped else "complete" if cursor is None else "bounded"
        return self._receipt(namespace, source, now, status, pages, stopped, retry_at, principal_id,
                             min_interval_ms=adapter.definition["media_metadata"]["min_interval_ms"])

    def _receipt(self, namespace, source, now, status, pages, stopped, retry_at, principal_id, note=None,
                 min_interval_ms=None):
        body = {
            "source_id": source["source_id"], "status": status, "pages": pages,
            "new_revisions": sum(p["counts"].get("created", 0) + p["counts"].get("revised", 0)
                                 + p["counts"].get("history", 0) for p in pages),
            "unchanged": sum(p["counts"].get("unchanged", 0) for p in pages),
            "conflicts": sum(p["counts"].get("conflict", 0) for p in pages),
            "stopped": stopped, "retry_at": iso_from_ms(retry_at), "requested_by": principal_id,
            "min_interval_ms": min_interval_ms, "at": iso_from_ms(now), "note": note,
        }
        receipt_id = "media-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO media_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["CONTRACT", "EVENT_KINDS", "MediaMetadataMonitor", "identifier_key"]
