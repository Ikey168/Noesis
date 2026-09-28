"""Monitor new activities, transaction updates, result postings and CRS vintages through subscriptions (#1932, D09).

A development-finance monitor follows :class:`src.kb.funding_monitoring.FundingMonitor`:
it is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`)
whose filters (publishers, funders, recipient countries, sectors, organisations
and CRS cells) are stored with it, with no scheduler and no delivery path of
its own. Each run evaluates one committed watermark - by default the newest
observation of any development-finance source in the namespace - against a
snapshot built per publisher from the revisions in force at that watermark:

* an activity item per (publisher, activity) with its revision, publication
  state, transaction digests and result digest;
* a CRS item per watched cell with its current vintage;
* a coverage item per publisher and selection.

The subscription store turns differences into events; each is classified as
``new_activity``, ``new_transaction``, ``corrected_transaction``,
``retracted_activity`` (withdrawn by the publisher, never "ended"),
``new_result_posting``, ``new_crs_vintage`` or ``coverage_change``, citing the
revision or vintage before and after. A failed or partial refresh is reported
as stale coverage; its activities stay in the snapshot unchanged. Nothing is
summed across publishers, and re-evaluating the recorded watermark after a
restart emits nothing new.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.development_finance import (
    READ_SCOPE,
    DevelopmentFinanceError,
    DevelopmentFinanceStore,
    authorize,
    diff_transactions,
    transaction_summary,
    canonical,
    digest,
    iso_from_ms,
    publisher_id,
    table_exists,
)

CONTRACT = "noesis-development-finance-notification-v1"
FILTER_KEYS = frozenset(
    {"publishers", "funders", "countries", "sectors", "organisations", "crs_cells"}
)
_DDL = """
CREATE TABLE IF NOT EXISTS devfin_monitors(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, filters_json TEXT NOT NULL);
"""
MESSAGES = {
    "new_activity": "A publisher reports a new activity",
    "new_transaction": "A publisher reports a new transaction",
    "corrected_transaction": "A publisher corrected a transaction",
    "removed_transaction": "A publisher no longer reports a transaction in this version (not reversed or ended)",
    "retracted_activity": "A publisher no longer publishes an activity (withdrawn from publication; not ended)",
    "new_result_posting": "A publisher posted new or changed results",
    "new_crs_vintage": "A new OECD CRS vintage of a watched cell",
    "coverage_change": "Publisher coverage changed",
    "no_longer_matching": "An activity no longer matches the monitor's filters (not ended)",
}


def evidence_scopes(
    namespace: str, scopes: set[str], filters: Mapping[str, Any]
) -> set[str]:
    """The scopes a monitor's subscription retains as its evidence access: the development-finance read scope in the
    namespace, the ownership read scope when funders or organisations resolve through identity decisions, and the
    subscription operation scopes - not every scope the caller happened to hold."""
    if "operator" in scopes:
        return set(scopes)
    retained = {READ_SCOPE, f"namespace:{namespace}:read"}
    if filters.get("funders") or filters.get("organisations"):
        retained |= set(scopes) & {"knowledge:ownership:read"}
    return retained | (
        set(scopes) & {"knowledge:subscriptions:read", "knowledge:subscriptions:write"}
    )


class DevelopmentFinanceMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = DevelopmentFinanceStore(conn, initialize=False, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    @staticmethod
    def _filters(filters: Mapping[str, Any]) -> dict[str, list[str]]:
        if not isinstance(filters, Mapping) or set(filters) - FILTER_KEYS:
            raise DevelopmentFinanceError(
                "invalid_filters", f"filters take {sorted(FILTER_KEYS)}"
            )
        clean = {
            k: sorted({str(v).strip() for v in filters.get(k) or [] if str(v).strip()})
            for k in FILTER_KEYS
        }
        clean = {k: v for k, v in clean.items() if v}
        if not clean:
            raise DevelopmentFinanceError(
                "invalid_filters",
                "watch at least one publisher, funder, country, sector, "
                "organisation or CRS cell",
            )
        return clean

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        filters: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
        delivery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        clean = self._filters(filters)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "funding",
                "query": {
                    "operation": "search",
                    "kind": "development-finance-monitor",
                    "filters": clean,
                },
                "filters": clean,
                "cadence": {"trigger": "watermark"},
                "delivery": dict(delivery or {"kind": "poll"}),
            },
            "development-finance-monitor:" + request_key,
            principal_id=principal_id,
            scopes=evidence_scopes(namespace, scopes, clean),
        )
        self.conn.execute(
            "INSERT INTO devfin_monitors VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            [created["subscription_id"], namespace, principal_id, canonical(clean)],
        )
        return {
            **created,
            "watch": clean,
            "refresh": "acquisitions and the economic-statistics-and-filings source pack record the observations "
            "whose newest time is the default watermark; no new scheduler",
        }

    def _monitor(self, subscription_id: str, principal_id: str) -> dict[str, Any]:
        row = (
            self.conn.execute(
                "SELECT namespace, owner, filters_json FROM devfin_monitors WHERE subscription_id=?",
                [subscription_id],
            ).fetchone()
            if table_exists(self.conn, "devfin_monitors")
            else None
        )
        if not row or row[1] != principal_id:
            raise DevelopmentFinanceError(
                "monitor_not_found", "development-finance monitor is unavailable"
            )
        return {"namespace": row[0], "filters": json.loads(row[2])}

    def snapshot(
        self,
        namespace: str,
        filters: Mapping[str, list[str]],
        *,
        as_of: int | None,
        scopes: set[str],
    ) -> dict[str, Any]:
        from src.kb.development_finance_queries import DevelopmentFinanceQueries

        queries = DevelopmentFinanceQueries(self.conn, now=self.now, record=False)
        items: list[dict[str, Any]] = []
        activity_filters = {k: v for k, v in filters.items() if k != "crs_cells"}
        requests = self._requests(activity_filters)
        revisions: dict[str, dict[str, Any]] = {}
        orgs_cache: dict[str, set[str] | None] = {}
        # Each publisher's revision in force is selected once, before any filter is applied.
        current = list(self.store.current(namespace, as_of=as_of).values())
        for request in requests:
            orgs = {}
            for field in ("funder", "organisation"):
                value = request.get(field)
                if value not in orgs_cache:
                    orgs_cache[value] = queries._organisation_keys(
                        namespace, value, scopes
                    )
                orgs[field] = orgs_cache[value]
            for revision in current:
                if queries._matches(revision, request, orgs):
                    revisions[revision["activity_key"]] = revision
        for key, revision in sorted(revisions.items()):
            state = self.store.publication_state(namespace, key, as_of=as_of)
            items.append(
                {
                    "id": f"activity:{key}",
                    "item": "activity",
                    "publisher_id": revision["publisher_id"],
                    "iati_identifier": revision["iati_identifier"],
                    "revision_id": revision["revision_id"],
                    "last_updated_at": revision["last_updated_at"],
                    "publication": state["state"],
                    "transactions": [
                        transaction_summary(tx) for tx in revision["transactions"]
                    ],
                    "results_digest": digest(revision["activity"].get("results") or []),
                }
            )
        for cell_id in filters.get("crs_cells") or []:
            vintages = (
                self.store.crs_vintages(namespace, cell_id, as_of=as_of)
                if self.store.ready()
                else []
            )
            if vintages:
                current = vintages[-1]
                items.append(
                    {
                        "id": f"crs:{cell_id}",
                        "item": "crs",
                        "cell_id": cell_id,
                        "vintage_id": current["vintage_id"],
                        "published_on": current["published_on"],
                        "release_label": current["release_label"],
                    }
                )
        publishers = {i["publisher_id"] for i in items if i["item"] == "activity"}
        publishers |= {
            publisher_id("iati", ref) for ref in filters.get("publishers") or []
        }
        stale = []
        for selection, row in sorted(
            self.store.latest_coverage(namespace, as_of=as_of).items()
        ):
            if not self._relevant(row, publishers, filters):
                continue
            is_stale = row["failure_code"] is not None or not row["complete"]
            items.append(
                {
                    "id": f"coverage:{selection}",
                    "item": "coverage",
                    "selection_key": selection,
                    "coverage_id": row["coverage_id"],
                    "complete": row["complete"],
                    "failure_code": row["failure_code"],
                    "stale": is_stale,
                }
            )
            if is_stale:
                stale.append(selection)
        return {
            "items": items,
            "coverage": {"complete": not stale, "stale_selections": stale},
        }

    @staticmethod
    def _relevant(
        row: Mapping[str, Any], publishers: set[str], filters: Mapping[str, list[str]]
    ) -> bool:
        """A selection matters to a monitor when it returned or requested a watched publisher, or asked for a
        watched country or sector."""
        requested = row["requested"]
        if set(row["returned"]) & publishers:
            return True
        if {
            publisher_id("iati", ref) for ref in requested.get("publishers") or []
        } & publishers:
            return True
        if set(requested.get("recipient_countries") or []) & {
            c.upper() for c in filters.get("countries") or []
        }:
            return True
        return bool(
            set(requested.get("sectors") or []) & set(filters.get("sectors") or [])
        )

    @staticmethod
    def _requests(filters: Mapping[str, list[str]]) -> list[dict[str, Any]]:
        """One query per watched value (an OR across values); each is an ordinary as-of activity query."""
        requests = []
        for field, key in (
            ("publishers", "publisher"),
            ("funders", "funder"),
            ("countries", "country"),
            ("sectors", "sector"),
            ("organisations", "organisation"),
        ):
            for value in filters.get(field) or []:
                requests.append({key: value})
        return requests

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        """The watermark of the namespace's current development-finance state.

        It is the newest observation time, unless a watermark at or after it is already committed: the one committed
        for this same store generation is reused (a restart replays it and emits nothing); otherwise the next integer
        after the highest committed one, so a state change never reuses a committed watermark.
        """
        latest = self.store.latest_observation_ms(namespace)
        if latest is None:
            raise DevelopmentFinanceError(
                "not_ready", "no development-finance observation yet; acquire first"
            )
        generation = self.store.generation(namespace)
        detail = {"development_finance_generation": generation}
        rows = (
            self.conn.execute(
                "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? "
                "ORDER BY watermark",
                [namespace],
            ).fetchall()
            if table_exists(self.conn, "knowledge_subscription_watermarks")
            else []
        )
        for watermark, committed in reversed(rows):
            if (
                json.loads(committed or "{}").get("development_finance_generation")
                == generation
            ):
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {**detail, "observed_at": iso_from_ms(latest)}

    def run(
        self,
        subscription_id: str,
        watermark: int | None = None,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        monitor = self._monitor(subscription_id, principal_id)
        namespace = monitor["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            watermark, detail = self.default_watermark(namespace)
        else:
            watermark = int(watermark)
            detail = {
                "development_finance_generation": self.store.generation(namespace)
            }
        # The snapshot holds everything observed so far; the watermark orders evaluations, it is not an as-of date.
        result = self.snapshot(namespace, monitor["filters"], as_of=None, scopes=scopes)
        self.subscriptions.commit_watermark(
            namespace, watermark, kind="ingestion", detail=detail
        )
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            watermark,
            result,
            principal_id=principal_id,
            scopes=evidence_scopes(namespace, scopes, monitor["filters"]),
            observed_at_ms=self.now(),
        )
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events "
                "WHERE event_id=?",
                [event_id],
            ).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "coverage": result["coverage"],
            "notifications": notifications,
            "note": "changes are reported per publisher; nothing is summed across publishers",
        }

    @staticmethod
    def _classify(
        event_id: str, event_type: str, key: str, before: str | None, after: str | None
    ):
        before = json.loads(before) if before else None
        after = json.loads(after) if after else None

        def note(kind, cites, **extra):
            return {
                "contract": CONTRACT,
                "notification_id": f"{event_id}:{kind}:{digest(cites)[:12]}",
                "event_id": event_id,
                "object": key,
                "kind": kind,
                "message": MESSAGES[kind] + ".",
                "cites": cites,
                **extra,
            }

        if event_type == "coverage-degraded":
            return []  # the coverage item of the stale selection carries the notification
        item = (after or before or {}).get("item")
        if item == "coverage":
            if event_type == "removed":
                return []
            return [
                note(
                    "coverage_change",
                    {
                        "before": None if before is None else before["coverage_id"],
                        "after": after["coverage_id"],
                    },
                    stale=after["stale"],
                    detail="refresh failed or stopped early; activities are unchanged, not ended"
                    if after["stale"]
                    else "coverage recorded",
                )
            ]
        if item == "crs":
            if event_type == "removed":
                return []
            return [
                note(
                    "new_crs_vintage",
                    {
                        "before": None if before is None else before["vintage_id"],
                        "after": after["vintage_id"],
                        "cell_id": after["cell_id"],
                    },
                    published_on=after["published_on"],
                    release_label=after["release_label"],
                )
            ]
        if event_type == "added":
            return [
                note(
                    "new_activity",
                    {"after": after["revision_id"]},
                    publisher_id=after["publisher_id"],
                    iati_identifier=after["iati_identifier"],
                )
            ]
        if event_type == "removed":
            return [
                note(
                    "no_longer_matching",
                    {"before": before["revision_id"]},
                    publisher_id=before["publisher_id"],
                )
            ]
        out = []
        cites = {"before": before["revision_id"], "after": after["revision_id"]}
        if (
            before["publication"] != after["publication"]
            and after["publication"] == "withdrawn-by-publisher"
        ):
            out.append(
                note(
                    "retracted_activity",
                    cites,
                    publisher_id=after["publisher_id"],
                    iati_identifier=after["iati_identifier"],
                )
            )
        kinds = {
            "new": "new_transaction",
            "corrected": "corrected_transaction",
            "removed": "removed_transaction",
        }
        for change in diff_transactions(before["transactions"], after["transactions"]):
            extra = {}
            if change.get("before"):
                extra["before_value"] = change["before"]
            if change.get("after"):
                extra["after_value"] = change["after"]
            out.append(
                note(
                    kinds[change["change"]],
                    {
                        **cites,
                        "before_transaction": (change.get("before") or {}).get(
                            "transaction_id"
                        ),
                        "after_transaction": (change.get("after") or {}).get(
                            "transaction_id"
                        ),
                    },
                    publisher_id=after["publisher_id"],
                    transaction_key=change["transaction_key"],
                    **extra,
                )
            )
        if before["results_digest"] != after["results_digest"]:
            out.append(
                note("new_result_posting", cites, publisher_id=after["publisher_id"])
            )
        return out

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict:
        scopes = set(scopes)
        monitor = self._monitor(subscription_id, principal_id)
        authorize(monitor["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )
