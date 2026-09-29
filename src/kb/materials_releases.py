"""Dataset releases, value changes between releases and release watches (MT11, #2089).

Refreshing a source appends its values under the new ``dataset_release``;
earlier releases stay queryable (:mod:`src.kb.materials_store`).
:func:`release_changes` diffs two releases of one provider per value identity
(record, property, condition set, method):

* ``added`` - in the later release only;
* ``changed`` - in both with a different value, uncertainty, unit, status or
  citation; both values are cited;
* ``deprecated`` / ``withdrawn`` - only when the source states it (value or
  record status);
* ``absent`` - in the earlier release only. The source did not say it was
  withdrawn, so it is reported as absence, never as withdrawal.

Watches reuse ``SubscriptionStore``: a watch is a knowledge subscription over
a material, a property and/or a provider, evaluated only at committed
watermarks (source-pack runs and the maintenance orchestrator commit them),
so there is no scheduler here and replaying a watermark creates no events. A
material target resolves through the same equivalence as lookups and
comparisons (:meth:`MaterialsComparison.resolve`). Notifications cite both
release values.
"""

from __future__ import annotations

import json
from typing import Any

from src.kb import materials_records as mr
from src.kb.materials_comparison import MaterialsComparison
from src.kb.materials_records import READ_SCOPE, canonical
from src.kb.materials_store import (
    MaterialsError,
    MaterialsStore,
    authorize,
    current_row,
    digest,
    table_exists,
    value_repr,
)

CONTRACT = "noesis-material-release-changes-v1"
NOTIFICATION_CONTRACT = "noesis-material-notification-v1"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
_DDL = """
CREATE TABLE IF NOT EXISTS materials_watches(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, targets_json TEXT NOT NULL);
"""


def _value(store, namespace, series_key):
    rows = store._versions(series_key)
    if not rows:
        return None
    row = current_row(rows)
    return {"version": int(row[0]), **json.loads(row[1])}


def _cite(value, release):
    result = {
        "release": release,
        "value": value["value"],
        "unit": value.get("unit"),
        "status": value["status"],
        "version": value["version"],
        "normalized": value["normalized"].get("normalized_value"),
    }
    if value.get("uncertainty"):
        result["uncertainty"] = value["uncertainty"]
    return {k: v for k, v in result.items() if v is not None}


