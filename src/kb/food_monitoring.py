"""Monitor label revisions, composition updates and newly linked notices through subscriptions (#2216, FC09).

A food monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`, ``platform.subscriptions``)
following :mod:`src.kb.product_safety_monitoring` and
:mod:`src.kb.trade_monitoring`: no watcher table and no scheduler. Targets are
GTINs, generic foods (a food id or ``provider:provider_key``) and composition
table editions (a table id such as ``ciqual``). Each run evaluates one
committed watermark against a cumulative snapshot; the subscription store turns
new items into events (poll or outbox). Items, each citing both revisions:

* ``label_revision`` - a revision that became current (the first one names no
  previous revision);
* ``nutrient_value_change`` - the nutrient values that changed between two
  consecutive current revisions, each with the value before and after;
* ``allergen_declaration_change`` - allergen or trace declarations added or
  removed between two consecutive current revisions;
* ``new_linked_notice`` - a notice revision newly citing a watched food product
  (FC07), with the food revision it was linked against;
* ``new_table_edition`` - a new edition of a watched composition table.

:meth:`FoodCompositionMonitor.refresh` acquires a source's pinned selection
through its runtime adapter within the page budget, records a receipt per run
and stops at the first rate-limit answer. Unchanged payloads add no revision,
so they emit no event; replaying a watermark emits nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.food_composition import (
    READ_SCOPE,
    SOURCE_PACK,
    WRITE_SCOPE,
    FoodCompositionError,
    FoodCompositionProjector,
    FoodCompositionStore,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    table_exists,
)

CONTRACT = "noesis-food-notification-v1"
EVENT_KINDS = ("label_revision", "nutrient_value_change", "allergen_declaration_change", "new_linked_notice",
               "new_table_edition")
WATCH_KEYS = ("gtins", "foods", "table_editions")
MESSAGES = {
    "label_revision": "A watched food has a new label revision",
    "nutrient_value_change": "Nutrient values of a watched food changed between revisions",
    "allergen_declaration_change": "Allergen declarations of a watched food changed between revisions",
    "new_linked_notice": "A notice held by Products safety now cites a watched food product",
    "new_table_edition": "A new edition of a watched composition table was acquired",
}
_DDL = """
CREATE TABLE IF NOT EXISTS food_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


class FoodCompositionMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = FoodCompositionStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ create

    def _watch(self, namespace: str, watch: Mapping[str, Any]) -> dict[str, list[str]]:
        raw = dict(watch or {})
        if set(raw) - set(WATCH_KEYS) or not any(raw.get(k) for k in WATCH_KEYS):
            raise FoodCompositionError("invalid_watch", f"watch at least one of {WATCH_KEYS}")
        result = {k: sorted({str(v).strip() for v in raw.get(k) or [] if str(v).strip()}) for k in WATCH_KEYS}
        from src.kb.food_composition import gtin_key

        for gtin in result["gtins"]:
            if gtin_key(gtin) is None:
                raise FoodCompositionError("invalid_watch", f"{gtin!r} is not a valid GTIN")
        result["foods"] = sorted(self.store.resolve_food(namespace, f) for f in result["foods"])
        from src.ingestion.food_composition_sources import TABLE_DECISIONS

        for table in result["table_editions"]:
            if table not in TABLE_DECISIONS:
                raise FoodCompositionError("invalid_watch", f"{table!r} is not an audited composition table")
        return result

    def create(self, namespace: str, request_key: str, *, watch: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        normalized = self._watch(namespace, watch)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "products",
             "query": {"operation": "search", "kind": "food-composition-monitor", "watch": normalized},
             "filters": {"watch": sorted(k for k, v in normalized.items() if v)},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "food-composition-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": f"the {SOURCE_PACK} food sources' schedule (or refresh()) acquires revisions "
                                      "and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "food-composition-monitor":
            raise FoodCompositionError("monitor_not_found", "subscription is not a food composition monitor")
        return subscription

    # ------------------------------------------------------------------ snapshot

    def _chain(self, namespace: str, food_id: str) -> list[dict[str, Any]]:
        """Revisions that became current when they arrived (by the provider's own order)."""
        chain, best = [], None
        for revision in self.store.revisions(namespace, food_id):
            if best is None or revision["order_key"] >= best:
                chain.append(revision)
                best = revision["order_key"]
        return chain

    def _cite(self, namespace: str, food_id: str, revision: Mapping[str, Any]) -> dict[str, Any]:
        head = self.store.item(namespace, food_id)
        return {"food_id": food_id, "provider": head["provider"], "provider_key": head["provider_key"],
                "provenance_class": head["provenance_class"], "revision_id": revision["revision_id"],
                "revision": revision["revision_value"], "revision_date": revision["revision_date"],
                "retrieved_at": revision["retrieved_at"]}

    @staticmethod
    def _nutrients(parts: Mapping[str, Any]) -> dict[tuple, dict[str, Any]]:
        return {(n["scheme"], n["nutrient_id"], n["basis"], n["value_kind"]):
                {"amount": n["amount"], "unit": n["unit"]["published"]} for n in parts["nutrient_values"]}

    def _food_items(self, namespace: str, food_id: str) -> list[dict[str, Any]]:
        items, previous = [], None
        for revision in self._chain(namespace, food_id):
            cite = self._cite(namespace, food_id, revision)
            parts = self.store.parts(namespace, revision["revision_id"])
            before = None if previous is None else previous[0]
            items.append({"id": f"label:{revision['revision_id']}", "kind": "label_revision", "cites": cite,
                          "previous_revision_id": None if before is None else before["revision_id"]})
            if previous is not None:
                old_n, new_n = self._nutrients(previous[1]), self._nutrients(parts)
                changes = [{"nutrient": {"scheme": k[0], "id": k[1]}, "basis": k[2], "value_kind": k[3],
                            "before": old_n.get(k), "after": new_n.get(k)}
                           for k in sorted(set(old_n) | set(new_n), key=str) if old_n.get(k) != new_n.get(k)]
                if changes:
                    items.append({"id": f"nutrients:{revision['revision_id']}", "kind": "nutrient_value_change",
                                  "cites": cite, "previous_revision_id": before["revision_id"], "changes": changes})
                old_a = {(a["relation"], a["value"]) for a in previous[1]["allergen_declarations"]}
                new_a = {(a["relation"], a["value"]) for a in parts["allergen_declarations"]}
                if old_a != new_a:
                    items.append({"id": f"allergens:{revision['revision_id']}", "kind": "allergen_declaration_change",
                                  "cites": cite, "previous_revision_id": before["revision_id"],
                                  "added": [{"relation": r, "value": v} for r, v in sorted(new_a - old_a)],
                                  "removed": [{"relation": r, "value": v} for r, v in sorted(old_a - new_a)]})
            previous = (revision, parts)
        if table_exists(self.conn, "food_notice_links"):
            from src.kb.food_notice_links import FoodNoticeLinks

            for row in FoodNoticeLinks(self.conn, initialize=False).rows(namespace, food_id):
                if row["state"] != "cited":
                    continue
                items.append({"id": f"notice:{row['link_id']}", "kind": "new_linked_notice",
                              "cites": {**self._cite(namespace, food_id, next(
                                  r for r in self.store.revisions(namespace, food_id)
                                  if r["revision_id"] == row["food_revision_id"])),
                                  "notice_id": row["notice_id"], "notice_revision_id": row["notice_revision_id"]},
                              "basis": row["basis"]})
        return items

    def _table_items(self, namespace: str, table: str) -> list[dict[str, Any]]:
        editions: dict[str, dict[str, Any]] = {}
        for item in self.store.items(namespace, provider="composition-table"):
            if not item["provider_key"].startswith(f"{table}:"):
                continue
            for revision in self.store.revisions(namespace, item["food_id"]):
                edition = editions.setdefault(revision["revision_value"], {
                    "edition": revision["revision_value"], "edition_date": revision["revision_date"],
                    "revision_ids": []})
                edition["revision_ids"].append(revision["revision_id"])
        items, previous = [], None
        for edition in sorted(editions.values(), key=lambda e: (e["edition_date"] or "", e["edition"])):
            items.append({"id": f"table:{table}:{edition['edition']}", "kind": "new_table_edition",
                          "cites": {"table": table, "edition": edition["edition"],
                                    "edition_date": edition["edition_date"],
                                    "revision_ids": sorted(edition["revision_ids"])},
                          "previous_edition": previous})
            previous = edition["edition"]
        return items

    def snapshot(self, subscription: Mapping[str, Any]) -> dict[str, Any]:
        namespace, watch = subscription["namespace"], subscription["query"]["watch"]
        foods = set(watch["foods"])
        for gtin in watch["gtins"]:
            foods.update(i["food_id"] for i in self.store.items(namespace, gtin=gtin))
        items = []
        for food_id in sorted(foods):
            try:
                items += self._food_items(namespace, food_id)
            except FoodCompositionError:
                continue  # a watched food not (yet) visible watches nothing
        for table in watch["table_editions"]:
            items += self._table_items(namespace, table)
        return {"items": items, "coverage": {"complete": True}}

    # ------------------------------------------------------------------ run

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        """The namespace's current food state as a watermark: reused when already committed, else a new one."""
        latest = self.conn.execute("SELECT max(retrieved_at_ms) FROM food_revisions WHERE namespace=?",
                                   [namespace]).fetchone()[0]
        if latest is None:
            raise FoodCompositionError("not_ready", "no food revision yet; acquire first")
        links = (self.conn.execute("SELECT link_id FROM food_notice_links WHERE namespace=? ORDER BY link_id",
                                   [namespace]).fetchall() if table_exists(self.conn, "food_notice_links") else [])
        revisions = self.conn.execute("SELECT revision_id FROM food_revisions WHERE namespace=? ORDER BY revision_id",
                                      [namespace]).fetchall()
        generation = digest([[r[0] for r in revisions], [r[0] for r in links]])[:24]
        rows = (self.conn.execute(
            "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark",
            [namespace]).fetchall() if table_exists(self.conn, "knowledge_subscription_watermarks") else [])
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("food_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(int(latest), highest + 1), {"food_generation": generation, "observed_at": iso_from_ms(latest)}

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
                "object": key, "kind": item["kind"], "message": MESSAGES[item["kind"]] + ".", "cites": item["cites"],
                **{k: item[k] for k in ("previous_revision_id", "changes", "added", "removed", "basis",
                                        "previous_edition") if k in item},
                "note": "a published record change, reported as published; no score, ranking or advice"})
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
                max_pages: int | None = None) -> dict[str, Any]:
        """Acquire a source's pinned selection within its page budget; idempotent, one receipt per run, stopped at
        the first rate-limit answer and refused before the provider's Retry-After has passed."""
        from src.ingestion.food_composition_sources import FoodCompositionAdapter
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT retry_at_ms FROM food_refresh_receipts WHERE namespace=? AND source_id=? "
            "ORDER BY started_at_ms DESC LIMIT 1", [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = FoodCompositionAdapter(source, transport=transport, secret=secret)
        limit = min(len(adapter.entries), int(max_pages or source["budgets"]["max_pages"]))
        projector = FoodCompositionProjector(self.conn)
        projector.store.now = self.now
        pages, stopped, retry_at, cursor = [], None, None, None
        for _ in range(limit):
            try:
                page = adapter.fetch_page({"operation": "food", "parameters": {},
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
        return self._receipt(namespace, source, now, status, pages, stopped, retry_at, principal_id)

    def _receipt(self, namespace, source, now, status, pages, stopped, retry_at, principal_id, note=None):
        body = {
            "source_id": source["source_id"], "status": status, "pages": pages,
            "new_revisions": sum(p["counts"].get("created", 0) + p["counts"].get("revised", 0)
                                 + p["counts"].get("reverted", 0) for p in pages),
            "unchanged": sum(p["counts"].get("unchanged", 0) for p in pages),
            "stopped": stopped, "retry_at": iso_from_ms(retry_at), "requested_by": principal_id,
            "at": iso_from_ms(now), "note": note,
        }
        receipt_id = "food-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO food_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["CONTRACT", "EVENT_KINDS", "FoodCompositionMonitor"]
