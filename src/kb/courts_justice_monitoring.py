"""Monitor new docket entries, decisions and statistic releases through subscriptions (#2218, CJ11).

A courts-justice monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), following
:mod:`src.kb.demographics_monitoring` and :mod:`src.kb.legislation_monitoring`:
its query names a **docket** (record key), a **court** (CourtListener court
id), an **organisational party** (the entity an accepted identity match links
party records to), a **provision** (``42 U.S.C. § 1983``) or a **statistic
series for a place** (``us-state:EX``, optionally an indicator). There is no
monitor table and no scheduler: the ``legal-research`` source-pack schedule
acquires, the maintenance orchestrator commits watermarks, and each evaluation
turns differences into subscription events delivered through the existing poll
and outbox paths.

Notices cite the new and the previous revision: ``new_docket_entry``,
``docket_revised``, ``new_opinion``, ``new_citing_decision``,
``new_citing_entry``, ``new_vintage``, ``revised_observation`` and
``observation_recorded``. Natural persons cannot be monitored. A revision
acquired *live* from a provider whose access is still ``unverified-live`` is
withheld until a dated live run verifies it; fixture replays are notified and
marked as fixture evidence. :meth:`CourtsJusticeMonitor.refresh` re-reads one
declared source selection through the real adapter within its budget;
unchanged responses add nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.courts_justice_sources import LIVE_VERIFICATION
from src.kb.courts_justice import (
    READ_SCOPE,
    WRITE_SCOPE,
    CourtsJusticeError,
    CourtsJusticeProjector,
    authorize,
)

CONTRACT = "noesis-court-justice-notification-v1"
WATCH_KINDS = ("docket", "court", "party", "provision", "series")


def notifiable(provider: str, origin: str | None) -> bool:
    return origin == "fixture" or LIVE_VERIFICATION.get(provider, {}).get("status") == "verified-live"


class CourtsJusticeMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.justice_statistics import JusticeStatisticsStore
        from src.kb.legal_dockets import LegalDocketStore
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.dockets = LegalDocketStore(conn, initialize=initialize, now=now)
        self.now = self.dockets.now
        self.statistics = JusticeStatisticsStore(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], indicator: str | None = None,
               delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise CourtsJusticeError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "docket" and not key.startswith("courts:docket:"):
            raise CourtsJusticeError("invalid_watch", "a docket is watched by its record key (courts:docket:...)")
        if watch == "party" and (key.startswith("natural person") or re.fullmatch(r"[A-Z][a-z]+( [A-Z][a-z.]+)+",
                                                                                 key)):
            raise CourtsJusticeError("natural_person_not_a_target", "natural persons cannot be monitored; watch an "
                                                                    "organisation's entity id")
        if watch == "provision":
            from src.kb.legal_court_citations import parse_us_citations

            parsed = [c for c in parse_us_citations(key) if c["kind"] in {"statute", "regulation"}]
            if len(parsed) != 1:
                raise CourtsJusticeError("invalid_watch", "a provision is one statutory citation")
        if watch == "series" and ":" not in key:
            raise CourtsJusticeError("invalid_watch", "a series is watched by place code (<scheme>:<code>)")
        query = {"operation": "search", "kind": "courts-justice-monitor", "watch": watch, "key": key,
                 **({"indicator": indicator} if indicator else {})}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "legal", "query": query, "filters": {"watch": watch},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "courts-justice-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the legal-research source-pack schedule and the maintenance orchestrator "
                                      "commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "courts-justice-monitor":
            raise CourtsJusticeError("monitor_not_found", "subscription is not a courts-justice monitor")
        return subscription

    # ------------------------------------------------------------------ snapshots

    def _docket_items(self, namespace: str, docket_keys: Iterable[str], items: list, withheld: list) -> None:
        for key in sorted(set(docket_keys)):
            revisions = self.dockets.docket_revisions(namespace, key)
            visible = [r for r in revisions if notifiable(r["provider"], r["evidence_origin"])]
            withheld[0] += len(revisions) - len(visible)
            if not visible:
                continue
            row = visible[-1]
            cite = {"record_key": key, "revision_id": row["revision_id"], "revision_no": row["revision_no"],
                    "source_id": row["source_id"], "evidence_origin": row["evidence_origin"]}
            items.append({"id": f"docket:{key}", "kind": "docket", "cite": cite,
                          "summary": {"date_terminated": row["date_terminated"]}})
            for entry in self.conn.execute("SELECT entry_number, date_filed, description FROM legal_docket_entries "
                                           "WHERE revision_id=? ORDER BY ordinal", [row["revision_id"]]).fetchall():
                items.append({"id": f"entry:{key}:{entry[0]}", "kind": "docket_entry", "cite": cite,
                              "summary": {"entry_number": entry[0], "date_filed": entry[1] and str(entry[1]),
                                          "description": entry[2]}})

    def _opinion_items(self, namespace: str, rows: Iterable[Mapping[str, Any]], items: list, withheld: list,
                       kind: str = "opinion") -> None:
        latest: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            if not notifiable(row["provider"], row["evidence_origin"]):
                withheld[0] += 1
                continue
            if row["record_key"] not in latest or row["revision_no"] > latest[row["record_key"]]["revision_no"]:
                latest[row["record_key"]] = row
        for key, row in sorted(latest.items()):
            items.append({"id": f"{kind}:{key}", "kind": kind,
                          "cite": {"record_key": key, "revision_id": row["revision_id"],
                                   "revision_no": row["revision_no"], "source_id": row["source_id"],
                                   "evidence_origin": row["evidence_origin"]},
                          "summary": {"date_filed": row["date_filed"], "case_name": row["case_name"],
                                      "disposition_quoted": row["disposition"]}})

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        namespace, query = subscription["namespace"], subscription["query"]
        items: list[dict[str, Any]] = []
        withheld = [0]
        watch, key = query["watch"], query["key"]
        if watch == "docket":
            self._docket_items(namespace, [key], items, withheld)
            docket_id = int(key.rsplit(":", 1)[1])
            self._opinion_items(namespace, self.dockets.cluster_revisions(namespace, docket_id=docket_id), items,
                                withheld)
        elif watch == "court":
            keys = [r[0] for r in self.conn.execute("SELECT DISTINCT record_key FROM legal_docket_revisions WHERE "
                                                    "namespace=? AND court_id=?", [namespace, key]).fetchall()]
            self._docket_items(namespace, keys, items, withheld)
            self._opinion_items(namespace, self.dockets.cluster_revisions(namespace, court_id=key), items, withheld)
        elif watch == "party":
            from src.kb.courts_justice_identity import CourtsIdentity

            links = CourtsIdentity(self.conn, initialize=False, now=self.now).accepted_parties(namespace, key,
                                                                                               scopes=scopes)
            keys = [link["docket_key"] for link in links]
            self._docket_items(namespace, keys, items, withheld)
            for docket_key in keys:
                self._opinion_items(namespace, self.dockets.cluster_revisions(
                    namespace, docket_id=int(docket_key.rsplit(":", 1)[1])), items, withheld)
        elif watch == "provision":
            from src.kb.legal_court_citations import CourtCitations, parse_us_citations

            target = next(c for c in parse_us_citations(key) if c["kind"] in {"statute", "regulation"})
            for link in CourtCitations(self.conn, initialize=False).links_to(namespace, target["key"]):
                where = link["locator"].get("entry_number", link["locator"].get("paragraph"))
                kind = "citing_entry" if link["citing_kind"] == "docket-entry" else "citing_decision"
                row = self.conn.execute(
                    "SELECT provider, evidence_origin FROM legal_docket_revisions WHERE revision_id=? UNION ALL "
                    "SELECT provider, evidence_origin FROM legal_opinion_revisions WHERE revision_id=?",
                    [link["citing_revision_id"], link["citing_revision_id"]]).fetchone()
                if row and not notifiable(row[0], row[1]):
                    withheld[0] += 1
                    continue
                items.append({"id": f"{kind}:{link['citing_record_key']}:{where}", "kind": kind,
                              "cite": {"record_key": link["citing_record_key"],
                                       "revision_id": link["citing_revision_id"], "link_id": link["link_id"],
                                       "evidence_origin": row[1] if row else None},
                              "summary": {"quoted": link["raw"], "locator": link["locator"]}})
            items = list({i["id"]: i for i in items}.values())  # the earliest revision's citation is kept stable
        else:
            answer = self.statistics.statistics_for_place(namespace, key, scopes=scopes,
                                                          indicator=query.get("indicator"))
            for column in answer["sources"]:
                vintage = column.get("vintage")
                if not vintage:
                    continue
                if not notifiable(column["provider"], vintage["evidence_origin"]):
                    withheld[0] += 1
                    continue
                cite = {"record_key": column["record_key"], "vintage_id": vintage["vintage_id"],
                        "vintage_no": vintage["vintage_no"], "release_label": vintage["release_label"],
                        "source_id": vintage["source_id"], "evidence_origin": vintage["evidence_origin"]}
                items.append({"id": f"vintage:{column['record_key']}", "kind": "vintage", "cite": cite,
                              "summary": {"release_label": vintage["release_label"]}})
                for series in column["series"]:
                    for obs in series["observations"]:
                        items.append({"id": f"obs:{series['series_key']}:{obs['period']}", "kind": "observation",
                                      "cite": cite, "summary": {"value": obs["value"], "flags": obs["flags"],
                                                                "unit": series["unit"]}})
        return {"items": items, "coverage": {"complete": True}}, withheld[0]

    # ------------------------------------------------------------------ evaluation

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
                raise CourtsJusticeError("watermark_uncommitted", "no committed watermark yet; source-pack runs and "
                                                                  "the maintenance orchestrator commit them")
            watermark = int(row[0])
        result, withheld = self.snapshot(subscription, scopes)
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
        if event_type == "removed" or new is None:
            return []

        def note(kind: str, message: str) -> dict[str, Any]:
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "object": key, "message": message, "summary": new["summary"],
                    "previous_summary": (old or {}).get("summary"),
                    "cites": {**new["cite"], "previous": (old or {}).get("cite")},
                    "evidence_origin": new["cite"].get("evidence_origin")}

        kind, summary = new["kind"], new["summary"]
        if kind == "docket_entry" and old is None:
            return [note("new_docket_entry", f"entry {summary['entry_number']} filed {summary['date_filed']}: "
                                             f"{summary['description']}")]
        if kind == "docket" and old is not None:
            return [note("docket_revised", f"{new['cite']['record_key']}: revision {new['cite']['revision_no']} "
                                           "recorded")]
        if kind == "opinion" and old is None:
            return [note("new_opinion", f"{summary['case_name']} decided {summary['date_filed']}; disposition as "
                                        f"published: {summary['disposition_quoted']}")]
        if kind == "citing_decision" and old is None:
            return [note("new_citing_decision", f"{new['cite']['record_key']} cites it: {summary['quoted']}")]
        if kind == "citing_entry" and old is None:
            return [note("new_citing_entry", f"{new['cite']['record_key']} entry cites it: {summary['quoted']}")]
        if kind == "vintage" and (old is None or old["cite"]["vintage_id"] != new["cite"]["vintage_id"]):
            return [note("new_vintage", f"{new['cite']['record_key']}: vintage {new['cite']['vintage_no']} "
                                        f"({summary['release_label']})")]
        if kind == "observation":
            if old is None:
                return [note("observation_recorded", f"{key}: {summary['value']} {summary['unit']}")]
            if old["summary"] != summary:
                return [note("revised_observation", f"{key}: {old['summary']['value']} -> {summary['value']} "
                                                    f"{summary['unit']} (vintage {new['cite']['vintage_no']})")]
        return []

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
        from src.ingestion.courts_justice_sources import CourtsJusticeAdapter

        scopes = set(scopes)
        namespace = CourtsJusticeProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = CourtsJusticeAdapter(source, transport=transport, secret=secret)
        projector = CourtsJusticeProjector(self.conn)
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < min(len(adapter.units), int(source["budgets"]["max_pages"])):
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = projector.project(namespace, [r["court_justice_record"] for r in page.records], run_id=run_id,
                                       source_id=source["source_id"], receipt=dict(page.receipt or {}),
                                       observed_at_ms=self.now())
            for change, count in result["counts"].items():
                counts[change] = counts.get(change, 0) + count
            units += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        del principal_id
        receipts = (self.dockets.receipts(namespace, run_id, scopes=scopes | {READ_SCOPE})
                    + self.statistics.receipts(namespace, run_id, scopes=scopes | {READ_SCOPE}))
        return {"run_id": run_id, "source_id": source["source_id"], "units": units, "counts": counts,
                "complete": cursor is None, "receipts": receipts}
