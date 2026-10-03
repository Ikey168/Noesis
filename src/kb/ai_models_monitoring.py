"""Monitor AI model and dataset records through subscriptions (#2795, AI10; track #2742).

An AI models monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
``platform.subscriptions``) following :mod:`src.kb.business_statistics_monitoring`: no watcher table and no scheduler.
Its target is one model or dataset (a record id, a Hub repository id, an OpenML dataset id or an Epoch model name) or a
whole source. Each run evaluates one committed watermark against a snapshot of every watched record's revisions; the
subscription store turns new items into events, so a replay, an idempotent re-acquisition or an unchanged source emits
nothing.

Notices are record changes, not assessments. Each cites the new, revised or removed record revision and the one before
it, and states what changed:

* ``new_revision`` - a new Hub ``sha``, a new OpenML dataset or task revision, a revised Epoch row;
* ``licence_change`` - the declared licence fields changed, quoted before and after as declared;
* ``withdrawn`` - the source states the repository gated or disabled, or the dataset deactivated;
* ``removed_by_source`` - the repository answers 404, or a complete later Epoch file no longer has a declared row;
* ``renamed`` - the source redirected the repository id (recorded, never followed to another host).

:meth:`AiModelsMonitor.refresh` re-reads one declared source through its real adapter within the source's page budget,
records a receipt per run and is idempotent (an unchanged unit adds nothing). A failed run records a failure receipt
and stops: it never produces a removal, withdrawal or not-returned record, so it never produces such a notice. Live
evidence from providers still ``unverified-live`` is withheld from notices until a dated live run verifies them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.ai_models_sources import PROVIDERS, unverified
from src.kb.ai_models_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    AiModelsError,
    authorize,
    canonical,
    digest,
    iso,
    table_exists,
)
from src.kb.ai_models_store import (
    AiModelsProjector,
    AiModelsStore,
    citation,
    revision_reference,
)

CONTRACT = "noesis-ai-model-notification-v1"
FILTER_KEYS = ("record_id", "repo_id", "kind", "openml_dataset_id", "epoch_model", "source")
MESSAGES = {
    "new_revision": "A source stated a new revision of a watched model or dataset record",
    "licence_change": "The declared licence of a watched model or dataset changed between revisions",
    "withdrawn": "A source states a watched repository gated or disabled, or a watched dataset deactivated",
    "removed_by_source": "A source no longer serves or lists a watched model or dataset record",
    "renamed": "A source redirected a watched repository to another id",
}
_DDL = """
CREATE TABLE IF NOT EXISTS ai_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def change_kinds(revision: Mapping[str, Any]) -> list[str]:
    state, changes = revision["state"], dict(revision.get("changes") or {})
    if state in {"withdrawn", "removed_by_source", "renamed"}:
        return [state]
    kinds = ["new_revision"]
    if changes.get("licence_change") and not changes.get("first_revision"):
        kinds.append("licence_change")
    return kinds


def notifiable(item: Mapping[str, Any]) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return item["evidence_origin"] != "live" or not unverified(item["source"])


class AiModelsMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = AiModelsStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(target: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(target or {})
        if set(raw) - set(FILTER_KEYS):
            raise AiModelsError("invalid_watch", f"an AI models monitor target uses {FILTER_KEYS}")
        if not any(raw.get(k) not in (None, "") for k in FILTER_KEYS if k != "kind"):
            raise AiModelsError("invalid_watch", "watch a record, a repository, an OpenML dataset, an Epoch model or a "
                                                 "source")
        if raw.get("source") is not None and raw["source"] not in PROVIDERS:
            raise AiModelsError("invalid_watch", f"source is one of {PROVIDERS}")
        return {k: raw.get(k) for k in FILTER_KEYS}

    def create(self, namespace: str, request_key: str, *, target: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = self._filter(target)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "technical",
             "query": {"operation": "search", "kind": "ai-models-monitor", "filter": wanted},
             "filters": {"watch": "ai-models"}, "cadence": {"trigger": "watermark"},
             "delivery": delivery or {"kind": "poll"}},
            "ai-models-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the technology-ai-models source-pack schedule (or refresh()) acquires revisions "
                "and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "ai-models-monitor":
            raise AiModelsError("monitor_not_found", "subscription is not an AI models monitor")
        return subscription

    def watched(self, namespace: str, wanted: Mapping[str, Any]) -> list[dict[str, Any]]:
        from src.kb.ai_models_queries import AiModelsQueries

        if any(wanted.get(k) not in (None, "") for k in ("record_id", "repo_id", "openml_dataset_id",
                                                           "epoch_model")):
            subject = {k: v for k, v in wanted.items() if v not in (None, "") and k != "source"}
            record = AiModelsQueries(self.conn).resolve(namespace, subject)
            return [record] if record else []
        return [r for r in self.store.records(namespace, source=wanted.get("source")) if r["record_kind"] != "hub-refs"]

    def _items(self, namespace: str, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        items, previous = [], None
        for revision in self.store.revision_rows(namespace, record["record_id"], order="seq"):
            for kind in change_kinds(revision):
                changes = dict(revision["changes"])
                detail = {"new_revision": {"revision": revision_reference(record, revision),
                                           "changed_fields": changes.get("changed_fields", []),
                                           "first_revision": bool(changes.get("first_revision"))},
                          "licence_change": changes.get("licence_change"),
                          "withdrawn": revision["statement"].get("state_detail") or {
                              "status": revision["statement"].get("status")},
                          "removed_by_source": revision["statement"].get("state_detail"),
                          "renamed": revision["statement"].get("state_detail")}[kind]
                items.append({
                    "id": f"revision:{revision['revision_id']}:{kind}", "item": kind,
                    "record": {k: record[k] for k in ("record_id", "record_kind", "native_key", "label", "source")},
                    "revision_id": revision["revision_id"],
                    "previous_revision_id": None if previous is None else previous["revision_id"],
                    "at": revision["source_time"], "detail": detail, "citation": citation(record, revision),
                    "evidence_origin": revision["evidence_origin"], "source": record["source"],
                })
            previous = revision
        return items

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        items = [i for r in self.watched(namespace, wanted) for i in self._items(namespace, r)]
        kept = [i for i in items if notifiable(i)]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        latest = self.store.latest_ms(namespace)
        if latest is None:
            raise AiModelsError("not_ready", "no AI models record yet; acquire first")
        generation = self.store.generation(namespace)
        rows = self.conn.execute(
            "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark",
            [namespace]).fetchall() if table_exists(self.conn, "knowledge_subscription_watermarks") else []
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("ai_models_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {"ai_models_generation": generation, "observed_at": iso(latest)}

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
            row = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": int(watermark),
                "notifications": notifications, "withheld_unverified_live_items": withheld,
                "delivery": subscription["delivery"],
                "note": "notices report record changes as the sources state them; nothing is assessed, ranked or "
                        "interpreted"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        return [{
            "contract": CONTRACT, "event_id": event_id, "notification_id": f"{event_id}:{item['item']}",
            "object": key, "kind": item["item"],
            "message": f"{MESSAGES[item['item']]} ({item['source']}, {item['record']['native_key']}, {item['at']}).",
            "record": item["record"], "revision_id": item["revision_id"],
            "previous_revision_id": item["previous_revision_id"], "what_changed": item["detail"],
            "citation": item["citation"],
        }]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, secret: str | None = None,
                max_units: int | None = None, retrieved_at_ms: int | None = None) -> dict[str, Any]:
        """Re-read a source's declared units within its budget; idempotent by content, one receipt per run."""
        from src.ingestion.ai_models_sources import AiModelsAdapter
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT max(retry_at_ms) FROM ai_refresh_receipts WHERE namespace=? AND source_id=?",
            [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = AiModelsAdapter(source, transport=transport, secret=secret)
        units = len(adapter.declared["units"])
        limit = min(units, int(max_units or units), int(source["budgets"]["max_pages"]))
        projector = AiModelsProjector(self.conn)
        projector.store.now = (lambda: retrieved_at_ms) if retrieved_at_ms is not None else self.now
        applied_units, stopped, retry_at, cursor = [], None, None, None
        run_id = f"refresh:{source['source_id']}:{now}"
        for _ in range(limit):
            try:
                page = adapter.fetch_page({"operation": "registry", "parameters": {},
                                           "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            except SourcePackError as exc:
                stopped = {"code": exc.code, "message": exc.message, **exc.details}
                if exc.code == "rate_limited":
                    retry_at = now + int(exc.details.get("retry_after_ms") or 86_400_000)
                break
            applied = projector.project_page(run_id=run_id, manifest=None, source=source, records=page.records,
                                             documents=None, page_receipt=page.receipt, principal_id=principal_id)
            applied_units += [{"unit_key": a["unit_key"], "status": a["status"], "revisions": a["revisions"],
                               "removed": a["removed"], "not_returned": a["not_returned"]} for a in applied]
            cursor = page.next_cursor
            if cursor is None:
                break
        if stopped:
            projector.store.record_failure(namespace, adapter.provider, code=stopped["code"], run_id=run_id,
                                           source_id=source["source_id"], scopes=scopes)
        status = "stopped" if stopped else "complete" if cursor is None else "bounded"
        return self._receipt(namespace, source, now, status, applied_units, stopped, retry_at, principal_id)

    def _receipt(self, namespace, source, now, status, units, stopped, retry_at, principal_id, note=None):
        body = {"source_id": source["source_id"], "status": status, "units": units,
                "new_units": sum(1 for u in units if u["status"] == "applied"),
                "unchanged_units": sum(1 for u in units if u["status"] == "unchanged"),
                "stopped": stopped, "retry_at": iso(retry_at), "requested_by": principal_id, "at": iso(now),
                "note": note}
        receipt_id = "ai-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO ai_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["CONTRACT", "FILTER_KEYS", "MESSAGES", "AiModelsMonitor", "change_kinds", "notifiable"]
