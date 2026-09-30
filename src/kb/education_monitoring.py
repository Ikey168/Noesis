"""Monitor new education-statistics releases, revised values and identity-match changes (#2227, ED11 #2433).

An education monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
delivered through :mod:`src.kb.subscription_delivery`) whose query names an institution (a ROR id or a source
institution id), a country or an indicator, optionally narrowed to providers: no watcher table and no scheduler.
Each run evaluates one committed watermark against a snapshot of the watched records, and the subscription store turns
new items into events (poll or outbox delivery). A replay, an idempotent re-acquisition or a restart emits nothing,
and a release that changes nothing emits nothing.

Notices are record changes cited to the source release; they never call a change an improvement or a decline:

* ``new_vintage`` - the first vintage of a watched series, or a later release that adds periods;
* ``revised_value`` - a release that changes values, each changed period with the value (status and code) before
  and after, the new vintage and the one it revises;
* ``identity_match_change`` - a watched institution's ROR match was proposed, accepted, rejected or reverted, or its
  ROR record changed status (never re-pointed).

:meth:`EducationMonitor.refresh` acquires a watched source's declared documents through its runtime adapter within the
source's page budget (the ED01 budgets), records a receipt and stops at the first rate-limit answer. Live releases from
providers still ``unverified-live`` are withheld from notices.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.education_sources import unverified
from src.kb.education_statistics import (
    READ_SCOPE,
    WRITE_SCOPE,
    EducationError,
    EducationProjector,
    EducationStatisticsStore,
    authorize,
    canonical,
    country_token,
    digest,
    iso_from_ms,
    table_exists,
)

CONTRACT = "noesis-education-notification-v1"
FILTER_KEYS = ("ror", "institution", "country", "indicator", "providers")
EVENT_TYPES = ("new_vintage", "revised_value", "identity_match_change")
MESSAGES = {
    "new_vintage": "A new release of a watched education statistic was published",
    "revised_value": "A release revised published values of a watched education statistic",
    "identity_match_change": "The ROR identity match of a watched institution changed",
}
_DDL = """
CREATE TABLE IF NOT EXISTS edu_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def notifiable(source_revision: Mapping[str, Any]) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return source_revision["evidence_origin"] != "live" or not unverified(source_revision["provider"])


class EducationMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = EducationStatisticsStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(watch: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(watch or {})
        chosen = [k for k in ("ror", "institution", "country", "indicator") if raw.get(k)]
        if set(raw) - set(FILTER_KEYS) or len(chosen) != 1:
            raise EducationError("invalid_watch", "a monitor names one ROR id, institution, country or indicator; "
                                 f"keys are {FILTER_KEYS}")
        out: dict[str, Any] = {"providers": sorted({str(p) for p in raw.get("providers") or []})}
        if raw.get("ror"):
            from src.kb.education_identity import ror_url

            out["ror"] = ror_url(raw["ror"])
        elif raw.get("institution"):
            institution = dict(raw["institution"])
            if not institution.get("scheme") or not institution.get("code"):
                raise EducationError("invalid_watch", "an institution is named by scheme and code")
            out["institution"] = {"scheme": str(institution["scheme"]), "code": str(institution["code"])}
        elif raw.get("country"):
            out["country"] = str(raw["country"])
        else:
            out["indicator"] = str(raw["indicator"])
        return out

    def create(self, namespace: str, request_key: str, *, watch: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = self._filter(watch)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "scientific",
                "query": {"operation": "search", "kind": "education-monitor", "filter": wanted},
                "filters": {"watch": "education-statistics"},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "education-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {**created, "refresh": "the primary-scientific-evidence source-pack schedule (or refresh()) acquires "
                "releases and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "education-monitor":
            raise EducationError("monitor_not_found", "subscription is not an education monitor")
        return subscription

    def _subjects(self, namespace: str, wanted: Mapping[str, Any]) -> list[tuple[str, str]]:
        if wanted.get("institution"):
            return [(wanted["institution"]["scheme"], wanted["institution"]["code"])]
        if wanted.get("ror") and table_exists(self.conn, "edu_identity_matches"):
            from src.kb.education_identity import EducationIdentity

            found = EducationIdentity(self.conn, initialize=False).institutions_for_ror(namespace, wanted["ror"])
            return [(s["scheme"], s["code"]) for s in found["subjects"]]
        return []

    def watched_series(self, namespace: str, wanted: Mapping[str, Any]) -> list[dict[str, Any]]:
        providers = set(wanted.get("providers") or [])
        if wanted.get("country"):
            token = country_token(wanted["country"])
            series = [s for s in self.store.find_series(namespace, record_kind="education_indicator")
                      if country_token(s["subject"].get("country") or s["subject"]["code"]) == token]
        elif wanted.get("indicator"):
            series = self.store.find_series(namespace, indicator=wanted["indicator"])
        else:
            subjects = self._subjects(namespace, wanted)
            series = self.store.find_series(namespace, subjects=subjects) if subjects else []
        return [s for s in series if not providers or s["provider"] in providers]

    def _series_items(self, namespace: str, series: Mapping[str, Any]) -> list[dict[str, Any]]:
        items, previous, before = [], None, {}
        for vintage in self.store.vintage_rows(namespace, series["series_id"]):
            values = {o["period"]: {"value": o["value"], "status": o["status"], "special_code": o["special_code"]}
                      for o in self.store.observations(namespace, vintage["vintage_id"])}
            changed = [{"period": p, "before": before[p], "after": values[p]}
                       for p in sorted(set(values) & set(before)) if values[p] != before[p]]
            added = sorted(set(values) - set(before))
            kind = "new_vintage" if previous is None else "revised_value" if changed else (
                "new_vintage" if added else None)
            if kind is not None:  # a release that changes nothing emits nothing
                items.append({
                    "id": f"vintage:{vintage['vintage_id']}",
                    "item": kind,
                    "series_id": series["series_id"],
                    "series": {"provider": series["provider"], "subject": series["subject"],
                               "indicator": series["indicator"], "unit": series["unit"]},
                    "vintage_id": vintage["vintage_id"],
                    "release_stage": vintage["release_stage"],
                    "release_at": vintage["release_at"],
                    "previous_vintage_id": None if previous is None else previous["vintage_id"],
                    "changed_values": changed,
                    "added_periods": added if previous is not None else [],
                    "record_ids": [series["series_id"], vintage["vintage_id"]]
                    + ([previous["vintage_id"]] if previous else []),
                    "source_revision": self.store.source_revision(namespace, vintage["release_id"]),
                })
            previous, before = vintage, values
        return items

    def _identity_items(self, namespace: str, wanted: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "edu_identity_matches") or not (wanted.get("ror") or wanted.get("institution")):
            return []
        from src.kb.education_identity import EducationIdentity

        identity = EducationIdentity(self.conn, initialize=False)
        matches = identity.matches(namespace, scopes={"operator"})
        if wanted.get("ror"):
            matches = [m for m in matches if m["ror_id"] == wanted["ror"]]
        else:
            matches = [m for m in matches if m["subject"] == wanted["institution"]]
        items = []
        for match in matches:
            for number, step in enumerate(match["history"]):
                items.append({
                    "id": f"identity:{match['match_id']}:{number}",
                    "item": "identity_match_change",
                    "match_id": match["match_id"],
                    "subject": match["subject"],
                    "ror_id": match["ror_id"],
                    "basis": match["basis"],
                    "state": step["state"],
                    "reason": step.get("reason"),
                    "decision_id": step.get("decision_id"),
                    "record_ids": [match["match_id"]] + ([step["decision_id"]] if step.get("decision_id") else []),
                    "source_revision": None,
                })
        for ror in sorted({m["ror_id"] for m in matches if m["ror_id"]}):
            snapshots = identity.ror_snapshots(namespace, ror)
            for before, after in zip(snapshots, snapshots[1:]):
                if before["status"] != after["status"]:
                    items.append({"id": f"ror:{ror}:{after['sha256']}", "item": "identity_match_change",
                                  "ror_id": ror, "state": f"ror_status:{after['status']}",
                                  "status": {"before": before["status"], "after": after["status"]},
                                  "record_ids": [ror, after["sha256"]], "source_revision": None,
                                  "note": "reported; matches are not re-pointed"})
        return items

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        items = [i for s in self.watched_series(namespace, wanted) for i in self._series_items(namespace, s)]
        kept = [i for i in items if notifiable(i["source_revision"])]
        return {"items": kept + self._identity_items(namespace, wanted), "coverage": {"complete": True}}, (
            len(items) - len(kept))

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        """The namespace's current education state as a watermark: reused when this state was already committed (a
        restart replays it and emits nothing), else a new one after every committed watermark."""
        latest = self.store.latest_release_ms(namespace)
        if latest is None:
            raise EducationError("not_ready", "no education release yet; acquire first")
        state = [r[0] for r in self.conn.execute(
            "SELECT vintage_id FROM edu_vintages WHERE namespace=? ORDER BY vintage_id", [namespace]).fetchall()]
        if table_exists(self.conn, "edu_identity_matches"):
            state += [f"{r[0]}:{r[1]}" for r in self.conn.execute(
                "SELECT match_id, history_json FROM edu_identity_matches WHERE namespace=? ORDER BY match_id",
                [namespace]).fetchall()]
        if table_exists(self.conn, "edu_ror_snapshots"):
            state += [r[0] for r in self.conn.execute(
                "SELECT sha256 FROM edu_ror_snapshots WHERE namespace=? ORDER BY sha256", [namespace]).fetchall()]
        generation = digest(state)[:24]
        rows = (
            self.conn.execute("SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? "
                              "ORDER BY watermark", [namespace]).fetchall()
            if table_exists(self.conn, "knowledge_subscription_watermarks") else []
        )
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("education_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {"education_generation": generation, "observed_at": iso_from_ms(latest)}

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
                "note": "notices report published record changes; they never judge a change"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        revision = item.get("source_revision")
        cited = f" ({revision['provider']}, {revision['release_stage']}, {revision['published_on']})" if revision \
            else ""
        return [{
            "contract": CONTRACT,
            "event_id": event_id,
            "notification_id": f"{event_id}:{item['item']}",
            "object": key,
            "kind": item["item"],
            "message": f"{MESSAGES[item['item']]}{cited}.",
            "record_ids": item["record_ids"],
            "source_revision": revision,
            **{k: item[k] for k in ("series_id", "vintage_id", "previous_vintage_id", "changed_values",
                                    "added_periods", "series", "match_id", "subject", "ror_id", "state", "status",
                                    "basis") if k in item},
            "note": "a publication or identity decision reported as recorded; nothing is concluded about the change",
        }]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, max_documents: int | None = None
                ) -> dict[str, Any]:
        """Acquire a source's declared documents within its page budget; idempotent by file, one receipt per run,
        stopped at the first rate-limit answer and refused before the provider's Retry-After has passed."""
        from src.ingestion.education_sources import EducationStatisticsAdapter
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT retry_at_ms FROM edu_refresh_receipts WHERE namespace=? AND source_id=? "
            "ORDER BY started_at_ms DESC LIMIT 1", [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = EducationStatisticsAdapter(source, transport=transport)
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents))
        projector = EducationProjector(self.conn)
        projector.store.now = self.now
        releases, stopped, retry_at, cursor = [], None, None, None
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
            releases += [{"release_id": a["release_id"], "status": a["status"], "vintages": a.get("vintages", 0),
                          "provider_receipt": dict(page.receipt or {})} for a in applied]
            cursor = page.next_cursor
            if cursor is None:
                break
        status = "stopped" if stopped else "complete" if cursor is None else "bounded"
        return self._receipt(namespace, source, now, status, releases, stopped, retry_at, principal_id)

    def _receipt(self, namespace, source, now, status, releases, stopped, retry_at, principal_id, note=None):
        body = {
            "source_id": source["source_id"], "status": status, "releases": releases,
            "new_releases": sum(1 for r in releases if r["status"] == "applied"),
            "unchanged_releases": sum(1 for r in releases if r["status"] == "unchanged"),
            "stopped": stopped, "retry_at": iso_from_ms(retry_at), "requested_by": principal_id,
            "at": iso_from_ms(now), "note": note,
        }
        receipt_id = "edu-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO edu_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["EVENT_TYPES", "EducationMonitor", "notifiable"]