def release_changes(
    conn,
    namespace,
    provider,
    *,
    scopes,
    from_release=None,
    to_release=None,
    material=None,
    prop=None,
):
    authorize(namespace, scopes, READ_SCOPE)
    store = MaterialsStore(conn, initialize=False)
    store.require_ready()
    if prop is not None:
        mr.property_definition(prop)
    releases = store.releases(namespace, provider)
    labels = [r["label"] for r in releases]
    if len(labels) < 2 and (from_release is None or to_release is None):
        return {
            "contract": CONTRACT,
            "provider": provider,
            "releases": releases,
            "changes": [],
            "n": 0,
            "note": "fewer than two releases acquired; nothing to compare",
        }
    later = to_release or labels[-1]
    earlier = from_release or labels[labels.index(later) - 1]
    for label in (earlier, later):
        if label not in labels:
            raise MaterialsError(
                "not_found", f"release {label!r} of {provider} was not acquired"
            )
    entry_ids = None
    if material is not None:
        entry_ids = set(MaterialsComparison(conn).resolve(namespace, material)[0])
    rows = conn.execute(
        "SELECT series_key, identity_key, entry_id, native_id, property, release_label FROM materials_values "
        "WHERE namespace=? AND provider=? AND release_label IN (?, ?) AND (? IS NULL OR property=?)",
        [namespace, provider, earlier, later, prop, prop],
    ).fetchall()
    by_identity: dict[str, dict[str, Any]] = {}
    for series_key, identity, eid, native, prop_id, label in rows:
        if entry_ids is not None and eid not in entry_ids:
            continue
        item = by_identity.setdefault(
            identity,
            {
                "identity_key": identity,
                "entry_id": eid,
                "native_id": native,
                "property": prop_id,
            },
        )
        item[label] = series_key
    in_release = {}
    for label in (earlier, later):
        in_release[label] = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT entry_id FROM materials_entry_versions WHERE release_label=?",
                [label],
            ).fetchall()
        }
    changes = []
    for identity, item in sorted(
        by_identity.items(),
        key=lambda kv: (kv[1]["native_id"], kv[1]["property"], kv[0]),
    ):
        before = _value(store, namespace, item[earlier]) if earlier in item else None
        after = _value(store, namespace, item[later]) if later in item else None
        base = {
            "identity_key": identity,
            "record_key": f"materials:entry:{provider}:{item['native_id']}",
            "property": item["property"],
        }
        if before is None and after is not None:
            kind = (
                after["status"]
                if after["status"] in {"deprecated", "withdrawn"}
                else "added"
            )
            changes.append({**base, "change": kind, "after": _cite(after, later)})
        elif after is None and before is not None:
            entry_in_later = item["entry_id"] in in_release[later]
            changes.append(
                {
                    **base,
                    "change": "absent",
                    "before": _cite(before, earlier),
                    "note": (
                        "the later release contains the record without this value"
                        if entry_in_later
                        else "the record is not in the later release as acquired"
                    )
                    + "; the source does not state it was withdrawn",
                }
            )
        elif before is not None and after is not None:
            if digest(value_repr(before)) == digest(value_repr(after)):
                continue
            kind = (
                after["status"]
                if after["status"] in {"deprecated", "withdrawn"}
                and before["status"] != after["status"]
                else "changed"
            )
            changes.append(
                {
                    **base,
                    "change": kind,
                    "before": _cite(before, earlier),
                    "after": _cite(after, later),
                }
            )
    entry_status = []
    for eid in sorted(in_release[earlier] & in_release[later]):
        states = []
        for label in (earlier, later):
            rows = conn.execute(
                "SELECT version, content_json, source_updated_at, change FROM materials_entry_versions "
                "WHERE entry_id=? AND release_label=? ORDER BY version",
                [eid, label],
            ).fetchall()
            states.append(json.loads(current_row(rows)[1])["status"])
        if states[0] != states[1] and (entry_ids is None or eid in entry_ids):
            head = conn.execute(
                "SELECT native_id FROM materials_entries WHERE entry_id=? AND provider=?",
                [eid, provider],
            ).fetchone()
            if head:
                entry_status.append(
                    {
                        "record_key": f"materials:entry:{provider}:{head[0]}",
                        "before": states[0],
                        "after": states[1],
                        "basis": "status stated by the source",
                    }
                )
    return {
        "contract": CONTRACT,
        "provider": provider,
        "from_release": earlier,
        "to_release": later,
        "changes": changes,
        "record_status_changes": entry_status,
        "n": len(changes),
        "semantics": "per value identity: added, changed (both values cited), deprecated/withdrawn only as the "
        "source states, absent otherwise",
    }


def evidence_scopes(namespace, scopes):
    """The scopes the watch's evidence needs (materials read on the namespace) plus the subscription operation scopes.

    Only these are retained with the subscription, so a reader with current
    materials read access can poll it; nothing the caller merely also held is
    required later.
    """

    wanted = {
        READ_SCOPE,
        f"namespace:{namespace}:read",
        SUBSCRIPTIONS_READ,
        SUBSCRIPTIONS_WRITE,
        "operator",
    }
    return set(scopes) & wanted


