"""Monitor life-science records through subscriptions (#2652, LS11 #2706).

A life-science monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
delivered through :mod:`src.kb.subscription_delivery`) whose query names one accession (any source), one target (a
ChEMBL target ID or a UniProt accession resolved through published target components) or one NCBI taxon: no watcher
table and no new scheduler. Each run evaluates a committed watermark against a snapshot of the watched records'
revisions; the subscription store turns new items into events. A replay, a restart or a release that changes
nothing emits nothing, because an unchanged record keeps its revision (only its release membership grows).

Notices are record changes cited to the new (and previous) revision; they never assess a change:

* ``new_entry`` - the first revision of a watched record (for a taxon, also a new record of that organism);
* ``entry_revised`` - a new version of a watched record, with the attribute fields, cross-references and citations
  that differ from the previous revision;
* ``entry_obsoleted`` - a revision that merges, replaces, obsoletes, demerges or deletes the record, with the
  successors the source names;
* ``new_activity`` / ``activity_revised`` - an activity against a watched target first published, or republished with
  a changed value, relation, unit or data-validity comment.

:meth:`LifeSciMonitor.refresh` re-reads a watched source's declared documents through the real adapter within its
page budget, idempotently, with a receipt per run; it stops at the first rate-limit answer and waits for Retry-After.
Live revisions from providers that are still ``unverified-live`` are withheld from notices.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.lifesci_sources import unverified
from src.kb.lifesci_records import (
    INACTIVE,
    NOTIFICATION_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    LifeSciError,
    authorize,
    canonical,
    detect_reference,
    digest,
    table_exists,
)
from src.kb.lifesci_store import LifeSciProjector, LifeSciStore, iso_from_ms

FILTER_KEYS = ("accession", "target", "taxon", "providers")
EVENT_TYPES = ("new_entry", "entry_revised", "entry_obsoleted", "new_activity", "activity_revised")
MESSAGES = {
    "new_entry": "A watched life-science record was first published",
    "entry_revised": "A new version of a watched life-science record was published",
    "entry_obsoleted": "A watched life-science record was merged, replaced, obsoleted or deleted by its source",
    "new_activity": "A new activity against a watched target was published",
    "activity_revised": "An activity against a watched target was republished with changes",
}
_DDL = """
CREATE TABLE IF NOT EXISTS lifesci_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def notifiable(revision: Mapping[str, Any], source: str) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return revision.get("evidence_origin") != "live" or not unverified(source)


def _changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    fields = sorted(k for k in set(before["attributes"]) | set(after["attributes"])
                    if before["attributes"].get(k) != after["attributes"].get(k))
    return {"attributes": fields, "xrefs_changed": before["xrefs"] != after["xrefs"],
            "citations_changed": before["citations"] != after["citations"],
            "status": {"before": before["status"], "after": after["status"]} if before["status"] != after["status"]
            else None}


class LifeSciMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = LifeSciStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(watch: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(watch or {})
        chosen = [k for k in ("accession", "target", "taxon") if raw.get(k)]
        if set(raw) - set(FILTER_KEYS) or len(chosen) != 1:
            raise LifeSciError("invalid_watch", f"a monitor names one accession, target or taxon; keys {FILTER_KEYS}")
        key = chosen[0]
        value = str(raw[key]).strip()
        if key == "taxon" and not value.isdigit():
            raise LifeSciError("invalid_watch", "a taxon is watched by its NCBI Tax ID")
        if key != "taxon" and not detect_reference(value):
            raise LifeSciError("invalid_watch", "name an accession the life-science sources publish")
        return {key: value, "providers": sorted({str(p) for p in raw.get("providers") or []})}

    def create(self, namespace: str, request_key: str, *, watch: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = self._filter(watch)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "scientific",
             "query": {"operation": "search", "kind": "lifesci-monitor", "filter": wanted},
             "filters": {"watch": "life-sciences"}, "cadence": {"trigger": "watermark"},
             "delivery": delivery or {"kind": "poll"}},
            "lifesci-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the primary-scientific-evidence source-pack schedule (or refresh()) acquires "
                "releases and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "lifesci-monitor":
            raise LifeSciError("monitor_not_found", "subscription is not a life-science monitor")
        return subscription

    # ------------------------------------------------------------------ snapshot

    def _record_items(self, namespace: str, record_id: str, *, first_kind: str = "new_entry",
                      revised_kind: str = "entry_revised") -> list[dict[str, Any]]:
        head = self.store.record(namespace, record_id)
        items, previous = [], None
        for revision in self.store.revisions(namespace, record_id):
            statement = self.store.statement(namespace, revision["revision_id"])
            if previous is None:
                kind, changes = first_kind, None
            else:
                changes = _changes(previous[1], statement)
                kind = "entry_obsoleted" if revision["status"] in INACTIVE and \
                    previous[0]["status"] not in INACTIVE else revised_kind
            items.append({
                "id": f"revision:{revision['revision_id']}", "item": kind, "record": head,
                "revision_id": revision["revision_id"], "version_marker": revision["marker"],
                "previous_revision_id": previous[0]["revision_id"] if previous else None,
                "status": revision["status"], "successors": revision["successors"], "changes": changes,
                "record_ids": [record_id, revision["revision_id"]] + ([previous[0]["revision_id"]] if previous else []),
                "source_revision": self.store.citation(namespace, revision, answered=False),
                "_notifiable": notifiable(revision, head["source"]),
            })
            previous = (revision, statement)
        return items

    def watched(self, namespace: str, wanted: Mapping[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if wanted.get("accession"):
            for record_id in self.store.resolve(namespace, wanted["accession"]):
                items += self._record_items(namespace, record_id)
        elif wanted.get("target"):
            from src.kb.lifesci_queries import LifeSciQueries

            targets, _ = LifeSciQueries(self.conn)._targets(namespace, wanted["target"], None)
            for target_id, _version in targets:
                items += self._record_items(namespace, target_id)
                chembl_id = self.store.record(namespace, target_id)["native_id"]
                activities = {row["record_id"] for row in self.store.xrefs_naming(namespace, {"ChEMBL"}, chembl_id)
                              if self.store.record(namespace, row["record_id"])["record_type"] == "activity"}
                for record_id in sorted(activities):
                    items += self._record_items(namespace, record_id, first_kind="new_activity",
                                                revised_kind="activity_revised")
        else:
            tax_id = wanted["taxon"]
            for record_id in self.store.find(namespace, "ncbi-taxonomy", tax_id, "taxon"):
                items += self._record_items(namespace, record_id)
            organisms = {row["record_id"] for row in self.store.xrefs_naming(namespace, {"NCBI Taxonomy"}, tax_id)
                         if row["relation"] == "organism"}
            for record_id in sorted(organisms):
                items += [i for i in self._record_items(namespace, record_id) if i["item"] == "new_entry"]
        providers = set(wanted.get("providers") or [])
        return [i for i in items if not providers or i["record"]["source"] in providers]

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        items = self.watched(subscription["namespace"], subscription["query"]["filter"])
        kept = [{k: v for k, v in i.items() if k != "_notifiable"} for i in items if i["_notifiable"]]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        """The namespace's life-science state as a watermark: reused when already committed (a restart replays it and
        emits nothing), else a new one after every committed watermark."""
        if not self.store.ready() or not self.store.records(namespace):
            raise LifeSciError("not_ready", "no life-science record yet; acquire first")
        generation = self.store.generation(namespace)
        latest = int(self.conn.execute("SELECT max(observed_at_ms) FROM lifesci_revisions WHERE namespace=?",
                                       [namespace]).fetchone()[0])
        rows = (self.conn.execute("SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE "
                                  "namespace=? ORDER BY watermark", [namespace]).fetchall()
                if table_exists(self.conn, "knowledge_subscription_watermarks") else [])
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("lifesci_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {"lifesci_generation": generation, "observed_at": iso_from_ms(latest)}

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            watermark, detail = self.default_watermark(namespace)
            self.subscriptions.commit_watermark(namespace, watermark, kind="ingestion", detail=detail)
        result, withheld = self.snapshot(subscription)
        evaluated = self.subscriptions.evaluate(subscription_id, int(watermark), result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute("SELECT event_type, object_key, after_json FROM knowledge_subscription_events "
                                    "WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": int(watermark),
                "notifications": notifications, "withheld_unverified_live_items": withheld,
                "delivery": subscription["delivery"],
                "note": "notices report published record changes; they never assess a change"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        cite = item["source_revision"]
        return [{
            "contract": NOTIFICATION_CONTRACT, "event_id": event_id, "notification_id": f"{event_id}:{item['item']}",
            "object": key, "kind": item["item"],
            "message": f"{MESSAGES[item['item']]} ({cite['source']} {cite['native_id']}, {cite['version_marker']}, "
                       f"release {cite['release']}).",
            "record": item["record"], "revision_id": item["revision_id"],
            "previous_revision_id": item["previous_revision_id"], "status": item["status"],
            "successors": item["successors"], "changes": item["changes"], "record_ids": item["record_ids"],
            "source_revision": cite,
            "note": "a published record change reported as recorded; nothing is concluded about it",
        }]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, max_documents: int | None = None,
                secret: str | None = None) -> dict[str, Any]:
        """Re-read a source's declared documents within its page budget; idempotent, one receipt per run, stopped at
        the first rate-limit answer and refused before the provider's Retry-After has passed."""
        from src.ingestion.lifesci_sources import LifeSciAdapter
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT retry_at_ms FROM lifesci_refresh_receipts WHERE namespace=? AND source_id=? "
            "ORDER BY started_at_ms DESC LIMIT 1", [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = LifeSciAdapter(source, transport=transport, secret=secret)
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents))
        projector = LifeSciProjector(self.conn)
        projector.store.now = self.now
        pages, stopped, retry_at, cursor = [], None, None, None
        for _ in range(limit):
            try:
                page = adapter.fetch_page({"operation": "release", "parameters": {},
                                           "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            except SourcePackError as exc:
                stopped = {"code": exc.code, "message": exc.message, **exc.details}
                if exc.code == "rate_limited":
                    retry_at = now + int(exc.details.get("retry_after_ms") or 60_000)
                break
            applied = projector.project_page(run_id=f"refresh:{source['source_id']}:{now}", manifest=None,
                                             source=source, records=page.records, documents=None,
                                             page_receipt=page.receipt, principal_id=principal_id)
            pages.append({"document": page.receipt.get("document"), "counts": applied["counts"],
                          "release": page.receipt.get("release")})
            cursor = page.next_cursor
            if cursor is None:
                break
        status = "stopped" if stopped else "complete" if cursor is None else "bounded"
        return self._receipt(namespace, source, now, status, pages, stopped, retry_at, principal_id)

    def _receipt(self, namespace, source, now, status, pages, stopped, retry_at, principal_id, note=None):
        body = {"source_id": source["source_id"], "status": status, "pages": pages,
                "new_revisions": sum(p["counts"]["created"] + p["counts"]["revised"] + p["counts"]["history"]
                                     for p in pages),
                "unchanged": sum(p["counts"]["unchanged"] for p in pages), "stopped": stopped,
                "retry_at": iso_from_ms(retry_at), "requested_by": principal_id, "at": iso_from_ms(now),
                "note": note}
        receipt_id = "lifesci-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO lifesci_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["EVENT_TYPES", "LifeSciMonitor", "notifiable"]
