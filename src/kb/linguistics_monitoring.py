"""Watch lexemes, senses, languoids and WALS parameters through ``platform.subscriptions`` (LG10, #2188).

A linguistics monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names the watch
target. There is no monitor table and no scheduler. Each evaluation runs at a
committed watermark, which source-pack runs and the maintenance orchestrator
commit. The subscription store turns differences between successive snapshots
into events on its poll and outbox paths. Re-evaluating a watermark, or a
refresh that changed nothing, produces no event.

Snapshots are built from the **current** record per source (source order, see
:mod:`src.kb.linguistics_store`). An older extract or release that arrives late
never changes what is current, so it never raises a false event. A lexeme watch
covers the lexeme's accepted equivalents: the same equivalence definition that
lookups use.

Events (each cites the old and new revision and the source release):

* ``definition_revised``: a definition's current revision changed;
* ``sense_added``, ``sense_removed``: the lexeme's current source revision
  states a sense it did not state before, or no longer states one;
* ``etymology_changed``: the lexeme's etymology assertions changed;
* ``classification_changed``: a languoid's parent or classification path
  changed between releases;
* ``iso_code_changed``: the languoid's ISO 639-3 code, or a retirement touching
  it, changed;
* ``feature_value_changed``: a WALS value changed.

The first evaluation is a baseline: its events are recorded but reported as
``in_view``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.linguistics_identity import LinguisticsIdentity
from src.kb.linguistics_records import (
    GLOTTOCODE,
    READ_SCOPE,
    WALS_FEATURE,
    LinguisticsError,
    authorize,
    content_hash,
    digest,
)

CONTRACT = "noesis-linguistic-notification-v1"
WATCH_KINDS = ("lexeme", "sense", "languoid", "wals-parameter")
EVENTS = (
    "definition_revised",
    "sense_added",
    "sense_removed",
    "etymology_changed",
    "classification_changed",
    "iso_code_changed",
    "feature_value_changed",
)


class LinguisticsMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.identity = LinguisticsIdentity(conn, initialize=False, now=now)
        self.store = self.identity.store
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        watch: str,
        target: str,
        principal_id: str,
        scopes: Iterable[str],
        delivery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        if watch not in WATCH_KINDS:
            raise LinguisticsError("invalid_watch", f"watch one of {WATCH_KINDS}")
        prefix = {"lexeme": "lexeme:", "sense": "sense:"}.get(watch)
        if prefix and (
            not target.startswith(prefix)
            or self.store.current(namespace, target) is None
        ):
            raise LinguisticsError(
                "invalid_watch", f"a {watch} watch names an acquired {watch} record key"
            )
        if watch == "languoid" and not GLOTTOCODE.fullmatch(target):
            raise LinguisticsError(
                "invalid_watch", "a languoid watch names a Glottocode"
            )
        if watch == "wals-parameter" and not WALS_FEATURE.fullmatch(target):
            raise LinguisticsError(
                "invalid_watch", "a WALS parameter watch names a feature id such as 81A"
            )
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "linguistics",
                "query": {
                    "operation": "search",
                    "kind": "linguistics-monitor",
                    "watch": watch,
                    "target": target,
                },
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": dict(delivery or {"kind": "poll"}),
            },
            "linguistics-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "events": list(EVENTS),
            "refresh": "source-pack runs and the maintenance orchestrator commit the watermarks this monitor "
            "evaluates; no separate scheduler",
        }

    def _subscription(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        from src.kb.linguistics_store import table_exists

        if not table_exists(self.conn, "knowledge_subscriptions"):
            raise LinguisticsError("not_ready", "no linguistics monitor exists yet")
        self.store.require_ready()
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "linguistics-monitor":
            raise LinguisticsError(
                "monitor_not_found", "subscription is not a linguistics monitor"
            )
        return subscription

    # ------------------------------------------------------------ snapshots

    def snapshot(self, subscription: Mapping[str, Any]) -> dict[str, Any]:
        namespace, query = subscription["namespace"], subscription["query"]
        watch, target = query["watch"], query["target"]
        if not self.store.ready():
            return {
                "items": [],
                "coverage": {
                    "complete": False,
                    "reason": "no linguistic source acquired",
                },
            }
        items: list[dict[str, Any]] = []
        if watch == "lexeme":
            for key in self.identity.equivalents(namespace, target, kind="lexeme"):
                items += self._lexeme_items(namespace, key)
        elif watch == "sense":
            items += self._definition_items(namespace, target)
        elif watch == "languoid":
            items += self._languoid_items(namespace, target)
        else:
            for value in self.store.currents(
                namespace, kind="typological_value", provider="wals"
            ):
                if value["body"]["parameter"] == target:
                    items.append(self._value_item(value))
        return {"items": items, "coverage": {"complete": True}}

    def _lexeme_items(self, namespace: str, key: str) -> list[dict[str, Any]]:
        lexeme = self.store.current(namespace, key)
        if lexeme is None:
            return []
        revision = self._last_seen(lexeme)
        # the group's membership: a lexeme joining or leaving through a reviewed identity decision is not a
        # source change, so its senses never read as added or removed
        items = [{"id": f"member:{key}", "kind": "member", "lexeme": key}]
        if lexeme["body"].get("status") == "deleted":
            return items + [
                {
                    "id": f"lexeme:{key}",
                    "kind": "lexeme",
                    "status": "deleted",
                    "revision_id": lexeme["revision_id"],
                    "source_revision": revision,
                }
            ]
        for sense in self.store.currents(namespace, kind="sense"):
            # a sense is stated only while the lexeme's current source revision restates it
            if sense["body"]["lexeme"] != key or self._last_seen(sense) != revision:
                continue
            items.append(
                {
                    "id": f"sense:{sense['record_key']}",
                    "kind": "sense",
                    "lexeme": key,
                    "sense": sense["record_key"],
                    "revision_id": sense["revision_id"],
                    "source_revision": sense["source_revision"],
                }
            )
            items += self._definition_items(
                namespace, sense["record_key"], lexeme_revision=revision
            )
        assertions = sorted(
            (
                a
                for a in self.store.currents(namespace, kind="etymology_assertion")
                if a["body"]["lexeme"] == key and self._last_seen(a) == revision
            ),
            key=lambda a: a["record_key"],
        )
        items.append(
            {
                "id": f"etymology:{key}",
                "kind": "etymology",
                "fingerprint": digest(
                    [content_hash(a["kind"], a["body"]) for a in assertions]
                ),
                "assertions": [
                    {
                        "record_key": a["record_key"],
                        "revision_id": a["revision_id"],
                        "relation": a["body"]["relation"],
                    }
                    for a in assertions
                ],
            }
        )
        return items

    @staticmethod
    def _last_seen(record: Mapping[str, Any]) -> str:
        return (record.get("last_seen") or {}).get("source_revision") or record[
            "source_revision"
        ]

    def _definition_items(
        self, namespace: str, sense_key: str, *, lexeme_revision: str | None = None
    ) -> list[dict]:
        items = []
        for definition in self.store.currents(namespace, kind="definition_revision"):
            if definition["body"]["sense"] != sense_key:
                continue
            if (
                lexeme_revision is not None
                and self._last_seen(definition) != lexeme_revision
            ):
                continue
            items.append(
                {
                    "id": f"definition:{definition['record_key']}",
                    "kind": "definition",
                    "definition": definition["record_key"],
                    "language": definition["body"]["language"],
                    "text": definition["body"]["text"],
                    "revision_id": definition["revision_id"],
                    "source_revision": definition["source_revision"],
                    "revision_date": definition["revision_date"],
                }
            )
        return items

    def _languoid_items(self, namespace: str, glottocode: str) -> list[dict[str, Any]]:
        items = []
        record = self.store.current(namespace, f"languoid:glottolog:{glottocode}")
        if record is not None:
            body = record["body"]
            items.append(
                {
                    "id": f"classification:{glottocode}",
                    "kind": "classification",
                    "parent": body.get("parent"),
                    "classification": body.get("classification") or [],
                    "level": body["level"],
                    "revision_id": record["revision_id"],
                    "release": record["source_revision"],
                }
            )
            iso = body.get("iso639_3")
            changes = [
                self.identity._change_event(c)
                for c in self.store.currents(namespace, kind="iso_code_change")
                if iso
                and (
                    c["body"]["code"] == iso
                    or iso in self.identity._change_event(c)["successors"]
                )
            ]
            items.append(
                {
                    "id": f"iso:{glottocode}",
                    "kind": "iso",
                    "iso639_3": iso,
                    "changes": sorted(
                        changes, key=lambda e: (e["effective"], e["code"])
                    ),
                    "revision_id": record["revision_id"],
                    "release": record["source_revision"],
                }
            )
        wals = {
            r["body"]["wals_code"]
            for r in self.store.currents(namespace, kind="typological_language")
            if r["body"].get("glottocode") == glottocode
        }
        for value in self.store.currents(
            namespace, kind="typological_value", provider="wals"
        ):
            if value["body"]["wals_code"] in wals:
                items.append(self._value_item(value))
        return items

    @staticmethod
    def _value_item(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": f"value:{value['record_key']}",
            "kind": "feature-value",
            "parameter": value["body"]["parameter"],
            "wals_code": value["body"]["wals_code"],
            "value": value["body"]["value"],
            "code_id": value["body"].get("code_id"),
            "revision_id": value["revision_id"],
            "release": value["source_revision"],
        }

    # ------------------------------------------------------------ run and poll

    def run(
        self,
        subscription_id: str,
        watermark: int | None = None,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            row = self.conn.execute(
                "SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                [namespace],
            ).fetchone()
            if row is None or row[0] is None:
                raise LinguisticsError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the "
                    "maintenance orchestrator commit them",
                )
            watermark = int(row[0])
        baseline = subscription.get("last_watermark") is None
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            watermark,
            self.snapshot(subscription),
            principal_id=principal_id,
            scopes=scopes,
            observed_at_ms=self.now(),
        )
        rows = [
            self.conn.execute(
                "SELECT event_id, event_type, object_key, before_json, after_json FROM "
                "knowledge_subscription_events WHERE event_id=?",
                [event_id],
            ).fetchone()
            for event_id in evaluated.get("event_ids", [])
        ]
        regrouped = {
            json.loads(row[4] or row[3])["lexeme"]
            for row in rows
            if row[1] in {"added", "removed"} and row[2].startswith("member:")
        }
        notifications = []
        for row in rows:
            notifications += classify(*row, baseline=baseline, regrouped=regrouped)
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "baseline": baseline,
            "notifications": notifications,
            "n": len(notifications),
            "delivery": subscription["delivery"],
        }

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )


def classify(
    event_id: str,
    event_type: str,
    key: str,
    before: str | None,
    after: str | None,
    *,
    baseline: bool,
    regrouped: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    old = json.loads(before) if before else None
    new = json.loads(after) if after else None
    item = new or old or {}
    if item.get("lexeme") in regrouped and event_type in {"added", "removed"}:
        return []  # joined or left the watched group through identity review, not a source change

    def note(kind: str, message: str, cites: dict[str, Any]) -> dict[str, Any]:
        return {
            "contract": CONTRACT,
            "notification_id": f"{event_id}:{kind}",
            "event_id": event_id,
            "kind": "in_view" if baseline else kind,
            "event_kind": kind,
            "object": key,
            "message": message,
            "cites": cites,
        }

    def cites() -> dict[str, Any]:
        out = {}
        for side, value in (("previous", old), ("current", new)):
            if value:
                out[side] = {
                    k: value.get(k)
                    for k in (
                        "revision_id",
                        "source_revision",
                        "release",
                        "revision_date",
                    )
                    if value.get(k) is not None
                }
        return out

    kind = item.get("kind")
    if event_type == "coverage-degraded":
        return []
    if kind == "sense":
        if event_type == "added":
            return [
                note(
                    "sense_added",
                    f"{new['sense']} is stated in source revision {new['source_revision']}.",
                    cites(),
                )
            ]
        if event_type == "removed":
            return [
                note(
                    "sense_removed",
                    f"{old['sense']} is no longer stated by the current source revision "
                    "(recorded, not deleted).",
                    cites(),
                )
            ]
        return []
    if (
        kind == "definition"
        and event_type == "changed"
        and old["revision_id"] != new["revision_id"]
    ):
        return [
            note(
                "definition_revised",
                f"{new['definition']}: '{old['text']}' -> '{new['text']}'.",
                cites(),
            )
        ]
    if (
        kind == "etymology"
        and event_type == "changed"
        and old["fingerprint"] != new["fingerprint"]
    ):
        return [
            note(
                "etymology_changed",
                f"{key}: etymology assertions changed.",
                {"previous": old["assertions"], "current": new["assertions"]},
            )
        ]
    if (
        kind == "classification"
        and event_type == "changed"
        and (
            (old["parent"], old["classification"], old["level"])
            != (new["parent"], new["classification"], new["level"])
        )
    ):
        return [
            note(
                "classification_changed",
                f"{key}: parent {old['parent']} -> {new['parent']} "
                f"(release {old['release']} -> {new['release']}).",
                cites(),
            )
        ]
    if (
        kind == "iso"
        and event_type == "changed"
        and (old["iso639_3"], old["changes"]) != (new["iso639_3"], new["changes"])
    ):
        return [
            note(
                "iso_code_changed",
                f"{key}: ISO 639-3 {old['iso639_3']} -> {new['iso639_3']}.",
                cites(),
            )
        ]
    if (
        kind == "feature-value"
        and event_type == "changed"
        and old["value"] != new["value"]
    ):
        return [
            note(
                "feature_value_changed",
                f"WALS {new['parameter']} for {new['wals_code']}: {old['value']} -> "
                f"{new['value']} (release {old['release']} -> {new['release']}).",
                cites(),
            )
        ]
    if baseline and event_type == "added":
        return [note("in_view", f"{key} is monitored.", cites())]
    return []
