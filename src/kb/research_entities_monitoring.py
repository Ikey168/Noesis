"""Monitor research-entity registry changes through subscriptions (#2579, RE11 #2634).

A research-entities monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
delivered through :mod:`src.kb.subscription_delivery`) whose query names one organisation (ROR ID), researcher
(ORCID iD), project (CORDIS programme and id) or dataset (DOI): no watcher table and no scheduler. Each run evaluates a
committed watermark against a snapshot of the watched records; a replay, an idempotent re-acquisition or a restart
emits nothing, and a release that changes nothing emits nothing.

Notices are record changes, never assessments. Each cites the new or revised record revision and states what changed:

* ``new_record`` - the first revision of a watched record;
* ``registry_change`` - a later revision: the changed fields (status, relationships, names, participants,
  contributions, related identifiers ...) with the revision it revises; removals are registry changes too;
* ``new_asserted_work`` - a work identifier newly asserted in a watched researcher's ORCID record (an assertion, not
  authorship);
* ``new_dataset`` - a DataCite dataset that newly names a watched organisation (affiliation identifier) or researcher
  (ORCID iD) or relates to a watched dataset;
* ``new_project`` - a CORDIS project in which a participant accepted as the watched organisation takes part.

Researcher monitors need the researchers scope. :meth:`ResearchEntitiesMonitor.refresh` re-acquires a source's declared
documents through its runtime adapter within the page budget, records one receipt per run, stops at the first
rate-limit answer and refuses to request again before its Retry-After. Live records from providers still
``unverified-live`` are withheld from notices.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.research_entities_sources import unverified
from src.kb.research_entities_records import (
    READ_SCOPE,
    RESEARCHER_SCOPE,
    WRITE_SCOPE,
    ResearchEntitiesError,
    ResearchEntitiesProjector,
    ResearchEntitiesStore,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    native_for,
    require_scope,
    table_exists,
)

CONTRACT = "noesis-research-entity-notification-v1"
WATCH_KEYS = ("ror", "orcid", "project", "doi")
EVENT_TYPES = ("new_record", "registry_change", "new_asserted_work", "new_dataset", "new_project")
MESSAGES = {
    "new_record": "A watched registry record was acquired for the first time",
    "registry_change": "A registry published a revised version of a watched record",
    "new_asserted_work": "A watched researcher's ORCID record asserts a new work identifier",
    "new_dataset": "A DataCite dataset newly names the watched record",
    "new_project": "A CORDIS project lists a participant accepted as the watched organisation",
}
KIND_OF = {"ror": "organisation", "orcid": "researcher", "project": "project", "doi": "dataset"}
_DDL = """
CREATE TABLE IF NOT EXISTS rentity_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def notifiable(citation: Mapping[str, Any]) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return citation["evidence_origin"] != "live" or not unverified(citation["provider"])


