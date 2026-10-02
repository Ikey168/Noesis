"""Monitor research-entity registry changes through subscriptions (#2579, RE11).

A research-entities monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
delivered through the existing poll and outbox paths), following :mod:`src.kb.campaign_finance_monitoring`: its query
watches an **organisation** (ROR id: its record, the datasets any acquired revision of which cites it - so a dataset
no longer served stays watched and its removal is notified - and the CORDIS participations reached through accepted
matches), a **researcher** (ORCID iD: the record and each asserted work; needs the researcher scope) or a
**project** (CORDIS programme and id: the record and the datasets naming it as their award). There is no monitor
table and no scheduler: the ``research-discovery`` source-pack schedule acquires, the maintenance orchestrator commits
the watermarks, and each evaluation turns differences into subscription events.

Notices are record changes, not assessments. Each cites the new (and previous) record revision and states what changed:
``new_record``, ``record_revised`` (the changed fields), ``status_changed`` (ROR active/inactive/withdrawn, ORCID
deactivation, a DataCite DOI no longer served), ``successor_published``, ``new_asserted_work`` and
``asserted_work_withdrawn`` (ORCID-asserted, never verified authorship), ``new_dataset`` and ``dataset_revised``,
``new_project_participation`` and ``project_revised``. Notices carry the minimised fields only (RE01).

A revision acquired *live* from a provider whose access is still ``unverified-live`` is withheld until a dated live
run verifies it; fixture replays are notified and marked as fixture evidence. :meth:`ResearchEntityMonitor.refresh`
re-reads one declared source selection through the real adapter within its page budget; re-reading unchanged responses
adds nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.research_entities_sources import (
    LIVE_VERIFICATION,
    normalize_doi,
    ror_id,
    valid_orcid,
)
from src.kb.research_entities_records import (
    READ_SCOPE,
    RESEARCHER_SCOPE,
    WRITE_SCOPE,
    ResearchEntityError,
    ResearchEntityStore,
    authorize,
    may_read_researchers,
    table_exists,
)

CONTRACT = "noesis-research-entity-notification-v1"
WATCH_KINDS = ("organisation", "researcher", "project")


def notifiable(row: Mapping[str, Any]) -> bool:
    return (row["evidence_origin"] == "fixture"
            or LIVE_VERIFICATION.get(row["provider"], {}).get("status") == "verified-live")


def resolve_watch(watch: str, key: str) -> str:
    key = str(key or "").strip()
    if watch == "organisation":
        rid = ror_id(key.rsplit(":", 1)[-1] if key.startswith("research-entities:") else key)
        if rid:
            return f"research-entities:ror:{rid}"
    elif watch == "researcher":
        orcid = valid_orcid(key.rsplit(":", 1)[-1] if key.startswith("research-entities:") else key)
        if orcid:
            return f"research-entities:orcid:{orcid}"
    elif watch == "project":
        if key.startswith("research-entities:cordis:"):
            return key
        programme, _, project_id = key.rpartition(":")
        if project_id.isdigit():
            return f"research-entities:cordis:{(programme or 'HORIZON').upper()}:{project_id}"
    raise ResearchEntityError("invalid_watch", f"watch one of {WATCH_KINDS} by ROR id, ORCID iD or "
                              "PROGRAMME:project id")


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    record = row["record"]
    fields = record["fields"]
    kind = row["record_kind"]
    if kind == "organisation":
        return {"status": fields["status"], "display_name": fields["display_name"],
                "names": sorted(n["value"] for n in fields["names"] if n.get("value")),
                "relationships": [[r["type"], r["id"]] for r in fields["relationships"]],
                "external_ids": [[e["type"], e["all"]] for e in fields["external_ids"]],
                "release": (record.get("release") or {}).get("label")}
    if kind == "researcher":
        return {"status": fields["status"], "last_modified": fields["last_modified"],
                "employments": sorted(e["put_code"] for e in fields.get("employments") or []),
                "works": sorted(w["put_code"] for w in fields.get("works") or [])}
    if kind == "dataset":
        return {"status": row["status"], "doi": fields["doi"], "metadata_version": fields.get("metadata_version"),
                "version": fields.get("version"),
                "related_identifiers": [[r["relation_type"], r["identifier"]]
                                        for r in fields.get("related_identifiers") or []]}
    return {"status": fields["status"], "content_update_date": fields["content_update_date"],
            "participants": [[p["pic"], p["role"], (p.get("ec_contribution") or {}).get("amount"),
                              (p.get("ec_contribution") or {}).get("currency")] for p in fields["participants"]]}


class ResearchEntityMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = ResearchEntityStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if watch not in WATCH_KINDS:
            raise ResearchEntityError("invalid_watch", f"watch one of {WATCH_KINDS}")
        if watch == "researcher" and not may_read_researchers(scopes):
            raise ResearchEntityError("unauthorized", f"{RESEARCHER_SCOPE} is required to watch a researcher")
        key = resolve_watch(watch, key)
        query = {"operation": "search", "kind": "research-entities-monitor", "watch": watch, "key": key}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "scientific", "query": query, "filters": {"watch": watch},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "research-entities-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the research-discovery source-pack schedule and the maintenance orchestrator "
                                      "commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "research-entities-monitor":
            raise ResearchEntityError("monitor_not_found", "subscription is not a research-entities monitor")
        if subscription["query"]["watch"] == "researcher" and not may_read_researchers(scopes):
            raise ResearchEntityError("unauthorized", f"{RESEARCHER_SCOPE} is required for a researcher monitor")
        return subscription

    def _related(self, namespace: str, key: str, scopes: set[str]) -> list[tuple[str, dict[str, Any]]]:
        """(item role, current record view) pairs a watched key reaches through links and accepted matches."""
        from src.kb.research_entities_links import ResearchEntityLinks

        if not table_exists(self.conn, "research_entity_links"):
            return []
        links = ResearchEntityLinks(self.conn, initialize=False).links(namespace, scopes=scopes, status="resolved")
        roles: dict[str, str] = {}
        if key.startswith("research-entities:ror:"):
            pics = {link["source_key"].rsplit(":", 1)[1] for link in links
                    if link["kind"] == "participant-organisation" and link["target_key"] == key}
            projects = {r["record_key"] for r in self.store.records(namespace, scopes=scopes, kinds=["project"])
                        if any(p.get("pic") in pics for p in r["record"]["fields"]["participants"])}
            roles.update({p: "project" for p in projects})
            for link in links:
                if (link["kind"] == "dataset-creator-affiliation" and link["target_key"] == key) or (
                        link["kind"] == "dataset-funded-by-project" and link["target_key"] in projects):
                    roles[link["source_key"]] = "dataset"
        elif key.startswith("research-entities:cordis:"):
            for link in links:
                if link["kind"] == "dataset-funded-by-project" and link["target_key"] == key:
                    roles[link["source_key"]] = "dataset"
        rows = self.store.records(namespace, scopes=scopes, record_keys=list(roles)) if roles else []
        return [(roles[r["record_key"]], r) for r in rows]

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        namespace, key = subscription["namespace"], subscription["query"]["key"]
        rows = [("record", r) for r in self.store.records(namespace, scopes=scopes, record_keys=[key])]
        rows += self._related(namespace, key, scopes)
        items, withheld = [], 0
        for role, row in rows:
            if not notifiable(row):
                withheld += 1
                continue
            base = {"kind": row["record_kind"], "role": role, "record_key": row["record_key"],
                    "source_id": row["source_id"], "provider": row["provider"], "revision_id": row["revision_id"],
                    "revision_no": row["revision_no"], "source_as_of": row["source_as_of"],
                    "evidence_origin": row["evidence_origin"], "summary": _summary(row)}
            items.append({"id": f"{role}:{row['record_key']}", **base})
            if row["record_kind"] == "researcher":
                for work in row["record"]["fields"].get("works") or []:
                    items.append({"id": f"work:{row['record_key']}:{work['put_code']}", **base, "role": "work",
                                  "summary": {"put_code": work["put_code"], "type": work["type"],
                                              "title": work["title"], "asserted_by": work["asserted_by"]["kind"],
                                              "dois": [normalize_doi(e["value"]) or e["value"]
                                                       for e in work.get("external_ids") or []
                                                       if e.get("type") == "doi"]}})
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
                raise ResearchEntityError("watermark_uncommitted", "no committed watermark yet; source-pack runs and "
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
        if event_type not in {"added", "changed", "corrected", "removed"}:
            return []
        subject = new or old

        def note(kind: str, message: str, **detail: Any) -> dict[str, Any]:
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "object": key, "record_key": subject["record_key"], "message": message,
                    "cites": {"record_key": subject["record_key"], "source_id": subject["source_id"],
                              "revision_id": new["revision_id"] if new else None,
                              "previous_revision_id": old["revision_id"] if old else None,
                              "source_as_of": subject["source_as_of"]},
                    "evidence_origin": subject["evidence_origin"], **detail}

        label = subject["record_key"]
        if subject["role"] == "work":
            if new is None:
                return [note("asserted_work_withdrawn", f"{label}: the public ORCID record no longer asserts work "
                             f"{old['summary']['put_code']}.", work=old["summary"])]
            if old is None:
                return [note("new_asserted_work", f"{label}: ORCID-asserted work {new['summary']['title']} "
                             f"{new['summary']['dois']} (not verified authorship).", work=new["summary"])]
            return []
        if new is None or (old and old["revision_id"] == new["revision_id"]):
            return []
        summary, prior = new["summary"], (old or {}).get("summary") or {}
        if old is None:
            kind = {"dataset": "new_dataset", "project": "new_project_participation"}.get(new["role"], "new_record")
            return [note(kind, f"{label}: {new['kind']} record on file (revision {new['revision_no']}, as of "
                         f"{new['source_as_of'] or 'not stated'}).", summary=summary)]
        changed = sorted(k for k in set(summary) | set(prior) if summary.get(k) != prior.get(k))
        notes = []
        if summary.get("status") != prior.get("status"):
            notes.append(note("status_changed", f"{label}: status {prior.get('status')} -> {summary.get('status')} "
                              "as published.", before=prior.get("status"), after=summary.get("status")))
        if new["kind"] == "organisation":
            added = [r for r in summary.get("relationships") or [] if r not in (prior.get("relationships") or [])]
            for relation in [r for r in added if r[0] == "successor"]:
                notes.append(note("successor_published", f"{label}: ROR publishes successor {relation[1]}.",
                                  successor=relation[1]))
        kind = {"dataset": "dataset_revised", "project": "project_revised"}.get(new["role"], "record_revised")
        notes.append(note(kind, f"{label}: revision {new['revision_no']} recorded; changed {changed}.",
                          changed_fields=changed, before={k: prior.get(k) for k in changed},
                          after={k: summary.get(k) for k in changed}))
        return notes

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
        from src.ingestion.research_entities_sources import ResearchEntitiesAdapter
        from src.kb.research_entities_records import ResearchEntityProjector

        scopes = set(scopes)
        namespace = ResearchEntityProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = ResearchEntitiesAdapter(source, transport=transport, secret=secret)
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < min(len(adapter.units), int(source["budgets"]["max_pages"])):
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = self.store.project(namespace, [r["research_entity_record"] for r in page.records],
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
