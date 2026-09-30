"""Monitor new filings, amendments and independent expenditures through subscriptions (#2209, CF11).

A campaign-finance monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), following
:mod:`src.kb.lobbying_monitoring` and :mod:`src.kb.legislation_monitoring`: its
query names a committee (FEC committee or Commission regulated entity), an
organisation (reached through accepted identity matches and cited ownership
relations, CF10) or a contest (CF08 links). There is no monitor table and no
scheduler: the ``official-political-records`` source-pack schedule acquires,
the maintenance orchestrator commits the watermarks, and each evaluation turns
differences into subscription events delivered through the existing poll and
outbox paths.

Notices are record changes, not assessments. Each cites the new (and the
previous) record revision and states what changed: ``filing_published``,
``amendment_filed`` (with the amendment chain and the version it amends),
``termination_filed``, ``most_recent_flag_changed`` (as the regulator
published it), ``filing_revised`` (totals as reported, before and after),
``independent_expenditure_reported`` (support/oppose verbatim, 24/48-hour
notice or periodic report) and ``contribution_reported``. Notices follow CF01:
they never carry an individual's contribution or a payee's identity.

A revision acquired *live* from a provider whose access is still
``unverified-live`` is withheld until a dated live run verifies it; fixture
replays are notified and marked as fixture evidence.
:meth:`CampaignFinanceMonitor.refresh` re-reads one declared source selection
through the real adapter within its page budget; re-reading unchanged responses
adds nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.campaign_finance_sources import LIVE_VERIFICATION
from src.kb.campaign_finance_queries import CampaignFinanceQueries, resolve_committee
from src.kb.campaign_finance_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    CampaignFinanceError,
    CampaignFinanceStore,
    authorize,
)

CONTRACT = "noesis-campaign-finance-notification-v1"
WATCH_KINDS = ("committee", "organisation", "contest")


def notifiable(row: Mapping[str, Any]) -> bool:
    provider = row.get("provider") or row["record"].get("provider")
    return (row["evidence_origin"] == "fixture"
            or LIVE_VERIFICATION.get(provider, {}).get("status") == "verified-live")


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = row["record"]["fields"]
    if row["record_kind"] == "filing":
        return {"form_type": fields.get("form_type"), "report_type": fields.get("report_type"),
                "file_number": fields.get("file_number"), "receipt_date": fields.get("receipt_date"),
                "amendment_indicator": fields.get("amendment_indicator"),
                "amendment_chain": fields.get("amendment_chain"), "most_recent": fields.get("most_recent"),
                "most_recent_file_number": fields.get("most_recent_file_number"),
                "totals_as_reported": fields.get("totals_as_reported"),
                "items_in_export": len(fields.get("items_in_export") or [])}
    if row["record_kind"] == "independent-expenditure":
        return {"support_oppose_indicator": fields.get("support_oppose_indicator"),
                "candidate_id": fields.get("candidate_id"), "amount_as_reported": fields.get("amount_as_reported"),
                "dissemination_date": fields.get("dissemination_date"),
                "source_assertion": fields.get("source_assertion")}
    return {"amount_as_reported": fields.get("amount_as_reported"),
            "date": fields.get("date_as_reported") or fields.get("accepted_date"),
            "donor": (fields.get("counterparty") or {}).get("name")}


class CampaignFinanceMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = CampaignFinanceStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], ownership_namespace: str | None = None,
               delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise CampaignFinanceError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "committee":
            key = resolve_committee(key)
            if not key.startswith(("campaign-finance:fec:committee:", "campaign-finance:ukec:entity:")):
                raise CampaignFinanceError("invalid_watch", "a committee is watched by its FEC id or Commission id")
        if watch == "contest" and not key.startswith("election-contest:"):
            raise CampaignFinanceError("invalid_watch", "a contest is watched by its elections contest id")
        query = {"operation": "search", "kind": "campaign-finance-monitor", "watch": watch, "key": key}
        if watch == "organisation" and ownership_namespace:
            query["ownership_namespace"] = ownership_namespace
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "political", "query": query, "filters": {"watch": watch},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "campaign-finance-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the official-political-records source-pack schedule and the maintenance "
                                      "orchestrator commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "campaign-finance-monitor":
            raise CampaignFinanceError("monitor_not_found", "subscription is not a campaign-finance monitor")
        return subscription

    def _rows(self, subscription: Mapping[str, Any], principal_id: str, scopes: set[str]) -> list[dict[str, Any]]:
        namespace, query = subscription["namespace"], subscription["query"]
        if query["watch"] == "committee":
            rows = self.store.records(namespace, scopes=scopes, kinds=["filing", "independent-expenditure"],
                                      committee_key=query["key"])
            return rows
        if query["watch"] == "contest":
            from src.kb.campaign_finance_links import CampaignFinanceLinks

            links = CampaignFinanceLinks(self.conn, initialize=False).links(namespace, scopes=scopes, kind="contest",
                                                                            target_key=query["key"])
            keys = sorted({link["record_key"] for link in links})
            return self.store.records(namespace, scopes=scopes, record_keys=keys) if keys else []
        answer = CampaignFinanceQueries(self.conn).affiliate_donations(
            namespace, query["key"], principal_id=principal_id, scopes=scopes - {"operator"},
            ownership_namespace=query.get("ownership_namespace"))
        keys = sorted({c["record_key"] for c in answer.get("contributions") or []})
        # affiliate items are organisations only (CF01), with the filing versions they were reported in
        filings = sorted({c["filing_key"] for c in answer.get("contributions") or [] if c.get("filing_key")})
        return self.store.records(namespace, scopes=scopes, record_keys=keys + filings) if keys else []

    def snapshot(self, subscription: Mapping[str, Any], principal_id: str, scopes: set[str]
                 ) -> tuple[dict[str, Any], int]:
        items, withheld = [], 0
        for row in self._rows(subscription, principal_id, scopes):
            if row["individual"] and row["record_kind"] == "contribution":
                continue  # never notified (CF01)
            if not notifiable(row):
                withheld += 1
                continue
            items.append({
                "id": f"{row['source_id']}:{row['record_key']}", "kind": row["record_kind"],
                "record_key": row["record_key"], "source_id": row["source_id"], "provider": row["provider"],
                "committee_key": row["committee_key"], "filing_key": row["record"].get("filing_key"),
                "revision_id": row["revision_id"], "revision_no": row["revision_no"],
                "filing_revision_id": row["filing_revision_id"], "evidence_origin": row["evidence_origin"],
                "summary": _summary(row),
            })
        return {"items": items, "coverage": {"complete": True}}, withheld

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                    [namespace]).fetchone()
            if row is None or row[0] is None:
                raise CampaignFinanceError("watermark_uncommitted", "no committed watermark yet; source-pack runs "
                                                                    "and the maintenance orchestrator commit them")
            watermark = int(row[0])
        result, withheld = self.snapshot(subscription, principal_id, scopes)
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute("SELECT event_type, object_key, before_json, after_json FROM "
                                    "knowledge_subscription_events WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "notifications": notifications, "withheld_unverified_live_revisions": withheld,
                "delivery": subscription["delivery"]}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, before: str | None, after: str | None
                  ) -> list[dict[str, Any]]:
        old = json.loads(before) if before else None
        new = json.loads(after) if after else None
        if event_type == "removed" or new is None or (old and old["revision_id"] == new["revision_id"]):
            return []

        def note(kind: str, message: str, **detail: Any) -> dict[str, Any]:
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "object": key, "record_key": new["record_key"], "message": message,
                    "cites": {"record_key": new["record_key"], "revision_id": new["revision_id"],
                              "previous_revision_id": old["revision_id"] if old else None,
                              "source_id": new["source_id"], "filing_key": new["filing_key"],
                              "filing_revision_id": new["filing_revision_id"]},
                    "evidence_origin": new["evidence_origin"], **detail}

        summary, prior = new["summary"], (old or {}).get("summary") or {}
        label = new["record_key"]
        if new["kind"] == "filing":
            if old is None:
                indicator = summary.get("amendment_indicator")
                if indicator == "A":
                    chain = summary.get("amendment_chain") or []
                    return [note("amendment_filed", f"{label}: amendment of {chain[-2] if len(chain) > 1 else '?'} "
                                                    f"received {summary.get('receipt_date')} (chain {chain}).",
                                 amendment_chain=chain, totals_as_reported=summary.get("totals_as_reported"))]
                if indicator == "T":
                    return [note("termination_filed", f"{label}: termination report received "
                                                      f"{summary.get('receipt_date')}.")]
                return [note("filing_published", f"{label}: {summary.get('form_type')} "
                                                 f"{summary.get('report_type') or ''} received "
                                                 f"{summary.get('receipt_date') or 'date not published'}.",
                             totals_as_reported=summary.get("totals_as_reported"))]
            notes = []
            if (summary.get("most_recent"), summary.get("most_recent_file_number")) != (
                    prior.get("most_recent"), prior.get("most_recent_file_number")):
                notes.append(note("most_recent_flag_changed", f"{label}: the regulator now publishes most_recent="
                                                              f"{summary.get('most_recent')} (most recent file "
                                                              f"{summary.get('most_recent_file_number')}).",
                                  before=[prior.get("most_recent"), prior.get("most_recent_file_number")],
                                  after=[summary.get("most_recent"), summary.get("most_recent_file_number")]))
            if summary.get("totals_as_reported") != prior.get("totals_as_reported") or \
                    summary.get("items_in_export") != prior.get("items_in_export"):
                notes.append(note("filing_revised", f"{label}: revision {new['revision_no']} recorded.",
                                  before=prior.get("totals_as_reported"), after=summary.get("totals_as_reported"),
                                  items_before=prior.get("items_in_export"), items_after=summary.get("items_in_export")))
            return notes or [note("filing_revised", f"{label}: revision {new['revision_no']} recorded.")]
        if new["kind"] == "independent-expenditure":
            return [note("independent_expenditure_reported" if old is None else "independent_expenditure_revised",
                         f"{label}: {summary.get('source_assertion')}, support/oppose "
                         f"{summary.get('support_oppose_indicator')} for {summary.get('candidate_id')} as reported.",
                         support_oppose_indicator=summary.get("support_oppose_indicator"),
                         source_assertion=summary.get("source_assertion"))]
        return [note("contribution_reported" if old is None else "contribution_revised",
                     f"{label}: contribution from {summary.get('donor')} as reported in {new['filing_key']}.")]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = ""
             ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    def refresh(self, source: Mapping[str, Any], *, run_id: str, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, secret: str | None = None
                ) -> dict[str, Any]:
        """Re-read one declared selection within its budget; idempotent, and every unit leaves a receipt."""
        from src.ingestion.campaign_finance_sources import CampaignFinanceAdapter
        from src.kb.campaign_finance_records import CampaignFinanceProjector

        scopes = set(scopes)
        namespace = CampaignFinanceProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = CampaignFinanceAdapter(source, transport=transport, secret=secret)
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < min(len(adapter.units), int(source["budgets"]["max_pages"])):
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = self.store.project(namespace, [r["campaign_finance_record"] for r in page.records],
                                        run_id=run_id, source_id=source["source_id"], receipt=dict(page.receipt or {}))
            for change, count in result["counts"].items():
                counts[change] = counts.get(change, 0) + count
            units += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        del principal_id
        return {"run_id": run_id, "source_id": source["source_id"], "units": units, "counts": counts,
                "complete": cursor is None,
                "receipts": self.store.receipts(namespace, run_id, scopes=scopes | {READ_SCOPE})}
