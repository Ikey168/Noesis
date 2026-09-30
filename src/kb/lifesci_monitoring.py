"""Monitor life-science records through ``platform.subscriptions`` (#2652, LS11 #2706).

A life-sciences monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), as the Campaign finance and
Biodiversity monitors are (:mod:`src.kb.campaign_finance_monitoring`,
:mod:`src.kb.biodiversity_monitoring`): no monitor table, queue or scheduler of
its own. The query names what is watched:

* **accessions** - any covered native key or subject key (UniProt accession,
  Gene ID, PDB ID, ChEMBL compound ID); successors the source names are
  followed, so a merged accession keeps reporting on its successor;
* **targets** - a ChEMBL target ID or a UniProt accession that a ChEMBL target
  component names;
* **taxa** - an NCBI Tax ID.

The ``primary-scientific-evidence`` source pack's schedule refreshes the
bounded selections under their budgets, with receipts. A monitor evaluates
only at a committed source-pack watermark whose run completed every
life-sciences source it ran; a replay at the same watermark delivers nothing.
Every stored revision that changes published content is one cumulative item;
a new release that republishes identical content is *unchanged* and emits
nothing. Notices are record changes, never assessments:

* ``entry_new`` / ``entry_revised`` (with the fields that changed, e.g. entry or
  sequence version) / ``entry_obsoleted`` (with the successors);
* ``activity_added`` / ``activity_revised`` / ``activity_removed`` for a target;
* ``taxon_new`` / ``taxon_revised`` / ``taxon_merged``.

Each notice cites the prior and the new revision.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.lifesci_records import (
    NOTIFICATION_CONTRACT,
    READ_SCOPE,
    SOURCE_PACK,
    LifeSciError,
    authorize,
)
from src.kb.lifesci_store import LifeSciStore, table_exists

KIND = "lifesci-monitor"
EVENT_KINDS = ("entry_new", "entry_revised", "entry_obsoleted", "activity_added", "activity_revised",
               "activity_removed", "taxon_new", "taxon_revised", "taxon_merged")
GENERATION_SPAN = 1_000_000_000
_IGNORED = {"release"}  # a republication in a new release is not a change


def _cite(record: Mapping[str, Any], revision: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if revision is None:
        return None
    return {"record_id": record["record_id"], "revision_id": revision["revision_id"], "event": revision["event"],
            "release": revision["release"], "retrieved_on": revision["retrieved_on"],
            "url": revision["statement"]["source"]["url"]}


def changed_fields(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Top-level published fields that differ (release excluded); sequences are summarised, never compared."""
    if before is None:
        return []
    out = []
    for key in sorted(set(before) | set(after)):
        if key in _IGNORED or before.get(key) == after.get(key):
            continue
        if key == "sequence":
            out.append({"field": "sequence", "change": "the published sequence differs (see sequence_version); no "
                                                       "sequence comparison is made"})
        elif isinstance(after.get(key), (dict, list)) or isinstance(before.get(key), (dict, list)):
            out.append({"field": key, "change": "published value differs"})
        else:
            out.append({"field": key, "before": before.get(key), "after": after.get(key)})
    return out


class LifeSciMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.lifesci_identity import LifeSciIdentity
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = LifeSciStore(conn, initialize=initialize, now=self.now)
        self.identity = LifeSciIdentity(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    # ------------------------------------------------------------------ resolution

    def _entry_records(self, namespace: str, key: str) -> list[dict[str, Any]]:
        """Records for a watched key and the successors their sources name (bounded)."""
        from src.kb.lifesci_queries import successors

        records = {r["subject_key"]: r for r in self.store.records(namespace)
                   if r["record_type"] in {"protein", "gene", "structure", "compound", "taxon"}}
        frontier = [k for k, r in records.items() if key in {k, r["record_key"]}]
        found: dict[str, dict[str, Any]] = {}
        while frontier and len(found) < 20:
            subject = frontier.pop()
            if subject in found or subject not in records:
                continue
            found[subject] = records[subject]
            for revision in self.store.revisions(namespace, records[subject]["record_id"]):
                frontier += successors(records[subject]["record_type"], revision["statement"]["as_published"])
        return list(found.values())

    def _targets(self, namespace: str, key: str) -> set[str]:
        out = set()
        for record in self.store.records(namespace, record_type="target"):
            if record["record_key"] == key:
                out.add(key)
                continue
            for revision in self.store.revisions(namespace, record["record_id"]):
                if key in {c.get("accession") for c in revision["statement"]["as_published"].get("components") or []}:
                    out.add(record["record_key"])
        return out

    # ------------------------------------------------------------------ subscriptions

    def create(self, namespace: str, request_key: str, *, principal_id: str, scopes: Iterable[str],
               accessions: list[str] | None = None, targets: list[str] | None = None, taxa: list[str] | None = None,
               delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        if not (accessions or targets or taxa):
            raise LifeSciError("invalid_watch", "watch at least one accession, target or taxon")
        for key in accessions or []:
            if not self._entry_records(namespace, key):
                raise LifeSciError("not_found", f"{key!r} reaches no acquired record")
        for key in targets or []:
            if not self._targets(namespace, key):
                raise LifeSciError("not_found", f"{key!r} reaches no acquired ChEMBL target")
        for key in taxa or []:
            if not self.store.find(namespace, "taxon", key):
                raise LifeSciError("not_found", f"Tax ID {key!r} is not acquired")
        watch = {"accessions": sorted(set(accessions or [])), "targets": sorted(set(targets or [])),
                 "taxa": sorted(set(taxa or []))}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "science",
             "query": {"operation": "search", "kind": KIND, "watch": watch},
             "filters": {"watch": ["accessions", "targets", "taxa"]},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "lifesci-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": f"evaluated at complete {SOURCE_PACK} source-pack runs of the life-sciences "
                                      "sources within their declared bounds and budgets; no separate scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != KIND:
            raise LifeSciError("monitor_not_found", "subscription is not a life-sciences monitor")
        return subscription

    def complete_watermark(self, namespace: str, watermark: int | None = None) -> dict[str, Any]:
        if not table_exists(self.conn, "source_pack_watermarks") or not table_exists(self.conn,
                                                                                      "lifesci_source_runs"):
            raise LifeSciError("watermark_uncommitted", "no committed life-sciences run yet")
        rows = self.conn.execute(
            "SELECT watermark, committed_at_ms, run_id FROM source_pack_watermarks WHERE pack_id=? "
            "AND (? IS NULL OR watermark=?) ORDER BY watermark DESC", [SOURCE_PACK, watermark, watermark]).fetchall()
        for mark, committed_at, run_id in rows:
            runs = self.store.runs(namespace, run_id)
            if not runs:
                continue
            if any(r["status"] != "complete" for r in runs):
                if watermark is not None:
                    raise LifeSciError("incomplete_run", "that run did not complete every source it ran")
                continue
            return {"source_watermark": int(mark), "committed_at_ms": int(committed_at), "run_id": run_id,
                    "cutoff_seq": max(r["cutoff_seq"] for r in runs),
                    "receipts": [{k: r[k] for k in ("source_id", "provider", "status", "evidence_origin")}
                                 for r in runs]}
        raise LifeSciError("watermark_uncommitted", "no complete life-sciences run is committed yet")

    # ------------------------------------------------------------------ snapshot

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        namespace = subscription["namespace"]
        watch = subscription["query"]["watch"]
        items: list[dict[str, Any]] = []

        def walk(watched: str, record: Mapping[str, Any], kinds: tuple[str, str, str], obsolete: str) -> None:
            previous = None
            for revision in self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff):
                published = revision["statement"]["as_published"]
                event = revision["event"]
                if previous is None:
                    kind = obsolete if event != "published" else kinds[0]
                elif event != "published":
                    kind = obsolete
                else:
                    kind = kinds[1]
                changes = changed_fields(previous["statement"]["as_published"] if previous else None, published)
                if previous is not None and not changes and event == previous["event"]:
                    previous = revision
                    continue  # republished unchanged in a new release: no notice
                item = {"id": f"{watched}:{kind}:{revision['revision_id']}", "kind": kind, "watched": watched,
                        "record_key": record["record_key"], "record_type": record["record_type"],
                        "prior": _cite(record, previous), "new": _cite(record, revision), "changes": changes}
                if event != "published":
                    from src.kb.lifesci_queries import successors

                    item["successors"] = successors(record["record_type"], published)
                    item["basis"] = revision["statement"]["effective"].get("date_basis")
                items.append(item)
                previous = revision

        for key in watch.get("accessions") or []:
            for record in self._entry_records(namespace, key):
                walk(f"accession:{key}", record, ("entry_new", "entry_revised", ""), "entry_obsoleted")
        for key in watch.get("targets") or []:
            targets = self._targets(namespace, key)
            for record in self.store.records(namespace, record_type="activity"):
                revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff)
                if revisions and revisions[0]["statement"]["as_published"]["target_chembl_id"] in targets:
                    walk(f"target:{key}", record, ("activity_added", "activity_revised", ""), "activity_removed")
        for key in watch.get("taxa") or []:
            record = self.store.find(namespace, "taxon", key)
            if record:
                walk(f"taxon:{key}", record, ("taxon_new", "taxon_revised", ""), "taxon_merged")
        return {"items": sorted(items, key=lambda i: i["id"]), "coverage": {"complete": True}}

    # ------------------------------------------------------------------ run

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        mark = self.complete_watermark(namespace, watermark)
        generation = self.identity.generation(namespace)
        combined = mark["source_watermark"] * GENERATION_SPAN + generation
        self.subscriptions.commit_watermark(
            namespace, combined, kind="ingestion",
            detail={"source_pack": SOURCE_PACK, "source_watermark": mark["source_watermark"],
                    "identity_generation": generation, "cutoff_seq": mark["cutoff_seq"]},
            committed_at_ms=mark["committed_at_ms"])
        baseline = subscription["last_watermark"] is None
        evaluated = self.subscriptions.evaluate(subscription_id, combined,
                                                self.snapshot(subscription, mark["cutoff_seq"]),
                                                principal_id=principal_id, scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            event_type, key, after = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id]).fetchone()
            if event_type != "added" or not after:
                continue  # items are cumulative per revision; only additions are news
            item = json.loads(after)
            notifications.append({"contract": NOTIFICATION_CONTRACT, "notification_id": f"{event_id}:{item['kind']}",
                                  "event_id": event_id, "kind": item["kind"], "object": key,
                                  "watched": item["watched"], "record_key": item["record_key"],
                                  "record_type": item["record_type"], "prior": item["prior"], "new": item["new"],
                                  "changes": item["changes"], "successors": item.get("successors"),
                                  "baseline": baseline,
                                  "notice": "a record change as the source published it; not an assessment"})
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": combined,
                "source_watermark": mark["source_watermark"], "run_id": mark["run_id"], "receipts": mark["receipts"],
                "baseline": baseline, "notifications": notifications, "delivery": subscription["delivery"]}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        if not table_exists(self.conn, "knowledge_subscriptions"):
            raise LifeSciError("not_ready", "no life-sciences monitor exists yet")
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)


__all__ = ["EVENT_KINDS", "KIND", "LifeSciMonitor", "changed_fields"]