def changed_fields(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Top-level differences between two statements (status and body keys), before and after as recorded."""
    if before is None:
        return []
    out = []
    if before["status"] != after["status"]:
        out.append({"field": "status", "before": before["status"], "after": after["status"]})
    keys = sorted(set(before["body"]) | set(after["body"]))
    for key in keys:
        if key == "provider_modified":
            continue
        if canonical(before["body"].get(key)) != canonical(after["body"].get(key)):
            out.append({"field": key, "before": before["body"].get(key), "after": after["body"].get(key)})
    return out


class ResearchEntitiesMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = ResearchEntitiesStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _watch(watch: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(watch or {})
        chosen = [k for k in WATCH_KEYS if raw.get(k)]
        if set(raw) - set(WATCH_KEYS) - {"programme"} or len(chosen) != 1:
            raise ResearchEntitiesError("invalid_watch", f"a monitor names one of {WATCH_KEYS}")
        key = chosen[0]
        return {"key": key, "kind": KIND_OF[key], "native_id": native_for(KIND_OF[key], raw[key], raw.get("programme"))}

    def create(self, namespace: str, request_key: str, *, watch: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = self._watch(watch)
        if wanted["kind"] == "researcher":
            require_scope(scopes, RESEARCHER_SCOPE)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "scientific",
                "query": {"operation": "search", "kind": "research-entities-monitor", "filter": wanted},
                "filters": {"watch": "research-entities"},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "research-entities-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {**created, "refresh": "the research-discovery source-pack schedule (or refresh()) acquires records "
                "and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "research-entities-monitor":
            raise ResearchEntitiesError("monitor_not_found", "subscription is not a research-entities monitor")
        return subscription

    # ------------------------------------------------------------------ snapshot

    def _chain(self, namespace: str, record_id: str) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        return [(r, self.store.statement(namespace, r["revision_id"]), self.store.citation(namespace, r))
                for r in self.store.revisions(namespace, record_id)]

    def _record_items(self, namespace: str, wanted: Mapping[str, Any]) -> list[dict[str, Any]]:
        record_id = self.store.find(namespace, wanted["kind"], wanted["native_id"])
        if record_id is None:
            return []
        items, previous = [], None
        for revision, statement, citation in self._chain(namespace, record_id):
            changes = changed_fields(previous, statement)
            if wanted["kind"] == "researcher":
                # Personal fields are named, never repeated in a notice (minimisation decision).
                changes = [{"field": c["field"], "changed": True} for c in changes]
            kind = "new_record" if previous is None else "registry_change"
            items.append({"id": f"revision:{revision['revision_id']}", "item": kind,
                          "record": {"kind": wanted["kind"], "native_id": wanted["native_id"]},
                          "status": statement["status"], "changes": changes,
                          "previous_revision_id": revision["previous_revision_id"],
                          "record_ids": [record_id, revision["revision_id"]]
                          + ([revision["previous_revision_id"]] if revision["previous_revision_id"] else []),
                          "citation": citation})
            if wanted["kind"] == "researcher":
                before = {i["value"] for w in (previous or {"body": {}})["body"].get("works") or []
                          for i in w["identifiers"]} if previous else set()
                for work in statement["body"].get("works") or []:
                    for identifier in work["identifiers"]:
                        if previous is not None and identifier["value"] not in before:
                            items.append({"id": f"work:{revision['revision_id']}:{identifier['value']}",
                                          "item": "new_asserted_work", "work": {**identifier,
                                                                               "title": work.get("title")},
                                          "assertion": "orcid-asserted, not verified authorship",
                                          "record_ids": [record_id, revision["revision_id"]], "citation": citation})
            previous = statement
        return items

    def _dataset_items(self, namespace: str, wanted: Mapping[str, Any]) -> list[dict[str, Any]]:
        items = []
        for record in self.store.records(namespace, kind="dataset"):
            if wanted["kind"] == "dataset" and record["native_id"] == wanted["native_id"]:
                continue
            for revision, statement, citation in self._chain(namespace, record["record_id"]):
                body = statement["body"]
                if not body:
                    continue
                names = False
                if wanted["kind"] == "organisation":
                    names = any(a["identifier"] == wanted["native_id"] for c in body["creators"]
                                for a in c["affiliation_identifiers"])
                elif wanted["kind"] == "researcher":
                    names = any(c.get("orcid") == wanted["native_id"] for c in body["creators"])
                elif wanted["kind"] == "dataset":
                    names = any(str(r.get("relatedIdentifier") or "").casefold() == wanted["native_id"]
                                for r in body["related_identifiers"])
                if names:
                    items.append({"id": f"dataset:{record['record_id']}", "item": "new_dataset",
                                  "dataset": {"doi": body["doi"], "titles": body["titles"]},
                                  "record_ids": [record["record_id"], revision["revision_id"]], "citation": citation})
                    break
        return items

    def _project_items(self, namespace: str, wanted: Mapping[str, Any]) -> list[dict[str, Any]]:
        if wanted["kind"] != "organisation" or not table_exists(self.conn, "rentity_identity_matches"):
            return []
        from src.kb.research_entities_identity import ResearchEntitiesIdentity

        pics = {a["pic"]: a for a in ResearchEntitiesIdentity(self.conn, initialize=False).accepted_pics(
            namespace, wanted["native_id"])}
        items = []
        for record in self.store.records(namespace, kind="project"):
            for revision, statement, citation in self._chain(namespace, record["record_id"]):
                mine = [p for p in statement["body"].get("participants") or [] if p["pic"] in pics]
                if mine:
                    items.append({"id": f"project:{record['record_id']}", "item": "new_project",
                                  "project": {"native_id": record["native_id"],
                                              "acronym": statement["body"].get("acronym"),
                                              "roles": sorted({str(p.get("role")) for p in mine})},
                                  "identity": [pics[p["pic"]] for p in mine],
                                  "record_ids": [record["record_id"], revision["revision_id"]],
                                  "citation": citation})
                    break
        return items

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        items = (self._record_items(namespace, wanted) + self._dataset_items(namespace, wanted)
                 + self._project_items(namespace, wanted))
        kept = [i for i in items if notifiable(i["citation"])]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        """The namespace's current research-entity state as a watermark: reused when this state was already
        committed (a restart replays it and emits nothing), else a new one after every committed watermark."""
        latest = self.store.latest_retrieval_ms(namespace)
        if latest is None:
            raise ResearchEntitiesError("not_ready", "no research-entity record yet; acquire first")
        state = [r[0] for r in self.conn.execute(
            "SELECT revision_id FROM rentity_revisions WHERE namespace=? ORDER BY revision_id", [namespace]).fetchall()]
        if table_exists(self.conn, "rentity_identity_matches"):
            state += [f"{r[0]}:{r[1]}" for r in self.conn.execute(
                "SELECT match_id, state FROM rentity_identity_matches WHERE namespace=? ORDER BY match_id",
                [namespace]).fetchall()]
        generation = digest(state)[:24]
        rows = (
            self.conn.execute("SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? "
                              "ORDER BY watermark", [namespace]).fetchall()
            if table_exists(self.conn, "knowledge_subscription_watermarks") else []
        )
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("research_entities_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {"research_entities_generation": generation,
                                          "observed_at": iso_from_ms(latest)}

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if subscription["query"]["filter"]["kind"] == "researcher":
            require_scope(scopes, RESEARCHER_SCOPE)
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
        citation = item["citation"]
        cited = f" ({citation['provider']} {citation['native_id']}, revision {citation['revision']}, " \
            f"{citation['revision_marker']})"
        return [{
            "contract": CONTRACT,
            "event_id": event_id,
            "notification_id": f"{event_id}:{item['item']}",
            "object": key,
            "kind": item["item"],
            "message": f"{MESSAGES[item['item']]}{cited}.",
            "record_ids": item["record_ids"],
            "citation": citation,
            **{k: item[k] for k in ("record", "status", "changes", "previous_revision_id", "work", "assertion",
                                    "dataset", "project", "identity") if k in item},
            "note": "a registry record change reported as recorded; nothing is concluded about the change",
        }]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        if subscription["query"]["filter"]["kind"] == "researcher":
            require_scope(scopes, RESEARCHER_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, max_documents: int | None = None,
                secret: str | None = None) -> dict[str, Any]:
        """Acquire a source's declared documents within its page budget; idempotent by file, one receipt per run,
        stopped at the first rate-limit answer and refused before the provider's Retry-After has passed."""
        from src.ingestion.research_entities_sources import ResearchEntitiesAdapter
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT retry_at_ms FROM rentity_refresh_receipts WHERE namespace=? AND source_id=? "
            "ORDER BY started_at_ms DESC, rowid DESC LIMIT 1", [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = ResearchEntitiesAdapter(source, transport=transport, secret=secret)
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents))
        projector = ResearchEntitiesProjector(self.conn)
        projector.store.now = self.now
        releases, stopped, retry_at, cursor = [], None, None, None
        for _ in range(limit):
            try:
                page = adapter.fetch_page({"operation": "registry-records", "parameters": {},
                                           "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            except SourcePackError as exc:
                stopped = {"code": exc.code, "message": exc.message, **exc.details}
                if exc.code == "rate_limited":
                    retry_at = now + int(exc.details.get("retry_after_ms") or 60_000)
                break
            applied = projector.project_page(run_id=f"refresh:{source['source_id']}:{now}", manifest=None,
                                             source=source, records=page.records, documents=None,
                                             page_receipt=page.receipt, principal_id=principal_id)
            releases += [{"release_id": a["release_id"], "status": a["status"],
                          "revisions": a.get("created", 0) + a.get("revised", 0) + a.get("removed", 0),
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
        receipt_id = "rentity-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO rentity_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["EVENT_TYPES", "ResearchEntitiesMonitor", "changed_fields", "notifiable"]