class MaterialsReleaseWatch:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = MaterialsStore(conn, initialize=initialize, now=now)
        self.comparison = MaterialsComparison(conn, now=now)
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    @staticmethod
    def _targets(targets):
        targets = {k: v for k, v in dict(targets or {}).items() if v is not None}
        if not targets or set(targets) - {"material", "property", "provider"}:
            raise MaterialsError(
                "invalid_watch", "watch a material, a property and/or a provider"
            )
        if "property" in targets:
            mr.property_definition(targets["property"])
        if "provider" in targets and targets["provider"] not in mr.PROVIDERS:
            raise MaterialsError("invalid_watch", "unknown materials provider")
        return dict(sorted(targets.items()))

    def create(
        self, namespace, request_key, *, targets, principal_id, scopes, delivery=None
    ):
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        targets = self._targets(targets)
        if "material" in targets:
            self.comparison.resolve(
                namespace, targets["material"]
            )  # must name acquired records
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "materials",
                "query": {
                    "operation": "search",
                    "kind": "materials-release-watch",
                    **targets,
                },
                "filters": targets,
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "materials-watch:" + request_key,
            principal_id=principal_id,
            scopes=evidence_scopes(namespace, scopes),
        )
        self.conn.execute(
            "INSERT INTO materials_watches VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            [created["subscription_id"], namespace, principal_id, canonical(targets)],
        )
        return {
            **created,
            "targets": targets,
            "refresh": "source-pack runs and the maintenance orchestrator commit the watermarks this watch evaluates",
        }

    def _watch(self, subscription_id, principal_id):
        self.store.require_ready()
        if not table_exists(self.conn, "materials_watches"):
            raise MaterialsError("not_ready", "no materials watch exists yet")
        row = self.conn.execute(
            "SELECT namespace, owner, targets_json FROM materials_watches WHERE subscription_id=?",
            [subscription_id],
        ).fetchone()
        if not row or row[1] != principal_id:
            raise MaterialsError("watch_not_found", "materials watch is unavailable")
        return {"namespace": row[0], "targets": json.loads(row[2])}

    def snapshot(self, watch):
        namespace, targets = watch["namespace"], watch["targets"]
        entry_ids = None
        if "material" in targets:
            entry_ids = self.comparison.resolve(namespace, targets["material"])[0]
        items, stale = [], set()
        for value in self.store.current_values(
            namespace,
            provider=targets.get("provider"),
            entry_ids=entry_ids,
            prop=targets.get("property"),
        ):
            if self.store.provider_state(namespace, value["provider"]).get("stale"):
                stale.add(value["provider"])
            items.append(
                {
                    "id": value["identity_key"],
                    "record_key": value["record_key"],
                    "property": value["property"],
                    "method_class": value["method_class"],
                    "release": value["release"]["label"],
                    "value": value["value"],
                    "unit": value.get("unit"),
                    "status": value["status"],
                    "version": value["version"],
                    "series_key": value["series_key"],
                    "corrected": value["change"] == "correction",
                }
            )
        return {
            "items": items,
            "coverage": {"complete": not stale, "stale_providers": sorted(stale)},
        }

    def run(self, subscription_id, watermark=None, *, principal_id, scopes):
        watch = self._watch(subscription_id, principal_id)
        authorize(watch["namespace"], scopes, READ_SCOPE)
        if watermark is None:
            row = self.conn.execute(
                "SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                [watch["namespace"]],
            ).fetchone()
            if row is None or row[0] is None:
                raise MaterialsError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the "
                    "maintenance orchestrator commit them",
                )
            watermark = int(row[0])
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            watermark,
            self.snapshot(watch),
            principal_id=principal_id,
            scopes=evidence_scopes(watch["namespace"], scopes),
            observed_at_ms=self.store.now(),
        )
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM "
                "knowledge_subscription_events WHERE event_id=?",
                [event_id],
            ).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "notifications": notifications,
            "delivery": "configured subscription channel (poll by default)",
        }

    @staticmethod
    def _classify(event_id, event_type, key, before, after):
        before = json.loads(before) if before else None
        after = json.loads(after) if after else None

        def note(kind, message, cites):
            return {
                "contract": NOTIFICATION_CONTRACT,
                "notification_id": f"{event_id}:{kind}",
                "event_id": event_id,
                "kind": kind,
                "object": key,
                "message": message,
                "cites": cites,
            }

        def cite(item):
            return {
                k: item.get(k)
                for k in (
                    "record_key",
                    "release",
                    "value",
                    "unit",
                    "status",
                    "version",
                    "series_key",
                )
                if item.get(k) is not None
            }

        if event_type == "coverage-degraded":
            return [
                note(
                    "stale_source",
                    "Refresh failed or never succeeded for "
                    + ", ".join(after.get("stale_providers") or [])
                    + "; values are unchanged, not removed.",
                    {},
                )
            ]
        if event_type == "added":
            return [
                note(
                    "value_in_view",
                    f"{after['record_key']} {after['property']} = {after['value']} "
                    f"{after.get('unit') or ''} (release {after['release']}).",
                    {"after": cite(after)},
                )
            ]
        if event_type == "removed":
            return [
                note(
                    "no_longer_in_view",
                    f"{before['record_key']} {before['property']} left the watched view "
                    "(not treated as withdrawn).",
                    {"before": cite(before)},
                )
            ]
        if before["release"] != after["release"]:
            kind, text = (
                "new_release_value",
                (
                    f"release {after['release']} replaces {before['release']}: "
                    f"{before['value']} -> {after['value']}"
                ),
            )
        elif before["status"] != after["status"]:
            kind, text = (
                "status_changed",
                f"status {before['status']} -> {after['status']} as the source states",
            )
        else:
            kind, text = (
                "corrected_value",
                (
                    f"release {after['release']} corrected: {before['value']} -> "
                    f"{after['value']}"
                ),
            )
        return [
            note(
                kind,
                f"{after['record_key']} {after['property']}: {text}.",
                {"before": cite(before), "after": cite(after)},
            )
        ]

    def poll(self, subscription_id, *, principal_id, scopes, cursor=""):
        watch = self._watch(subscription_id, principal_id)
        authorize(watch["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(
            subscription_id,
            principal_id=principal_id,
            scopes=set(scopes),
            cursor=cursor,
        )
